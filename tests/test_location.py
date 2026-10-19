import importlib.util
import json
import os
import pathlib
import secrets
import sqlite3
import stat
import sys
import tempfile
import types
from datetime import datetime, timezone

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey, X25519PublicKey
from fastapi import FastAPI
from fastapi.testclient import TestClient

ROOT = pathlib.Path(__file__).resolve().parents[1]
BASE = "/api/plugins/conduit_push"

if "hermes_constants" not in sys.modules:
    _hermes_constants = types.ModuleType("hermes_constants")
    _hermes_constants.get_hermes_home = lambda: pathlib.Path(tempfile.gettempdir())
    sys.modules["hermes_constants"] = _hermes_constants
if "conduit_push" not in sys.modules:
    _pkg = types.ModuleType("conduit_push")
    _pkg.__path__ = [str(ROOT)]
    sys.modules["conduit_push"] = _pkg


def _load(name, path):
    if name in sys.modules:
        return sys.modules[name]
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


location = _load("conduit_push.location_store", ROOT / "location_store.py")
tool = _load("conduit_push.location_tool", ROOT / "location_tool.py")
calls_store = _load("conduit_push.calls_store", ROOT / "calls_store.py")
calls = _load("conduit_push.calls", ROOT / "calls.py")
_api_spec = importlib.util.spec_from_file_location("conduit_plugin_api_location", ROOT / "dashboard" / "plugin_api.py")
api = importlib.util.module_from_spec(_api_spec)
_api_spec.loader.exec_module(api)

DEVICE = "phone-installation-1"
# Tuesday 13 October 2026, 07:00 in London (06:00 UTC).
NOW = datetime(2026, 10, 13, 6, 0, tzinfo=timezone.utc).timestamp()
VECTOR = json.loads((ROOT / "tests" / "location_vector.json").read_text())


class Phone:
    """Conduit's side of the protocol, as the Swift code does it."""

    def __init__(self, device_id=DEVICE):
        self.device_id = device_id
        self.private = X25519PrivateKey.generate()
        self.public = self.private.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)
        self.kid = None
        self.key = None
        self.count = 0

    def key_request(self):
        return {"device_id": self.device_id, "device_name": "Eric's iPhone", "public_key": location.b64u(self.public)}

    def accept(self, answer):
        host_public = location.unb64u(answer["host_public_key"])
        shared = self.private.exchange(X25519PublicKey.from_public_bytes(host_public))
        self.key = location.derive_key(shared, self.device_id, self.public, host_public)
        self.kid = answer["kid"]
        assert self.kid == location.key_id(self.public, host_public)

    def seal(self, batch):
        batch_id = batch["batch_id"]
        nonce, ct = location.seal(self.key, json.dumps(batch).encode(),
                                  location.ingest_aad(self.kid, self.device_id, batch_id))
        return {"v": 1, "device_id": self.device_id, "kid": self.kid, "batch_id": batch_id, "nonce": nonce, "ct": ct}

    def batch(self, **extra):
        self.count += 1
        batch = {"batch_id": f"batch-{self.count:06d}", "sent_at": "2026-10-13T07:00:00+01:00",
                 "sharing": sharing(), "places": PLACES}
        batch.update(extra)
        return batch


PLACES = [
    {"id": "p_home", "name": "Home", "kind": "home", "private": False},
    {"id": "p_work", "name": "Work", "kind": "work", "private": False},
    {"id": "p_clinic", "name": "Clinic", "kind": "other", "private": True},
]


def sharing(**changes):
    value = {"talking": True, "talking_precision": "precise", "background": True, "background_precision": "area",
             "retention_days": 7, "paused_until": None, "time_zone": "Europe/London"}
    value.update(changes)
    return value


def fix(source="message", **changes):
    value = {"id": f"f_{source}", "at": "2026-10-13T06:58:00+01:00", "source": source, "lat": 51.532141,
             "lon": -0.075512, "accuracy_m": 12, "place_id": None,
             "area": {"name": "Shoreditch", "locality": "London", "country": "United Kingdom", "country_code": "GB"},
             "distances_m": {"p_home": 1234, "p_work": 4321, "p_clinic": 900}, "speed_mps": 0.4}
    value.update(changes)
    return value


@pytest.fixture()
def store(tmp_path):
    return location.LocationStore(tmp_path, clock=lambda: NOW)


@pytest.fixture()
def phone(store):
    phone = Phone()
    phone.accept(store.register_key(phone.key_request()))
    return phone


# --- The store ---------------------------------------------------------------


def test_a_sealed_fix_lands_at_the_detail_the_user_chose(store, phone):
    result = store.ingest(phone.seal(phone.batch(fix=fix())))
    assert result["fix"] is True
    view = store.snapshot()
    assert view["fix"]["lat"] == 51.532141 and view["fix"]["distances_m"]["p_home"] == 1230
    assert [place["name"] for place in view["places"].values()] == ["Home", "Work", "Clinic"]


def test_area_detail_rounds_on_the_host_too(store, phone):
    store.ingest(phone.seal(phone.batch(fix=fix("significant"))))
    stored = store.snapshot()["fix"]
    assert (stored["lat"], stored["lon"], stored["accuracy_m"]) == (51.53, -0.08, 1000.0)
    assert stored["distances_m"] == {"p_home": 1200, "p_work": 4300, "p_clinic": 900}
    assert stored["speed_mps"] is None


def test_places_only_keeps_no_coordinates_or_area(store, phone):
    batch = phone.batch(sharing=sharing(talking_precision="places"), fix=fix(place_id="p_home"))
    store.ingest(phone.seal(batch))
    stored = store.snapshot()["fix"]
    assert stored["lat"] is None and stored["area"] is None and stored["distances_m"] == {}
    assert stored["place_id"] == "p_home"


def test_a_private_place_hides_everything_but_itself(store, phone):
    store.ingest(phone.seal(phone.batch(fix=fix(place_id="p_clinic"))))
    stored = store.snapshot()["fix"]
    assert stored["lat"] is None and stored["area"] is None and stored["precision"] == "places"


