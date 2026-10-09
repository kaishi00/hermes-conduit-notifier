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
    assert store.add_watch(["st-1", "rt-1"], "Check the server") == {"status": "ended", "outcome": "failed"}
    assert store.watch_count() == 0


def test_recent_ends_are_forgotten_after_half_an_hour(tmp_path):
    clock = Clock()
    store = _store(tmp_path, clock=clock, enabled=True)
    store.fire("rt-1", "done")
    clock.now += 31 * 60
    assert store.add_watch(["rt-1"], "Check the server")["status"] == "watching"


def test_recent_ends_are_not_recorded_while_calls_are_off(tmp_path):
    store = _store(tmp_path)
    store.fire("rt-1", "done")
    store.update_settings({"enabled": True})
    assert store.add_watch(["rt-1"], "Check the server")["status"] == "watching"


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
