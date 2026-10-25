import importlib.util
import json
import pathlib
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


inbox = _load("conduit_push.inbox_store", ROOT / "inbox_store.py")
tool = _load("conduit_push.inbox_tool", ROOT / "inbox_tool.py")
_api_spec = importlib.util.spec_from_file_location("conduit_plugin_api_inbox", ROOT / "dashboard" / "plugin_api.py")
api = importlib.util.module_from_spec(_api_spec)
_api_spec.loader.exec_module(api)

DEVICE = "phone-installation-1"
NOW = 1_792_000_000  # 2026-10-14T...Z, after every event below


class Phone:
    """Conduit's side of the protocol, as the Swift code does it."""

    def __init__(self, device_id=DEVICE):
        self.device_id = device_id
        self.private = X25519PrivateKey.generate()
        self.public = self.private.public_key().public_bytes(serialization.Encoding.Raw,
                                                             serialization.PublicFormat.Raw)
        self.kid = None
        self.key = None

    def key_request(self):
        return {"device_id": self.device_id, "device_name": "Michel's iPhone", "public_key": inbox.b64u(self.public)}

    def accept(self, answer):
        host_public = inbox.unb64u(answer["host_public_key"])
        shared = self.private.exchange(X25519PublicKey.from_public_bytes(host_public))
        self.key = inbox.derive_key(shared, self.device_id, self.public, host_public)
        self.answer_key = inbox.derive_key(shared, self.device_id, self.public, host_public, inbox.INFO_ANSWER)
        self.kid = answer["kid"]
        assert self.kid == inbox.key_id(self.public, host_public)

    def seal(self, batch):
        batch_id = batch["batch_id"]
        nonce, ct = inbox.seal(self.key, json.dumps(batch, ensure_ascii=False).encode(), inbox.ingest_aad(self.kid, self.device_id, batch_id))
        return {"v": 1, "device_id": self.device_id, "kid": self.kid, "batch_id": batch_id, "nonce": nonce, "ct": ct}


def _event(event_id="evt-1", kind="notification", at="2026-10-14T09:15:00-04:00", **fields):
    event = {"id": event_id, "kind": kind, "at": at}
    event.update(fields or {"source": "Desjardins", "title": "Purchase", "text": "A $45.20 purchase at IGA"})
    return event


def _batch(*events, batch_id="batch-0001", **extra):
    batch = {"batch_id": batch_id, "device": {"name": "Michel's iPhone", "time_zone": "America/Toronto"},
             "events": list(events) or [_event()]}
    batch.update(extra)
    return batch


@pytest.fixture()
def store(tmp_path):
    return inbox.InboxStore(tmp_path, clock=lambda: NOW)


@pytest.fixture()
def phone(store):
    phone = Phone()
    phone.accept(store.register_key(phone.key_request()))
    return phone


def test_the_vector_matches_what_the_phone_computes():
    vector = json.loads((ROOT / "tests" / "inbox_vector.json").read_text())
    host_private, device_public = inbox.unb64u(vector["host_private"]), inbox.unb64u(vector["device_public"])
    host_public = inbox.unb64u(vector["host_public"])
    assert inbox.key_id(device_public, host_public) == vector["kid"]
    key = inbox.derive_key(inbox.agree(host_private, device_public), vector["device_id"], device_public, host_public)
    assert key.hex() == vector["key"]
    aad = inbox.ingest_aad(vector["kid"], vector["device_id"], vector["batch_id"])
    assert aad.decode() == vector["aad"]
    assert inbox.open_sealed(key, vector["nonce"], vector["ct"], aad).decode() == vector["plaintext"]
    answer_key = inbox.derive_key(inbox.agree(host_private, device_public), vector["device_id"], device_public,
                                  host_public, inbox.INFO_ANSWER)
    assert answer_key.hex() == vector["answer_key"]
    list_aad = inbox.list_aad(vector["kid"], vector["device_id"], vector["request_id"])
    assert list_aad.decode() == vector["list_aad"]
    assert inbox.open_sealed(answer_key, vector["nonce"], vector["answer_ct"], list_aad).decode() == \
        vector["answer_plaintext"]