def test_making_a_place_private_forgets_what_it_revealed(store, phone):
    arrive = {"id": "e_home", "kind": "arrive", "place_id": "p_home", "at": "2026-10-13T06:50:00+01:00"}
    store.ingest(phone.seal(phone.batch(fix=fix(place_id="p_home"), events=[arrive])))
    # The user marks Home private: Conduit sends just the settings.
    store.ingest(phone.seal(phone.batch(places=[{**PLACES[0], "private": True}, *PLACES[1:]])))
    stored = store.snapshot()["fix"]
    assert stored["place_id"] == "p_home" and stored["precision"] == "places"
    assert (stored["lat"], stored["lon"], stored["accuracy_m"], stored["area"]) == (None, None, None, None)
    assert stored["distances_m"] == {} and stored["speed_mps"] is None
    assert store.history(DEVICE, 0, NOW + 1, 10)[0] == []


def test_a_fix_elsewhere_keeps_its_detail_when_a_place_turns_private(store, phone):
    store.ingest(phone.seal(phone.batch(fix=fix(place_id="p_work"))))
    store.ingest(phone.seal(phone.batch(places=[{**PLACES[0], "private": True}, *PLACES[1:]])))
    stored = store.snapshot()["fix"]
    assert stored["lat"] == 51.532141 and stored["area"]["name"] == "Shoreditch" and stored["precision"] == "precise"


def test_saved_places_never_carry_coordinates(store, phone):
    places = [{**PLACES[0], "lat": 51.5, "lon": -0.1}]
    with pytest.raises(location.LocationError) as error:
        store.ingest(phone.seal(phone.batch(places=places)))
    assert error.value.code == "malformed"


def test_a_layer_that_is_off_sends_nothing(store, phone):
    with pytest.raises(location.LocationError) as error:
        store.ingest(phone.seal(phone.batch(sharing=sharing(talking=False), fix=fix())))
    assert error.value.code == "layer_off"
    events = [{"id": "e1", "kind": "leave", "place_id": "p_home", "at": "2026-10-13T06:58:00+01:00"}]
    with pytest.raises(location.LocationError):
        store.ingest(phone.seal(phone.batch(sharing=sharing(background=False), events=events)))


def test_pausing_forgets_the_latest_fix(store, phone):
    store.ingest(phone.seal(phone.batch(fix=fix())))
    store.ingest(phone.seal(phone.batch(sharing=sharing(paused_until="2099-01-01T00:00:00+00:00"))))
    view = store.snapshot()
    assert view["fix"] is None
    assert location.sharing_paused(view["sharing"], NOW)


def test_a_late_older_batch_never_undoes_a_pause(store, phone):
    # Sent before the pause but arriving after it (a retry, or a replay).
    older = phone.seal(phone.batch(sent_at="2026-10-13T06:58:30+01:00", fix=fix()))
    store.ingest(phone.seal(phone.batch(sent_at="2026-10-13T06:59:30+01:00",
                                        sharing=sharing(paused_until="2099-01-01T00:00:00+00:00"))))
    result = store.ingest(older)
    view = store.snapshot()
    assert location.sharing_paused(view["sharing"], NOW)
    assert view["fix"] is None
    assert result["stale"] is True and result["fix"] is False


def test_a_batch_stamped_in_the_future_doesnt_make_the_next_one_stale(store, phone):
    clock = [NOW]
    store.clock = lambda: clock[0]
    store.ingest(phone.seal(phone.batch(sent_at="2026-10-13T20:00:00+01:00", fix=fix())))
    clock[0] += 60
    store.ingest(phone.seal(phone.batch(sent_at="2026-10-13T07:00:30+01:00",
                                        sharing=sharing(paused_until="2099-01-01T00:00:00+00:00"))))
    assert store.latest_place()["paused"] is True


@pytest.mark.parametrize("version", ["1", "2"])
def test_an_older_store_upgrades_and_still_takes_batches(store, phone, version):
    import sqlite3
    store.ingest(phone.seal(phone.batch(fix=fix())))
    db = sqlite3.connect(store.path)
    db.execute("ALTER TABLE devices DROP COLUMN last_sent")
    if version == "1":
        db.execute("DROP TABLE maps")
    db.execute("UPDATE meta SET value=? WHERE key='schema'", (version,))
    db.commit()
    db.close()
    assert store.snapshot()["fix"]["id"] == "f_message"
    store.ingest(phone.seal(phone.batch(sent_at="2026-10-13T06:59:30+01:00",
                                        sharing=sharing(paused_until="2099-01-01T00:00:00+00:00"))))
    assert store.ingest(phone.seal(phone.batch(sent_at="2026-10-13T06:58:30+01:00", fix=fix())))["stale"] is True
    assert store.snapshot()["fix"] is None


def test_a_second_device_without_a_fix_doesnt_hide_the_first_ones(store, phone):
    clock = [NOW]
    store.clock = lambda: clock[0]
    store.ingest(phone.seal(phone.batch(fix=fix(place_id="p_work"))))
    clock[0] += 60
    ipad = Phone("ipad-device-02")
    ipad.accept(store.register_key(ipad.key_request()))
    store.ingest(ipad.seal(ipad.batch()))  # synced later, nothing to say
    assert store.snapshot()["fix"]["place_id"] == "p_work"
    clock[0] += 60
    # But pausing on the device used last is the user's latest word.
    store.ingest(ipad.seal(ipad.batch(sharing=sharing(paused_until="2099-01-01T00:00:00+00:00"))))
    assert store.latest_place()["paused"] is True


def test_a_fix_stamped_in_the_future_doesnt_hide_later_ones(store, phone):
    clock = [NOW]
    store.clock = lambda: clock[0]
    store.ingest(phone.seal(phone.batch(fix=fix(id="f_ahead", at="2026-10-13T20:00:00+01:00"))))
    assert store.snapshot()["fix"]["at"] == NOW
    clock[0] += 60
    store.ingest(phone.seal(phone.batch(fix=fix(id="f_real", at="2026-10-13T07:00:30+01:00"))))
    assert store.snapshot()["fix"]["id"] == "f_real"


def test_a_move_stamped_in_the_future_runs_no_rule(store, phone):
    _rule(store, days=[], hours="")
    leave = {"id": "e1", "kind": "leave", "place_id": "p_home", "at": "2026-10-13T18:00:00+01:00"}
    assert store.ingest(phone.seal(phone.batch(events=[leave])))["rules_fired"] == []
    assert store.rules()[0]["last_fired"] is None


