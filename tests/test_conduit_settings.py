import contextlib
import importlib.util
import json
import pathlib
import sys
import tempfile
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


store_module = _load("conduit_push.settings_store", ROOT / "settings_store.py")
tool = _load("conduit_push.settings_tool", ROOT / "settings_tool.py")
_api_spec = importlib.util.spec_from_file_location("conduit_plugin_api_settings", ROOT / "dashboard" / "plugin_api.py")
api = importlib.util.module_from_spec(_api_spec)
_api_spec.loader.exec_module(api)

SettingsStore = store_module.SettingsStore
SettingsError = store_module.SettingsError

PHONE = "phone-installation-1"
IPAD = "ipad-installation-2"


def _settings(**values):
    tone = {"key": "voice.live.tone", "title": "Tone", "section": "Live voice", "description": "How live calls sound.",
            "kind": "choice", "tier": "small", "value": values.get("tone", "default"),
            "choices": [{"value": "default", "title": "Model's own"}, {"value": "relaxed", "title": "Relaxed"},
                        {"value": "professional", "title": "Professional"}]}
    memory = {"key": "voice.gemini.memory", "title": "Gemini Live memory", "kind": "bool", "tier": "ask",
              "description": "Sends Hermes' memory to Google.", "value": values.get("memory", False)}
    greeting = {"key": "voice.live.greeting", "title": "Greeting", "kind": "text", "tier": "small", "max_length": 20,
                "value": values.get("greeting", "")}
    return [tone, memory, greeting]


def _sync(store, installation=PHONE, mode="ask", results=(), **values):
    return store.sync({"installation_id": installation, "device": "Eric's iPhone", "app_version": "1.0",
                       "mode": mode, "settings": _settings(**values), "results": list(results)})


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
    return SettingsStore(tmp_path, clock=clock)


# --- Store ---------------------------------------------------------------------


def test_a_request_waits_for_its_phone_and_its_result_reaches_hermes(store):
    assert _sync(store) == {"pending": []}
    made = store.request([{"key": "voice.live.tone", "value": "relaxed"}], "You asked for calmer calls.",
                         session_id="s1")
    assert made["device"] == PHONE and made["mode"] == "ask"
    [request] = made["requests"]
    assert request["from"] == "default" and request["value"] == "relaxed" and request["tier"] == "small"
    assert request["title"] == "Tone" and request["session_id"] == "s1"

    assert _sync(store)["pending"] == [request]
    assert _sync(store, results=[{"id": request["id"], "status": "applied"}], tone="relaxed") == {"pending": []}
    [result] = store.view()["results"]
    assert result["status"] == "applied" and result["key"] == "voice.live.tone" and result["value"] == "relaxed"


def test_an_undo_after_apply_is_kept_as_the_latest_result(store):
    _sync(store)
    [request] = store.request([{"key": "voice.live.tone", "value": "relaxed"}], "Calmer.")["requests"]
    _sync(store, results=[{"id": request["id"], "status": "applied"}])
    _sync(store, results=[{"id": request["id"], "status": "undone", "note": "user tapped Undo"}])
    [result] = store.view()["results"]
    assert result["status"] == "undone" and result["note"] == "user tapped Undo"


def test_a_result_for_another_device_or_unknown_request_is_ignored(store):
    _sync(store)
    _sync(store, installation=IPAD)
    [request] = store.request([{"key": "voice.live.tone", "value": "relaxed"}], "Calmer.", device=PHONE)["requests"]
    _sync(store, installation=IPAD, results=[{"id": request["id"], "status": "applied"},
                                             {"id": "nope-nope-nope", "status": "applied"}])
    assert [item["id"] for item in store.view()["pending"]] == [request["id"]]
    assert store.view()["results"] == []


def test_values_are_checked_against_what_the_phone_listed(store):
    _sync(store)
    with pytest.raises(SettingsError) as error:
        store.request([{"key": "voice.live.tone", "value": "shouty"}], "x")
    assert error.value.code == "invalid_value" and "relaxed" in str(error.value)
    with pytest.raises(SettingsError) as error:
        store.request([{"key": "calls.enabled", "value": True}], "x")
    assert error.value.code == "unknown_key"
    with pytest.raises(SettingsError) as error:
        store.request([{"key": "voice.gemini.memory", "value": "yes"}], "x")
    assert error.value.code == "invalid_value"
    with pytest.raises(SettingsError) as error:
        store.request([{"key": "voice.live.greeting", "value": "x" * 21}], "x")
    assert error.value.code == "invalid_value"
    with pytest.raises(SettingsError) as error:
        store.request([{"key": "voice.live.tone", "value": "relaxed"}], "  ")
    assert error.value.code == "no_reason"
    assert store.view()["pending"] == []


