"""Image generation models: the plugin's /media-models lists Hermes' catalog
for the profile's backend and saves a pick where that backend reads it.

Hermes' own pick (``PUT /api/tools/toolsets/image_gen/model``, and ``hermes
tools``) writes ``image_gen.model`` alone, which xAI's image backend never
reads. These check that a pick through the plugin reaches the backend: the
model its own resolution returns, in the profile named.

The file's name sorts after the modules that start a dashboard of their own
(test_bot_work, test_compaction_skip, test_cron_bot_chat): Hermes 0.23.4
refuses a second dashboard while the shared one here is running."""

from __future__ import annotations

import json
import os
import subprocess
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

import yaml

from conftest import TOKEN, _hermes_bin


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
        with urllib.request.urlopen(req, timeout=60) as resp:
            return resp.status, json.loads(resp.read() or b"null")
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read().decode(errors="replace")


def _file(home):
    return yaml.safe_load((home / "config.yaml").read_text()) or {}


def _pick_backend(dashboard, profile, provider_row):
    status, body = _hermes(dashboard, "PUT", "/api/tools/toolsets/image_gen/provider",
                           body={"provider": provider_row}, profile=profile)
    assert status == 200, body


def _row(dashboard, profile, plugin):
    status, config = _hermes(dashboard, "GET", "/api/tools/toolsets/image_gen/config", profile=profile)
    assert status == 200, config
    for row in config["providers"]:
        if plugin.lower() in row["name"].lower():
            return row["name"]
    raise AssertionError(f"no {plugin} row in {[row['name'] for row in config['providers']]}")


def _resolved(home, module, function="_resolve_model"):
    """The model the backend's own resolution picks in ``home``, run as Hermes runs it."""
    code = (f"from plugins.image_gen.{module} import {function} as r; m = r(); "
            "print(m[0] if isinstance(m, tuple) else m)")
    env = {**os.environ, "HERMES_HOME": str(home)}
    env.pop("XAI_IMAGE_MODEL", None)
    checkout = Path(_hermes_bin()).parents[2]
    out = subprocess.run([str(checkout / ".venv" / "bin" / "python"), "-c", code], cwd=checkout, env=env,
                         capture_output=True, text=True, timeout=120)
    assert out.returncode == 0, out.stderr
    return out.stdout.strip().splitlines()[-1]


def test_an_xai_pick_reaches_xais_backend_in_the_profile_named(dashboard):
    second = dashboard.home / "profiles" / "second"
    _pick_backend(dashboard, "second", _row(dashboard, "second", "xai"))
    status, listed = dashboard.request("GET", "media-models/image_gen", profile="second")
    assert status == 200, listed
    assert listed["plugin"] == "xai" and listed["has_models"], listed
    other = next(m["id"] for m in listed["models"] if m["id"] != listed["current"])

    status, body = dashboard.request("PUT", "media-models/image_gen", body={"model": other}, profile="second")
    assert status == 200, body
    assert body["plugin"] == "xai" and body["model"] == other
    saved = _file(second)["image_gen"]
    assert saved["model"] == other and saved["xai"]["model"] == other
    assert "xai" not in (_file(dashboard.home).get("image_gen") or {}), "The launch profile kept its own"
    assert _resolved(second, "xai") == other

    status, listed = dashboard.request("GET", "media-models/image_gen", profile="second")
    assert status == 200 and listed["current"] == other, listed


def test_the_listing_matches_hermes_own_catalog(dashboard):
    _pick_backend(dashboard, "second", _row(dashboard, "second", "openai"))
    status, ours = dashboard.request("GET", "media-models/image_gen", profile="second")
    assert status == 200, ours
    status, theirs = _hermes(dashboard, "GET", "/api/tools/toolsets/image_gen/models", profile="second")
    assert status == 200, theirs
    assert [m["id"] for m in ours["models"]] == [m["id"] for m in theirs["models"]]
    assert ours["default"] == theirs["default"] and ours["plugin"] == theirs["plugin"]

    status, body = dashboard.request("PUT", "media-models/image_gen", body={"model": "not-a-model"},
                                     profile="second")
    assert status == 400, body
