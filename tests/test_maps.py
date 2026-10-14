import importlib.util
import json
import pathlib
import sqlite3
import sys

import pytest
from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PublicKey
from fastapi import FastAPI
from fastapi.testclient import TestClient

ROOT = pathlib.Path(__file__).resolve().parents[1]

# The location tests' phone, places and helpers (and their module loading).
_spec = importlib.util.spec_from_file_location("conduit_maps_location_helpers", ROOT / "tests" / "test_location.py")
helpers = importlib.util.module_from_spec(_spec)
sys.modules[_spec.name] = helpers
_spec.loader.exec_module(helpers)

location, api, Phone, NOW, BASE = helpers.location, helpers.api, helpers.Phone, helpers.NOW, helpers.BASE
sharing, fix, FakeClient = helpers.sharing, helpers.fix, helpers.FakeClient
maps_host = helpers._load("conduit_push.maps_host", ROOT / "maps_host.py")
maps = helpers._load("conduit_push.maps_tool", ROOT / "maps_tool.py")
events = helpers._load("conduit_push.events", ROOT / "events.py")
e2e = helpers._load("conduit_push.e2e", ROOT / "e2e.py")

DEVICE = helpers.DEVICE


class MapsPhone(Phone):
    """Conduit's side of a maps lookup, as the Swift code does it."""

    def keys(self, store):
        with sqlite3.connect(store.path) as db:
            host_public = bytes(db.execute("SELECT host_public FROM devices WHERE id=?", (self.device_id,)).fetchone()[0])
        shared = self.private.exchange(X25519PublicKey.from_public_bytes(host_public))
        return (location.derive_key(shared, self.device_id, self.public, host_public, location.INFO_MAPS_REQUEST),
                location.derive_key(shared, self.device_id, self.public, host_public, location.INFO_MAPS_ANSWER))

    def open_request(self, store, sealed, request_id):
        request_key, _ = self.keys(store)
        return json.loads(location.open_sealed(request_key, sealed["nonce"], sealed["ct"],
                                               location.maps_aad("request", self.kid, self.device_id, request_id)))

    def seal_answer(self, store, request_id, answer):
        _, answer_key = self.keys(store)
        nonce, ct = location.seal(answer_key, json.dumps(answer).encode(),
                                  location.maps_aad("answer", self.kid, self.device_id, request_id))
        return {"v": 1, "device_id": self.device_id, "kid": self.kid, "nonce": nonce, "ct": ct}


def maps_sharing(**changes):
    return sharing(features=["maps"], **changes)


@pytest.fixture()
def store(tmp_path):
    return location.LocationStore(tmp_path, clock=lambda: NOW)


def _phone(store, **batch):
    phone = MapsPhone()
    phone.accept(store.register_key(phone.key_request()))
    batch.setdefault("sharing", maps_sharing())
    store.ingest(phone.seal(phone.batch(**batch)))
    return phone


ROUTE = {"from": {"name": "Shoreditch"}, "to": {"name": "Home"}, "mode": "driving",
         "routes": [{"name": "A10", "duration_s": 1500, "typical_duration_s": 1200, "distance_m": 9000,
                     "steps": [{"text": "Turn left‮onto High St", "distance_m": 400}]}]}


# --- The store ---------------------------------------------------------------


def test_only_a_phone_that_says_it_can_answers(store):
    phone = MapsPhone()
    phone.accept(store.register_key(phone.key_request()))
    store.ingest(phone.seal(phone.batch(sharing=sharing())))
    assert store.maps_device() is None, "an older Conduit doesn't list maps"
    store.ingest(phone.seal(phone.batch(sharing=sharing(features=["maps", "teleport"]))))
    assert store.maps_device()["device_id"] == DEVICE
    assert store.snapshot()["sharing"]["features"] == ["maps"]
    store.ingest(phone.seal(phone.batch(sharing=maps_sharing(background=False), fix=fix())))
    assert store.maps_device() is None, "it needs background sharing (the wake)"