def test_one_bad_change_refuses_the_whole_request(store):
    _sync(store)
    with pytest.raises(SettingsError):
        store.request([{"key": "voice.live.tone", "value": "relaxed"}, {"key": "nope", "value": 1}], "x")
    assert store.view()["pending"] == []


def test_an_unchanged_value_asks_for_nothing(store):
    _sync(store, tone="relaxed")
    assert store.request([{"key": "voice.live.tone", "value": "relaxed"}], "x")["unchanged"] is True


def test_off_refuses_changes(store):
    _sync(store, mode="off")
    with pytest.raises(SettingsError) as error:
        store.request([{"key": "voice.live.tone", "value": "relaxed"}], "x")
    assert error.value.code == "off"


def test_a_newer_request_replaces_an_older_one_for_the_same_setting(store):
    _sync(store)
    store.request([{"key": "voice.live.tone", "value": "relaxed"}], "first")
    store.request([{"key": "voice.live.tone", "value": "professional"}], "second")
    [request] = store.view()["pending"]
    assert request["value"] == "professional" and request["reason"] == "second"


def test_changes_go_to_the_device_seen_last_unless_named(store, clock):
    _sync(store)
    clock.now += 10
    _sync(store, installation=IPAD)
    assert store.request([{"key": "voice.live.tone", "value": "relaxed"}], "x")["device"] == IPAD
    assert store.request([{"key": "voice.live.tone", "value": "relaxed"}], "x", device=PHONE)["device"] == PHONE
    with pytest.raises(SettingsError) as error:
        store.request([{"key": "voice.live.tone", "value": "relaxed"}], "x", device="Someone's phone")
    assert error.value.code == "unknown_device"