def test_an_event_lands_sealed_and_reads_back(store, phone):
    result = store.ingest(phone.seal(_batch()))
    assert result["stored"] == 1 and result["duplicates"] == 0 and result["codes_dropped"] == 0
    found, more = store.events()
    assert not more
    assert found == [{"id": "evt-1", "kind": "notification", "at": "2026-10-14T09:15:00-04:00",
                      "source": "Desjardins", "title": "Purchase", "text": "A $45.20 purchase at IGA",
                      "device": "Michel's iPhone"}]
    assert stat.S_IMODE(store.path.stat().st_mode) == 0o600


def test_the_same_event_twice_is_kept_once(store, phone):
    store.ingest(phone.seal(_batch()))
    again = store.ingest(phone.seal(_batch(batch_id="batch-0002")))
    assert again["stored"] == 0 and again["duplicates"] == 1
    assert len(store.events()[0]) == 1


def test_sign_in_codes_never_land(store, phone):
    result = store.ingest(phone.seal(_batch(
        _event("evt-code", kind="message", source="Google", text="G-482913 is your Google verification code"),
        _event("evt-link", kind="email", source="Slack", title="Sign in to Slack",
               text="Tap to sign in: https://slack.com/magic/abc"),
        _event("evt-alert", kind="notification", source="Desjardins", title="Security alert",
               text="A $45.20 charge on card 4821 looks unusual"),
    )))
    assert result["stored"] == 1 and result["codes_dropped"] == 2
    assert [event["id"] for event in store.events()[0]] == ["evt-alert"]


def test_a_code_in_any_field_never_lands(store, phone):
    # Conduit's ShortcutEventFilter reads the same fields: From, Title, Details and Amount.
    result = store.ingest(phone.seal(_batch(
        _event("evt-source", kind="other", source="Your verification code is 482913", text="Hello"),
        _event("evt-amount", kind="transaction", title="Bank", amount="Tap to sign in: https://b.example/magic"),
        _event("evt-coffee", kind="transaction", source="Café Olimpico", amount="4,50 $"),
    )))
    assert result["stored"] == 1 and result["codes_dropped"] == 2
    assert [event["id"] for event in store.events()[0]] == ["evt-coffee"]


@pytest.mark.parametrize("text,code", [
    ("Your verification code is 482913", True),
    ("Votre code de vérification est 123456", True),
    ("Your Uber code: 4821", True),
    ("Order 123456 shipped", False),
    ("Can you pick up milk on the way home?", False),
    ("Security alert: a charge of $45.20 on card 4821 looks unusual", False),
])
def test_what_counts_as_a_code(text, code):
    assert inbox.looks_like_code(text) is code


def test_a_body_sealed_for_another_key_is_refused(store, phone):
    other = Phone()
    other.accept(store.register_key({**other.key_request(), "device_id": "phone-installation-2"}))
    envelope = other.seal(_batch())
    envelope["device_id"] = DEVICE
    envelope["kid"] = phone.kid
    with pytest.raises(inbox.InboxError) as refused:
        store.ingest(envelope)
    assert refused.value.code == "bad_seal"
    assert store.events() == ([], False)


def test_a_new_device_key_drops_what_the_old_one_sent(store, phone):
    store.ingest(phone.seal(_batch()))
    fresh = Phone()
    fresh.accept(store.register_key(fresh.key_request()))
    assert store.events() == ([], False)


def test_a_new_key_keeps_when_the_device_first_paired(tmp_path):
    clock = {"now": NOW}
    store = inbox.InboxStore(tmp_path, clock=lambda: clock["now"])
    store.register_key(Phone().key_request())
    clock["now"] = NOW + 86400
    store.register_key(Phone().key_request())
    with store._read() as db:
        assert db.execute("SELECT created_at FROM devices").fetchone()[0] == NOW


def test_status_counts_only_what_is_still_kept(tmp_path):
    clock = {"now": NOW}
    store = inbox.InboxStore(tmp_path, clock=lambda: clock["now"])
    phone = Phone()
    phone.accept(store.register_key(phone.key_request()))
    store.ingest(phone.seal(_batch(keep_days=2)))
    assert store.status(DEVICE)["device"]["events"] == {"notification": 1}
    clock["now"] = NOW + 3 * 86400  # past its days, not pruned yet
    assert store.status(DEVICE)["device"]["events"] == {}


