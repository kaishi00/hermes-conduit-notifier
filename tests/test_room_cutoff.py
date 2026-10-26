"""A member whose Hermes no longer accepts a room fails its turn once (room_cutoff)."""
import importlib.util
import logging
import sys
import types
from dataclasses import dataclass
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
DB = "/rooms/state.db"
ROOM = "n1-rules"
CUT_OFF = "cut off: laifu's Hermes no longer accepts this room"

# Hermes' driver records (gateway/hosted_room_driver.py), as far as the
# attempt path below uses them.
HERMES_STATE = '''
from dataclasses import dataclass


class DriverStateError(RuntimeError):
    pass


class StaleLeaseError(DriverStateError):
    pass


class StaleTaskError(DriverStateError):
    pass


class RoomUnavailableError(RuntimeError):
    pass


@dataclass(frozen=True)
class TaskIdentity:
    room_id: str
    task_id: str
    thread_id: str
    turn_id: str


@dataclass(frozen=True)
class TaskAttempt:
    identity: TaskIdentity
    lease: object
    execution_generation: int
    cancel_generation: int


settled = []
requeued = []


def settle_task(db_path, attempt, *, settlement_id, status, result=None, clock=None, **receipt):
    settled.append((attempt.execution_generation, status, result))


def requeue_not_admitted_task(db_path, attempt, *, clock):
    requeued.append(attempt.execution_generation)
'''

