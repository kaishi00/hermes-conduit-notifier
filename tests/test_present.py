import contextlib
import importlib.util
import json
import pathlib
import re
import sys
import tempfile
import threading
import time
import types

import pytest
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


store_module = _load("conduit_push.present_store", ROOT / "present_store.py")
tool = _load("conduit_push.present_tool", ROOT / "present_tool.py")
events_module = _load("conduit_push.events", ROOT / "events.py")
_api_spec = importlib.util.spec_from_file_location("conduit_plugin_api_present", ROOT / "dashboard" / "plugin_api.py")
api = importlib.util.module_from_spec(_api_spec)
_api_spec.loader.exec_module(api)

PresentStore = store_module.PresentStore
PresentError = store_module.PresentError

PHONE = "phone-installation-1"
IPAD = "ipad-installation-2"
PREVIEW = "\n  **To:** Mei  \nRunning 10 minutes late, sorry!"


def _exchange(store, installation=PHONE, results=()):
    return store.exchange({"installation_id": installation, "device": "Eric's iPhone", "app_version": "2.0",
                           "results": list(results)})


def _confirm(store, **overrides):
    fields = {"title": "Send to Mei?", "preview": PREVIEW, "action_label": "Send", "spoken_yes": ["send it"],
              "session_id": "s1"}
    fields.update(overrides)
    return store.confirm(fields.pop("title"), fields.pop("preview"), fields.pop("action_label"), **fields)


class Clock:
    def __init__(self):
        self.now = 1_000_000.0

    def __call__(self):
        return self.now


@pytest.fixture()
def clock():
    return Clock()


@pytest.fixture()
def store(tmp_path, clock):
    return PresentStore(tmp_path, clock=clock)


# --- Store ---------------------------------------------------------------------


def test_a_prompt_waits_for_the_phone_and_its_answer_reaches_hermes_once(store):
    assert _exchange(store) == {"prompts": []}
    prompt = _confirm(store)
    assert prompt["status"] == "waiting" and prompt["expires_at"] == prompt["created_at"] + 30 * 60
    [shown] = _exchange(store)["prompts"]
    assert shown == {"id": prompt["id"], "title": "Send to Mei?", "preview": "**To:** Mei\nRunning 10 minutes late, sorry!",
                     "action_label": "Send", "spoken_yes": ["send it"], "session_id": "s1",
                     "created_at": prompt["created_at"], "expires_at": prompt["expires_at"]}

    assert _exchange(store, results=[{"id": prompt["id"], "outcome": "confirmed", "via": "voice"}]) == {"prompts": []}
    [answered] = store.status()
    assert answered["status"] == "confirmed" and answered["via"] == "voice" and answered["device"] == PHONE
    # Reported once: the next status without an id no longer lists it, by id it still shows.
    assert store.status() == []
    assert store.status(prompt["id"])[0]["status"] == "confirmed"


def test_a_quiet_exchange_rewrites_the_file_at_most_once_a_minute(store, clock):
    _exchange(store)
    stamp = store.path.stat().st_mtime_ns
    clock.now += 30
    _exchange(store)
    assert store.path.stat().st_mtime_ns == stamp
    clock.now += store_module.SEEN_REFRESH_S
    _exchange(store)
    assert store._load()["devices"][PHONE]["seen_at"] == clock.now


def test_the_first_answer_wins_and_unknown_ids_are_ignored(store):
    _exchange(store)
    prompt = _confirm(store)
    _exchange(store, results=[{"id": prompt["id"], "outcome": "declined", "via": "tap"},
                              {"id": "unknown-prompt-id", "outcome": "confirmed", "via": "tap"}])
    _exchange(store, installation=IPAD, results=[{"id": prompt["id"], "outcome": "confirmed", "via": "tap"}])
    assert store.get(prompt["id"])["status"] == "declined"