def test_old_events_go_after_the_days_the_phone_asked(tmp_path):
    clock = {"now": NOW}
    store = inbox.InboxStore(tmp_path, clock=lambda: clock["now"])
    phone = Phone()
    phone.accept(store.register_key(phone.key_request()))
    store.ingest(phone.seal(_batch(keep_days=2)))
    clock["now"] = NOW + 3 * 86400
    assert store.events() == ([], False)
    assert store.overview()["devices"][0]["kinds"] == {}
    pruned = store.ingest(phone.seal(_batch(_event("evt-2", at="2026-10-17T08:00:00-04:00"),
                                            batch_id="batch-0002", keep_days=2)))
    assert pruned["pruned"] == 1


def test_events_narrow_by_kind_source_words_and_time(store, phone):
    store.ingest(phone.seal(_batch(
        _event("evt-1", kind="message", at="2026-10-14T08:00:00-04:00", source="Renée-Anne",
               text="Can you grab milk and bread?"),
        _event("evt-2", kind="transaction", at="2026-10-14T09:00:00-04:00", source="IGA", amount="$45.20 CAD"),
        _event("evt-3", kind="notification", at="2026-10-14T10:00:00-04:00", source="Canada Post",
               title="Delivered", text="Your parcel was delivered"),
    )))
    assert [e["id"] for e in store.events()[0]] == ["evt-3", "evt-2", "evt-1"]
    assert [e["id"] for e in store.events(kinds=["message"])[0]] == ["evt-1"]
    assert [e["id"] for e in store.events(source="iga")[0]] == ["evt-2"]
    assert [e["id"] for e in store.events(query="MILK bread")[0]] == ["evt-1"]
    since = datetime(2026, 10, 14, 13, 30, tzinfo=timezone.utc).timestamp()
    assert [e["id"] for e in store.events(since=since)[0]] == ["evt-3"]
    found, more = store.events(limit=2)
    assert len(found) == 2 and more


def test_text_is_cleaned_and_cut(store, phone):
    store.ingest(phone.seal(_batch(_event(source="App\u202eevil", title="Line\nbreak", text="x" * 5000))))
    event = store.events()[0][0]
    assert event["source"] == "App evil" and event["title"] == "Line break"
    assert len(event["text"]) == inbox.MAX_TEXT_CHARS and event["text"].endswith("…")


def test_malformed_batches_are_refused(store, phone):
    for bad in (_batch(_event(kind="sms")), _batch(_event(at="yesterday")), {"batch_id": "batch-0001", "events": []},
                _batch(_event(), keep_days=90), _batch({"id": "evt-1", "kind": "other", "at": "2026-10-14T09:15:00Z"})):
        with pytest.raises(inbox.InboxError):
            store.ingest(phone.seal(bad))


def test_wipe_removes_the_file_when_no_device_is_left(store, phone):
    store.ingest(phone.seal(_batch()))
    assert store.wipe(DEVICE) == {"deleted": True, "remaining_devices": 0}
    assert not store.path.exists()


def test_the_tool_reads_and_marks_events_as_data(store, phone):
    store.ingest(phone.seal(_batch()))
    now = lambda: datetime.fromtimestamp(NOW, timezone.utc)  # noqa: E731
    overview = json.loads(tool.handle({"action": "overview"}, store=store, now=now))
    assert overview["ok"] and overview["devices"][0]["kinds"] == {"notification": 1}
    recent = json.loads(tool.handle({"action": "recent", "since": "24h"}, store=store, now=now))
    assert recent["count"] == 1 and "not instructions" in recent["note"]
    search = json.loads(tool.handle({"action": "search", "query": "IGA"}, store=store, now=now))
    assert [e["id"] for e in search["events"]] == ["evt-1"]
    assert json.loads(tool.handle({"action": "search"}, store=store, now=now))["error"] == "invalid"