# tui_gateway/hosted_room_driver.py at hermes-agent c73cbcc4 (930e5ed has the
# same; 9e01b44 differs only in a log line): HostedRoomRuntime's attempt path
# and the helpers it calls, verbatim. Hermes' constructor and the waits around
# a submit take more than the attempt path reads, so those are stand-ins.
HERMES_DRIVER = '''
from __future__ import annotations

import logging
import threading
import time
from collections.abc import Mapping
from contextlib import nullcontext, suppress
from dataclasses import asdict
from typing import Any

from gateway import hosted_room_driver as state

logger = logging.getLogger("hermes.hosted_room_driver")
ROOM_SESSION_SOURCE = "bot_room"


class AttachmentsUnsupportedError(RuntimeError):
    pass


class HostedRoomRuntime:
    def __init__(self, *, db_path, transport_resolver=None, rpc=None, clock=time.monotonic):
        self.db_path, self.transport_resolver, self.rpc, self.clock = db_path, transport_resolver, rpc, clock
        self.unavailable_retry_min_seconds, self.unavailable_retry_max_seconds = 1.0, 30.0
        self.turn_timeout_seconds = 600.0
        self._unavailable_route_retries = {}
        self._status_lock = threading.Lock()
        self._current_tasks = {}
        self._rooms_needing_reschedule = set()
        self._last_error = None
        self.ambiguous = []

    def turn_lock(self, profile):
        return nullcontext()

    def _drop_lease(self, room_id):
        pass

    def _mark_ambiguous(self, binding, attempt):
        self.ambiguous.append(attempt)

    def _on_terminal(self, binding, attempt, receipt):
        pass

    def _wait_for_terminal(self, binding, *, profile, session_id, attempt, transport, deadline_monotonic):
        return transport.receipt

    def _defer_unavailable_route(self, task: Mapping[str, Any]) -> float:
        key = (task["identity"].room_id, _member_id(task))
        previous = self._unavailable_route_retries.get(key)
        lo, hi = self.unavailable_retry_min_seconds, self.unavailable_retry_max_seconds
        delay = lo if previous is None else min(hi, max(lo, previous["delay"] * 2))
        self._unavailable_route_retries[key] = {
            "delay": delay, "next_attempt_at": self.clock() + delay}
        return delay

    def _execute_attempt(
        self, binding: HostedRoomBinding, task: Mapping[str, Any], attempt: state.TaskAttempt
    ) -> None:
        profile, submit_attempted = task["payload"]["target_profile"], False
        attachments = task["payload"].get("attachments")
        with self._status_lock:
            self._current_tasks[binding.room_id] = attempt.identity
        try:
            # Inside the try: a member whose destination is gone (an unserved bot) fails its own
            # turn before submit instead of leaving the task running and the room wedged.
            transport = self._transport_for(binding, task)
            if attachments and not getattr(transport, "accepts_attachments", False):
                # A route that cannot carry the files would answer without them, and a
                # retry cannot change the route: settle a proven failure before submit.
                raise AttachmentsUnsupportedError(
                    "attachments_unsupported_on_route: this member's route cannot carry "
                    "attached files")
            with self.turn_lock(profile):
                session = self._resolve_or_create(transport, profile, binding.room_id)
                # A submit should fail before admission or return after it; an unexpected
                # exception at that boundary is ambiguous, never a proven failure.
                submit_attempted, session_id = True, _session_id(session)
                deadline_monotonic = time.monotonic() + self.turn_timeout_seconds
                transport.submit(
                    **_session_kw(profile, session_id), prompt=task["payload"]["prompt"],
                    task=attempt.identity, execution_generation=attempt.execution_generation,
                    attachments=attachments,
                    member_id=_member_id(task),
                    on_terminal=lambda receipt: self._on_terminal(binding, attempt, receipt))
                self._unavailable_route_retries.pop(
                    (task["identity"].room_id, _member_id(task)), None)
                receipt = self._wait_for_terminal(
                    binding, profile=profile, session_id=session_id, attempt=attempt,
                    transport=transport, deadline_monotonic=deadline_monotonic)
                if receipt is None:
                    return
                state.settle_task(self.db_path, attempt, **asdict(receipt), clock=self.clock)
        except (state.StaleLeaseError, state.StaleTaskError) as exc:
            self._drop_lease(binding.room_id)
            self._record_task_error(attempt, f"fenced: {exc}")
        except Exception as exc:
            # Exceptions may contain prompt text or credentials. Persist only a fixed
            # reason, not the free-form messages retained in the in-memory status.
            reason = 'request_too_large' if getattr(exc, 'reason', None) == 'request_too_large' else 'attempt_failed'
            logger.warning('hosted room task %s: %s', attempt.identity.task_id, reason)
            if submit_attempted and bool(getattr(exc, "not_admitted", False)):
                try:
                    state.requeue_not_admitted_task(self.db_path, attempt, clock=self.clock)
                except (state.StaleLeaseError, state.StaleTaskError) as fence_exc:
                    self._mark_ambiguous(binding, attempt)
                    self._record_task_error(
                        attempt, f"not-admitted proof lost its fence: {fence_exc}")
                else:
                    delay = self._defer_unavailable_route(task)
                    self._record_task_error(
                        attempt, f"was not admitted; queued for retry in {delay:g}s")
            elif submit_attempted:
                self._mark_ambiguous(binding, attempt)
                self._record_task_error(attempt, f"observation failed after submit: {exc}")
            else:
                self._settle_failure_if_current(attempt, exc)
        finally:
            with self._status_lock:
                self._current_tasks.pop(binding.room_id, None)
                # The task may have published a reply or exposed the next turn while this
                # thread held its slot: schedule exactly one follow-up after it leaves
                # (idle room scans never set this marker).
                self._rooms_needing_reschedule.add(binding.room_id)

    def _transport_for(
        self, binding: HostedRoomBinding, task: Mapping[str, Any]) -> InternalSessionRPC:
        if self.transport_resolver is not None:
            return self.transport_resolver(binding, task)
        if self.rpc is None:
            raise RuntimeError("hosted room transport is unavailable")
        return self.rpc

    def _resolve_or_create(
        self, transport: InternalSessionRPC, profile: str, room_id: str, *, create: bool = True
    ) -> Mapping[str, Any] | None:
        """Resolve + resume the canonical room session; create it (or return None) when absent."""
        coords = {
            "profile": profile, "title": room_session_title(room_id), "source": ROOM_SESSION_SOURCE}
        session = transport.resolve_exact(**coords)
        if session is None:
            return transport.create(**coords) if create else None
        return transport.resume(**_session_kw(profile, _session_id(session)))

    def _settle_failure_if_current(self, attempt: state.TaskAttempt, exc: Exception) -> None:
        with suppress(state.DriverStateError, state.RoomUnavailableError):
            state.settle_task(
                self.db_path, attempt,
                settlement_id=f"failure:{attempt.identity.task_id}:{attempt.execution_generation}",
                status="failed", result={"error": str(exc)}, clock=self.clock)
        self._record_task_error(attempt, f"failed: {exc}")

    def _record_task_error(self, attempt: state.TaskAttempt, message: str) -> None:
        self._record_error(f"task {attempt.identity.task_id} {message}")

    def _record_error(self, message: str) -> None:
        with self._status_lock:
            self._last_error = message


def room_session_title(room_id: str) -> str:
    """Return the canonical hidden session title for one hosted room."""
    return f"Group: {room_id}"


def _member_id(task: Mapping[str, Any]) -> str:
    p = task.get("payload") or {}
    return str(p.get("target_member_id") or p.get("target_profile") or "")


def _session_id(session: Mapping[str, Any]) -> str:
    value = session.get("session_id", session.get("id"))
    if not isinstance(value, str) or not value:
        raise ValueError("session adapter returned no session_id")
    return value


def _session_kw(profile: str, session_id: str) -> dict[str, str]:
    return {"profile": profile, "session_id": session_id, "source": ROOM_SESSION_SOURCE}
'''

