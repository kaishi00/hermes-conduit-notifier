import importlib.util
import json
import pathlib
import sys

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[1]


def _load(name, path):
    if name in sys.modules:
        return sys.modules[name]
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


calls_store = _load("conduit_calls_store_test", ROOT / "calls_store.py")
CallStore = calls_store.CallStore


class Clock:
    def __init__(self, now=1_000_000.0):
        self.now = now

    def __call__(self):
        return self.now


def _store(tmp_path, clock=None, **settings):
    store = CallStore(tmp_path, clock=clock or Clock())
    if settings:
        store.update_settings(settings)
    return store


def test_calls_start_off_with_the_agreed_defaults(tmp_path):
    assert _store(tmp_path).settings() == {
        "enabled": False,
        "when_asked": True,
        "decides": False,
        "alerts": False,
        "min_gap_s": 120,
        "per_hour": 6,
        "per_day": 20,
    }


def test_settings_change_within_bounds_and_persist(tmp_path):
    store = _store(tmp_path)
    assert store.update_settings({"enabled": True, "min_gap_s": 30, "per_hour": 30, "per_day": 60})["per_day"] == 60
    assert CallStore(tmp_path).settings()["enabled"] is True


@pytest.mark.parametrize("changes", [
    {"min_gap_s": 29},
    {"min_gap_s": 3601},
    {"per_hour": 0},
    {"per_hour": 31},
    {"per_day": 61},
    {"per_day": 2.5},
    {"per_day": True},
    {"enabled": "yes"},
    {"surprise": 1},
])
def test_settings_outside_the_bounds_are_refused(tmp_path, changes):
    store = _store(tmp_path)
    with pytest.raises(ValueError):
        store.update_settings(changes)
    assert store.settings()["per_day"] == 20


def test_a_watch_needs_calls_on_and_call_when_asked(tmp_path):
    store = _store(tmp_path)
    with pytest.raises(ValueError, match="calls_off"):
        store.add_watch(["rt-1"], "Check the server")
    store.update_settings({"enabled": True, "when_asked": False})
    with pytest.raises(ValueError, match="calls_off"):
        store.add_watch(["rt-1"], "Check the server")


def test_a_watch_fires_exactly_once_for_any_of_its_session_ids(tmp_path):
    store = _store(tmp_path, enabled=True)
    added = store.add_watch(["rt-1", "st-1"], "Check the server")
    assert added["status"] == "watching"
    fired = store.fire("st-1", "done")
    assert fired["status"] == "call"
    assert fired["outcome"] == "done"
    assert fired["watch"]["id"] == added["id"]
    assert fired["watch"]["title"] == "Check the server"
    assert fired["watch"]["session_ids"] == ["rt-1", "st-1"]
    # A replayed hook, or the other id, finds nothing left.
    assert store.fire("st-1", "done") is None
    assert store.fire("rt-1", "failed") is None


def test_an_unwatched_session_is_left_alone(tmp_path):
    store = _store(tmp_path, enabled=True)
    store.add_watch(["rt-1"], "Check the server")
    assert store.fire("rt-2", "done") is None
    assert store.watch_count() == 1


def test_no_file_means_nothing_to_fire(tmp_path):
    store = _store(tmp_path)
    assert store.fire("rt-1", "done") is None
    assert not (tmp_path / "conduit-calls.json").exists()


def test_calls_turned_off_later_consume_the_watch_without_calling(tmp_path):
    store = _store(tmp_path, enabled=True)
    store.add_watch(["rt-1"], "Check the server")
    store.update_settings({"enabled": False})
    assert store.fire("rt-1", "done")["status"] == "off"
    assert store.watch_count() == 0