def test_edit_keeps_its_text_and_malformed_results_are_ignored(store):
    _exchange(store)
    first, second = _confirm(store), _confirm(store, title="Delete the draft?")
    _exchange(store, results=["junk", {"id": first["id"], "outcome": "maybe", "via": "tap"},
                              {"id": first["id"], "outcome": "confirmed", "via": "telepathy"},
                              {"id": second["id"], "outcome": "edit", "via": "tap", "edit_text": "Say 15 minutes"}])
    assert store.get(first["id"])["status"] == "waiting"
    edited = store.get(second["id"])
    assert edited["status"] == "edit" and edited["edit_text"] == "Say 15 minutes" and edited["via"] == "tap"


def test_a_missing_via_counts_as_a_tap(store):
    _exchange(store)
    prompt = _confirm(store)
    _exchange(store, results=[{"id": prompt["id"], "outcome": "edit"}])
    edited = store.get(prompt["id"])
    assert edited["via"] == "tap" and "edit_text" not in edited


def test_a_prompt_nobody_answers_expires_and_the_phone_stops_showing_it(store, clock):
    _exchange(store)
    prompt = _confirm(store, ttl_minutes=5)
    clock.now += 5 * 60
    assert _exchange(store) == {"prompts": []}
    assert store.get(prompt["id"])["status"] == "expired"
    # A late answer doesn't revive it.
    _exchange(store, results=[{"id": prompt["id"], "outcome": "confirmed", "via": "tap"}])
    [expired] = store.status()
    assert expired["status"] == "expired" and expired["finished_at"] == prompt["expires_at"]


def test_finished_prompts_are_forgotten_after_a_week(store, clock):
    _exchange(store)
    prompt = _confirm(store)
    store.cancel(prompt["id"])
    clock.now += store_module.FINISHED_TTL_S + 1
    assert store.get(prompt["id"]) is None


def test_cancel_takes_back_a_waiting_prompt_but_not_an_answer(store):
    _exchange(store)
    waiting, answered = _confirm(store), _confirm(store)
    _exchange(store, results=[{"id": answered["id"], "outcome": "confirmed", "via": "tap"}])
    assert store.cancel(waiting["id"])["status"] == "cancelled"
    assert store.cancel(answered["id"])["status"] == "confirmed"
    assert store.cancel("no-such-prompt") is None
    assert _exchange(store) == {"prompts": []}


def test_prompts_per_hour_are_limited(store, clock):
    _exchange(store)
    for _ in range(store_module.PER_HOUR):
        store.cancel(_confirm(store)["id"])
    with pytest.raises(PresentError) as error:
        _confirm(store)
    assert error.value.code == "rate_limited"
    clock.now += store_module.HOUR_S
    _exchange(store)
    _confirm(store)


def test_input_is_checked_against_the_contracts_caps(store):
    _exchange(store)
    for overrides, code in (
        ({"title": " "}, "no_title"),
        ({"title": "x" * 81}, "too_long"),
        ({"preview": "\n \n"}, "no_preview"),
        ({"preview": "x" * 2001}, "too_long"),
        ({"action_label": "x" * 25}, "too_long"),
        ({"action_label": " "}, "invalid"),
        ({"spoken_yes": ["a", "b", "c", "d", "e", "f"]}, "invalid"),
        ({"spoken_yes": ["x" * 41]}, "invalid"),
        ({"spoken_yes": "send it"}, "invalid"),
        ({"ttl_minutes": 0}, "invalid"),
        ({"ttl_minutes": 1441}, "invalid"),
        ({"ttl_minutes": True}, "invalid"),
    ):
        with pytest.raises(PresentError) as error:
            _confirm(store, **overrides)
        assert error.value.code == code, overrides
    assert _exchange(store) == {"prompts": []}
    assert _confirm(store, action_label=None, spoken_yes=None, ttl_minutes=1440)["action_label"] == "Confirm"


def test_no_prompt_without_a_capable_phone(store):
    with pytest.raises(PresentError) as error:
        _confirm(store)
    assert error.value.code == "no_device"
    assert not store.has_device()
    _exchange(store)
    assert store.has_device()