def test_a_tampered_or_foreign_body_is_refused(store, phone):
    sealed = phone.seal(phone.batch(fix=fix()))
    with pytest.raises(location.LocationError) as error:
        store.ingest({**sealed, "batch_id": "batch-999999"})
    assert error.value.code == "bad_seal"
    with pytest.raises(location.LocationError) as error:
        store.ingest({**sealed, "kid": "0" * 32})
    assert error.value.code == "unknown_key"


def test_the_file_is_owner_only(store, phone):
    assert stat.S_IMODE(os.stat(store.path).st_mode) == 0o600


def test_history_keeps_arrivals_for_the_chosen_days_and_never_private_ones(store, phone):
    events = [
        {"id": "e_old", "kind": "arrive", "place_id": "p_work", "at": "2026-10-01T09:00:00+01:00"},
        {"id": "e_new", "kind": "leave", "place_id": "p_home", "at": "2026-10-13T06:50:00+01:00"},
        {"id": "e_private", "kind": "arrive", "place_id": "p_clinic", "at": "2026-10-13T06:55:00+01:00"},
    ]
    store.ingest(phone.seal(phone.batch(events=events)))
    rows, more = store.history(DEVICE, 0, NOW + 1, 10)
    assert rows == [{"at": "2026-10-13T06:50:00+01:00", "event": "left", "place": "Home"}] and not more
    store.ingest(phone.seal(phone.batch(sharing=sharing(retention_days=0))))
    assert store.history(DEVICE, 0, NOW + 1, 10) == ([], False)


def _rule(store, **changes):
    rule = {"id": "r_brief0001", "job_id": "job-1", "what": "morning brief", "place_id": "p_home",
            "place_name": "Home", "event": "leave", "days": ["mon", "tue", "wed", "thu", "fri"],
            "hours": "06:00-10:00", "task": "Brief me.", "call": True}
    rule.update(changes)
    return store.put_rule(rule)


def test_leaving_home_on_a_weekday_morning_runs_the_rule_once(store, phone):
    _rule(store)
    leave = {"id": "e1", "kind": "leave", "place_id": "p_home", "at": "2026-10-13T06:55:00+01:00"}
    fired = store.ingest(phone.seal(phone.batch(events=[leave])))["rules_fired"]
    assert [rule["job_id"] for rule in fired] == ["job-1"]
    assert "left Home at 06:55 (Tue 13 Oct" in fired[0]["prompt"]
    again = {**leave, "id": "e2", "at": "2026-10-13T06:57:00+01:00"}
    assert store.ingest(phone.seal(phone.batch(events=[again])))["rules_fired"] == []
    assert store.rules()[0]["last_fired"] == "2026-10-13"


@pytest.mark.parametrize("event", [
    {"kind": "arrive"},
    {"at": "2026-10-13T06:30:00+01:00"},  # reported over 15 minutes late
    {"place_id": "p_work"},
])
def test_a_rule_runs_only_for_its_own_move(store, phone, event):
    _rule(store)
    leave = {"id": "e1", "kind": "leave", "place_id": "p_home", "at": "2026-10-13T06:55:00+01:00", **event}
    assert store.ingest(phone.seal(phone.batch(events=[leave])))["rules_fired"] == []


def test_a_rule_for_any_place_and_either_move_runs_every_time_with_a_gap(store, phone):
    clock = [NOW]
    store.clock = lambda: clock[0]
    _rule(store, place_id=location.ANY_PLACE, place_name="any saved place", event="either", days=[], hours="",
          repeat="every")
    arrive = {"id": "e1", "kind": "arrive", "place_id": "p_work", "at": "2026-10-13T06:55:00+01:00"}
    fired = store.ingest(phone.seal(phone.batch(events=[arrive])))["rules_fired"]
    assert len(fired) == 1 and "arrived at Work" in fired[0]["prompt"]
    leave = {"id": "e2", "kind": "leave", "place_id": "p_work", "at": "2026-10-13T06:58:00+01:00"}
    clock[0] += 180
    assert store.ingest(phone.seal(phone.batch(events=[leave])))["rules_fired"] == []  # within 30 minutes
    clock[0] += location.RULE_EVERY_MIN_GAP_S
    later = {**leave, "id": "e3", "at": datetime.fromtimestamp(clock[0], timezone.utc).isoformat()}
    fired = store.ingest(phone.seal(phone.batch(events=[later])))["rules_fired"]
    assert len(fired) == 1 and "left Work" in fired[0]["prompt"]
    assert store.rules()[0]["repeat"] == "every"


def test_a_rule_outside_its_hours_or_days_waits(store, phone):
    _rule(store, hours="08:00-10:00")
    leave = {"id": "e1", "kind": "leave", "place_id": "p_home", "at": "2026-10-13T06:55:00+01:00"}
    assert store.ingest(phone.seal(phone.batch(events=[leave])))["rules_fired"] == []
    _rule(store, hours="", days=["sat", "sun"])
    assert store.ingest(phone.seal(phone.batch(events=[{**leave, "id": "e2"}])))["rules_fired"] == []


def test_a_paused_phone_runs_no_rules(store, phone):
    _rule(store)
    leave = {"id": "e1", "kind": "leave", "place_id": "p_home", "at": "2026-10-13T06:55:00+01:00"}
    batch = phone.batch(sharing=sharing(paused_until="2026-10-13T12:00:00+01:00"), events=[leave])
    assert store.ingest(phone.seal(batch))["rules_fired"] == []
    # Nor keeps the move in history.
    assert store.history(store.snapshot()["device_id"], 0, NOW + 3600, 50)[0] == []


def test_damaged_stored_json_reads_as_missing(store, phone):
    store.ingest(phone.seal(phone.batch(fix=fix())))
    with sqlite3.connect(store.path) as db:
        db.execute("UPDATE devices SET sharing='{'")
        db.execute("UPDATE fixes SET area='[', distances='{'")
    view = store.snapshot()
    assert view["sharing"] == {} and view["fix"]["area"] is None and view["fix"]["distances_m"] == {}
    assert store.status(view["device_id"])["device"]["sharing"] is None


def test_a_private_place_rule_never_names_the_place(store, phone):
    _rule(store, place_id="p_clinic", place_name="Clinic", event="arrive", days=[], hours="")
    arrive = {"id": "e1", "kind": "arrive", "place_id": "p_clinic", "at": "2026-10-13T06:55:00+01:00"}
    fired = store.ingest(phone.seal(phone.batch(events=[arrive])))["rules_fired"]
    assert "a private place" in fired[0]["prompt"] and "Clinic" not in fired[0]["prompt"]