def test_the_tool_reads_a_bare_date_as_the_phones_day(tmp_path):
    store = inbox.InboxStore(tmp_path, clock=lambda: NOW)
    phone = Phone()
    phone.accept(store.register_key(phone.key_request()))
    # 8:00 on the 14th in Auckland is still the 13th in UTC and on this host.
    store.ingest(phone.seal(_batch(_event("evt-morning", at="2026-10-14T08:00:00+13:00"),
                                   device={"name": "Michel's iPhone", "time_zone": "Pacific/Auckland"})))
    now = lambda: datetime.fromtimestamp(NOW, timezone.utc)  # noqa: E731
    day = json.loads(tool.handle({"action": "recent", "since": "2026-10-14", "until": "2026-10-14"},
                                 store=store, now=now))
    assert [e["id"] for e in day["events"]] == ["evt-morning"]
    before = json.loads(tool.handle({"action": "recent", "since": "2026-10-13", "until": "2026-10-13"},
                                    store=store, now=now))
    assert before["count"] == 0


def test_the_tool_stays_with_the_main_conversation(store, phone):
    store.ingest(phone.seal(_batch()))
    answer = json.loads(tool.handle({"action": "recent"}, session_id="child", is_child=lambda s: s == "child",
                                    store=store))
    assert answer["error"] == "subagent"


def test_the_tool_says_how_to_start_when_nothing_was_logged(store):
    answer = json.loads(tool.handle({"action": "recent"}, store=store))
    assert answer["error"] == "nothing_logged" and "Log to Hermes" in answer["note"]


def _client(monkeypatch, home):
    monkeypatch.setattr(api, "_calls_home", lambda: home)
    app = FastAPI()
    app.include_router(api.router, prefix=BASE)
    return TestClient(app)


def test_the_routes_key_ingest_report_counts_and_wipe(monkeypatch, tmp_path):
    client = _client(monkeypatch, tmp_path)
    phone = Phone()
    keyed = client.post(f"{BASE}/inbox/key", json=phone.key_request())
    assert keyed.status_code == 200 and keyed.headers["cache-control"] == "no-store"
    phone.accept(keyed.json())
    event = _event(at=datetime.now(timezone.utc).isoformat(timespec="seconds"))
    landed = client.post(f"{BASE}/inbox/ingest", json=phone.seal(_batch(event)))
    assert landed.status_code == 200 and landed.json()["stored"] == 1
    status = client.get(f"{BASE}/inbox/status", params={"device_id": DEVICE}).json()
    assert status["device"]["events"] == {"notification": 1}
    assert "IGA" not in json.dumps(status)
    assert client.post(f"{BASE}/inbox/wipe", json={}).status_code == 400
    wiped = client.post(f"{BASE}/inbox/wipe", json={"device_id": DEVICE})
    assert wiped.status_code == 200 and wiped.json()["remaining_devices"] == 0


def test_a_lost_key_answers_with_its_code(monkeypatch, tmp_path):
    client = _client(monkeypatch, tmp_path)
    phone = Phone()
    phone.accept(client.post(f"{BASE}/inbox/key", json=phone.key_request()).json())
    client.post(f"{BASE}/inbox/wipe", json={"all": True})
    refused = client.post(f"{BASE}/inbox/ingest", json=phone.seal(_batch()))
    assert refused.status_code == 409 and refused.json()["code"] == "unknown_key"


def _opened(phone, answer, request_id):
    return json.loads(inbox.open_sealed(phone.answer_key, answer["nonce"], answer["ct"],
                                        inbox.list_aad(phone.kid, phone.device_id, request_id)))


def test_the_settings_page_reads_its_own_events_sealed_a_page_at_a_time(store, phone):
    events = [_event(f"evt-{n}", at=f"2026-10-14T0{n}:00:00-04:00") for n in range(1, 6)]
    store.ingest(phone.seal(_batch(*events)))
    other = Phone("phone-installation-2")
    other.accept(store.register_key(other.key_request()))
    store.ingest(other.seal(_batch(_event("theirs"), batch_id="batch-0002")))
    sealed = store.list_sealed({"device_id": DEVICE, "kid": phone.kid, "request_id": "request-0001", "limit": 3})
    assert "IGA" not in json.dumps(sealed)
    first = _opened(phone, sealed, "request-0001")
    assert [event["id"] for event in first["events"]] == ["evt-5", "evt-4", "evt-3"]
    assert first["events"][0]["at"] == "2026-10-14T05:00:00-04:00" and first["next"]
    second = _opened(phone, store.list_sealed({"device_id": DEVICE, "kid": phone.kid, "request_id": "request-0002",
                                               "limit": 3, "before": first["next"]}), "request-0002")
    assert [event["id"] for event in second["events"]] == ["evt-2", "evt-1"] and second["next"] is None
    with pytest.raises(inbox.InboxError):  # an answer opens only for the request it answers
        _opened(phone, sealed, "request-0002")