def test_a_go_prompt_goes_only_to_a_conduit_that_can_open_maps(store, clock):
    # Conduit without maps-navigate (an older build, or another device) would
    # show a Go prompt as a plain confirm: a tap there never opens Maps.
    def exchange(installation, features=(), results=()):
        return store.exchange({"installation_id": installation, "features": list(features),
                               "results": list(results)})["prompts"]

    exchange(IPAD)
    exchange(PHONE, ["maps-navigate"])
    go = store.confirm("Directions to Paris?", "Opens Apple Maps.", "Go", maps={"to": "Paris", "mode": "walking"})
    plain = _confirm(store)
    assert [prompt["id"] for prompt in exchange(IPAD)] == [plain["id"]]
    exchange(IPAD, results=[{"id": go["id"], "outcome": "confirmed", "via": "tap"}])
    assert store.status(go["id"])[0]["status"] == "waiting", "a Conduit that can't open Maps doesn't answer Go"
    assert [prompt["id"] for prompt in exchange(PHONE, ["maps-navigate"])] == [go["id"], plain["id"]]
    exchange(PHONE, ["maps-navigate"], results=[{"id": go["id"], "outcome": "confirmed", "via": "tap"}])
    [answered] = store.status(go["id"])
    assert answered["status"] == "confirmed" and answered["device"] == PHONE
    # A Conduit that said so long ago doesn't count any more.
    assert store.has_feature("maps-navigate")
    clock.now += store_module.CARD_DEVICE_RECENT_S
    assert not store.has_feature("maps-navigate")


def test_a_malformed_exchange_is_refused(store):
    for body in (None, {"installation_id": "x"}, {"installation_id": PHONE, "results": "no"},
                 {"installation_id": PHONE, "results": [{}] * (store_module.MAX_RESULTS + 1)}):
        with pytest.raises(PresentError):
            store.exchange(body)
    assert not store.has_device()


def test_a_corrupted_file_reads_as_what_is_still_well_formed(store, tmp_path):
    _exchange(store)
    prompt = _confirm(store)
    path = tmp_path / store_module.FILE_NAME
    state = json.loads(path.read_text())
    state["prompts"] += ["junk", {"id": "short"}, {**state["prompts"][0], "id": "another-good-id", "status": "nope"}]
    path.write_text(json.dumps(state))
    assert [item["id"] for item in _exchange(store)["prompts"]] == [prompt["id"]]


def test_the_file_is_private(store, tmp_path):
    _exchange(store)
    assert (tmp_path / store_module.FILE_NAME).stat().st_mode & 0o777 == 0o600


def test_the_wake_body_is_the_first_non_empty_preview_line():
    assert store_module.first_line("\n   \n  **To:** Mei  \nsecond") == "**To:** Mei"


# --- Tool ----------------------------------------------------------------------


@pytest.fixture()
def tool_home(tmp_path, monkeypatch):
    monkeypatch.setattr(tool, "get_hermes_home", lambda: tmp_path)
    monkeypatch.setattr(tool.client, "load_state", lambda: {"credential": "c"})
    return tmp_path


@pytest.fixture()
def pushes(monkeypatch):
    sent = []
    monkeypatch.setattr(tool.client, "enqueue", lambda event: sent.append(event))
    return sent


def _call(args, session_id="s1", **kwargs):
    return json.loads(tool.handle(args, session_id=session_id, **kwargs))


CONFIRM = {"action": "confirm", "title": "Send to Mei?", "preview": PREVIEW, "action_label": "Send",
           "spoken_yes": ["send it", "yes send"]}


def test_the_tool_is_hidden_until_a_phone_exchanges_with_present_v1(tool_home):
    assert tool.available() is False
    _exchange(PresentStore(tool_home))
    assert tool.available() is True


def test_the_tool_is_hidden_without_a_pairing(tool_home, monkeypatch):
    _exchange(PresentStore(tool_home))
    monkeypatch.setattr(tool.client, "load_state", lambda: None)
    assert tool.available() is False