def test_hours_can_wrap_past_midnight():
    hours = location.parse_hours("22:00-06:00")
    assert location.in_hours(23 * 60, hours) and location.in_hours(5 * 60, hours)
    assert not location.in_hours(12 * 60, hours)
    assert location.parse_hours("06:00-24:00") == (360, 1440)
    assert location.parse_hours("25:00-26:00") is None and location.parse_hours("06:00-06:00") is None


def test_locate_keeps_to_its_budget(store, phone):
    clock = [NOW]
    store.clock = lambda: clock[0]
    first = store.start_locate()
    with pytest.raises(location.LocationError) as error:
        store.start_locate()
    assert error.value.code == "too_soon"
    store.cancel_locate(first)
    second = store.start_locate()  # a request the relay couldn't send doesn't count
    answer = fix("locate", id="f_locate")
    store.ingest(phone.seal(phone.batch(fix=answer, locate_id=second)))
    assert store.locate_answered(second)
    for _ in range(location.LOCATE_PER_DAY - 1):
        clock[0] += location.LOCATE_MIN_GAP_S
        store.start_locate()
    clock[0] += location.LOCATE_MIN_GAP_S
    with pytest.raises(location.LocationError) as error:
        store.start_locate()
    assert error.value.code == "daily_limit"


def test_reads_are_logged_without_what_was_read(store, phone):
    store.log_read("now", "session-1", "Trip planning")
    assert store.reads() == [{"at": location.iso_utc(NOW), "action": "now", "session_title": "Trip planning"}]
    assert store.status(DEVICE)["reads_24h"] == 1


def test_wipe_all_deletes_the_file_and_names_the_rule_jobs(store, phone):
    _rule(store)
    store.ingest(phone.seal(phone.batch(fix=fix())))
    assert store.wipe(DEVICE)["remaining_devices"] == 0
    assert store.path.exists(), "rules outlive one device's data"
    assert store.wipe()["rule_jobs"] == ["job-1"]
    assert not store.path.exists()


def test_shared_vector_matches_conduit():
    # ConduitTests checks the same values.
    host_private = location.unb64u(VECTOR["host_private"])
    device_private = location.unb64u(VECTOR["device_private"])
    raw = (serialization.Encoding.Raw, serialization.PublicFormat.Raw)
    host_public = X25519PrivateKey.from_private_bytes(host_private).public_key().public_bytes(*raw)
    device_public = X25519PrivateKey.from_private_bytes(device_private).public_key().public_bytes(*raw)
    kid = location.key_id(device_public, host_public)
    key = location.derive_key(location.agree(host_private, device_public), VECTOR["device_id"], device_public, host_public)
    aad = location.ingest_aad(kid, VECTOR["device_id"], VECTOR["batch_id"])
    nonce, ct = location.seal(key, VECTOR["plaintext"].encode(), aad, nonce=bytes(12))
    assert (kid, key.hex(), aad.decode(), nonce, ct) == (VECTOR["kid"], VECTOR["key"], VECTOR["aad"], VECTOR["nonce"], VECTOR["ct"])
    assert location.open_sealed(key, nonce, ct, aad) == VECTOR["plaintext"].encode()


# --- The tool -------------------------------------------------------------------


@pytest.fixture()
def home(tmp_path, monkeypatch):
    monkeypatch.setattr(tool, "get_hermes_home", lambda: tmp_path)
    monkeypatch.setattr(tool.session_titles, "title", lambda key: "Trip planning")
    original = tool.LocationStore
    monkeypatch.setattr(tool, "LocationStore", lambda path: original(path, clock=lambda: NOW))
    return tmp_path


def _shared(home, **batch):
    store = location.LocationStore(home, clock=lambda: NOW)
    phone = Phone()
    phone.accept(store.register_key(phone.key_request()))
    store.ingest(phone.seal(phone.batch(**batch)))
    return store, phone


def _call(args, **kw):
    return json.loads(tool.handle(args, **kw))


def test_the_tool_hides_until_location_arrives(home):
    assert tool.available() is False
    assert _call({"action": "now"})["error"] == "nothing_shared"
    _shared(home, fix=fix())
    assert tool.available() is True


def test_listing_rules_after_a_wipe_doesnt_bring_the_file_back(home):
    store, _ = _shared(home, fix=fix())
    store.wipe()
    assert _call({"action": "rules"}, session_id="session-1")["rules"] == []
    assert not store.path.exists()


def test_now_says_where_how_old_and_never_a_private_distance(home):
    store, _ = _shared(home, fix=fix())
    answer = _call({"action": "now"}, session_id="session-1")
    assert answer["ok"] and answer["as_of"].endswith("(2 min ago)")
    assert answer["area"] == "Shoreditch, London, United Kingdom"
    assert answer["coordinates"]["lat"] == 51.532141
    assert answer["distance_to_saved_places"] == {"Home": "1.2 km", "Work": "4.3 km"}
    assert store.reads()[0]["action"] == "now"


def test_distance_to_and_places_respect_private(home):
    _shared(home, fix=fix())
    assert _call({"action": "distance_to", "place": "home"})["distance"] == "1.2 km"
    assert _call({"action": "distance_to", "place": "Clinic"})["error"] == "private"
    assert {"name": "a private place", "private": True} in _call({"action": "places"})["places"]


def test_a_paused_user_is_never_guessed(home):
    _shared(home, sharing=sharing(paused_until="2099-01-01T00:00:00+00:00"))
    assert _call({"action": "now"})["error"] == "paused"


def test_subagents_cannot_read_location(home):
    _shared(home, fix=fix())
    answer = _call({"action": "now"}, session_id="child", is_child=lambda sid: sid == "child")
    assert answer["error"] == "subagent"


def test_history_reads_local_days(home):
    events = [{"id": "e1", "kind": "leave", "place_id": "p_home", "at": "2026-10-13T06:50:00+01:00"}]
    _shared(home, events=events)
    answer = _call({"action": "history", "from": "2026-10-13", "to": "2026-10-13"})
    assert answer["history"] == [{"at": "2026-10-13T06:50:00+01:00", "event": "left", "place": "Home"}]
    assert _call({"action": "history", "from": "2026-08-01"})["error"] == "malformed"