def test_a_lookup_goes_sealed_to_its_phone_and_the_answer_comes_back(store):
    phone = _phone(store)
    request_id = store.start_maps(DEVICE, {"action": "search", "query": "pharmacy", "near": {"me": True}})["id"]
    sealed = store.maps_request(request_id, DEVICE, phone.kid)
    opened = phone.open_request(store, sealed, request_id)
    assert opened["query"] == "pharmacy" and opened["id"] == request_id and opened["expires_at"]
    answer = {"id": request_id, "ok": True, "result": {"places": [{"name": "Boots"}]}}
    store.answer_maps(request_id, phone.seal_answer(store, request_id, answer))
    assert store.maps_answer(request_id) == answer
    with pytest.raises(location.LocationError) as error:
        store.maps_request(request_id, DEVICE, phone.kid)
    assert error.value.code == "unknown", "an answered lookup can't be fetched again"


def test_another_device_or_a_late_fetch_gets_nothing(store):
    phone = _phone(store)
    request_id = store.start_maps(DEVICE, {"action": "lookup", "query": "Heathrow"})["id"]
    ipad = MapsPhone("ipad-device-02")
    ipad.accept(store.register_key(ipad.key_request()))
    with pytest.raises(location.LocationError) as error:
        store.maps_request(request_id, ipad.device_id, ipad.kid)
    assert error.value.status == 404
    store.clock = lambda: NOW + location.MAPS_TTL_S + 1
    with pytest.raises(location.LocationError):
        store.maps_request(request_id, DEVICE, phone.kid)


def test_an_answer_for_another_lookup_is_refused(store):
    phone = _phone(store)
    first = store.start_maps(DEVICE, {"action": "lookup", "query": "a"})["id"]
    second = store.start_maps(DEVICE, {"action": "lookup", "query": "b"})["id"]
    swapped = phone.seal_answer(store, first, {"id": second, "ok": True, "result": {}})
    with pytest.raises(location.LocationError):
        store.answer_maps(second, swapped)
    with pytest.raises(location.LocationError) as error:
        store.answer_maps(first, swapped)
    assert error.value.code == "malformed"


def test_a_repeat_is_answered_from_the_last_answer_and_the_budget_holds(store):
    clock = [NOW]
    store.clock = lambda: clock[0]
    phone = _phone(store)
    question = {"action": "travel_times", "from": {"me": True}, "to": [{"saved": "p_work"}], "mode": "driving"}
    request_id = store.start_maps(DEVICE, question)["id"]
    answer = {"id": request_id, "ok": True, "result": {"results": []}}
    store.answer_maps(request_id, phone.seal_answer(store, request_id, answer))
    assert store.start_maps(DEVICE, dict(question)) == {"cached": answer}
    clock[0] += location.MAPS_CACHE_S + 1
    assert "id" in store.start_maps(DEVICE, question)
    for index in range(location.MAPS_PER_HOUR - 2):
        store.start_maps(DEVICE, {**question, "mode": "walking", "n": index})
    with pytest.raises(location.LocationError) as error:
        store.start_maps(DEVICE, {**question, "n": "one more"})
    assert error.value.code == "too_soon"


def test_cancelled_lookups_dont_count_and_old_questions_are_forgotten(store):
    clock = [NOW]
    store.clock = lambda: clock[0]
    _phone(store)
    request_id = store.start_maps(DEVICE, {"action": "lookup", "query": "the clinic on Elm St"})["id"]
    store.cancel_maps(request_id)
    other = store.start_maps(DEVICE, {"action": "lookup", "query": "the dentist"})["id"]
    clock[0] += location.MAPS_TTL_S + location.MAPS_CACHE_S + 1
    store.start_maps(DEVICE, {"action": "lookup", "query": "later"})
    with sqlite3.connect(store.path) as db:
        rows = dict(db.execute("SELECT id, request FROM maps").fetchall())
    assert request_id not in rows and rows[other] == ""


