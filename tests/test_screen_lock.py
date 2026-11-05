"""One of a bot's chats drives its screen at a time (screen_lock; Eric
2026-11-05): the Bot Chat, its workstreams and any other chat of the bot,
a delegated child as its parent; who holds it now is kept apart from who
used it last; a workstream refused the screen hears when it's free; and a
workstream that needs the user on the screen says so on its card."""
import sys
import time

import pytest

from test_turn_end_pushes import plugin as plugin_init
from test_work import BOT_CHAT, OTHER_CHAT, WORKER, call, home, hermes, on, start, store_mod, work  # noqa: F401

lock = sys.modules["conduit_push.screen_lock"]


@pytest.fixture(autouse=True)
def bot_screen(monkeypatch):
    monkeypatch.setattr(lock, "scope", lambda: lock.BOT)
    monkeypatch.setattr(work, "SCREEN_POLL_S", 0.01)
    monkeypatch.setattr(work, "screen_possible", lambda: True)
    lock._parents.clear()
    work._screen_watchers.clear()
    yield


def click(session_id, **kwargs):
    return work.screen_block(session_id, "computer_use", {"action": "click", "x": 1, "y": 2, **kwargs})


def test_the_first_chat_to_click_holds_the_screen_and_others_are_refused(on, hermes):
    start()
    assert click(BOT_CHAT) is None
    refused = click(WORKER)
    assert refused["action"] == "block"
    assert "Furina's Bot Chat" in refused["message"] and "Browser tools still work" in refused["message"]
    # The holder keeps it across its calls; another chat of the bot is refused too.
    assert click(BOT_CHAT) is None
    assert "Furina's Bot Chat" in click(OTHER_CHAT)["message"]


def test_a_workstream_holding_the_screen_refuses_the_bot_chat(on, hermes):
    start()
    assert click(WORKER) is None
    refused = click(BOT_CHAT)
    assert "Furina's W1 “Login bug”" in refused["message"]
    # Not a workstream: nobody sends it on, so it isn't told it will hear.
    assert "You'll get a message" not in refused["message"]


def test_browser_tools_arent_held(on, hermes):
    start()
    assert click(WORKER) is None
    assert work.screen_block(BOT_CHAT, "browser_navigate", {"url": "https://example.com"}) is None
    assert work.screen_block(BOT_CHAT, "terminal", {"command": "ls"}) is None


def test_a_bridged_computer_use_is_held_too(on, hermes):
    start()
    assert click(WORKER) is None
    bridged = work.screen_block(BOT_CHAT, "tool_call", {"name": "computer_use", "arguments": {"action": "click"}})
    assert bridged["action"] == "block"


def test_a_delegated_child_drives_the_screen_as_its_parent(on, hermes):
    start()
    lock.child_started("child-1", WORKER)
    assert click("child-1") is None
    assert click(WORKER) is None  # the same turn's screen
    assert click(BOT_CHAT)["action"] == "block"
    assert lock.snapshot(on.home)["controlling"]["session_id"] == WORKER


def test_the_screen_is_free_once_the_holders_turn_ends(on, hermes):
    start()
    assert click(BOT_CHAT) is None
    work.screen_turn_ended(OTHER_CHAT)  # not the holder: nothing changes
    assert click(WORKER)["action"] == "block"
    work.screen_turn_ended(BOT_CHAT)
    assert click(WORKER) is None


def test_a_hold_nobody_renewed_runs_out(on, hermes, monkeypatch):
    start()
    assert click(BOT_CHAT) is None
    later = time.time() + lock.HOLD_IDLE_S + 1
    monkeypatch.setattr(lock.time, "time", lambda: later)
    assert click(WORKER) is None


def test_a_hold_whose_process_is_gone_is_over(on, hermes):
    start()
    path = on.home / lock.FILE
    lock.claim(on.home, BOT_CHAT, {"label": "Furina's Bot Chat"})
    state = __import__("json").loads(path.read_text())
    state["holder"]["pid"] = 2 ** 22 + 12345  # no such process
    path.write_text(__import__("json").dumps(state))
    assert click(WORKER) is None


def test_who_holds_it_now_is_apart_from_who_used_it_last(on, hermes):
    start()
    assert click(WORKER) is None
    listed = work.app_list(on.home)
    assert listed["screen"]["controlling"]["ref"] == "W1"
    row = listed["work"][0]
    assert row["screen"] == "controlling" and row["screen_used_at"] > 0
    work.screen_turn_ended(WORKER)
    listed = work.app_list(on.home)
    # Used last, but nobody drives it now.
    assert listed["screen"]["controlling"] is None
    assert listed["screen"]["last"]["ref"] == "W1"
    assert listed["work"][0]["screen"] == "" and listed["work"][0]["screen_used_at"] > 0