def test_the_gap_between_calls_holds_back_a_second_call(tmp_path):
    clock = Clock()
    store = _store(tmp_path, clock=clock, enabled=True, min_gap_s=120)
    store.add_watch(["a"], "A")
    store.add_watch(["b"], "B")
    store.add_watch(["c"], "C")
    assert store.fire("a", "done")["status"] == "call"
    clock.now += 119
    limited = store.fire("b", "done")
    assert limited["status"] == "limited"
    assert limited["reason"] == "gap"
    clock.now += 2
    assert store.fire("c", "done")["status"] == "call"


def test_the_hourly_and_daily_limits_hold(tmp_path):
    clock = Clock()
    store = _store(tmp_path, clock=clock, enabled=True, min_gap_s=30, per_hour=2, per_day=3)
    outcomes = []
    for index in range(5):
        store.add_watch([f"s{index}"], f"Job {index}")
    for index in range(3):
        outcomes.append(store.fire(f"s{index}", "done"))
        clock.now += 31
    assert [o["status"] for o in outcomes] == ["call", "call", "limited"]
    assert outcomes[2]["reason"] == "hour"
    clock.now += 3600
    assert store.fire("s3", "done")["status"] == "call"
    clock.now += 3600
    day = store.fire("s4", "done")
    assert day["status"] == "limited"
    assert day["reason"] == "day"


def test_a_job_that_ended_just_before_its_watch_is_reported_as_ended(tmp_path):
    clock = Clock()
    store = _store(tmp_path, clock=clock, enabled=True)
    assert store.fire("rt-1", "failed") is None  # the turn ended, no watch yet
    clock.now += 60
    # The job's request went out 5 minutes ago on the phone's clock.
    assert store.add_watch(["st-1", "rt-1"], "Check the server", ended_within_s=300) == {"status": "ended", "outcome": "failed"}
    assert store.watch_count() == 0


def test_an_earlier_turn_of_the_same_chat_is_not_the_job_ending(tmp_path):
    clock = Clock()
    store = _store(tmp_path, clock=clock, enabled=True)
    store.fire("chat-1", "done")  # the chat's typed turn, before the voice request
    clock.now += 120
    # The voice request went out 60 seconds ago: that end was before it.
    assert store.add_watch(["chat-1"], "Check the server", ended_within_s=60)["status"] == "watching"


def test_without_a_window_recent_ends_are_not_checked(tmp_path):
    store = _store(tmp_path, enabled=True)
    store.fire("rt-1", "done")
    assert store.add_watch(["rt-1"], "Check the server")["status"] == "watching"


def test_recent_ends_are_forgotten_after_half_an_hour(tmp_path):
    clock = Clock()
    store = _store(tmp_path, clock=clock, enabled=True)
    store.fire("rt-1", "done")
    clock.now += 31 * 60
    assert store.add_watch(["rt-1"], "Check the server", ended_within_s=1800)["status"] == "watching"


def test_recent_ends_are_not_recorded_while_calls_are_off(tmp_path):
    store = _store(tmp_path)
    store.fire("rt-1", "done")
    store.update_settings({"enabled": True})
    assert store.add_watch(["rt-1"], "Check the server", ended_within_s=60)["status"] == "watching"


# --- Holds: registered during the call, called after it ---------------------


def test_a_job_that_ends_during_the_call_waits_for_the_hold(tmp_path):
    clock = Clock()
    store = _store(tmp_path, clock=clock, enabled=True)
    watch_id = store.add_watch(["rt-1", "st-1"], "Check the server", hold_s=180)["id"]
    held = store.fire("st-1", "done")
    assert held["status"] == "held"
    assert held["until"] == clock.now + 180
    # A replayed hook keeps the first outcome and still doesn't call.
    assert store.fire("rt-1", "failed")["outcome"] == "done"
    assert store.watch_count() == 1
    assert store.fire_due() == []
    assert store.next_due() == clock.now + 180
    assert store.hold(watch_id, 180) == {"status": "watching"}  # still on the call