# Europe/London's 25-hour day (clocks back) and 23-hour day (clocks forward).
@pytest.mark.parametrize("now, day, inside, after", [
    (datetime(2025, 10, 27, 12, 0, tzinfo=timezone.utc), "2025-10-26",
     "2025-10-26T23:30:00+00:00", "2025-10-27T00:30:00+00:00"),
    (datetime(2026, 3, 30, 12, 0, tzinfo=timezone.utc), "2026-03-29",
     "2026-03-29T23:30:00+01:00", "2026-03-30T00:30:00+01:00"),
])
def test_history_days_follow_dst(tmp_path, monkeypatch, now, day, inside, after):
    monkeypatch.setattr(tool, "get_hermes_home", lambda: tmp_path)
    monkeypatch.setattr(tool.session_titles, "title", lambda key: "Trip planning")
    original = tool.LocationStore
    monkeypatch.setattr(tool, "LocationStore", lambda path: original(path, clock=now.timestamp))
    store = location.LocationStore(tmp_path, clock=now.timestamp)
    phone = Phone()
    phone.accept(store.register_key(phone.key_request()))
    events = [{"id": "e_in", "kind": "arrive", "place_id": "p_home", "at": inside},
              {"id": "e_next", "kind": "leave", "place_id": "p_home", "at": after}]
    store.ingest(phone.seal(phone.batch(sent_at=now.isoformat(), events=events)))
    answer = _call({"action": "history", "from": day, "to": day})
    assert [entry["at"] for entry in answer["history"]] == [inside]


class FakeScheduler:
    def __init__(self):
        self.jobs = {}
        self.calls = []
        self.list_fails = False

    def __call__(self, **kwargs):
        self.calls.append(kwargs)
        action = kwargs["action"]
        if action == "create":
            job_id = f"job-{len(self.jobs) + 1}"
            self.jobs[job_id] = {"job_id": job_id, **kwargs}
            return json.dumps({"success": True, "job_id": job_id})
        if action == "update":
            self.jobs[kwargs["job_id"]].update(kwargs)
            return json.dumps({"success": True})
        if action == "list":
            if self.list_fails:
                return json.dumps({"success": False, "error": "busy"})
            return json.dumps({"success": True, "jobs": list(self.jobs.values())})
        if action == "remove":
            self.jobs.pop(kwargs["job_id"], None)
            return json.dumps({"success": True})
        raise AssertionError(action)


@pytest.fixture()
def scheduler(monkeypatch):
    fake = FakeScheduler()
    monkeypatch.setattr(tool.call_watches, "_cronjob", lambda: fake)
    monkeypatch.setattr(tool.call_watches, "_cron_toolsets", lambda: None)
    return fake


RULE = {"what": "weekday morning brief", "place": "Home", "event": "leave", "days": ["mon", "fri", "tue"],
        "hours": "06:00-10:00", "task": "Give the user their daily brief.", "call": True}


def test_set_rule_writes_one_job_and_changes_it_in_place(home, scheduler):
    _shared(home, fix=fix())
    answer = _call({"action": "set_rule", "rule": RULE})
    assert answer["result"] == "created" and answer["rule"]["days"] == ["mon", "tue", "fri"]
    job = scheduler.jobs["job-1"]
    assert job["name"] == "Place rule: weekday morning brief" and job["schedule"] == tool.RULE_SCHEDULE
    assert tool.EVENT_MARK in job["prompt"] and "conduit_call_user" in job["prompt"]
    answer = _call({"action": "set_rule", "rule": {**RULE, "what": "Weekday morning brief", "call": False}})
    assert answer["result"] == "changed" and list(scheduler.jobs) == ["job-1"]
    assert "conduit_call_user" not in scheduler.jobs["job-1"]["prompt"]
    rules = _call({"action": "rules"})["rules"]
    assert len(rules) == 1 and rules[0]["calls"] is False
    assert _call({"action": "remove_rule", "id": rules[0]["id"]})["result"] == "removed"
    assert scheduler.jobs == {} and _call({"action": "rules"})["rules"] == []


def test_set_rule_scrubs_the_task_but_keeps_its_lines(home, scheduler):
    _shared(home, fix=fix())
    answer = _call({"action": "set_rule", "rule": {**RULE, "task": "Brief me\u202e.\nThen\x07 stop."}})
    assert answer["rule"]["task"] == "Brief me .\nThen  stop."


def test_a_rule_that_cant_be_saved_leaves_no_job(home, scheduler, monkeypatch):
    _shared(home, fix=fix())

    def fail(self, rule, **kwargs):
        raise location.LocationError("too_many_rules", "full", status=409)

    monkeypatch.setattr(location.LocationStore, "put_rule", fail)
    assert _call({"action": "set_rule", "rule": RULE})["error"] == "too_many_rules"
    assert scheduler.jobs == {}


def test_editing_a_rule_while_jobs_cant_be_listed_makes_no_second_job(home, scheduler):
    _shared(home, fix=fix())
    _call({"action": "set_rule", "rule": RULE})
    scheduler.list_fails = True
    assert _call({"action": "set_rule", "rule": {**RULE, "call": False}})["error"] == "scheduler"
    assert list(scheduler.jobs) == ["job-1"]


def test_set_rule_takes_any_place_either_move_and_every_time(home, scheduler):
    _shared(home, fix=fix())
    rule = {"what": "log my day", "place": "any", "event": "either", "repeat": "every",
            "task": "Note where the user went in today's journal."}
    answer = _call({"action": "set_rule", "rule": rule})
    assert answer["rule"]["place"] == "any saved place" and answer["rule"]["repeat"] == "every"
    assert "arrives at or leaves any saved place" in scheduler.jobs["job-1"]["prompt"]
    assert "every 30 minutes" in answer["note"]


@pytest.mark.parametrize("change, error", [
    ({"place": "Gym"}, "unknown_place"),
    ({"event": "stay"}, "invalid"),
    ({"days": ["someday"]}, "invalid"),
    ({"repeat": "hourly"}, "invalid"),
    ({"what": "x" * 200}, "invalid"),
    ({"hours": "morning"}, "invalid"),
    ({"task": ""}, "invalid"),
])
def test_set_rule_refuses_what_it_cant_run(home, scheduler, change, error):
    _shared(home, fix=fix())
    assert _call({"action": "set_rule", "rule": {**RULE, **change}})["error"] == error
    assert scheduler.jobs == {}