def test_a_bot_with_no_work_still_says_who_has_its_screen(home, hermes):
    assert click(BOT_CHAT) is None
    assert work.app_list(home)["screen"]["controlling"]["kind"] == "bot_chat"


def test_a_refused_workstream_waits_and_hears_when_the_screen_is_free(on, hermes):
    start()
    work.turn_started(WORKER)
    assert click(BOT_CHAT) is None
    refused = click(WORKER)
    assert "You'll get a message in this chat when it's free" in refused["message"]
    assert work.app_list(on.home)["work"][0]["screen"] == "waiting"
    assert "W1 waits for it" in work.digest(BOT_CHAT)
    work.turn_ended(WORKER, "Waiting for the screen.")
    work.turn_finished(WORKER, failed=False, interrupted=False)
    hermes.calls.clear()
    work.screen_turn_ended(BOT_CHAT)
    sent = [params for name, params, _ in hermes.calls if name == "prompt.submit"
            and params.get("text") == work.SCREEN_FREE]
    assert len(sent) == 1
    assert lock.snapshot(on.home)["waiting"] == []


def test_a_hold_another_process_let_go_is_noticed_by_the_watch(on, hermes):
    start()
    work.turn_started(WORKER)
    lock.claim(on.home, "someone-else", {"label": "a chat in another window"})
    assert click(WORKER)["action"] == "block"
    work.turn_ended(WORKER, "Waiting for the screen.")
    work.turn_finished(WORKER, failed=False, interrupted=False)
    hermes.calls.clear()
    lock.release(on.home, "someone-else")  # as the gateway's process would
    deadline = time.time() + 5
    while time.time() < deadline and not any(params.get("text") == work.SCREEN_FREE
                                             for name, params, _ in hermes.calls if name == "prompt.submit"):
        time.sleep(0.02)
    assert any(params.get("text") == work.SCREEN_FREE for name, params, _ in hermes.calls if name == "prompt.submit")


def test_the_coordinators_note_names_who_drives_the_screen(on, hermes):
    start()
    assert click(WORKER) is None
    assert "Furina's W1 “Login bug” is driving it now" in work.digest(BOT_CHAT)
    work.screen_turn_ended(WORKER)
    assert "The bot's screen" not in work.digest(BOT_CHAT)


def test_a_workstream_that_needs_the_user_on_the_screen_says_so(on, hermes):
    start()
    work.turn_started(WORKER)
    hermes.live(BOT_CHAT, transport=object())
    hermes.calls.clear()
    answer = call("work_progress", {"screen": "Sign in to GitHub"}, session_id=WORKER)
    assert answer["ok"] and "End your turn now" in answer["note"]
    work.turn_ended(WORKER, "I need you to sign in to GitHub on the screen.")
    work.turn_finished(WORKER, failed=False, interrupted=False)
    work.flush_wakes()
    (_, params, _), = hermes.named("prompt.submit")
    assert "needs the user on the bot's screen" in params["text"] and "Sign in to GitHub" in params["text"]
    listed = work.app_list(on.home)
    assert listed["work"][0]["screen_ask"] == "Sign in to GitHub"
    assert listed["counts"]["needs_you"] == 1
    assert "waiting on the user on the bot's screen" in work.digest(BOT_CHAT)
    # Told to continue: a new turn, and the ask is gone.
    work.turn_started(WORKER)
    assert work.app_list(on.home)["work"][0]["screen_ask"] == ""
    assert "screen" not in on.get("W1").progress


def test_a_screen_ask_keeps_the_checklist_and_takes_only_words(on, hermes):
    start()
    call("work_progress", {"steps": [{"step": "Open the site", "state": "done"}]}, session_id=WORKER)
    assert call("work_progress", {"screen": 5}, session_id=WORKER)["error"] == "screen"
    assert call("work_progress", {"screen": "Solve the CAPTCHA"}, session_id=WORKER)["ok"]
    progress = on.get("W1").progress
    assert progress["steps"][0]["step"] == "Open the site" and progress["screen"] == "Solve the CAPTCHA"
    assert call("work_progress", {"screen": ""}, session_id=WORKER)["ok"]
    assert "screen" not in on.get("W1").progress


