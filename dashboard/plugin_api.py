"""Conduit routes on the Hermes dashboard, mounted at /api/plugins/conduit_push/.

Hermes loads this file by path (manifest.json's "api"). The routes and the
tools behind them are in dashboard/api/, one module per feature, in the order
they run:

    core                  What three or more features share: errors, rate
                          limits, request bodies, the profile a request acts
                          for, and the plugin's own files
    gemini_qwen_live      Gemini Live and Qwen Live tokens, and the web search
                          their lookups use
    memory                Memory and Personality: what this host's Hermes
                          remembers, and the profile's SOUL.md
    gpt_live              GPT-Live on a ChatGPT/Codex subscription, or on this
                          host's OpenAI key
    grok_live             Grok Live: Conduit's realtime socket relayed to xAI
    speech_server         Your own speech server: its settings, its test and
                          its relay
    local_live            Local Live: a live call through the user's own voice
                          server
    local_live_read_back  Local Live on a read-back speech server
    local_live_agent      Local Live agent mode: a fast front model in front of
                          Hermes
    voice_calls           Voice transcripts and call end: a live call's turns
                          saved into its chat
    chats                 Chat takeover, where in Conduit a message was sent,
                          and predictions
    history               Chat history pages without the rows Conduit already
                          has
    pairing               End-to-end encrypted notifications and pairing from
                          Conduit
    notifications         Cron and reply notifications, the chat Conduit has on
                          screen, and confirm prompts
    hermes_calls          Hermes calls you
    settings              Conduit settings Hermes may change, and preferences
                          synced across devices
    linked_rooms          Linked Rooms: your agent's choices in each room,
                          links, invites, drafts, approvals and room setup
    link_asks             Asks between linked agents: each Link's rules and
                          the asks waiting for their owner
    pulse                 Pulse settings
    phone_data            What Conduit shares: health data and the voice tools
                          that read it, location, the Shortcuts inbox,
                          calendar, reminders, Focus and phone status
    watch_tools           Watch tools (wrist-down lookups for a Watch call) and
                          Watch chats
    watch_jobs            Watch jobs through the relay
    watch_audio           Watch audio: GPT-Live and Grok on a Watch call, the
                          helper's Python, the sessions and the bridge
    watch_local_live      Local Live on the Watch
    link_status           Link status: the dashboard's Conduit card
    desktop_views         Desktop views: the chats Hermes Desktop or the web
                          dashboard has open
    rooms                 Room files, room pictures and the room planner
    workspace             Artifacts and workspace files
    workstreams           Bot workstreams: the Work panel, and Reply, Stop,
                          Continue and Check first on a workstream
    capabilities          Capabilities: the features this plugin serves, as
                          Conduit reads them
    self_update           Self restart and self update

A module takes what it uses from another by module (``core.TokenError``), never
by name, so a test that replaces ``plugin_api.core._calls_home`` replaces it
for every module. CONTRIBUTING.md has the rest of the layout's rules.

Routes sit behind the dashboard's own auth, the same as /api/audio/*.
The API key is never returned, logged, or written anywhere.
"""

from __future__ import annotations

import importlib.util
import os
import sys


def _load_api():
    """dashboard/api/ as the package ``<this module>.api``. Hermes loads this
    file as a plain module, outside any package, so the package is loaded by
    path too. Loading it again (a test, or Hermes loading the plugin anew)
    gets fresh modules."""
    name = f"{__name__}.api"

    def forget() -> None:
        for key in [key for key in sys.modules if key == name or key.startswith(f"{name}.")]:
            del sys.modules[key]

    forget()
    folder = os.path.join(os.path.dirname(os.path.abspath(__file__)), "api")
    spec = importlib.util.spec_from_file_location(name, os.path.join(folder, "__init__.py"),
                                                  submodule_search_locations=[folder])
    package = importlib.util.module_from_spec(spec)
    sys.modules[name] = package
    try:
        spec.loader.exec_module(package)
    except BaseException:
        forget()
        raise
    return package


_api = _load_api()
router = _api.router
# Each module, for the tests and probes: plugin_api.watch_jobs.WATCH_JOB_TOOLS.
for _name in _api.MODULES:
    globals()[_name] = getattr(_api, _name)