def test_a_list_read_across_a_new_device_key_is_refused(store, phone, monkeypatch):
    # The device registers again between the key check and the events read: its
    # new key's events must never be sealed to the old kid.
    fresh = Phone()
    real_key = store._device_key

    def key_then_register(db, device_id, kid, purpose=inbox.INFO_INGEST):
        key = real_key(db, device_id, kid, purpose)
        monkeypatch.setattr(store, "_device_key", real_key)
        fresh.accept(store.register_key(fresh.key_request()))
        store.ingest(fresh.seal(_batch(_event("new-key"))))
        return key

    monkeypatch.setattr(store, "_device_key", key_then_register)
    with pytest.raises(inbox.InboxError) as refused:
        store.list_sealed({"device_id": DEVICE, "kid": phone.kid, "request_id": "request-0001"})
    assert refused.value.code == "unknown_key"


def test_the_settings_page_deletes_only_what_it_names_from_its_own(store, phone):
    store.ingest(phone.seal(_batch(_event("evt-1"), _event("evt-2"))))
    other = Phone("phone-installation-2")
    other.accept(store.register_key(other.key_request()))
    store.ingest(other.seal(_batch(_event("evt-1"), batch_id="batch-0002")))
    assert store.delete_events({"device_id": DEVICE, "kid": phone.kid, "ids": ["evt-1", "nope"]}) == {"deleted": 1}
    left = {(event["id"]) for event in _opened(phone, store.list_sealed(
        {"device_id": DEVICE, "kid": phone.kid, "request_id": "request-0001"}), "request-0001")["events"]}
    assert left == {"evt-2"}
    assert len(store.events()[0]) == 2  # the other phone's evt-1 stays
    with pytest.raises(inbox.InboxError) as refused:
        store.delete_events({"device_id": DEVICE, "kid": "0" * 32, "ids": ["evt-2"]})
    assert refused.value.code == "unknown_key"


@pytest.mark.parametrize("body", [
    {"device_id": DEVICE, "kid": "0" * 32},
    {"device_id": DEVICE, "kid": "0" * 32, "request_id": "request-0001", "limit": 0},
    {"device_id": DEVICE, "kid": "0" * 32, "request_id": "request-0001", "before": ["x", "evt-1"]},
])
def test_malformed_list_requests_are_refused(store, phone, body):
    with pytest.raises(inbox.InboxError) as refused:
        store.list_sealed(body)
    assert refused.value.code == "malformed"


def test_the_list_and_delete_routes(monkeypatch, tmp_path):
    client = _client(monkeypatch, tmp_path)
    phone = Phone()
    phone.accept(client.post(f"{BASE}/inbox/key", json=phone.key_request()).json())
    event = _event(at=datetime.now(timezone.utc).isoformat(timespec="seconds"))
    client.post(f"{BASE}/inbox/ingest", json=phone.seal(_batch(event)))
    listed = client.post(f"{BASE}/inbox/list", json={"device_id": DEVICE, "kid": phone.kid,
                                                     "request_id": "request-0001"})
    assert listed.status_code == 200 and listed.headers["cache-control"] == "no-store"
    assert [found["id"] for found in _opened(phone, listed.json(), "request-0001")["events"]] == ["evt-1"]
    deleted = client.post(f"{BASE}/inbox/delete", json={"device_id": DEVICE, "kid": phone.kid, "ids": ["evt-1"]})
    assert deleted.status_code == 200 and deleted.json()["deleted"] == 1


def test_the_delete_route_takes_the_largest_delete_the_store_does(monkeypatch, tmp_path):
    client = _client(monkeypatch, tmp_path)
    phone = Phone()
    phone.accept(client.post(f"{BASE}/inbox/key", json=phone.key_request()).json())
    ids = [f"{n:03d}" + "x" * (inbox.MAX_EVENT_ID_CHARS - 3) for n in range(inbox.MAX_DELETE)]
    deleted = client.post(f"{BASE}/inbox/delete", json={"device_id": DEVICE, "kid": phone.kid, "ids": ids})
    assert deleted.status_code == 200 and deleted.json()["deleted"] == 0