def test_released_at_hang_up_after_the_job_ended_answers_ended(tmp_path):
    store = _store(tmp_path, enabled=True)
    watch_id = store.add_watch(["rt-1"], "Check the server", hold_s=180)["id"]
    store.fire("rt-1", "failed")
    assert store.hold(watch_id, 0) == {"status": "ended", "outcome": "failed"}
    assert store.watch_count() == 0
    assert store.hold(watch_id, 0) == {"status": "gone"}
    assert store.fire_due() == []


def test_released_before_the_job_ends_calls_when_it_does(tmp_path):
    store = _store(tmp_path, enabled=True)
    watch_id = store.add_watch(["rt-1"], "Check the server", hold_s=180)["id"]
    assert store.hold(watch_id, 0) == {"status": "watching"}
    assert store.fire("rt-1", "done")["status"] == "call"


def test_a_hold_that_runs_out_calls_for_a_job_that_ended_during_it(tmp_path):
    clock = Clock()
    store = _store(tmp_path, clock=clock, enabled=True)
    store.add_watch(["rt-1", "st-1"], "Check the server", hold_s=180)
    store.fire("st-1", "done")
    clock.now += 179
    assert store.fire_due() == []
    clock.now += 2  # the phone went away mid-call: no renewal, no release
    [due] = store.fire_due()
    assert due["status"] == "call"
    assert due["outcome"] == "done"
    assert due["session_id"] == "st-1"
    assert due["watch"]["title"] == "Check the server"
    assert store.fire_due() == []
    assert store.next_due() is None


def test_a_renewed_hold_keeps_waiting(tmp_path):
    clock = Clock()
    store = _store(tmp_path, clock=clock, enabled=True)
    watch_id = store.add_watch(["rt-1"], "Check the server", hold_s=180)["id"]
    store.fire("rt-1", "done")
    clock.now += 120
    store.hold(watch_id, 180)
    clock.now += 120
    assert store.fire_due() == []


def test_a_due_call_still_obeys_the_limits(tmp_path):
    clock = Clock()
    store = _store(tmp_path, clock=clock, enabled=True)
    store.add_watch(["a"], "A")
    store.add_watch(["b"], "B", hold_s=60)
    assert store.fire("a", "done")["status"] == "call"
    store.fire("b", "done")
    clock.now += 61
    [due] = store.fire_due()
    assert due["status"] == "limited"
    assert due["reason"] == "gap"


def test_a_correction_during_the_call_is_not_the_job_ending(tmp_path):
    clock = Clock()
    store = _store(tmp_path, clock=clock, enabled=True)
    watch_id = store.add_watch(["rt-1"], "Check the server", hold_s=90)["id"]
    # The call interrupts the job to put a correction into it.
    held = store.fire("rt-1", "stopped")
    assert held["status"] == "held"
    assert held["outcome"] is None
    assert store.next_due() is None
    # Hang-up while the corrected turn runs: still watching.
    assert store.hold(watch_id, 0) == {"status": "watching"}
    assert store.fire("rt-1", "done")["status"] == "call"


def test_a_correction_before_the_watch_is_not_a_recent_end(tmp_path):
    clock = Clock()
    store = _store(tmp_path, clock=clock, enabled=True)
    store.fire("rt-1", "stopped")  # the call put a correction into the job
    clock.now += 5
    assert store.add_watch(["rt-1"], "Check the server", hold_s=90, ended_within_s=300)["status"] == "watching"


def test_a_job_stopped_after_hang_up_still_calls(tmp_path):
    store = _store(tmp_path, enabled=True)
    store.add_watch(["rt-1"], "Check the server")
    result = store.fire("rt-1", "stopped")
    assert result["status"] == "call"
    assert result["outcome"] == "stopped"


def test_a_removed_held_watch_never_calls(tmp_path):
    clock = Clock()
    store = _store(tmp_path, clock=clock, enabled=True)
    watch_id = store.add_watch(["rt-1"], "Check the server", hold_s=60)["id"]
    store.fire("rt-1", "done")
    assert store.remove_watch(watch_id) is True  # Conduit told the user in the call
    clock.now += 61
    assert store.fire_due() == []