def test_confirm_keeps_the_prompt_and_wakes_the_phone(tool_home, pushes):
    _exchange(PresentStore(tool_home))
    answer = _call(CONFIRM, profile="work")
    assert answer["ok"] is True and answer["status"] == "waiting" and "Don't do the action" in answer["note"]
    [event] = pushes
    assert event["type"] == "phone.changes" and event["event_id"] == f"present:{answer['id']}"
    assert event["title"] == "Send to Mei?" and event["body"] == "**To:** Mei"
    assert event["mode"] == "alert" and event["profile"] == "work"
    # The relay's event_id rule (relay/src/server.mjs validateEvent).
    assert re.fullmatch(r"[A-Za-z0-9:_-]{8,128}", event["event_id"])
    assert "present-v1" in event["plugin_capabilities"]
    [shown] = _exchange(PresentStore(tool_home))["prompts"]
    assert shown["session_id"] == "s1" and shown["spoken_yes"] == ["send it", "yes send"]


def test_a_push_failure_never_fails_the_tool(tool_home, monkeypatch):
    _exchange(PresentStore(tool_home))

    def broken(event):
        raise OSError("relay down")
    monkeypatch.setattr(tool.client, "enqueue", broken)
    assert _call(CONFIRM)["status"] == "waiting"


def test_confirm_refuses_bad_input_without_waking_the_phone(tool_home, pushes):
    _exchange(PresentStore(tool_home))
    assert _call({**CONFIRM, "title": "x" * 81})["error"] == "too_long"
    assert _call({**CONFIRM, "preview": ""})["error"] == "no_preview"
    assert _call({**CONFIRM, "wait_seconds": 121})["error"] == "invalid"
    assert _call({**CONFIRM, "wait_seconds": -1})["error"] == "invalid"
    assert _call({**CONFIRM, "ttl_minutes": 2000})["error"] == "invalid"
    assert pushes == []


def test_wait_seconds_returns_as_soon_as_the_user_answers(tool_home, pushes):
    store = PresentStore(tool_home)
    _exchange(store)

    def answer_soon():
        deadline = time.monotonic() + 5
        while not pushes and time.monotonic() < deadline:
            time.sleep(0.02)
        prompt_id = pushes[0]["event_id"].split(":", 1)[1]
        time.sleep(0.3)
        _exchange(PresentStore(tool_home), results=[{"id": prompt_id, "outcome": "confirmed", "via": "voice"}])

    answerer = threading.Thread(target=answer_soon)
    answerer.start()
    started = time.monotonic()
    answer = _call({**CONFIRM, "wait_seconds": 30})
    answerer.join()
    assert time.monotonic() - started < 5
    assert answer["status"] == "confirmed" and answer["via"] == "voice" and "Go ahead" in answer["note"]
    # Returned to the agent, so it isn't reported again.
    assert _call({"action": "status"})["prompts"] == []


def test_a_wait_that_runs_out_is_never_a_yes(tool_home, pushes):
    _exchange(PresentStore(tool_home))
    now = [0.0]
    sleeps = []

    def sleep(seconds):
        sleeps.append(seconds)
        now[0] += seconds
    answer = _call({**CONFIRM, "wait_seconds": 3}, sleep=sleep, monotonic=lambda: now[0])
    assert answer["status"] == "waiting" and "Don't do the action" in answer["note"]
    assert sum(sleeps) == pytest.approx(3) and sleeps[0] == tool.POLL_START_S and max(sleeps) <= tool.POLL_MAX_S


def test_status_reports_each_answer_once_and_edit_says_to_wait(tool_home, pushes):
    store = PresentStore(tool_home)
    _exchange(store)
    first = _call(CONFIRM)
    second = _call({**CONFIRM, "title": "Delete the old drafts?", "action_label": "Delete"})
    _exchange(store, results=[{"id": first["id"], "outcome": "edit", "via": "tap"}])
    prompts = _call({"action": "status"})["prompts"]
    assert {(item["id"], item["status"]) for item in prompts} == {(first["id"], "edit"), (second["id"], "waiting")}
    edit = next(item for item in prompts if item["status"] == "edit")
    assert "send the corrected request themselves" in edit["note"]
    assert [item["id"] for item in _call({"action": "status"})["prompts"]] == [second["id"]]
    assert _call({"action": "status", "id": first["id"]})["prompts"][0]["status"] == "edit"
    assert _call({"action": "status", "id": "no-such-prompt"})["error"] == "unknown_id"


