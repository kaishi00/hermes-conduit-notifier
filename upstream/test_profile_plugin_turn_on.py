"""Turning the plugin on for a fresh bot profile (hermes-conduit#2803).

A profile made without --clone (Conduit's New Bot without Start From) hasn't
turned the plugin on, so Hermes answers its plugin requests with "Plugin not
found". POST /profiles/plugin on the dashboard's own profile links it to the
main install and runs Hermes' own `hermes -p <name> plugins enable`; after
that Hermes serves the plugin's routes for it, with no restart.
"""

from __future__ import annotations

import os
import subprocess

import yaml

from conftest import PLUGIN, _hermes_bin


def test_turn_on_makes_a_fresh_profile_serve_the_plugin(dashboard):
    env = {**os.environ, "HERMES_HOME": str(dashboard.home), "NO_COLOR": "1"}
    subprocess.run([_hermes_bin(), "profile", "create", "fresh-bot"], env=env, check=True, capture_output=True,
                   timeout=120)
    home = dashboard.home / "profiles" / "fresh-bot"

    status, body = dashboard.get("capabilities", profile="fresh-bot")
    assert status == 404, body
    status, body = dashboard.get("profiles/plugin", name="fresh-bot")
    assert status == 200, body
    assert body["on"] is False and body["can_turn_on"] is True, body

    status, body = dashboard.request("POST", "profiles/plugin", body={"name": "fresh-bot"})
    assert status == 200, body
    assert body["on"] is True, body

    # Hermes' own enable wrote the profile's allow-list, and its chats load the main install.
    config = yaml.safe_load((home / "config.yaml").read_text()) or {}
    assert PLUGIN in ((config.get("plugins") or {}).get("enabled") or []), config
    assert (home / "plugins" / PLUGIN).resolve() == (dashboard.home / "plugins" / PLUGIN).resolve()
    # The landing check: the profile's own plugin request is served now, by this plugin.
    status, body = dashboard.get("capabilities", profile="fresh-bot")
    assert status == 200, body
    assert "bot-plugin" in str(body), body


def test_turn_on_refuses_a_profile_that_doesnt_exist(dashboard):
    status, body = dashboard.request("POST", "profiles/plugin", body={"name": "nobody-here"})
    assert status == 404, body