def test_wipe_takes_the_lookups_too(store):
    _phone(store)
    ipad = MapsPhone("ipad-device-02")
    ipad.accept(store.register_key(ipad.key_request()))  # keeps the file
    store.start_maps(DEVICE, {"action": "lookup", "query": "x"})
    store.wipe(DEVICE)
    with sqlite3.connect(store.path) as db:
        assert db.execute("SELECT COUNT(*) FROM maps").fetchone()[0] == 0


def test_a_schema_1_store_gains_the_maps_table(tmp_path):
    store = location.LocationStore(tmp_path, clock=lambda: NOW)
    _phone(store)
    with sqlite3.connect(store.path) as db:
        db.execute("DROP TABLE maps")
        db.execute("UPDATE meta SET value='1' WHERE key='schema'")
    assert store.maps_device() is not None
    store.start_maps(DEVICE, {"action": "lookup", "query": "x"})
    with sqlite3.connect(store.path) as db:
        assert db.execute("SELECT value FROM meta WHERE key='schema'").fetchone()[0] == "2"


# --- The push ---------------------------------------------------------------


def test_the_push_carries_only_the_lookup_id_sealed():
    event = events.push_event("location.requested", identifier="maps:m_x", profile="work",
                              maps={"id": "m_" + "a" * 20})
    assert event["maps"] == {"id": "m_" + "a" * 20} and "locate" not in event
    assert "maps" in e2e.INNER_FIELDS
    assert events.push_event("location.requested", identifier="x", maps={"id": "bad"}).get("maps") is None
    assert "maps-v1" in events.PLUGIN_CAPABILITIES


# --- The tool ---------------------------------------------------------------


@pytest.fixture()
def home(tmp_path, monkeypatch):
    monkeypatch.setattr(maps, "get_hermes_home", lambda: tmp_path)
    monkeypatch.setattr(maps.location_tool.session_titles, "title", lambda key: "Trip planning")
    original = location.LocationStore
    monkeypatch.setattr(maps, "LocationStore", lambda path: original(path, clock=lambda: NOW))
    # The host engine is off unless a test turns it on (HostApple).
    monkeypatch.setattr(maps.maps_host, "configured", lambda: False)
    return tmp_path


def _shared(home, **batch):
    store = location.LocationStore(home, clock=lambda: NOW)
    return store, _phone(store, **batch)


def _call(args, **kw):
    kw.setdefault("sleep", lambda seconds: None)
    return json.loads(maps.handle(args, **kw))


def _answering(store, phone, result=None, seen=None):
    def answer(event):
        request_id = event["maps"]["id"]
        opened = phone.open_request(store, store.maps_request(request_id, DEVICE, phone.kid), request_id)
        if seen is not None:
            seen.append(opened)
        store.answer_maps(request_id, phone.seal_answer(
            store, request_id, {"id": request_id, "ok": True, "result": result or ROUTE}))
    return answer


def test_the_tool_hides_until_a_phone_can_answer(home):
    assert maps.available() is False
    assert _call({"action": "search", "query": "cafe"})["error"] == "unavailable"
    _shared(home, fix=fix("significant"))
    assert maps.available() is True