def test_an_answer_the_agent_missed_rides_along_with_the_next_confirm(tool_home, pushes):
    store = PresentStore(tool_home)
    _exchange(store)
    first = _call(CONFIRM)
    _exchange(store, results=[{"id": first["id"], "outcome": "declined", "via": "tap"}])
    answer = _call({**CONFIRM, "title": "Buy the tickets?"})
    [earlier] = answer["earlier_answers"]
    assert earlier["id"] == first["id"] and earlier["status"] == "declined"


def test_expired_prompts_read_as_no(tool_home, pushes, monkeypatch):
    clock = Clock()
    monkeypatch.setattr(tool, "_store", lambda: PresentStore(tool_home, clock=clock))
    _exchange(PresentStore(tool_home, clock=clock))
    prompt = _call({**CONFIRM, "ttl_minutes": 1})
    clock.now += 61
    [expired] = _call({"action": "status"})["prompts"]
    assert expired["id"] == prompt["id"] and expired["status"] == "expired" and "not a yes" in expired["note"]


def test_cancel(tool_home, pushes):
    store = PresentStore(tool_home)
    _exchange(store)
    prompt = _call(CONFIRM)
    assert _call({"action": "cancel"})["error"] == "no_id"
    assert _call({"action": "cancel", "id": "no-such-prompt"})["error"] == "unknown_id"
    assert _call({"action": "cancel", "id": prompt["id"]})["status"] == "cancelled"
    assert _exchange(store) == {"prompts": []}


def test_a_subagent_cannot_ask_the_user(tool_home, pushes):
    _exchange(PresentStore(tool_home))
    child = lambda session: session == "child"  # noqa: E731
    for args in (CONFIRM, {"action": "status"}, {"action": "cancel", "id": "x" * 16}):
        assert _call(args, session_id="child", is_child=child)["error"] == "subagent"
    assert pushes == []
    assert _exchange(PresentStore(tool_home)) == {"prompts": []}


def test_no_phone_says_so(tool_home, pushes):
    assert _call(CONFIRM)["error"] == "no_device"


def test_a_bad_action_is_refused(tool_home):
    assert _call({"action": "show"})["ok"] is False


def test_the_schema_matches_the_contract():
    properties = tool.SCHEMA["parameters"]["properties"]
    assert properties["action"]["enum"] == ["confirm", "status", "cancel"]
    assert properties["title"]["maxLength"] == 80 and properties["preview"]["maxLength"] == 2000
    assert properties["action_label"]["maxLength"] == 24 and properties["spoken_yes"]["maxItems"] == 5
    assert properties["ttl_minutes"]["maximum"] == 1440 and properties["wait_seconds"]["maximum"] == 120
    description = tool.SCHEMA["description"]
    for phrase in ("sending a message", "voice", "never a yes", "corrected request themselves"):
        assert phrase in description


def test_present_v1_is_a_plugin_capability():
    assert "present-v1" in events_module.PLUGIN_CAPABILITIES


# --- Route ---------------------------------------------------------------------


@pytest.fixture()
def route_home(tmp_path, monkeypatch):
    monkeypatch.setattr(api, "_calls_home", lambda: tmp_path)
    monkeypatch.setattr(api, "_pairing_state_path", lambda: tmp_path / "conduit-push.json")
    monkeypatch.setattr(api, "_profile_scope", lambda profile: contextlib.nullcontext())
    api._present_exchange_limiter._mints.clear()
    (tmp_path / "conduit-push.json").write_text(json.dumps({"credential": "c", "installation_id": "i", "gateway_id": "g"}))
    return tmp_path


def _http():
    app = FastAPI()
    app.include_router(api.router, prefix=BASE)
    return TestClient(app)


