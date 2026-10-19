"""Hermes session keys travel whole.

Gateway session keys (``build_session_key``) carry the platform's own ids:
Matrix rooms and users (``!room:server``, ``@user:server``), WhatsApp group
JIDs (``…@g.us``), a profile named ``main`` (``agent:main~``), and custom API
ids up to 256 characters. The plugin passes them on unchanged; only a key with
a control character or over ``SESSION_KEY_MAX_BYTES`` is dropped, never cut.
"""

import importlib.util
import pathlib
import sys

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from events import SESSION_KEY_MAX_BYTES, approval_decision, push_event, sanitize_call, sanitize_decision  # noqa: E402


def _load(name, path):
    if name in sys.modules:
        return sys.modules[name]
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


calls_store = _load("conduit_calls_store_session_keys", ROOT / "calls_store.py")
present_store = _load("conduit_present_store_session_keys", ROOT / "present_store.py")
settings_store = _load("conduit_settings_store_session_keys", ROOT / "settings_store.py")

GATEWAY_KEY_NAMES = ["matrix-group", "whatsapp-group", "profile-main", "signal-dm", "matrix-non-ascii", "api-230"]
GATEWAY_KEYS = [
    "agent:main:matrix:group:!AbCdEf:matrix.org:@bob:matrix.org",
    "agent:main:whatsapp:group:120363012345678901@g.us:15551234567",
    "agent:main~:telegram:dm:12345",
    "agent:main:signal:dm:+15551234567",
    "agent:main:matrix:dm:@zoë:matrix.org",
    # Over the old 180 cut, under Hermes' 256-character custom id cap.
    "agent:main:api:dm:" + "k" * 230,
]
NOT_KEY_NAMES = ["empty", "over-bytes", "over-bytes-utf8", "newline", "nul", "del", "lone-surrogate"]
NOT_KEYS = [
    "",
    "k" * (SESSION_KEY_MAX_BYTES + 1),
    "é" * (SESSION_KEY_MAX_BYTES // 2 + 1),
    "agent:main\n:x",
    "a\x00b",
    "a\x7fb",
    "\ud800",
]


class Clock:
    def __init__(self):
        self.now = 1_000_000.0

    def __call__(self):
        return self.now


@pytest.mark.parametrize("key", GATEWAY_KEYS, ids=GATEWAY_KEY_NAMES)
def test_a_push_keeps_a_gateway_session_key_whole(key):
    event = push_event("approval.needed", identifier="approval:12345678", session_id=key,
                       decision=approval_decision(session_key=key, description="Run rm -rf build?"))
    assert event["session_id"] == key
    assert event["decision"]["session_key"] == key


@pytest.mark.parametrize("key", NOT_KEYS, ids=NOT_KEY_NAMES)
def test_a_push_drops_a_session_key_it_cannot_carry_instead_of_cutting_it(key):
    event = push_event("approval.needed", identifier="approval:12345678", session_id=key,
                       decision=approval_decision(session_key=key, description="Run rm -rf build?"))
    assert "session_id" not in event
    assert "decision" not in event
    assert sanitize_decision({"kind": "approval", "session_key": key, "description": "d", "choices": ["once"]}) == {}


def test_the_byte_limit_is_inclusive():
    key = "k" * SESSION_KEY_MAX_BYTES
    assert push_event("response.ready", identifier="response:12345678", session_id=key)["session_id"] == key


def test_a_call_carries_gateway_session_keys_whole():
    call = sanitize_call({"id": "0123456789abcdef01234567", "kind": "approval", "session_ids": GATEWAY_KEYS[:4]})
    assert call["session_ids"] == GATEWAY_KEYS[:4]
    for key in NOT_KEYS:
        assert sanitize_call({"id": "0123456789abcdef01234567", "kind": "done", "session_ids": ["st-1", key]}) is None


@pytest.mark.parametrize("key", GATEWAY_KEYS, ids=GATEWAY_KEY_NAMES)
def test_an_unanswered_approval_in_a_gateway_chat_still_calls(tmp_path, key):
    clock = Clock()
    store = calls_store.CallStore(tmp_path, clock=clock)
    store.update_settings({"enabled": True, "alerts": True})
    assert store.add_alert(key, "approval", "Run rm -rf build?")
    clock.now += calls_store.ALERT_DELAY_S + 1
    [due] = store.fire_due()
    assert (due["status"], due["session_id"]) == ("call", key)
    # The call event the plugin sends for it carries the same key.
    assert sanitize_call({"id": due["watch"]["id"], "kind": "approval", "session_ids": due["watch"]["session_ids"]})["session_ids"] == [key]


def test_the_call_store_refuses_a_key_it_cannot_carry(tmp_path):
    store = calls_store.CallStore(tmp_path, clock=Clock())
    store.update_settings({"enabled": True, "alerts": True})
    for key in NOT_KEYS:
        with pytest.raises(ValueError):
            store.add_alert(key, "approval", "Run rm -rf build?")


@pytest.mark.parametrize("key", GATEWAY_KEYS, ids=GATEWAY_KEY_NAMES)
def test_a_confirmation_and_a_card_keep_the_session_key_whole(tmp_path, key):
    store = present_store.PresentStore(tmp_path, clock=Clock())
    store.exchange({"installation_id": "phone-installation-1", "device": "iPhone", "app_version": "2.0", "results": []})
    store.confirm("Send to Mei?", "Running late", "Send", spoken_yes=["send it"], session_id=key)
    [shown] = store.exchange({"installation_id": "phone-installation-1", "device": "iPhone", "app_version": "2.0",
                              "results": []})["prompts"]
    assert shown["session_id"] == key


def test_a_confirmation_drops_a_session_key_it_cannot_carry(tmp_path):
    store = present_store.PresentStore(tmp_path, clock=Clock())
    store.exchange({"installation_id": "phone-installation-1", "device": "iPhone", "app_version": "2.0", "results": []})
    for key in NOT_KEYS:
        store.confirm("Send to Mei?", "Running late", "Send", spoken_yes=["send it"], session_id=key)
    prompts = store.exchange({"installation_id": "phone-installation-1", "device": "iPhone", "app_version": "2.0",
                              "results": []})["prompts"]
    assert prompts and all(prompt["session_id"] == "" for prompt in prompts)


@pytest.mark.parametrize("key", GATEWAY_KEYS, ids=GATEWAY_KEY_NAMES)
def test_a_settings_request_keeps_the_session_key_whole(tmp_path, key):
    store = settings_store.SettingsStore(tmp_path, clock=Clock())
    store.sync({"installation_id": "phone-installation-1", "device": "iPhone", "app_version": "1.0", "mode": "ask", "results": [],
                "settings": [{"key": "voice.live.tone", "title": "Tone", "kind": "choice", "tier": "small",
                              "value": "default", "choices": [{"value": "default", "title": "Default"},
                                                              {"value": "relaxed", "title": "Relaxed"}]}]})
    [request] = store.request([{"key": "voice.live.tone", "value": "relaxed"}], "Calmer calls.",
                              session_id=key)["requests"]
    assert request["session_id"] == key


@pytest.mark.parametrize("key", [*GATEWAY_KEYS, *NOT_KEYS, None, 7],
                         ids=[*GATEWAY_KEY_NAMES, *NOT_KEY_NAMES, "none", "number"])
def test_every_module_applies_the_same_session_key_rule(key):
    # The stores are loaded by path by the dashboard and import nothing from
    # the plugin package, so each keeps its own copy of the rule.
    import events

    expected = events.session_key(key)
    assert calls_store._session_key(key) == expected
    assert present_store._session_key(key) == expected
    assert settings_store._session_key(key) == expected
    assert calls_store.SESSION_KEY_MAX_BYTES == present_store.SESSION_KEY_MAX_BYTES \
        == settings_store.SESSION_KEY_MAX_BYTES == SESSION_KEY_MAX_BYTES