def test_directions_go_to_the_phone_and_come_back_readable(home, monkeypatch):
    store, phone = _shared(home, fix=fix("significant"))
    seen = []
    fake = FakeClient({"accepted": True, "delivered": True}, on_send=_answering(store, phone, seen=seen))
    monkeypatch.setattr(maps, "client", fake)
    answer = _call({"action": "directions", "to": "home", "arrive_by": "2026-10-13T08:00"}, profile="work",
                   session_id="session-1")
    assert answer["ok"] is True and answer["source"].startswith("Apple Maps")
    route = answer["routes"][0]
    assert route["duration"] == "about 25 min" and route["traffic_delay"] == "about 5 min"
    assert route["steps"][0]["text"] == "Turn left onto High St", "invisible characters are cleaned out"
    sent = fake.sent[0]
    assert sent["type"] == "location.requested" and sent["profile"] == "work" and "locate" not in sent
    assert seen[0]["from"] == {"me": True} and seen[0]["to"] == [{"saved": "p_home"}]
    assert seen[0]["detail"] == "area" and seen[0]["arrive_by"] == "2026-10-13T08:00"
    assert store.reads()[0]["action"] == "maps directions"


@pytest.mark.parametrize("place, ref", [
    ("me", {"me": True}),
    ("Work", {"saved": "p_work"}),
    ("place_id:I63A4F2F1E8C9", {"place_id": "I63A4F2F1E8C9"}),
    ("51.5, -0.12", {"lat": 51.5, "lon": -0.12}),
    ("Heathrow Terminal 5", {"text": "Heathrow Terminal 5"}),
])
def test_places_are_read_the_way_people_write_them(home, monkeypatch, place, ref):
    store, phone = _shared(home, fix=fix("significant"))
    seen = []
    monkeypatch.setattr(maps, "client", FakeClient({"delivered": True}, on_send=_answering(store, phone, seen=seen)))
    _call({"action": "travel_times", "from": "Home", "to": [place]})
    assert seen[0]["to"] == [ref] and seen[0]["from"] == {"saved": "p_home"}


@pytest.mark.parametrize("args, error", [
    ({"action": "directions", "to": "Clinic"}, "private"),
    ({"action": "directions", "to": ["Home", "Work"]}, "invalid"),
    ({"action": "travel_times", "to": ["x"] * 11}, "invalid"),
    ({"action": "directions", "to": "Home", "mode": "teleport"}, "invalid"),
    ({"action": "directions", "to": "Home", "depart_at": "soon"}, "invalid"),
    ({"action": "directions", "to": "Home", "depart_at": "2026-10-13T08:00", "arrive_by": "2026-10-13T09:00"},
     "invalid"),
    ({"action": "search"}, "invalid"),
    ({"action": "fly"}, "invalid"),
])
def test_the_tool_refuses_what_it_cant_ask(home, monkeypatch, args, error):
    _shared(home, fix=fix("significant"))
    fake = FakeClient({"delivered": True})
    monkeypatch.setattr(maps, "client", fake)
    assert _call(args)["error"] == error
    assert fake.sent == []


def test_me_follows_the_detail_and_pause(home, monkeypatch):
    store, phone = _shared(home, sharing=maps_sharing(background_precision="places"))
    monkeypatch.setattr(maps, "client", FakeClient({"delivered": True}))
    assert _call({"action": "search", "query": "cafe"})["error"] == "detail"
    # Not about the user: fine at any detail.
    seen = []
    monkeypatch.setattr(maps, "client", FakeClient({"delivered": True}, on_send=_answering(store, phone, seen=seen)))
    assert _call({"action": "search", "query": "cafe", "near": "Covent Garden"})["ok"] is True
    store.ingest(phone.seal(phone.batch(sharing=maps_sharing(talking=False, background=False))))
    assert _call({"action": "search", "query": "cafe"})["error"] == "unavailable"


def test_a_subagent_cant_look_things_up(home):
    _shared(home, fix=fix("significant"))
    assert _call({"action": "search", "query": "cafe"}, session_id="child",
                 is_child=lambda session: True)["error"] == "subagent"


def test_no_answer_gives_a_labelled_guess_to_a_saved_place(home, monkeypatch):
    _shared(home, fix=fix("significant"))
    monkeypatch.setattr(maps, "client", FakeClient({"delivered": True}))
    answer = _call({"action": "travel_times", "to": ["Home", "Heathrow"], "mode": "walking"})
    assert answer["error"] == "timeout"
    assert answer["rough_estimate"] == [{"to": "Home", "estimate": "about 21 min", "road_distance_guess": "1.6 km"}]
    assert "rough guess" in answer["note"]