def test_new_events_wake_the_pulse_loop_that_runs_here(monkeypatch):
    import threading
    woken = []
    stop = threading.Event()
    running = threading.Thread(target=stop.wait, daemon=True)
    running.start()
    try:
        live = types.SimpleNamespace(_thread=running, wake=lambda: woken.append("live"))
        idle = types.SimpleNamespace(_thread=None, wake=lambda: woken.append("idle"))
        monkeypatch.setitem(sys.modules, "hermes_plugins.conduit_push.pulse", live)
        monkeypatch.setitem(sys.modules, "conduit_push_dashboard_pkg.pulse", idle)
        # Other suites may leave a real loop running in this process too.
        assert api._wake_running_pulse() >= 1
        assert woken == ["live"]  # the running loop, not an import whose thread never started
    finally:
        stop.set()


def test_the_route_caps_follow_the_store_limits():
    # The routes can't import inbox_store when they load; this keeps their
    # caps in step with it.
    fields = (inbox.MAX_SOURCE_CHARS + inbox.MAX_TITLE_CHARS + inbox.MAX_TEXT_CHARS + inbox.MAX_AMOUNT_CHARS
              + inbox.MAX_TRIGGER_CHARS)
    assert api.INBOX_MAX_INGEST_BODY_BYTES == \
        (inbox.MAX_EVENTS_PER_BATCH * (4 * fields + 512)) * 4 // 3 + 4096
    assert api.INBOX_MAX_DELETE_BODY_BYTES == inbox.MAX_DELETE * (inbox.MAX_EVENT_ID_CHARS + 3) + 1024


def test_a_full_batch_of_long_fields_fits_the_ingest_route(monkeypatch, tmp_path):
    client = _client(monkeypatch, tmp_path)
    phone = Phone()
    phone.accept(client.post(f"{BASE}/inbox/key", json=phone.key_request()).json())
    at = datetime.now(timezone.utc).isoformat(timespec="seconds")
    events = [_event(f"evt-{n}", at=at, source="é" * inbox.MAX_SOURCE_CHARS, title="ü" * inbox.MAX_TITLE_CHARS,
                     text="👍" * inbox.MAX_TEXT_CHARS, amount="€" * inbox.MAX_AMOUNT_CHARS)
              for n in range(inbox.MAX_EVENTS_PER_BATCH)]
    landed = client.post(f"{BASE}/inbox/ingest", json=phone.seal(_batch(*events)))
    assert landed.status_code == 200 and landed.json()["stored"] == inbox.MAX_EVENTS_PER_BATCH


def test_search_folds_case_beyond_ascii(store, phone):
    store.ingest(phone.seal(_batch(_event("evt-1", source="CAFÉ OLIMPICO", title="REÇU", text="Merci"))))
    assert [e["id"] for e in store.events(source="café")[0]] == ["evt-1"]
    assert [e["id"] for e in store.events(query="reçu")[0]] == ["evt-1"]


def test_the_tool_reads_a_bare_date_in_the_named_phones_zone(tmp_path):
    store = inbox.InboxStore(tmp_path, clock=lambda: NOW)
    auckland, toronto = Phone("phone-installation-1"), Phone("phone-installation-2")
    auckland.accept(store.register_key(auckland.key_request()))
    toronto.accept(store.register_key({**toronto.key_request(), "device_name": "Work iPhone"}))
    store.ingest(auckland.seal(_batch(_event("evt-morning", at="2026-10-14T08:00:00+13:00"),
                                      device={"name": "Michel's iPhone", "time_zone": "Pacific/Auckland"})))
    store.ingest(toronto.seal(_batch(_event("evt-work", at="2026-10-14T09:00:00-04:00"), batch_id="batch-0002",
                                     device={"name": "Work iPhone", "time_zone": "America/Toronto"})))
    now = lambda: datetime.fromtimestamp(NOW, timezone.utc)  # noqa: E731
    day = json.loads(tool.handle({"action": "recent", "since": "2026-10-14", "until": "2026-10-14",
                                  "device": "michel's iphone"}, store=store, now=now))
    assert [e["id"] for e in day["events"]] == ["evt-morning"]