def test_the_route_records_answers_and_returns_waiting_prompts(route_home):
    http = _http()
    body = {"installation_id": PHONE, "device": "iPhone", "app_version": "2.0", "results": []}
    response = http.post(f"{BASE}/present/exchange?profile=work", json=body)
    assert response.status_code == 200 and response.headers["cache-control"] == "no-store"
    assert response.json() == {"ok": True, "prompts": []}
    store = PresentStore(route_home)
    first, second = _confirm(store), _confirm(store, title="Buy it?")
    assert [item["id"] for item in http.post(f"{BASE}/present/exchange", json=body).json()["prompts"]] \
        == [first["id"], second["id"]]
    answered = {**body, "results": [{"id": first["id"], "outcome": "confirmed", "via": "tap"},
                                    {"id": "already-gone-id", "outcome": "declined", "via": "voice"}]}
    assert [item["id"] for item in http.post(f"{BASE}/present/exchange", json=answered).json()["prompts"]] \
        == [second["id"]]
    assert store.get(first["id"])["status"] == "confirmed"
    # Answering again is ignored, not an error.
    assert http.post(f"{BASE}/present/exchange", json=answered).status_code == 200


def test_the_route_refuses_a_malformed_exchange(route_home):
    assert _http().post(f"{BASE}/present/exchange", json={"installation_id": "x"}).status_code == 400
    assert _http().post(f"{BASE}/present/exchange", json=[1]).status_code == 400


def test_the_route_needs_a_paired_profile(route_home):
    (route_home / "conduit-push.json").unlink()
    assert _http().post(f"{BASE}/present/exchange", json={"installation_id": PHONE}).status_code == 409


def test_the_route_is_rate_limited(route_home):
    http = _http()
    for _ in range(api.PRESENT_EXCHANGE_LIMIT):
        assert http.post(f"{BASE}/present/exchange", json={"installation_id": PHONE}).status_code == 200
    assert http.post(f"{BASE}/present/exchange", json={"installation_id": PHONE}).status_code == 429


def test_the_route_is_a_reported_capability():
    assert "present-v1" in api.ROUTE_CAPABILITIES


def test_session_ids_lose_control_characters_and_a_nested_file_reads_as_empty(store, tmp_path):
    _exchange(store)
    _confirm(store, session_id="s1\n\x1b[2Jx")
    [shown] = _exchange(store)["prompts"]
    assert shown["session_id"] == "s1 [2Jx"
    store.path.write_text("[" * 100_000, encoding="utf-8")
    assert _exchange(store) == {"prompts": []}



def test_a_hand_edited_store_is_cleaned_before_the_phone_sees_it(store):
    _exchange(store)
    _confirm(store)
    state = json.loads(store.path.read_text(encoding="utf-8"))
    state["prompts"][0].update(title="Send\x1b[2J?" + "x" * 500, spoken_yes=["send\x07 it", 3, ""], action_label="\x00")
    store.path.write_text(json.dumps(state), encoding="utf-8")
    [shown] = _exchange(store)["prompts"]
    assert shown["title"].startswith("Send [2J?") and len(shown["title"]) == store_module.MAX_TITLE_CHARS
    assert shown["spoken_yes"] == ["send it"] and shown["action_label"] == "Confirm"


def test_an_unreadable_store_is_not_overwritten(store, monkeypatch):
    _exchange(store)
    _confirm(store)
    saved = store.path.read_text(encoding="utf-8")
    real = type(store.path).read_text

    def unreadable(path, *args, **kwargs):
        if path == store.path:
            raise PermissionError("busy")
        return real(path, *args, **kwargs)

    monkeypatch.setattr(type(store.path), "read_text", unreadable)
    with pytest.raises(OSError):
        _exchange(store)
    monkeypatch.undo()
    assert store.path.read_text(encoding="utf-8") == saved


def test_the_wake_shows_the_cleaned_first_line(tool_home, pushes):
    _exchange(PresentStore(tool_home))
    _call({**CONFIRM, "preview": "\x1b]0;evil\x07 hi\nmore"})
    [event] = pushes
    assert event["body"] == "]0;evil hi"
    assert "\x1b" not in event["body"] and "\x07" not in event["body"]