def test_a_relay_that_cant_wake_the_phone_spends_nothing(home, monkeypatch):
    store, _ = _shared(home, fix=fix("significant"))
    monkeypatch.setattr(maps, "client", FakeClient({"accepted": True, "delivered": False}))
    assert _call({"action": "lookup", "query": "Heathrow"})["error"] == "not_set_up"
    with sqlite3.connect(store.path) as db:
        assert db.execute("SELECT COUNT(*) FROM maps").fetchone()[0] == 0


def test_a_phone_error_reads_plainly(home, monkeypatch):
    store, phone = _shared(home, fix=fix("significant"))

    def fail(event):
        request_id = event["maps"]["id"]
        store.answer_maps(request_id, phone.seal_answer(store, request_id,
                                                        {"id": request_id, "ok": False, "error": "no_transit"}))

    monkeypatch.setattr(maps, "client", FakeClient({"delivered": True}, on_send=fail))
    answer = _call({"action": "directions", "to": "Work", "mode": "transit"})
    assert answer == {"ok": False, "error": "no_transit", "note": maps._ERROR_NOTES["no_transit"]}


# --- Routes ---------------------------------------------------------------------


@pytest.fixture()
def http(tmp_path, monkeypatch):
    monkeypatch.setattr(api, "_calls_home", lambda: tmp_path)
    for name in ("_location_ingest_limiter", "_location_key_limiter", "_location_maps_limiter"):
        monkeypatch.setattr(api, name, api._MintLimiter(1000, 60))
    app = FastAPI()
    app.include_router(api.router, prefix=BASE)
    return TestClient(app)


def test_routes_fetch_and_answer_a_lookup(http, tmp_path):
    store = location.LocationStore(tmp_path)
    phone = _phone(store)
    request_id = store.start_maps(DEVICE, {"action": "lookup", "query": "Heathrow"})["id"]
    fetched = http.get(f"{BASE}/location/maps/{request_id}", params={"device_id": DEVICE, "kid": phone.kid})
    assert fetched.status_code == 200 and fetched.headers["cache-control"] == "no-store"
    assert phone.open_request(store, fetched.json(), request_id)["query"] == "Heathrow"
    answer = {"id": request_id, "ok": True, "result": {"places": [{"name": "Heathrow Airport"}]}}
    posted = http.post(f"{BASE}/location/maps/{request_id}", json=phone.seal_answer(store, request_id, answer))
    assert posted.status_code == 200 and posted.json() == {"ok": True}
    assert store.maps_answer(request_id) == answer
    again = http.post(f"{BASE}/location/maps/{request_id}", json=phone.seal_answer(store, request_id, answer))
    assert again.status_code == 404
    bad = http.get(f"{BASE}/location/maps/nope", params={"device_id": DEVICE, "kid": phone.kid})
    assert bad.status_code == 400


# --- The host engine ------------------------------------------------------------


class HostApple:
    """Apple Maps Server API as the host sees it: answers by path."""

    def __init__(self, answers):
        self.answers = answers
        self.urls = []

    def __call__(self, url, token):
        assert token == "maps-token"
        self.urls.append(url)
        path = url.split("/v1/", 1)[1].split("?", 1)[0]
        answer = self.answers[path]
        if isinstance(answer, Exception):
            raise answer
        return answer


@pytest.fixture()
def apple(home, monkeypatch):
    fake = HostApple({})
    monkeypatch.setattr(maps.maps_host, "configured", lambda: True)
    monkeypatch.setattr(maps.maps_host, "_access_token", lambda refresh=False: "maps-token")
    original = maps.maps_host.Engine.__init__

    def engine(self, home_path, fetch=None, zone=None, clock=None):
        original(self, home_path, fetch=fake, zone=zone, clock=lambda: NOW)

    monkeypatch.setattr(maps.maps_host.Engine, "__init__", engine)
    return fake


