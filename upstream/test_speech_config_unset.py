"""Clearing a Hermes speech setting (hermes-conduit issue 3155): Hermes'
PUT /api/config merges what it's sent over the file, so a key a save leaves
out stays; the plugin's /config/unset takes it out of the profile named.

The file's name sorts after the modules that start a dashboard of their own
(test_bot_work, test_compaction_skip, test_cron_bot_chat): Hermes 0.23.4
refuses a second dashboard while the shared one here is running, and that one
starts with the first test that asks for it."""

from __future__ import annotations

import json
import urllib.error
import urllib.parse
import urllib.request

import yaml

from conftest import TOKEN


def _hermes(dashboard, method, path, body=None, **query):
    qs = f"?{urllib.parse.urlencode(query)}" if query else ""
    headers = {"X-Hermes-Session-Token": TOKEN}
    data = None
    if body is not None:
        data = json.dumps(body).encode()
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(f"http://127.0.0.1:{dashboard.port}{path}{qs}", data=data, method=method,
                                 headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return resp.status, json.loads(resp.read() or b"null")
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read().decode(errors="replace")


def _file(home):
    return yaml.safe_load((home / "config.yaml").read_text()) or {}


def _save(dashboard, profile, changes):
    """A Conduit save: the whole config read, changed and put back."""
    query = {"profile": profile} if profile else {}
    status, config = _hermes(dashboard, "GET", "/api/config", **query)
    assert status == 200, config
    for section, values in changes.items():
        config.setdefault(section, {}).update(values)
    status, body = _hermes(dashboard, "PUT", "/api/config", body={"config": config}, **query)
    assert status == 200, body


def _unset(dashboard, profile, keys):
    query = {"profile": profile} if profile else {}
    return dashboard.request("POST", "config/unset", body={"keys": keys}, **query)


def test_a_cleared_key_leaves_the_profile_named_and_the_rest_stays(dashboard):
    second = dashboard.home / "profiles" / "second"
    for profile in ("second", None):
        _save(dashboard, profile, {"tts": {"provider": "openai", "use_gateway": True,
                                           "openai": {"voice": "nova", "model": "tts-1-hd"}}})
    assert _file(second)["tts"]["openai"] == {"voice": "nova", "model": "tts-1-hd"}
    before = _file(second)

    status, body = _unset(dashboard, "second", ["tts.openai.voice", "tts.use_gateway", "tts.openai.speed"])
    assert status == 200, body
    assert body == {"ok": True, "removed": ["tts.openai.voice", "tts.use_gateway"]}

    after = _file(second)
    assert after["tts"] == {"provider": "openai", "openai": {"model": "tts-1-hd"}}
    assert {k: v for k, v in after.items() if k != "tts"} == {k: v for k, v in before.items() if k != "tts"}
    launch = _file(dashboard.home)["tts"]
    assert launch["openai"]["voice"] == "nova" and launch["use_gateway"] is True, "The launch profile kept its own"
    # Hermes answers with its default again, not the cleared value.
    status, config = _hermes(dashboard, "GET", "/api/config", profile="second")
    assert status == 200 and config["tts"]["openai"].get("voice") != "nova"

    status, body = _unset(dashboard, None, ["tts.openai.voice", "tts.openai.model", "tts.use_gateway"])
    assert status == 200 and len(body["removed"]) == 3, body
    assert _file(dashboard.home)["tts"] == {"provider": "openai"}, "A section left empty goes too"


def test_a_key_outside_speech_is_refused(dashboard):
    before = _file(dashboard.home)
    status, body = _unset(dashboard, None, ["model.default"])
    assert status == 400, body
    assert _file(dashboard.home) == before