def test_requests_per_hour_are_limited(store, clock):
    _sync(store)
    for index in range(store_module.PER_HOUR // 2):
        store.request([{"key": "voice.live.tone", "value": "relaxed" if index % 2 else "professional"},
                       {"key": "voice.gemini.memory", "value": True}], "x")
    with pytest.raises(SettingsError) as error:
        store.request([{"key": "voice.live.tone", "value": "relaxed"}], "x")
    assert error.value.code == "rate_limited"
    clock.now += store_module.HOUR_S
    store.request([{"key": "voice.live.tone", "value": "relaxed"}], "x")


def test_waiting_requests_expire(store, clock):
    _sync(store)
    store.request([{"key": "voice.live.tone", "value": "relaxed"}], "x")
    clock.now += store_module.PENDING_TTL_S + 1
    _sync(store)
    assert store.view()["pending"] == []


def test_cancel_takes_back_a_waiting_request(store):
    _sync(store)
    [request] = store.request([{"key": "voice.live.tone", "value": "relaxed"}], "x")["requests"]
    assert store.cancel([request["id"]]) == 1
    assert _sync(store) == {"pending": []}


def test_a_malformed_upload_is_refused(store):
    for body in (
        None,
        {"installation_id": "x", "mode": "ask", "settings": []},
        {"installation_id": PHONE, "mode": "always", "settings": []},
        {"installation_id": PHONE, "mode": "ask", "settings": [{"key": "Bad Key", "title": "t", "kind": "bool",
                                                                 "tier": "small", "value": True}]},
        {"installation_id": PHONE, "mode": "ask", "settings": [{"key": "a", "title": "t", "kind": "bool",
                                                                 "tier": "never", "value": True}]},
        {"installation_id": PHONE, "mode": "ask", "settings": [{"key": "a", "title": "t", "kind": "choice",
                                                                 "tier": "small", "value": "z",
                                                                 "choices": [{"value": "y"}]}]},
    ):
        with pytest.raises(SettingsError):
            store.sync(body)
    assert not store.has_device()


def test_a_corrupted_file_reads_as_what_is_still_well_formed(store, tmp_path):
    _sync(store)
    [request] = store.request([{"key": "voice.live.tone", "value": "relaxed"}], "x")["requests"]
    path = tmp_path / store_module.FILE_NAME
    state = json.loads(path.read_text())
    state["pending"] += [{"installation_id": ["x"], "id": "a", "key": "k", "at": 1}, {"id": "b"}, "junk",
                         {**state["pending"][0], "id": "no-value-here", "value": None}]
    del state["pending"][-1]["value"]
    state["results"] = [{"id": "r", "at": state["pending"][0]["at"]}, 7]
    path.write_text(json.dumps(state))
    assert [item["id"] for item in store.view()["pending"]] == [request["id"]]
    assert store.view()["results"] == []
    assert _sync(store)["pending"][0]["id"] == request["id"]


def test_a_burst_for_one_device_never_drops_anothers_requests(store, clock):
    _sync(store)
    _sync(store, installation=IPAD)
    [kept] = store.request([{"key": "voice.live.tone", "value": "relaxed"}], "x", device=PHONE)["requests"]
    many = [{"key": f"voice.k{index}", "title": "t", "kind": "bool", "tier": "small", "value": False}
                       for index in range(store_module.MAX_PENDING + 2)]
    store.sync({"installation_id": IPAD, "mode": "ask", "settings": many})
    for start in range(0, len(many), 10):
        clock.now += store_module.HOUR_S
        store.request([{"key": item["key"], "value": True} for item in many[start:start + 10]], "x",
                      device=IPAD)
    pending = store.view()["pending"]
    assert kept["id"] in [item["id"] for item in pending]
    assert len([item for item in pending if item["device"] == IPAD]) == store_module.MAX_PENDING


def test_the_file_is_private(store, tmp_path):
    _sync(store)
    assert (tmp_path / store_module.FILE_NAME).stat().st_mode & 0o777 == 0o600


# --- Tool ----------------------------------------------------------------------


@pytest.fixture()
def tool_home(tmp_path, monkeypatch):
    monkeypatch.setattr(tool, "get_hermes_home", lambda: tmp_path)
    monkeypatch.setattr(tool.client, "load_state", lambda: {"credential": "c"})
    return tmp_path


def _call(args, session_id="s1"):
    return json.loads(tool.handle(args, session_id=session_id))


def test_the_tool_is_hidden_until_a_conduit_shares_its_settings(tool_home):
    assert tool.available() is False
    _sync(SettingsStore(tool_home))
    assert tool.available() is True


def test_get_shows_settings_mode_and_what_hermes_owns(tool_home):
    _sync(SettingsStore(tool_home))
    answer = _call({"action": "get"})
    assert answer["ok"] is True
    assert answer["device"] == {"id": PHONE, "name": "Eric's iPhone", "mode": "ask"}
    tone = next(setting for setting in answer["settings"] if setting["key"] == "voice.live.tone")
    assert tone["choices"][1] == {"value": "relaxed", "title": "Relaxed"}
    assert tone["tier"] == "applies with Undo when the user allows"
    assert "stt.provider" in answer["hermes_owned"]
    assert "tap Apply" in answer["note"]
    greeting = next(setting for setting in answer["settings"] if setting["key"] == "voice.live.greeting")
    assert greeting["value"] == ""


def test_change_says_it_waits_for_conduit(tool_home):
    _sync(SettingsStore(tool_home))
    answer = _call({"action": "change", "changes": [{"key": "voice.gemini.memory", "value": True}],
                    "reason": "You asked Gemini to remember things."})
    assert answer["ok"] is True and answer["status"] == "waiting_for_conduit"
    assert "approve" in answer["note"] and "isn't changed yet" in answer["note"]
    assert _call({"action": "get"})["waiting"][0]["key"] == "voice.gemini.memory"


def test_small_mode_says_small_changes_apply(tool_home):
    _sync(SettingsStore(tool_home), mode="small")
    answer = _call({"action": "change", "changes": [{"key": "voice.live.tone", "value": "relaxed"}], "reason": "x"})
    assert "Undo" in answer["note"]


def test_off_points_at_the_switch(tool_home):
    _sync(SettingsStore(tool_home), mode="off")
    answer = _call({"action": "change", "changes": [{"key": "voice.live.tone", "value": "relaxed"}], "reason": "x"})
    assert answer["error"] == "off" and "conduit://settings/voice" in answer["note"]
    assert "conduit://settings/voice" in _call({"action": "get"})["note"]


def test_an_unknown_key_names_hermes_owned_settings(tool_home):
    _sync(SettingsStore(tool_home))
    answer = _call({"action": "change", "changes": [{"key": "tts.provider", "value": "edge"}], "reason": "x"})
    assert answer["error"] == "unknown_key" and "Hermes config" in answer["note"]


def test_get_without_a_device_says_so(tool_home):
    assert _call({"action": "get"})["error"] == "no_device"


def test_cancel_without_ids_says_what_it_needs(tool_home):
    _sync(SettingsStore(tool_home))
    assert _call({"action": "cancel"})["error"] == "no_ids"


def test_a_name_two_devices_share_is_refused(tool_home):
    store = SettingsStore(tool_home)
    _sync(store)
    _sync(store, installation=IPAD)
    answer = _call({"action": "get", "device": "Eric's iPhone"})
    assert answer["error"] == "unknown_device"
    answer = _call({"action": "get", "device": IPAD})
    assert answer["device"]["id"] == IPAD
    # The others are every device but the one shown.
    assert [entry["id"] for entry in answer["other_devices"]] == [PHONE]


def test_a_subagent_reads_settings_but_cannot_change_them(tool_home):
    _sync(SettingsStore(tool_home))
    child = lambda session: session == "child"  # noqa: E731
    answer = json.loads(tool.handle({"action": "change", "reason": "Calmer.",
                                     "changes": [{"key": "voice.live.tone", "value": "relaxed", "from": "default"}]},
                                    session_id="child", is_child=child))
    assert answer["error"] == "subagent"
    assert json.loads(tool.handle({"action": "get"}, session_id="child", is_child=child))["ok"] is True
    assert SettingsStore(tool_home).view()["pending"] == []


def test_line_breaks_in_text_become_spaces():
    assert store_module._line("a\nb\tc\u00a0d", 40) == "a b c d"


def test_a_bad_action_is_refused(tool_home):
    assert _call({"action": "delete"})["ok"] is False


# --- Route ---------------------------------------------------------------------


@pytest.fixture()
def route_home(tmp_path, monkeypatch):
    monkeypatch.setattr(api, "_calls_home", lambda: tmp_path)
    monkeypatch.setattr(api, "_pairing_state_path", lambda: tmp_path / "conduit-push.json")
    monkeypatch.setattr(api, "_profile_scope", lambda profile: contextlib.nullcontext())
    api._conduit_settings_limiter._mints.clear()
    (tmp_path / "conduit-push.json").write_text(json.dumps({"credential": "c", "installation_id": "i", "gateway_id": "g"}))
    return tmp_path


def _http():
    app = FastAPI()
    app.include_router(api.router, prefix=BASE)
    return TestClient(app)


def test_the_route_syncs_and_returns_waiting_requests(route_home):
    http = _http()
    body = {"installation_id": PHONE, "device": "iPhone", "mode": "ask", "settings": _settings()}
    response = http.put(f"{BASE}/conduit-settings", json=body)
    assert response.status_code == 200 and response.headers["cache-control"] == "no-store"
    assert response.json() == {"ok": True, "pending": []}
    SettingsStore(route_home).request([{"key": "voice.live.tone", "value": "relaxed"}], "x")
    assert http.put(f"{BASE}/conduit-settings", json=body).json()["pending"][0]["value"] == "relaxed"


def test_the_route_refuses_a_malformed_upload(route_home):
    response = _http().put(f"{BASE}/conduit-settings", json={"installation_id": PHONE, "mode": "maybe", "settings": []})
    assert response.status_code == 400


def test_the_route_needs_a_paired_profile(route_home):
    (route_home / "conduit-push.json").unlink()
    body = {"installation_id": PHONE, "mode": "ask", "settings": _settings()}
    assert _http().put(f"{BASE}/conduit-settings", json=body).status_code == 409


def test_the_route_is_a_reported_capability():
    assert "conduit-settings" in api.ROUTE_CAPABILITIES