BOOTS = {"name": "Boots", "formattedAddressLines": ["123 Oxford St", "London W1D 2HG"], "poiCategory": "Pharmacy",
         "coordinate": {"latitude": 51.5153, "longitude": -0.1418}, "id": "I1234ABCD"}


def test_a_lookup_not_about_the_user_is_answered_here_without_waking_the_phone(home, apple, monkeypatch):
    _shared(home, fix=fix("significant"))
    fake = FakeClient({"delivered": True})
    monkeypatch.setattr(maps, "client", fake)
    apple.answers["search"] = {"results": [BOOTS]}
    answer = _call({"action": "search", "query": "pharmacy", "near": "51.52,-0.14"})
    assert answer["ok"] and answer["source"] == "Apple Maps, from this Hermes host"
    assert answer["places"][0]["name"] == "Boots" and answer["places"][0]["place_id"] == "I1234ABCD"
    assert answer["places"][0]["distance_m"] == 537
    assert fake.sent == [], "the phone wasn't woken"
    assert "searchLocation=51.520000%2C-0.140000" in apple.urls[0]


def test_without_a_phone_me_is_the_hosts_last_fix_at_its_detail(tmp_path, home, apple):
    store = location.LocationStore(home, clock=lambda: NOW)
    phone = MapsPhone()
    phone.accept(store.register_key(phone.key_request()))
    store.ingest(phone.seal(phone.batch(sharing=sharing(), fix=fix("significant"))))  # an older Conduit: no maps
    apple.answers["etas"] = {"etas": [{"expectedTravelTimeSeconds": 1500, "staticTravelTimeSeconds": 1200,
                                       "distanceMeters": 9000}]}
    apple.answers["search"] = {"results": [BOOTS]}
    answer = _call({"action": "travel_times", "to": "Boots Oxford St", "arrive_by": "2026-10-13T08:00"})
    assert answer["ok"] and answer["from_position"].startswith("around Shoreditch, London")
    result = answer["results"][0]
    assert result["duration"] == "about 25 min" and result["traffic_delay"] == "about 5 min"
    assert result["leave_by"] == "2026-10-13T07:35+01:00"
    assert "origin=51.530000%2C-0.080000" in apple.urls[-1], "area detail: the rounded fix"
    assert "arrivalDate=2026-10-13T07%3A00%3A00Z" in apple.urls[-1]


def test_saved_places_and_transit_routes_need_the_phone(home, apple):
    store = location.LocationStore(home, clock=lambda: NOW)
    phone = MapsPhone()
    phone.accept(store.register_key(phone.key_request()))
    store.ingest(phone.seal(phone.batch(sharing=sharing(), fix=fix("significant"))))
    assert _call({"action": "directions", "to": "Home"})["error"] == "unavailable"
    assert _call({"action": "directions", "to": "Heathrow", "mode": "transit"})["error"] == "unavailable"
    assert apple.urls == []


def test_the_phone_answers_me_first_and_the_host_steps_in_when_it_cant(home, apple, monkeypatch):
    _shared(home, fix=fix("significant"))
    monkeypatch.setattr(maps, "client", FakeClient({"accepted": True, "delivered": False}))
    apple.answers["search"] = {"results": [BOOTS]}
    answer = _call({"action": "search", "query": "pharmacy"})
    assert answer["ok"] and answer["source"] == "Apple Maps, from this Hermes host"
    assert answer["places"][0]["distance_m"] % 100 == 0, "area detail rounds distances from me"


