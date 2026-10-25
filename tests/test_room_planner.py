"""Group rooms keep answering once a thread passes 24 messages (room_planner)."""
import importlib.util
import logging
import sys
import types
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
DIGEST = "0123456789abcdef01234567"
# Hermes' policy checkpoint keeps this many messages per thread
# (gateway/hosted_room_policy_checkpoint.py MAX_THREAD_TRANSCRIPT_EVENTS).
WINDOW = 24

# gateway/hosted_room_discussion.py at hermes-agent 9e01b44, as far as the
# planner's watermarks go. Run in the fake module's own namespace, so
# _effective_watermarks finds _derive_member_watermarks the way Hermes' does:
# by the module's global name, at call time.
HERMES_DISCUSSION = '''
from dataclasses import dataclass


class DiscussionPolicyError(ValueError):
    pass


class DiscussionValidationError(DiscussionPolicyError):
    pass


_TERMINAL_EVENT_KINDS = frozenset({"turn.settled", "turn.failed", "turn.cancelled", "turn.deferred"})


@dataclass(frozen=True)
class _ValidatedEvent:
    raw: dict
    seq: int
    event_id: str
    kind: str
    actor: dict
    payload: dict


def _derive_member_watermarks(events):
    messages_by_id = {event.event_id: event for event in events if event.kind == "message.member"}
    terminal_by_task = {}
    watermarks = {}
    for event in events:
        if event.kind not in _TERMINAL_EVENT_KINDS:
            continue
        task_id = str(event.payload["task_id"])
        if (previous := terminal_by_task.get(task_id)) is not None:
            if previous.kind != "turn.deferred":
                raise DiscussionValidationError(f"task '{task_id}' has more than one terminal room event")
        terminal_by_task[task_id] = event
        key = (str(event.payload["thread_id"]), str(event.payload["member_id"]))
        watermark = int(event.payload["seen_through_seq"])
        if event.kind == "turn.settled" and not event.payload["passed"]:
            message = messages_by_id.get(str(event.payload["message_event_id"]))
            if message is None or any(
                message.payload.get(f) != event.payload.get(f) for f in ("task_id", "member_id", "thread_id")):
                raise DiscussionValidationError("turn.settled references no matching member message")
            discussion_seq = next((candidate.seq for candidate in events
                                   if candidate.event_id == event.payload["discussion_event_id"]), None)
            if discussion_seq is None:
                raise DiscussionValidationError("turn.settled references no matching user discussion")
            if watermark >= discussion_seq:
                watermark = max(watermark, message.seq)
        watermarks[key] = max(watermarks.get(key, 0), watermark)
    return watermarks


def _effective_watermarks(validated, initial_watermarks):
    watermarks = {
        (str(thread_id), str(member_id)): int(value)
        for (thread_id, member_id), value in (initial_watermarks or {}).items() if int(value) >= 0}
    for key, value in _derive_member_watermarks(validated).items():
        watermarks[key] = max(watermarks.get(key, 0), value)
    return watermarks
'''