@pytest.mark.parametrize("hold_s", [-1, 601, "60", True, 1.5])
def test_a_hold_is_whole_seconds_within_bounds(tmp_path, hold_s):
    store = _store(tmp_path, enabled=True)
    with pytest.raises(ValueError, match="hold_s"):
        store.add_watch(["rt-1"], "Job", hold_s=hold_s)
    watch_id = store.add_watch(["rt-1"], "Job")["id"]
    with pytest.raises(ValueError, match="hold_s"):
        store.hold(watch_id, hold_s)


def test_holding_an_unknown_watch_is_gone(tmp_path):
    store = _store(tmp_path, enabled=True)
    assert store.hold("0" * 24, 60) == {"status": "gone"}


def test_watches_expire_after_a_day(tmp_path):
    clock = Clock()
    store = _store(tmp_path, clock=clock, enabled=True)
    store.add_watch(["rt-1"], "Check the server")
    clock.now += 24 * 3600 + 1
    assert store.fire("rt-1", "done") is None
    assert store.watch_count() == 0


def test_a_profile_holds_at_most_twenty_watches(tmp_path):
    store = _store(tmp_path, enabled=True)
    for index in range(20):
        store.add_watch([f"s{index}"], "Job")
    with pytest.raises(ValueError, match="too_many"):
        store.add_watch(["s20"], "Job")


def test_a_watch_is_bounded_and_cleaned(tmp_path):
    store = _store(tmp_path, enabled=True)
    added = store.add_watch(["a", "a", " b ", "c", "d", "e"], "  Check\nthe server  " + "x" * 200)
    watch = store.fire("a", "done")["watch"]
    assert watch["id"] == added["id"]
    assert watch["session_ids"] == ["a", "b", "c", "d"]
    assert watch["title"].startswith("Check the server")
    assert len(watch["title"]) == 120


@pytest.mark.parametrize("session_ids", [[], [""], ["bad id"], "rt-1", [3]])
def test_a_watch_needs_a_valid_session_id(tmp_path, session_ids):
    store = _store(tmp_path, enabled=True)
    with pytest.raises(ValueError):
        store.add_watch(session_ids, "Job")


def test_remove_watch(tmp_path):
    store = _store(tmp_path, enabled=True)
    added = store.add_watch(["rt-1"], "Job")
    assert store.remove_watch(added["id"]) is True
    assert store.remove_watch(added["id"]) is False
    assert store.fire("rt-1", "done") is None


def test_two_stores_share_one_file(tmp_path):
    clock = Clock()
    dashboard = _store(tmp_path, clock=clock, enabled=True)
    agent = CallStore(tmp_path, clock=clock)
    dashboard.add_watch(["rt-1"], "Job")
    assert agent.fire("rt-1", "done")["status"] == "call"
    assert dashboard.fire("rt-1", "done") is None


def test_the_file_is_private(tmp_path):
    _store(tmp_path, enabled=True)
    path = tmp_path / "conduit-calls.json"
    assert path.stat().st_mode & 0o777 == 0o600
    assert json.loads(path.read_text())["v"] == 1


def test_a_corrupt_file_reads_as_defaults(tmp_path):
    (tmp_path / "conduit-calls.json").write_text("{not json")
    store = CallStore(tmp_path)
    assert store.settings()["enabled"] is False
    assert store.fire("rt-1", "done") is None


def test_an_unknown_outcome_is_refused(tmp_path):
    store = _store(tmp_path, enabled=True)
    with pytest.raises(ValueError):
        store.fire("rt-1", "maybe")


def test_a_turn_end_that_changes_nothing_writes_nothing(tmp_path):
    store = _store(tmp_path, enabled=True)
    store.add_watch(["rt-1"], "Check the server")
    store.update_settings({"enabled": False})  # no recent ends while off
    before = store.path.stat()
    assert store.fire("other", "done") is None
    assert store.fire_due() == []
    after = store.path.stat()
    assert (after.st_ino, after.st_mtime_ns) == (before.st_ino, before.st_mtime_ns)  # saving replaces the file