# gateway/hosted_room_links.py: the stored routes, as far as the guard reads
# them (Hermes' StoredRoomLink has more fields, the grant among them).
HERMES_LINKS = '''
from dataclasses import dataclass


@dataclass(frozen=True)
class StoredRoomLink:
    room_id: str
    member_id: str
    status: str


stored = {}
reads = []


def load_room_links_tolerant(db_path):
    reads.append(str(db_path))
    return tuple(StoredRoomLink(room_id, member_id, status)
                 for (path, room_id, member_id), status in stored.items() if path == str(db_path)), ()
'''


def _module(name, source):
    module = types.ModuleType(name)
    sys.modules[name] = module
    exec(compile(source, f"<hermes {name}>", "exec"), module.__dict__)
    return module


def _load(name="conduit_room_cutoff"):
    spec = importlib.util.spec_from_file_location(name, ROOT / "room_cutoff.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def hermes(monkeypatch):
    """Hermes' room driver and stored routes: ``state``, ``driver``, ``links``."""
    for name in ("gateway", "tui_gateway"):
        package = types.ModuleType(name)
        package.__path__ = []
        monkeypatch.setitem(sys.modules, name, package)
    for name in ("gateway.hosted_room_driver", "tui_gateway.hosted_room_driver", "gateway.hosted_room_links"):
        monkeypatch.delitem(sys.modules, name, raising=False)
    state = _module("gateway.hosted_room_driver", HERMES_STATE)
    sys.modules["gateway"].hosted_room_driver = state
    links = _module("gateway.hosted_room_links", HERMES_LINKS)
    driver = _module("tui_gateway.hosted_room_driver", HERMES_DRIVER)
    yield types.SimpleNamespace(state=state, driver=driver, links=links)
    for name in ("gateway.hosted_room_driver", "tui_gateway.hosted_room_driver", "gateway.hosted_room_links"):
        sys.modules.pop(name, None)


@pytest.fixture
def guard(hermes):
    room_cutoff = _load()
    assert room_cutoff.install()
    yield room_cutoff
    room_cutoff.uninstall()


class _Refused(RuntimeError):
    """A peer's 403 for a revoked or expired grant, as Hermes raises it
    (tui_gateway/hosted_room_peer_http.PeerRunsHTTPError): refused before
    admission, and asking for reauthorization."""

    def __init__(self):
        super().__init__("peer room authorization needs renewal")
        self.not_admitted, self.needs_reauthorization, self.status_code = True, True, 403


@dataclass
class _Receipt:
    settlement_id: str
    status: str
    result: dict


class _PeerRoute:
    """A peer member's route as Hermes' service hands it out. Its client
    records the route's status as Hermes' _RouteStatusPeerClient does."""

    accepts_attachments = False

    def __init__(self, hermes, member="laifu", refuse=False):
        self.hermes, self.member, self.refuse, self.submits = hermes, member, refuse, 0
        self.receipt = None

    def resolve_exact(self, **coords):
        return {"session_id": "room_0123456789abcdef0123456789abcdef"}

    def resume(self, **kwargs):
        return {"session_id": kwargs["session_id"]}

    def create(self, **coords):
        return self.resolve_exact()

    def submit(self, *, task, execution_generation, **kwargs):
        self.submits += 1
        if self.refuse:
            self.hermes.links.stored[(DB, ROOM, self.member)] = "needs_reauthorization"
            raise _Refused()
        self.receipt = _Receipt(f"done:{task.task_id}:{execution_generation}", "completed", {"text": "hello"})


_LOCAL = types.SimpleNamespace(name="this host's own agents")


def _binding():
    return types.SimpleNamespace(room_id=ROOM)


def _task(hermes, member="laifu", status="queued"):
    identity = hermes.state.TaskIdentity(room_id=ROOM, task_id=f"task-{member}", thread_id="main", turn_id="d1")
    return {"identity": identity, "status": status, "execution_generation": 0, "cancel_generation": 0,
            "payload": {"target_profile": "default", "target_member_id": member, "prompt": "Where shall we go?"}}


def _attempt(hermes, task, generation):
    return hermes.state.TaskAttempt(identity=task["identity"], lease=None, execution_generation=generation,
                                    cancel_generation=0)


def _runtime(hermes, route):
    def resolve(binding, task):
        if task["payload"]["target_member_id"] != route.member:
            return _LOCAL
        # Building the member's transport marks its stored route ready: Hermes'
        # route status client counts the transport's own bind_room_scope call
        # as an answer from the member's host (tui_gateway/hosted_room_service
        # _RouteStatusPeerClient, c73cbcc4 and 9e01b44).
        if (DB, ROOM, route.member) in hermes.links.stored:
            hermes.links.stored[(DB, ROOM, route.member)] = "ready"
        return route

    return hermes.driver.HostedRoomRuntime(db_path=DB, rpc=_LOCAL, transport_resolver=resolve)


def test_hermes_alone_retries_a_cut_off_member_forever(hermes):
    hermes.links.stored[(DB, ROOM, "laifu")] = "ready"
    route = _PeerRoute(hermes, refuse=True)
    runtime, task = _runtime(hermes, route), _task(hermes)

    for generation in (1, 2, 3):
        runtime._execute_attempt(_binding(), task, _attempt(hermes, task, generation))

    assert route.submits == 3 and hermes.state.requeued == [1, 2, 3] and hermes.state.settled == []
    assert hermes.links.stored[(DB, ROOM, "laifu")] == "needs_reauthorization"


def test_a_cut_off_member_fails_its_turn_instead_of_retrying(hermes, guard):
    hermes.links.stored[(DB, ROOM, "laifu")] = "ready"
    route = _PeerRoute(hermes, refuse=True)
    runtime, task = _runtime(hermes, route), _task(hermes)

    # The refusal itself is Hermes' to handle: the turn goes back in the queue.
    runtime._execute_attempt(_binding(), task, _attempt(hermes, task, 1))
    assert route.submits == 1 and hermes.state.requeued == [1]
    # Its next attempt fails before it's sent, with the reason, and the room goes on.
    runtime._execute_attempt(_binding(), task, _attempt(hermes, task, 2))
    assert route.submits == 1 and hermes.state.requeued == [1]
    assert hermes.state.settled == [(2, "failed", {"error": CUT_OFF})]
    assert runtime.ambiguous == []
    # The member's route still says so, and its next turn fails at once.
    assert hermes.links.stored[(DB, ROOM, "laifu")] == "needs_reauthorization"
    later = _task(hermes)
    runtime._execute_attempt(_binding(), later, _attempt(hermes, later, 3))
    assert route.submits == 1 and hermes.state.settled[-1] == (3, "failed", {"error": CUT_OFF})


def test_a_relinked_member_answers_again(hermes, guard):
    hermes.links.stored[(DB, ROOM, "laifu")] = "needs_reauthorization"
    route = _PeerRoute(hermes)
    runtime, task = _runtime(hermes, route), _task(hermes)
    runtime._execute_attempt(_binding(), task, _attempt(hermes, task, 1))
    assert route.submits == 0 and hermes.state.settled == [(1, "failed", {"error": CUT_OFF})]

    # Hermes stores a fresh link as ready (hosted_room_links.make_stored_link).
    hermes.links.stored[(DB, ROOM, "laifu")] = "ready"
    later = _task(hermes)
    runtime._execute_attempt(_binding(), later, _attempt(hermes, later, 2))

    assert route.submits == 1 and hermes.state.settled[-1] == (2, "completed", {"text": "hello"})


def test_a_member_that_is_only_offline_is_left_to_hermes(hermes, guard):
    # Unavailable is Hermes' backoff to keep: the member's host may be back soon.
    hermes.links.stored[(DB, ROOM, "laifu")] = "unavailable"
    route = _PeerRoute(hermes)
    runtime, task = _runtime(hermes, route), _task(hermes)

    runtime._execute_attempt(_binding(), task, _attempt(hermes, task, 1))

    assert route.submits == 1 and hermes.state.settled == [(1, "completed", {"text": "hello"})]


def test_recovery_and_stop_paths_are_left_alone(hermes, guard):
    # Recovering or stopping a turn already sent needs the member's route, cut off or not.
    hermes.links.stored[(DB, ROOM, "laifu")] = "needs_reauthorization"
    route = _PeerRoute(hermes)
    runtime = _runtime(hermes, route)

    for status in ("running", "stopping", "indeterminate"):
        assert runtime._transport_for(_binding(), _task(hermes, status=status)) is route
    assert hermes.links.reads == []


def test_the_home_hosts_own_agents_go_on_while_a_member_is_cut_off(hermes, guard):
    hermes.links.stored[(DB, ROOM, "laifu")] = "needs_reauthorization"
    route = _PeerRoute(hermes)
    runtime, task = _runtime(hermes, route), _task(hermes, member="furina")

    assert runtime._transport_for(_binding(), task) is _LOCAL


def test_a_store_it_cant_read_leaves_hermes_behaviour(hermes, guard, monkeypatch, caplog):
    def unreadable(db_path):
        raise OSError("database is locked")

    monkeypatch.setattr(hermes.links, "load_room_links_tolerant", unreadable)
    route = _PeerRoute(hermes, refuse=True)
    runtime, task = _runtime(hermes, route), _task(hermes)

    with caplog.at_level(logging.WARNING):
        for generation in (1, 2):
            runtime._execute_attempt(_binding(), task, _attempt(hermes, task, generation))

    assert route.submits == 2 and hermes.state.requeued == [1, 2] and hermes.state.settled == []
    assert caplog.text.count("couldn't read") == 1


def test_a_member_with_no_stored_link_is_left_to_hermes(hermes, guard):
    route = _PeerRoute(hermes)
    runtime, task = _runtime(hermes, route), _task(hermes)

    runtime._execute_attempt(_binding(), task, _attempt(hermes, task, 1))

    assert route.submits == 1


def test_installing_twice_wraps_once_and_uninstall_puts_hermes_own_back(hermes):
    runtime_class = hermes.driver.HostedRoomRuntime
    own = runtime_class._transport_for
    room_cutoff = _load()

    assert room_cutoff.install() and room_cutoff.install()
    assert runtime_class._transport_for.__wrapped__ is own
    room_cutoff.uninstall()
    assert runtime_class._transport_for is own


def test_a_second_load_wraps_hermes_own_method_not_the_first_guard(hermes):
    # The plugin's gateway load and its dashboard routes each install one.
    runtime_class = hermes.driver.HostedRoomRuntime
    own = runtime_class._transport_for
    first, second = _load("conduit_room_cutoff_first"), _load("conduit_room_cutoff_second")

    assert first.install() and second.install()
    assert runtime_class._transport_for.__wrapped__ is own
    newer = runtime_class._transport_for
    first.uninstall()
    assert runtime_class._transport_for is newer
    second.uninstall()
    assert runtime_class._transport_for is own


def test_a_hermes_driver_without_the_method_is_left_alone(hermes, caplog):
    del hermes.driver.HostedRoomRuntime._transport_for

    with caplog.at_level(logging.INFO):
        assert _load().install() is False
    assert caplog.text.count("_transport_for") == 1
    assert not hasattr(hermes.driver.HostedRoomRuntime, "_transport_for")


def test_no_hermes_rooms_at_all_is_not_an_error(monkeypatch, caplog):
    monkeypatch.setitem(sys.modules, "tui_gateway.hosted_room_driver", None)

    with caplog.at_level(logging.WARNING):
        assert _load().install() is False
    assert caplog.text == ""


def test_the_dashboard_routes_guard_it_as_they_load(hermes, monkeypatch):
    # The dashboard drives the rooms it hosts itself, and imports these routes before it starts them.
    runtime_class = hermes.driver.HostedRoomRuntime
    own = runtime_class._transport_for
    if "hermes_constants" not in sys.modules:
        constants = types.ModuleType("hermes_constants")
        constants.get_hermes_home = lambda: Path("/nonexistent-hermes-home")
        monkeypatch.setitem(sys.modules, "hermes_constants", constants)
    spec = importlib.util.spec_from_file_location("conduit_plugin_api_room_cutoff", ROOT / "dashboard" / "plugin_api.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    assert runtime_class._transport_for is not own
    assert runtime_class._transport_for.__wrapped__ is own
    module._room_cutoff_module.uninstall()