def test_directions_from_me_at_area_detail_leave_out_the_first_turns(home, apple, monkeypatch):
    store = location.LocationStore(home, clock=lambda: NOW)
    phone = MapsPhone()
    phone.accept(store.register_key(phone.key_request()))
    store.ingest(phone.seal(phone.batch(sharing=sharing(), fix=fix("significant"))))
    apple.answers["search"] = {"results": [BOOTS]}
    apple.answers["directions"] = {"routes": [{"name": "A10", "durationSeconds": 900, "distanceMeters": 5000,
                                               "hasTolls": False, "stepIndexes": [0]}],
                                   "steps": [{"instructions": "Turn left onto Old St", "distanceMeters": 200}]}
    answer = _call({"action": "directions", "to": "Boots Oxford St"})
    assert answer["ok"] and "steps" not in answer["routes"][0]
    assert answer["from"] == {"name": "around Shoreditch, London"}
    answer = _call({"action": "directions", "from": "51.5,-0.12", "to": "Boots Oxford St"})
    assert answer["routes"][0]["steps"] == [{"text": "Turn left onto Old St", "distance_m": 200}]


def test_keep_maps_on_the_phone_turns_the_host_engine_off(home, apple, monkeypatch):
    _shared(home, sharing=maps_sharing(maps_host=False), fix=fix("significant"))
    monkeypatch.setattr(maps, "client", FakeClient({"accepted": True, "delivered": False}))
    assert _call({"action": "lookup", "query": "Heathrow"})["error"] == "not_set_up"
    assert apple.urls == []


def test_the_hosts_daily_share_of_apple_maps_holds(home, apple, monkeypatch):
    monkeypatch.setattr(maps_host, "DAILY_CALLS", 2)
    _shared(home, sharing=maps_sharing(background=False), fix=fix())
    apple.answers["search"] = {"results": [BOOTS]}
    assert _call({"action": "lookup", "query": "Boots"})["ok"]
    assert _call({"action": "lookup", "query": "Boots"})["ok"]
    assert _call({"action": "lookup", "query": "Boots"})["error"] == "daily_limit"


def test_a_content_free_push_keeps_the_lookup_id():
    event = events.push_event("location.requested", identifier="maps:m_x", maps={"id": "m_" + "b" * 20})
    assert e2e.content_free(event, None)["maps"] == {"id": "m_" + "b" * 20}


def test_impossible_times_are_refused_before_a_wake(home, monkeypatch):
    _shared(home, fix=fix("significant"))
    fake = FakeClient({"delivered": True})
    monkeypatch.setattr(maps, "client", fake)
    assert _call({"action": "directions", "to": "Home", "depart_at": "2026-10-15T99:99+99:99"})["error"] == "invalid"
    assert fake.sent == []


def test_the_phones_result_cant_replace_the_envelope():
    answer = maps._shaped({"ok": True, "result": {"ok": False, "action": "x", "places": []}}, {"action": "search"})
    assert answer["ok"] is True and answer["action"] == "search"


def test_a_late_answer_is_refused_and_old_lookups_go_on_upload(store):
    clock = [NOW]
    store.clock = lambda: clock[0]
    phone = _phone(store)
    request_id = store.start_maps(DEVICE, {"action": "lookup", "query": "the dentist"})["id"]
    clock[0] += location.MAPS_TTL_S + 1
    with pytest.raises(location.LocationError) as error:
        store.answer_maps(request_id, phone.seal_answer(store, request_id, {"id": request_id, "ok": True, "result": {}}))
    assert error.value.status == 404
    clock[0] += location.MAPS_CACHE_S
    store.ingest(phone.seal(phone.batch(sharing=maps_sharing())))
    with sqlite3.connect(store.path) as db:
        assert db.execute("SELECT request FROM maps WHERE id=?", (request_id,)).fetchone()[0] == ""


def test_a_phone_error_that_isnt_text_reads_as_failed():
    answer = maps._shaped({"ok": False, "error": {"x": 1}, "note": ["y"]}, {"action": "search"})
    assert answer == {"ok": False, "error": "failed", "note": "The lookup failed."}