class FakeClient:
    def __init__(self, answer, on_send=None):
        self.answer = answer
        self.on_send = on_send
        self.sent = []

    def load_state(self):
        return {"relay_url": "https://relay.example"}

    def send_now(self, event):
        self.sent.append(event)
        if self.on_send:
            self.on_send(event)
        return self.answer


def test_locate_asks_the_phone_and_returns_its_answer(home, monkeypatch):
    store, phone = _shared(home, fix=fix("significant"))

    def answer(event):
        locate_id = event["locate"]["id"]
        store.ingest(phone.seal(phone.batch(fix=fix("locate", id="f_fresh", at="2026-10-13T07:00:00+01:00"),
                                                    locate_id=locate_id)))

    fake = FakeClient({"accepted": True, "delivered": True}, on_send=answer)
    monkeypatch.setattr(tool, "client", fake)
    result = _call({"action": "locate"}, profile="work", sleep=lambda seconds: None)
    assert result["fresh"] is True and result["as_of"].startswith("2026-10-13T07:00:00+01:00")
    assert fake.sent[0]["type"] == "location.requested" and fake.sent[0]["profile"] == "work"


def test_locate_without_a_token_falls_back_and_spends_nothing(home, monkeypatch):
    store, _ = _shared(home, fix=fix("significant"))
    monkeypatch.setattr(tool, "client", FakeClient({"accepted": True, "delivered": False, "reason": "no_location_token"}))
    result = _call({"action": "locate"}, sleep=lambda seconds: None)
    assert result["error"] == "not_set_up" and result["known"] is True
    store.start_locate()  # the budget wasn't spent


def test_locate_needs_background_sharing(home, monkeypatch):
    _shared(home, sharing=sharing(background=False), fix=fix())
    assert _call({"action": "locate"})["error"] == "background_off"


# --- Calls held back by place --------------------------------------------------------


def _blocked(home, settings):
    monkey_now = calls._clock
    try:
        calls._clock = lambda: NOW
        return calls._place_blocked(home, {"avoid_places": [], "avoid_unknown_place": False, **settings})
    finally:
        calls._clock = monkey_now


def test_no_call_rings_at_a_place_the_user_avoids(tmp_path):
    _shared(tmp_path, fix=fix("place", place_id="p_work"))
    assert _blocked(tmp_path, {"avoid_places": ["p_work"]}) is True
    assert _blocked(tmp_path, {"avoid_places": ["p_home"]}) is False


def test_an_unknown_place_counts_only_when_asked(tmp_path):
    assert _blocked(tmp_path, {"avoid_places": ["p_home"]}) is False
    assert _blocked(tmp_path, {"avoid_places": ["p_home"], "avoid_unknown_place": True}) is True
    assert _blocked(tmp_path, {"avoid_unknown_place": True}) is True  # on its own too
    _shared(tmp_path, sharing=sharing(paused_until="2099-01-01T00:00:00+00:00"))
    assert _blocked(tmp_path, {"avoid_places": ["p_home"], "avoid_unknown_place": True}) is True


def test_a_call_at_an_avoided_place_is_held_back_and_not_counted(tmp_path):
    store = calls_store.CallStore(tmp_path, clock=lambda: NOW, place_blocked=lambda settings: True)
    store.update_settings({"enabled": True, "avoid_places": ["p_work", "p_work"]})
    assert store.settings()["avoid_places"] == ["p_work"]
    store.add_tool_watch("session-1", "Done.", True)
    result = store.fire("session-1", "done")
    assert (result["status"], result["reason"]) == ("limited", "place")
    with pytest.raises(ValueError):
        store.update_settings({"avoid_places": ["not a place id!"]})


# --- Routes ---------------------------------------------------------------------


@pytest.fixture()
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(api, "_calls_home", lambda: tmp_path)
    for name in ("_location_ingest_limiter", "_location_key_limiter", "_location_wipe_limiter"):
        monkeypatch.setattr(api, name, api._MintLimiter(1000, 60))
    started, removed = [], []
    cron = types.ModuleType("cron")
    jobs = types.ModuleType("cron.jobs")
    jobs.trigger_job = lambda job_id, extra_prompt=None: started.append((job_id, extra_prompt)) or {"id": job_id}
    jobs.remove_job = lambda job_id: removed.append(job_id) or True
    cron.jobs = jobs
    monkeypatch.setitem(sys.modules, "cron", cron)
    monkeypatch.setitem(sys.modules, "cron.jobs", jobs)
    app = FastAPI()
    app.include_router(api.router, prefix=BASE)
    http = TestClient(app)
    http.started, http.removed, http.home = started, removed, tmp_path
    return http


def test_routes_round_trip_and_start_rule_jobs(client):
    phone = Phone()
    response = client.post(f"{BASE}/location/key", json=phone.key_request())
    assert response.status_code == 200 and response.headers["cache-control"] == "no-store"
    phone.accept(response.json())
    # Rules come from the agent's tool; here straight into the store.
    store = location.LocationStore(client.home)
    leave_at = datetime.now(timezone.utc).replace(microsecond=0)
    _rule(store, days=[], hours="")
    leave = {"id": "e1", "kind": "leave", "place_id": "p_home", "at": leave_at.isoformat()}
    batch = phone.batch(sent_at=leave_at.isoformat(), fix=fix("place", at=leave_at.isoformat()), events=[leave])
    response = client.post(f"{BASE}/location/ingest", json=phone.seal(batch))
    assert response.status_code == 200, response.text
    assert response.json()["rules_fired"] == 1
    assert client.started[0][0] == "job-1" and "left Home" in client.started[0][1]
    status = client.get(f"{BASE}/location/status", params={"device_id": DEVICE}).json()
    assert status["device"]["kid"] == phone.kid and status["rules"] == 1 and status["crypto"] is True
    assert client.get(f"{BASE}/location/rules").json()["rules"][0]["place_name"] == "Home"
    assert client.get(f"{BASE}/location/reads").json()["reads"] == []
    assert client.post(f"{BASE}/location/ingest", json={**phone.seal(batch), "kid": "f" * 32}).status_code == 409
    assert client.delete(f"{BASE}/location/rules/r_unknown01").status_code == 404
    assert client.delete(f"{BASE}/location/rules/r_brief0001").json()["removed"] is True
    assert client.removed == ["job-1"]
    assert client.post(f"{BASE}/location/wipe", json={}).status_code == 400
    assert client.post(f"{BASE}/location/wipe", json={"all": True}).json()["remaining_devices"] == 0
    assert client.get(f"{BASE}/location/status").json()["stored"] is False