def test_a_retried_watch_for_the_same_job_is_the_same_watch(tmp_path):
    clock = Clock()
    store = _store(tmp_path, clock=clock, enabled=True)
    first = store.add_watch(["rt-1", "st-1"], "Check the server", hold_s=90)["id"]
    clock.now += 30
    # The first answer was lost; the phone asks again.
    assert store.add_watch(["st-1", "rt-1"], "Check the server", hold_s=90) == {"status": "watching", "id": first}
    assert store.watch_count() == 1
    store.fire("rt-1", "done")
    assert store.next_due() == clock.now + 90


def test_a_watch_with_a_malformed_id_is_dropped(tmp_path):
    store = _store(tmp_path, enabled=True)
    store.add_watch(["rt-1"], "Check the server")
    state = json.loads(store.path.read_text())
    state["watches"][0]["id"] = "bad id!"
    store.path.write_text(json.dumps(state))
    assert store.watch_count() == 0
    assert store.fire("rt-1", "done") is None


def test_call_history_on_disk_is_capped_to_the_newest_calls(tmp_path):
    clock = Clock()
    store = _store(tmp_path, clock=clock, enabled=True)
    store.add_watch(["rt-1"], "Check the server")
    state = json.loads(store.path.read_text())
    state["history"] = [clock.now - 10 - at for at in range(500)]
    normalized = calls_store._normalized(state, clock.now)
    assert len(normalized["history"]) == calls_store.MAX_HISTORY == 60
    assert normalized["history"][-1] == clock.now - 10
    assert normalized["history"] == sorted(normalized["history"])


def test_hermes_can_ask_for_a_call_only_as_the_settings_allow(tmp_path):
    store = _store(tmp_path)
    with pytest.raises(ValueError, match="calls_off"):
        store.add_tool_watch("st-1", "The deploy finished.", asked=True)
    store.update_settings({"enabled": True})
    with pytest.raises(ValueError, match="calls_off"):
        store.add_tool_watch("st-1", "The deploy finished.", asked=False)
    watch_id = store.add_tool_watch("st-1", "  The deploy\nfinished. ", asked=True)["id"]
    [watch] = store._load()["watches"]
    assert (watch["id"], watch["origin"], watch["reason"], watch["asked"]) == (watch_id, "tool", "The deploy finished.", True)
    store.update_settings({"decides": True})
    assert store.add_tool_watch("st-2", "x" * 300, asked=False)["id"] != watch_id
    assert len(store._load()["watches"][1]["reason"]) == 200


def test_a_session_already_watched_keeps_its_watch_when_hermes_asks_too(tmp_path):
    store = _store(tmp_path, enabled=True)
    watch_id = store.add_watch(["rt-1", "st-1"], "Check the server")["id"]
    assert store.add_tool_watch("st-1", "The server is back.", asked=True) == {"status": "watching", "id": watch_id}
    [watch] = store._load()["watches"]
    assert (watch["origin"], watch["reason"], watch["title"]) == ("conduit", "The server is back.", "Check the server")
    result = store.fire("rt-1", "done")
    assert result["status"] == "call" and result["watch"]["reason"] == "The server is back."


def test_a_tool_watch_turned_off_before_its_turn_ends_does_not_call(tmp_path):
    store = _store(tmp_path, enabled=True, decides=True)
    store.add_tool_watch("st-1", "Heads up.", asked=False)
    store.update_settings({"decides": False})
    assert store.fire("st-1", "done")["status"] == "off"
    store.add_tool_watch("st-2", "You asked.", asked=True)
    assert store.fire("st-2", "done")["status"] == "call"