def _load(name="conduit_room_planner"):
    spec = importlib.util.spec_from_file_location(name, ROOT / "room_planner.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _discussion_module(source=HERMES_DISCUSSION):
    module = types.ModuleType("gateway.hosted_room_discussion")
    exec(compile(source, "<hermes gateway.hosted_room_discussion>", "exec"), module.__dict__)
    return module


@pytest.fixture
def hermes(monkeypatch):
    discussion = _discussion_module()
    gateway = types.ModuleType("gateway")
    gateway.__path__ = []
    gateway.hosted_room_discussion = discussion
    monkeypatch.setitem(sys.modules, "gateway", gateway)
    monkeypatch.setitem(sys.modules, "gateway.hosted_room_discussion", discussion)
    return discussion


@pytest.fixture
def guard(hermes):
    room_planner = _load()
    assert room_planner.install()
    yield room_planner
    room_planner.uninstall()


class _Room:
    """A room log the way Hermes writes one: each user message in the thread
    gets one reply from each member, and each reply its turn.settled."""

    def __init__(self, hermes, members=("furina", "nahida")):
        self.hermes = hermes
        self.members = members
        self.events = []

    def _append(self, kind, event_id, thread, payload):
        seq = len(self.events) + 1
        self.events.append(self.hermes._ValidatedEvent(raw={}, seq=seq, event_id=event_id, kind=kind, actor={},
                                                       payload={"thread_id": thread, **payload}))
        return self.events[-1]

    def ask(self, number, *, thread="main", answered=True, turn_id=None, names=None):
        """One question and its replies. ``turn_id`` replaces the replies'
        turn ids; ``names`` maps a member to the seq its turn id names."""
        prefix = "" if thread == "main" else f"{thread}-"
        user = self._append("message.user", f"{prefix}user-{number}", thread, {"text": f"question {number}"})
        if not answered:
            return user
        for index, member in enumerate(self.members):
            seen = self.events[-1].seq
            named = (names or {}).get(member, user.seq)
            coordinates = {
                "task_id": f"{prefix}task-{number}-{member}", "member_id": member, "member_index": index,
                "round_index": 0, "discussion_event_id": user.event_id,
                "turn_id": turn_id or f"d{named}.r0.p{index}.s{seen}.m{DIGEST}"}
            reply = self._append("message.member", f"{prefix}reply-{number}-{member}", thread,
                                 {**coordinates, "text": "an answer"})
            self._append("turn.settled", f"{prefix}settled-{number}-{member}", thread, {
                **coordinates, "message_event_id": reply.event_id, "passed": False, "seen_through_seq": seen})
        return user

    def window(self):
        """What the policy checkpoint hands the planner: each thread's last 24
        messages, and the turn.settled of each member reply among them."""
        messages = []
        for thread in dict.fromkeys(event.payload["thread_id"] for event in self.events):
            messages += [event for event in self.events
                         if event.kind.startswith("message.") and event.payload["thread_id"] == thread][-WINDOW:]
        kept = {event.event_id for event in messages}
        settled = [event for event in self.events
                   if event.kind == "turn.settled" and event.payload["message_event_id"] in kept]
        return tuple(sorted(messages + settled, key=lambda event: event.seq))


def _long_thread(hermes, *, first=None):
    """Eight answered questions and a ninth just asked: 25 messages, so the
    window starts at the first question's first reply. ``first`` is how the
    first question's replies name it."""
    room = _Room(hermes)
    room.ask(1, **(first or {}))
    for number in range(2, 9):
        room.ask(number)
    room.ask(9, answered=False)
    return room


def _two_long_threads(hermes, *, names=None):
    """Two threads that each lost their first question: thread b's at seq 1,
    then thread a's. ``names`` is how thread a's first replies name theirs."""
    room = _Room(hermes)
    room.ask(1, thread="b")
    room.ask(1, thread="a", names=names)
    for number in range(2, 9):
        room.ask(number, thread="a")
        room.ask(number, thread="b")
    room.ask(9, thread="a", answered=False)
    room.ask(9, thread="b", answered=False)
    return room


def test_hermes_planner_stalls_once_a_thread_outgrows_its_window(hermes):
    window = _long_thread(hermes).window()

    with pytest.raises(hermes.DiscussionValidationError, match="no matching user discussion"):
        hermes._effective_watermarks(window, None)


def test_a_thread_past_the_window_gets_the_watermarks_of_its_whole_log(guard, hermes):
    room = _long_thread(hermes)
    window = room.window()
    assert "user-1" not in {event.event_id for event in window}

    assert hermes._effective_watermarks(window, None) == hermes._effective_watermarks(tuple(room.events), None)


def test_each_long_thread_gets_its_own_trimmed_message_back(guard, hermes):
    room = _two_long_threads(hermes)
    window = room.window()
    assert {"user-1", "b-user-1"}.isdisjoint(event.event_id for event in window)

    assert hermes._effective_watermarks(window, None) == hermes._effective_watermarks(tuple(room.events), None)


def test_a_thread_inside_the_window_is_planned_as_hermes_plans_it(guard, hermes):
    room = _Room(hermes)
    for number in range(1, 4):
        room.ask(number)
    own = hermes._derive_member_watermarks.__wrapped__

    assert hermes._derive_member_watermarks(room.window()) == own(room.window())


def test_a_window_with_another_fault_fails_as_hermes_fails_it(guard, hermes, caplog):
    room = _long_thread(hermes)
    window = tuple(event for event in room.window() if event.event_id != "reply-1-furina")

    with caplog.at_level(logging.INFO), pytest.raises(hermes.DiscussionValidationError,
                                                      match="no matching member message"):
        hermes._effective_watermarks(window, None)
    # Hermes stopped at the missing reply, so nothing was filled in.
    assert "trimmed" not in caplog.text


@pytest.mark.parametrize("first", [
    # Not a Discussion turn id.
    {"turn_id": "not-a-discussion-turn"},
    # Names a message the window still has, under another id.
    {"names": {"furina": 2, "nahida": 2}},
    # A discussion can't come after the replies that settle it.
    {"names": {"furina": 1000, "nahida": 1000}},
])
def test_a_turn_id_that_disagrees_with_the_log_still_fails(guard, hermes, first):
    window = _long_thread(hermes, first=first).window()

    with pytest.raises(hermes.DiscussionValidationError, match="no matching user discussion"):
        hermes._effective_watermarks(window, None)


@pytest.mark.parametrize("names", [
    # Thread a's discussion named by thread b's seq: two messages at one place.
    {"furina": 1, "nahida": 1},
    # Its two replies naming different places for it.
    {"nahida": 1},
])
def test_turn_ids_that_disagree_with_each_other_still_fail(guard, hermes, names):
    window = _two_long_threads(hermes, names=names).window()

    with pytest.raises(hermes.DiscussionValidationError, match="no matching user discussion"):
        hermes._effective_watermarks(window, None)


def test_a_member_that_passed_needs_no_place_for_the_question(guard, hermes):
    room = _long_thread(hermes)
    # Hermes looks nothing up for a pass, so its turn id isn't read either.
    passed = room._append("turn.settled", "settled-1-zhongli", "main", {
        "task_id": "task-1-zhongli", "member_id": "zhongli", "member_index": 2, "round_index": 0,
        "discussion_event_id": "user-1", "turn_id": "a turn id of another shape", "message_event_id": None,
        "passed": True, "seen_through_seq": 5})

    watermarks = hermes._effective_watermarks(room.window() + (passed,), None)
    assert watermarks == hermes._effective_watermarks(tuple(room.events), None)


def test_filling_in_a_trimmed_discussion_is_logged_once(guard, hermes, caplog):
    window = _long_thread(hermes).window()

    with caplog.at_level(logging.INFO):
        hermes._effective_watermarks(window, None)
        hermes._effective_watermarks(window, None)
    assert caplog.text.count("trimmed") == 1


def test_installing_twice_wraps_once_and_uninstall_puts_hermes_own_back(hermes):
    own = hermes._derive_member_watermarks
    room_planner = _load()

    assert room_planner.install() and room_planner.install()
    assert hermes._derive_member_watermarks.__wrapped__ is own
    room_planner.uninstall()
    assert hermes._derive_member_watermarks is own


def test_a_second_load_wraps_hermes_own_function_not_the_first_guard(hermes):
    # The plugin's gateway load and its dashboard routes each install one.
    own = hermes._derive_member_watermarks
    first, second = _load("conduit_room_planner_first"), _load("conduit_room_planner_second")

    assert first.install() and second.install()
    assert hermes._derive_member_watermarks.__wrapped__ is own
    newer = hermes._derive_member_watermarks
    first.uninstall()
    assert hermes._derive_member_watermarks is newer
    second.uninstall()
    assert hermes._derive_member_watermarks is own


def test_a_hermes_planner_without_the_function_is_left_alone(monkeypatch, caplog):
    discussion = _discussion_module("def plan_next_task():\n    pass\n")
    monkeypatch.setitem(sys.modules, "gateway", types.ModuleType("gateway"))
    monkeypatch.setitem(sys.modules, "gateway.hosted_room_discussion", discussion)

    with caplog.at_level(logging.INFO):
        assert _load().install() is False
    assert "_derive_member_watermarks" in caplog.text
    assert not hasattr(discussion, "_derive_member_watermarks")


def test_no_hermes_rooms_at_all_is_not_an_error(monkeypatch, caplog):
    monkeypatch.setitem(sys.modules, "gateway.hosted_room_discussion", None)

    with caplog.at_level(logging.WARNING):
        assert _load().install() is False
    assert caplog.text == ""


def test_the_dashboard_routes_guard_the_planner_as_they_load(hermes, monkeypatch):
    # The dashboard plans the turns of the rooms it hosts itself, and imports
    # these routes before it starts them.
    own = hermes._derive_member_watermarks
    if "hermes_constants" not in sys.modules:
        constants = types.ModuleType("hermes_constants")
        constants.get_hermes_home = lambda: Path("/nonexistent-hermes-home")
        monkeypatch.setitem(sys.modules, "hermes_constants", constants)
    spec = importlib.util.spec_from_file_location("conduit_plugin_api_room_planner", ROOT / "dashboard" / "plugin_api.py")
    spec.loader.exec_module(importlib.util.module_from_spec(spec))

    assert hermes._derive_member_watermarks is not own
    assert hermes._derive_member_watermarks.__wrapped__ is own