def test_status_says_whether_hermes_can_ask_for_a_fresh_position(client, monkeypatch):
    # Conduit's Location settings read `locate.available` to tell whether
    # conduit_location's locate can reach this phone; it mirrors locate's own
    # not_paired gate (background sharing is the phone's own setting).
    pairing = client.home / "conduit-push.json"
    monkeypatch.setattr(api, "_pairing_state_path", lambda: pairing)
    assert client.get(f"{BASE}/location/status").json()["locate"] == {"available": False}
    pairing.write_text(json.dumps({"credential": "c" * 32, "installation_id": "i" * 16}))
    assert client.get(f"{BASE}/location/status").json()["locate"] == {"available": True}


def test_ingest_refuses_an_oversized_body(client):
    response = client.post(f"{BASE}/location/ingest", content=b"x" * (api.LOCATION_MAX_INGEST_BODY_BYTES + 1),
                           headers={"content-type": "application/json"})
    assert response.status_code == 413


def test_a_rule_deleted_meanwhile_isnt_brought_back(store):
    _rule(store)
    store.remove_rule("r_brief0001")
    with pytest.raises(location.LocationError) as error:
        store.put_rule({"id": "r_brief0001", "job_id": "job-1", "what": "morning brief", "place_id": "p_home",
                        "place_name": "Home", "event": "leave", "days": [], "hours": "", "task": "Brief me.",
                        "call": True}, replacing=True)
    assert error.value.code == "rule_gone" and store.rules() == []


def test_a_redacted_profile_still_sends_the_locate_id():
    from conduit_push import events
    event = events.push_event("location.requested", identifier="locate:x", profile="default",
                              locate={"id": "l_" + "a" * 20})
    assert events.redact_event(event)["locate"] == {"id": "l_" + "a" * 20}
    assert "locate" not in events.redact_event({**event, "locate": {"id": "bad\n"}})


def test_a_content_free_locate_carries_only_a_valid_id():
    from conduit_push import e2e
    event = {"type": "location.requested", "event_id": "e1", "locate": {"id": "l_" + "a" * 20}}
    assert e2e.content_free(event)["locate"] == {"id": "l_" + "a" * 20}
    assert "locate" not in e2e.content_free({**event, "locate": {"id": "bad\n"}})


# --- Voice reads (POST /voice-tools/call) -------------------------------------------


class VoicePhone(Phone):
    """Conduit's side of a location voice read, as the Swift code does it."""

    def accept(self, answer):
        super().accept(answer)
        host_public = location.unb64u(answer["host_public_key"])
        shared = self.private.exchange(X25519PublicKey.from_public_bytes(host_public))
        self.request_key = location.derive_key(shared, self.device_id, self.public, host_public,
                                               location.INFO_VOICE_REQUEST)
        self.answer_key = location.derive_key(shared, self.device_id, self.public, host_public,
                                              location.INFO_VOICE_ANSWER)

    def ask(self, args, tool_name="conduit_location", call_id="call-voice-0001"):
        body = json.dumps({"call_id": call_id, "args": args, "provider": "gemini-live"}).encode()
        nonce, ct = location.seal(self.request_key, body,
                                  location.voice_aad("request", tool_name, self.kid, self.device_id, call_id))
        return {"v": 1, "tool": tool_name, "device_id": self.device_id, "kid": self.kid, "call_id": call_id,
                "nonce": nonce, "ct": ct}

    def read(self, envelope, answer):
        plain = location.open_sealed(self.answer_key, answer["nonce"], answer["ct"],
                                     location.voice_aad("answer", envelope["tool"], self.kid, self.device_id,
                                                        envelope["call_id"]))
        opened = json.loads(plain)
        assert opened["call_id"] == envelope["call_id"]
        return opened["result"]


_ORIGINAL_MODULES = api._voice_tool_modules


@pytest.fixture()
def voice(tmp_path, monkeypatch):
    """The voice routes on a profile whose location store runs at NOW."""
    monkeypatch.setattr(api, "_calls_home", lambda: tmp_path)
    monkeypatch.setattr(api, "_voice_tools_limiter", api._MintLimiter(1000, 60))
    modules = api._voice_tool_modules()
    entry = modules["conduit_location"]
    dashboard_store = entry[1]
    modules["conduit_location"] = (entry[0], lambda home: dashboard_store(home, clock=lambda: NOW), *entry[2:])
    monkeypatch.setattr(api, "_voice_tool_modules", lambda: modules)
    app = FastAPI()
    app.include_router(api.router, prefix=BASE)
    store = location.LocationStore(tmp_path, clock=lambda: NOW)
    return TestClient(app), store


def _voice_phone(store, device_id=DEVICE, **batch):
    phone = VoicePhone(device_id)
    phone.accept(store.register_key(phone.key_request()))
    if batch:
        store.ingest(phone.seal(phone.batch(**batch)))
    return phone


def _voice_call(client, phone, args, **kw):
    envelope = phone.ask(args, **kw)
    response = client.post(f"{BASE}/voice-tools/call", json=envelope)
    assert response.status_code == 200, response.text
    assert response.headers["cache-control"] == "no-store"
    raw = response.json()
    assert set(raw) == {"ok", "v", "nonce", "ct"}
    return phone.read(envelope, raw)


def test_voice_list_offers_location_reads_once_it_arrived(voice):
    client, store = voice
    assert client.get(f"{BASE}/voice-tools").json()["tools"] == []
    _voice_phone(store, fix=fix())
    tools = {item["name"]: item for item in client.get(f"{BASE}/voice-tools").json()["tools"]}
    entry = tools["conduit_location"]
    assert entry["key"] == "location"
    assert entry["parameters"]["properties"]["action"]["enum"] == ["now", "places", "history", "distance_to"]
    assert "rule" not in entry["parameters"]["properties"]
    assert "set_rule" not in entry["description"] and "locate (" not in entry["description"]
    assert "voice call" in entry["description"]