def test_an_unanswered_approval_calls_after_a_minute(tmp_path):
    clock = Clock()
    store = _store(tmp_path, clock=clock, enabled=True)
    assert store.add_alert("sk-1", "approval", "Run rm -rf build?") is None, "alerts are off"
    store.update_settings({"alerts": True})
    alert_id = store.add_alert("sk-1", "approval", "Run rm -rf build?")
    assert alert_id and store.add_alert("sk-1", "approval", "again") is None, "one at a time"
    assert store.next_due() == clock.now + calls_store.ALERT_DELAY_S
    clock.now += 59
    assert store.fire_due() == []
    clock.now += 2
    [due] = store.fire_due()
    assert (due["status"], due["outcome"], due["session_id"]) == ("call", "approval", "sk-1")
    assert (due["watch"]["id"], due["watch"]["reason"]) == (alert_id, "Run rm -rf build?")
    assert store.watch_count() == 0


def test_an_answered_question_or_a_finished_turn_calls_no_more(tmp_path):
    clock = Clock()
    store = _store(tmp_path, clock=clock, enabled=True, alerts=True)
    store.add_alert("st-1", "question", "Which branch?")
    store.add_alert("st-1", "approval", "Push?")
    assert store.cancel_alerts("st-1", "question") == 1
    assert store.cancel_alerts("st-1", "question") == 0
    assert store.fire("st-1", "done") is None, "an alert is not a job watch"
    clock.now += 120
    assert store.fire_due() == [], "the turn ending dropped the approval"
    assert store.watch_count() == 0


def test_an_alert_does_not_write_anything_while_alerts_are_off(tmp_path):
    store = CallStore(tmp_path, clock=Clock())
    assert store.add_alert("st-1", "question", "Which branch?") is None
    assert store.alert_now("st-1", "failed", "") is None
    assert store.cancel_alerts("st-1") == 0
    assert not store.path.exists()


def test_a_failed_turn_alert_counts_against_the_limits(tmp_path):
    clock = Clock()
    store = _store(tmp_path, clock=clock, enabled=True, alerts=True)
    first = store.alert_now("st-1", "failed", "")
    assert first["status"] == "call" and first["watch"]["origin"] == "alert"
    assert store.alert_now("st-2", "failed", "")["status"] == "limited"
    assert store.watch_count() == 0


def test_nothing_rings_while_the_user_is_in_a_call(tmp_path):
    clock = Clock()
    store = _store(tmp_path, clock=clock, enabled=True, alerts=True)
    store.add_watch(["st-1"], "Check the server")
    assert store.set_presence(90) == {"status": "present"}
    assert store.fire("st-1", "done")["status"] == "busy"
    assert store.alert_now("st-2", "failed", "")["status"] == "busy"
    clock.now += 91
    assert store.alert_now("st-3", "failed", "")["status"] == "call", "a presence that ran out rings again"
    store.set_presence(90)
    assert store.set_presence(0) == {"status": "away"}
    clock.now += 200
    store.add_watch(["st-4"], "Check the server")
    assert store.fire("st-4", "done")["status"] == "call"


def test_presence_released_before_any_state_writes_nothing(tmp_path):
    store = CallStore(tmp_path, clock=Clock())
    assert store.set_presence(0) == {"status": "away"}
    assert not store.path.exists()
    with pytest.raises(ValueError):
        store.set_presence(601)


def test_watches_from_an_older_plugin_read_as_conduit_watches(tmp_path):
    clock = Clock()
    store = _store(tmp_path, clock=clock, enabled=True)
    store.add_watch(["rt-1"], "Check the server")
    state = json.loads(store.path.read_text())
    for key in ("origin", "reason", "asked"):
        del state["watches"][0][key]
    state["watches"].append({**state["watches"][0], "id": "b" * 24, "origin": "alert", "pending": None})
    store.path.write_text(json.dumps(state))
    [watch] = store._load()["watches"]
    assert (watch["origin"], watch["reason"], watch["asked"]) == ("conduit", "", False)