def test_the_brief_says_how_to_ask_for_the_user_on_the_screen(on, hermes, monkeypatch):
    start()
    (_, params, _), = [entry for entry in hermes.named("prompt.submit")][:1]
    assert "call work_progress with screen set to what they need to do there" in params["text"]
    monkeypatch.setattr(work, "screen_possible", lambda: False)
    start(title="Other")
    last = hermes.named("prompt.submit")[-1][1]["text"]
    assert "work_progress with screen" not in last


def test_the_hooks_hold_the_screen_and_let_it_go(on, hermes, monkeypatch):
    on.set_policy("off")  # a Bot Chat whose bot runs its own tools
    start()
    monkeypatch.setattr(plugin_init, "_is_child", lambda session_id: False)
    first = plugin_init._pre_tool_call(session_id=BOT_CHAT, tool_name="computer_use", args={"action": "click"},
                                       turn_id="t1", tool_call_id="c1")
    assert first is None
    held = plugin_init._pre_tool_call(session_id=WORKER, tool_name="computer_use", args={"action": "click"},
                                      turn_id="t2", tool_call_id="c2")
    assert held["action"] == "block"
    plugin_init._on_session_end(session_id=BOT_CHAT, turn_id="t1")
    assert lock.snapshot(on.home)["controlling"] is None


def test_a_call_the_bots_policy_refuses_never_takes_the_screen(on, hermes, monkeypatch):
    start()
    monkeypatch.setattr(plugin_init, "_is_child", lambda session_id: False)
    refused = plugin_init._pre_tool_call(session_id=BOT_CHAT, tool_name="computer_use", args={"action": "click"},
                                         turn_id="t1", tool_call_id="c1")
    assert "coordinator" in refused["message"]
    assert lock.snapshot(on.home)["controlling"] is None


def test_an_unreadable_hold_file_lets_the_call_through(on, hermes, monkeypatch):
    start()

    def broken(*_args, **_kwargs):
        raise OSError("disk gone")

    monkeypatch.setattr(lock, "claim", broken)
    assert click(WORKER) is None


def test_a_waiter_whose_message_couldnt_go_stays_waiting(on, hermes, monkeypatch):
    # No watch thread: it would take the waiter off and put it back meanwhile.
    monkeypatch.setattr(work, "_watch_screen", lambda home: None)
    start()
    work.turn_started(WORKER)
    assert click(BOT_CHAT) is None
    assert click(WORKER)["action"] == "block"
    work.turn_ended(WORKER, "Waiting for the screen.")
    work.turn_finished(WORKER, failed=False, interrupted=False)
    monkeypatch.setattr(work, "_deliver", lambda *_a, **_k: {"ok": False, "note": "at the limit"})
    work.screen_turn_ended(BOT_CHAT)
    assert [item["ref"] for item in lock.snapshot(on.home)["waiting"]] == ["W1"]


def test_a_compacted_chat_keeps_its_own_screen(on, hermes):
    start()
    # W1's hold under the id it had before a compaction.
    assert lock.claim(on.home, "before-compaction", work.screen_who(on.home, WORKER)) is None
    assert click(WORKER) is None
    assert lock.snapshot(on.home)["controlling"]["session_id"] == WORKER
    # Another chat is still refused.
    assert click(BOT_CHAT)["action"] == "block"
    # Its turn's end lets go of a hold still under the older id.
    lock.release(on.home, WORKER)
    assert lock.claim(on.home, "before-compaction", work.screen_who(on.home, WORKER)) is None
    work.screen_turn_ended(WORKER)
    assert lock.snapshot(on.home)["controlling"] is None


def test_a_restart_watches_those_still_waiting(on, hermes, monkeypatch):
    start()
    lock.wait(on.home, WORKER, work.screen_who(on.home, WORKER))
    started = []
    monkeypatch.setattr(work, "_watch_screen", lambda home: started.append(home))
    work._replay(on.home, store_mod.Store(on.home))
    assert started == [on.home]


def test_no_screen_ask_where_the_host_has_no_screen(on, hermes, monkeypatch):
    start()
    monkeypatch.setattr(work, "screen_possible", lambda: False)
    answer = call("work_progress", {"screen": "Sign in"}, session_id=WORKER)
    assert answer["error"] == "screen" and "no screen" in answer["note"]
    assert "screen" not in (on.get("W1").progress or {})


def test_unloading_ends_the_screen_watch(on, hermes):
    start()
    before = work._screen_generation[0]
    work._screen_watchers.add(str(on.home))
    work.stop()
    assert work._screen_generation[0] == before + 1 and not work._screen_watchers