def test_a_voice_read_answers_sealed_and_is_logged_as_a_call(voice):
    client, store = voice
    phone = _voice_phone(store, fix=fix())
    answer = _voice_call(client, phone, {"action": "now"})
    assert answer["ok"] and answer["area"] == "Shoreditch, London, United Kingdom"
    assert "coordinates" not in answer and "51.53" not in json.dumps(answer)
    assert _voice_call(client, phone, {"action": "distance_to", "place": "Home"})["distance"] == "1.2 km"
    assert _voice_call(client, phone, {"action": "distance_to", "place": "Clinic"})["error"] == "private"
    assert store.reads()[0] == {"at": store.reads()[0]["at"], "action": "distance_to",
                                "session_title": "Gemini Live call"}
    assert "conduit://" not in json.dumps(_voice_call(client, phone, {"action": "places"}))


@pytest.mark.parametrize("action", ["locate", "set_rule", "remove_rule", "rules", None, ["now"], {"a": 1}])
def test_a_voice_call_can_only_read(voice, action):
    client, store = voice
    phone = _voice_phone(store, fix=fix())
    answer = _voice_call(client, phone, {"action": action, "rule": {"what": "x"}, "id": "r_abcdefgh"})
    assert answer["error"] == "invalid"
    assert store.reads() == []


def test_a_voice_read_covers_only_the_asking_phone(voice):
    client, store = voice
    _voice_phone(store, fix=fix())
    other = _voice_phone(store, "ipad-installation-2")
    # The iPad has a key but shared nothing: it hears nothing about the phone.
    answer = _voice_call(client, other, {"action": "now"})
    assert answer["error"] == "nothing_shared" and "conduit://" not in answer["note"]


def test_a_paused_phone_isnt_read_by_voice(voice):
    client, store = voice
    phone = _voice_phone(store, fix=fix())
    store.ingest(phone.seal(phone.batch(sharing=sharing(paused_until="2099-01-01T00:00:00+00:00"))))
    assert _voice_call(client, phone, {"action": "now"})["error"] == "paused"


def test_a_location_voice_request_needs_the_device_key_and_its_tool(voice):
    client, store = voice
    phone = _voice_phone(store, fix=fix())
    envelope = phone.ask({"action": "now"})
    # Relabelled for health, it opens with neither key: the tool is sealed in.
    relabelled = client.post(f"{BASE}/voice-tools/call", json={**envelope, "tool": "conduit_health"})
    assert relabelled.status_code in (404, 409) and "1.2 km" not in relabelled.text
    with pytest.raises(location.LocationError) as error:
        store.open_voice_request({**envelope, "tool": "conduit_health"})
    assert error.value.code == "bad_seal"
    forged = VoicePhone()
    forged.kid, forged.request_key = phone.kid, secrets.token_bytes(32)
    response = client.post(f"{BASE}/voice-tools/call", json=forged.ask({"action": "now"}))
    assert response.status_code == 409 and "1.2 km" not in response.text, response.text
    # Ingest's key doesn't open a voice request either.
    body = json.dumps({"call_id": "call-voice-0001", "args": {"action": "now"}}).encode()
    nonce, ct = location.seal(phone.key, body, location.voice_aad("request", "conduit_location", phone.kid,
                                                                  phone.device_id, "call-voice-0001"))
    assert client.post(f"{BASE}/voice-tools/call", json={**envelope, "nonce": nonce, "ct": ct}).status_code == 409


def test_shared_location_voice_vector_matches_conduit():
    # ConduitTests checks the same values.
    voice_vector = VECTOR["voice"]
    host_private = location.unb64u(VECTOR["host_private"])
    device_private = location.unb64u(VECTOR["device_private"])
    raw = (serialization.Encoding.Raw, serialization.PublicFormat.Raw)
    host_public = X25519PrivateKey.from_private_bytes(host_private).public_key().public_bytes(*raw)
    device_public = X25519PrivateKey.from_private_bytes(device_private).public_key().public_bytes(*raw)
    kid = location.key_id(device_public, host_public)
    shared = location.agree(host_private, device_public)
    request_key = location.derive_key(shared, VECTOR["device_id"], device_public, host_public,
                                      location.INFO_VOICE_REQUEST)
    answer_key = location.derive_key(shared, VECTOR["device_id"], device_public, host_public,
                                     location.INFO_VOICE_ANSWER)
    request_aad = location.voice_aad("request", "conduit_location", kid, VECTOR["device_id"], voice_vector["call_id"])
    answer_aad = location.voice_aad("answer", "conduit_location", kid, VECTOR["device_id"], voice_vector["call_id"])
    assert (request_key.hex(), answer_key.hex()) == (voice_vector["request_key"], voice_vector["answer_key"])
    assert (request_aad.decode(), answer_aad.decode()) == (voice_vector["request_aad"], voice_vector["answer_aad"])
    assert location.seal(request_key, voice_vector["request"].encode(), request_aad,
                         nonce=bytes(12))[1] == voice_vector["request_ct"]
    assert location.seal(answer_key, voice_vector["answer"].encode(), answer_aad,
                         nonce=bytes(12))[1] == voice_vector["answer_ct"]


@pytest.mark.parametrize("field", ["nonce", "ct"])
@pytest.mark.parametrize("value", [None, 7, ["x"]])
def test_a_voice_envelope_with_a_bad_nonce_or_body_is_a_400(voice, field, value):
    client, store = voice
    phone = _voice_phone(store, fix=fix())
    response = client.post(f"{BASE}/voice-tools/call", json={**phone.ask({"action": "now"}), field: value})
    assert response.status_code == 400


def test_a_broken_location_module_leaves_health_voice_reads(tmp_path, monkeypatch):
    real = api._plugin_package_module

    def broken(name):
        if name == "location_tool":
            raise ImportError("half installed")
        return real(name)

    monkeypatch.setattr(api, "_plugin_package_module", broken)
    assert list(_ORIGINAL_MODULES()) == ["conduit_health"]


def test_a_damaged_location_store_hides_only_location_from_the_voice_list(voice, monkeypatch):
    client, store = voice
    _voice_phone(store, fix=fix())
    # Health has something shared here, so it stays listed beside the damage.
    monkeypatch.setattr(api._plugin_package_module("health_tool"), "voice_shared", lambda _store: True)
    store.path.write_bytes(b"not a database" * 100)
    response = client.get(f"{BASE}/voice-tools")
    assert response.status_code == 200
    assert [item["name"] for item in response.json()["tools"]] == ["conduit_health"]
