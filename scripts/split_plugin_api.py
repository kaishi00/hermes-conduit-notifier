#!/usr/bin/env python3
"""Splits dashboard/plugin_api.py into one module per feature under dashboard/api/.

A one-time move (notifier#443): the file had grown to 880 KB, more than an
agent or a reviewer reads whole. Every top-level statement moves whole, with
the comments above it, into the module of the section it was in, in the same
order; a name it takes from another module becomes ``module.name``. The file
becomes the index and the loader Hermes keeps loading by path, and the tests
and probes that reach a name through the loaded file get the same rewrite.

Nothing else changes, and the script checks that before it writes: each moved
statement parses to the old one once the rewrites are undone, and every
comment arrives exactly once.

    python3 scripts/split_plugin_api.py analyze       # module sizes, what uses what, the load order
    python3 scripts/split_plugin_api.py write [REF]   # splits the working tree's single file, or REF's
    python3 scripts/split_plugin_api.py check REF     # the split files are what write makes of REF's

A branch still carrying the single file merges main from just before the
split, runs ``write`` on itself and commits, then merges main: only its own
changes conflict. A section or a statement the tables below don't place makes
it stop and say so. Once no branch carries the single file, this script can go.
"""

from __future__ import annotations

import argparse
import ast
import builtins
import collections
import difflib
import io
import re
import subprocess
import sys
import textwrap
import tokenize
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Set, Tuple

ROOT = Path(__file__).resolve().parents[1]
PLUGIN_API = "dashboard/plugin_api.py"
API_DIR = "dashboard/api"
REWRITTEN_DIRS = ("tests", "probes")

HEADER = re.compile(r"^# --- (?P<title>.+?) -{3,}\s*$")

# What the split makes, not the list a module is added to afterwards (that's
# dashboard/api/__init__.py's MODULES, and plugin_api.py's index). The modules
# in the order they run, which is also the order their routes are tried, and
# what each holds: plugin_api.py's index, and the first line of each module's
# docstring. A module's top-level statements may use only the modules above it
# while it loads (``analyze`` says when one doesn't).
MODULES: List[Tuple[str, str]] = [
    ("core", "What three or more features share: errors, rate limits, request bodies, the profile a request "
             "acts for, and the plugin's own files"),
    ("gemini_qwen_live", "Gemini Live and Qwen Live tokens, and the web search their lookups use"),
    ("memory", "Memory and Personality: what this host's Hermes remembers, and the profile's SOUL.md"),
    ("gpt_live", "GPT-Live on a ChatGPT/Codex subscription, or on this host's OpenAI key"),
    ("grok_live", "Grok Live: Conduit's realtime socket relayed to xAI"),
    ("speech_server", "Your own speech server: its settings, its test and its relay"),
    ("local_live", "Local Live: a live call through the user's own voice server"),
    ("local_live_read_back", "Local Live on a read-back speech server"),
    ("local_live_agent", "Local Live agent mode: a fast front model in front of Hermes"),
    ("voice_calls", "Voice transcripts and call end: a live call's turns saved into its chat"),
    ("chats", "Chat takeover, where in Conduit a message was sent, and predictions"),
    ("pairing", "End-to-end encrypted notifications and pairing from Conduit"),
    ("notifications", "Cron and reply notifications, the chat Conduit has on screen, and confirm prompts"),
    ("hermes_calls", "Hermes calls you"),
    ("settings", "Conduit settings Hermes may change, and preferences synced across devices"),
    ("linked_rooms", "Linked Rooms: your agent's choices in each room, links, invites, drafts, approvals "
                     "and room setup"),
    ("pulse", "Pulse settings"),
    ("phone_data", "What Conduit shares: health data and the voice tools that read it, location, the "
                   "Shortcuts inbox, calendar, reminders, Focus and phone status"),
    ("watch_tools", "Watch tools (wrist-down lookups for a Watch call) and Watch chats"),
    ("watch_jobs", "Watch jobs through the relay"),
    ("watch_audio", "Watch audio: GPT-Live and Grok on a Watch call, the helper's Python, the sessions "
                    "and the bridge"),
    ("watch_local_live", "Local Live on the Watch"),
    ("link_status", "Link status: the dashboard's Conduit card"),
    ("desktop_views", "Desktop views: the chats Hermes Desktop or the web dashboard has open"),
    ("rooms", "Room files, room pictures and the room planner"),
    ("workspace", "Artifacts and workspace files"),
    ("capabilities", "Capabilities: the features this plugin serves, as Conduit reads them"),
    ("self_update", "Self restart and self update"),
]
MODULE_NAMES = [name for name, _ in MODULES]

# Each section, by the start of its title, and the module it moves to. The
# lines before the first section (the Gemini Live and Qwen Live tokens and the
# web search) are "".
SECTIONS: List[Tuple[str, str]] = [
    ("", "gemini_qwen_live"),
    ("Profiles this dashboard serves itself", "gemini_qwen_live"),
    ("Memory", "memory"),
    ("Personality", "memory"),
    ("GPT-Live on a ChatGPT/Codex subscription", "gpt_live"),
    ("Grok Live", "grok_live"),
    ("Your own speech server", "speech_server"),
    ("Local Live: a live call through the user's own voice server", "local_live"),
    ("Local Live on a read-back speech server", "local_live_read_back"),
    ("Local Live agent mode", "local_live_agent"),
    ("Voice transcripts", "voice_calls"),
    ("Call end", "voice_calls"),
    ("Chat takeover", "chats"),
    ("Where in Conduit a message was sent", "chats"),
    ("Predictions", "chats"),
    ("End-to-end encrypted notifications", "pairing"),
    ("Pairing from Conduit", "pairing"),
    ("Cron notifications", "notifications"),
    ("Reply notifications by source", "notifications"),
    ("A chat Conduit has on screen", "notifications"),
    ("Hermes calls you", "hermes_calls"),
    ("Conduit settings Hermes may change", "settings"),
    ("Conduit preferences synced across devices", "settings"),
    ("Linked Rooms: your agent's choices in each room", "linked_rooms"),
    ("Pulse settings", "pulse"),
    ("Confirm prompts Hermes shows on the phone", "notifications"),
    ("Health data", "phone_data"),
    ("Voice tools", "phone_data"),
    ("Location", "phone_data"),
    ("Shortcuts inbox", "phone_data"),
    ("Calendar, Reminders, Focus and phone status", "phone_data"),
    ("Watch tools", "watch_tools"),
    ("Watch chats", "watch_tools"),
    ("Watch jobs through the relay", "watch_jobs"),
    ("Watch audio: GPT-Live and Grok on a Conduit Watch call", "watch_audio"),
    ("The helper's Python", "watch_audio"),
    ("Sessions", "watch_audio"),
    ("Local Live on the Watch", "watch_local_live"),
    ("The bridge", "watch_audio"),
    ("Desktop views", "desktop_views"),
    ("Link status", "link_status"),
    ("Room files", "rooms"),
    ("Room pictures", "rooms"),
    ("Room planner", "rooms"),
    ("Artifacts", "workspace"),
    ("Workspace files", "workspace"),
    ("Capabilities", "capabilities"),
    ("Self restart", "self_update"),
    ("Self update", "self_update"),
]

# Statements that sit in another section than the feature they belong to,
# by the name they define (or, for a bare statement, how its text starts).
MOVES: Dict[str, str] = {
    "_note_conduit_request": "core",
    "_desktop_view_store": "desktop_views",
    "_desktop_view_hook": "desktop_views",
    "_desktop_views_limiter": "desktop_views",
    "atexit.register(_desktop_view_store.flush)": "desktop_views",
    'if "hermes_cli.web_server" in sys.modules:\n    # Loaded by the dashboard (not a test or the CLI): start watching':
        "desktop_views",
    "get_desktop_views": "desktop_views",
}

# The router, and helpers three or more features use with what they need:
# api/core.py. It runs first, so any module can use it while it loads.
CORE: Set[str] = {
    "_note_conduit_request", "router",
    "TokenError", "_MintLimiter", "LIMITER_MAX_KEYS", "_limiter_key", "_profile_scope",
    "CONDUIT_PROFILE_PARAM", "CONDUIT_PROFILE_TICKET_REASON", "_conduit_route_profile",
    "_conduit_route_profile_no_store", "_plugin_refused_on", "_Scoped", "_run_scoped", "_limited",
    "_scoped_acquire", "_unexpected", "_read_json_body", "SEARCH_MAX_BODY_BYTES",
    "_calls_home", "_calls_store_lock", "_load_pairing_state", "_pairing_state_path",
    "DASHBOARD_PACKAGE", "_plugin_package_module", "PLUGIN_NAME",
    "_native_ticket_home", "_job_profile_on_home", "_NO_PRINCIPAL", "_profile_dir", "_job_profile_home_guess",
    "logger",
}

# Names every module takes from core under the same name, so the code using
# them doesn't change: ``logger = core.logger``, ``router = core.router`` (one
# router, which gets each module's routes in the order the modules run).
SHARED = ("logger", "router")
# The logger is defined anew at the top of core, under the name it had.
LOGGER = "logger"
LOGGER_LINE = "logger = logging.getLogger(__name__)"

# The plugin's folders, which the code named from where plugin_api.py sits.
PLUGIN_ROOT_EXPR = "os.path.dirname(os.path.dirname(os.path.abspath(__file__)))"
DASHBOARD_DIR_EXPR = "os.path.dirname(os.path.abspath(__file__))"
CORE_PREAMBLE = '''\
# The plugin's logger, under the name it always had (the module Hermes loads,
# plugin_api.py): every module logs through it.
logger = logging.getLogger(__package__.rpartition(".")[0] or __name__)


def _dashboard_dir() -> str:
    """dashboard/ (this file is dashboard/api/core.py)."""
    return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _plugin_root() -> str:
    """The plugin's folder, which dashboard/ is in."""
    return os.path.dirname(_dashboard_dir())
'''
CORE_DEFINED = ("_dashboard_dir", "_plugin_root")

# Section header blocks that move to another module than their section's.
SECTION_HEADERS: Dict[str, str] = {"Profiles this dashboard serves itself": "core"}
# core's statements come from many sections: a header says what each run of
# them is, above the statement that starts it (by the name it defines).
CORE_HEADERS: Dict[str, str] = {
    "_note_conduit_request": "The router, which every module adds its routes to",
    "LIMITER_MAX_KEYS": "Errors, rate limits and a profile's scope",
    "_pairing_state_path": "The pairing state",
    "_calls_store_lock": "The calls store's home",
    "DASHBOARD_PACKAGE": "The plugin's own files, imported as a package",
    "_NO_PRINCIPAL": "The profile a request acts for",
    "PLUGIN_NAME": "The plugin's name",
}

# The module docstring's paragraphs, by how they start: the module they
# describe, or plugin_api.py.
DOC_PARAGRAPHS: List[Tuple[str, str]] = [
    ("Conduit routes on the Hermes dashboard", "plugin_api"),
    ("Gemini Live:", "gemini_qwen_live"),
    ("Web search:", "gemini_qwen_live"),
    ("Memory:", "memory"),
    ("Personality:", "memory"),
    ("GPT-Live:", "gpt_live"),
    ("Qwen Live:", "gemini_qwen_live"),
    ("Grok Live:", "grok_live"),
    ("Watch tools:", "watch_tools"),
    ("Routes sit behind the dashboard's own auth", "plugin_api"),
]

# Comments and docstrings the move makes wrong: (old, new, how many there are).
TEXT_FIXES: List[Tuple[str, str, int]] = [
    ('    being seen, for the dashboard\'s Conduit card (see "Link status" below).\n'
     "    Defined before the router, which takes it.",
     '    being seen, for the dashboard\'s Conduit card (see "Link status" in\n'
     "    api/link_status.py). Defined before the router, which takes it.", 1),
    ("The version in plugin.yaml beside this folder,", "The version in the plugin's plugin.yaml,", 1),
    ("beside this folder", "in the plugin's folder", 29),
    ("# The speech server above can also run whole live calls", "# Your own speech server can also run whole live calls", 1),
    ("jobs too (Watch jobs, below)", "jobs too (watch_jobs.py)", 1),
    ("Local Live (Watch audio, below)", "Local Live (watch_audio.py)", 1),
    ("run here: Local Live on the Watch, below)", "run here: watch_local_live.py)", 1),
]

# The same in tests and probes, made before their rewrite: (path, old, new).
TEST_TEXT_FIXES: List[Tuple[str, str, str]] = [
    # A worker script in a string, run in another process.
    ("tests/test_local_live.py", "    api.secrets = types.SimpleNamespace(token_urlsafe=slow)\n"
                                 "    print(api.local_live_key(None))\n",
     "    api.local_live.secrets = types.SimpleNamespace(token_urlsafe=slow)\n"
     "    print(api.local_live.local_live_key(None))\n"),
    # A stand-in for the loaded file, laid out as its modules are.
    ("tests/test_grok_watch_token_probe.py",
     "    api = types.SimpleNamespace(\n"
     "        _profile_scope=lambda profile: contextlib.nullcontext(),\n"
     "        grok_live_credentials=lambda: (\"bearer\", \"test sign-in\"),\n"
     "        grok_live_model_voice=lambda: (\"grok-test\", \"Ara\"),\n"
     "        TokenError=TokenError,\n"
     "    )\n",
     "    api = types.SimpleNamespace(\n"
     "        core=types.SimpleNamespace(_profile_scope=lambda profile: contextlib.nullcontext(), TokenError=TokenError),\n"
     "        grok_live=types.SimpleNamespace(grok_live_credentials=lambda: (\"bearer\", \"test sign-in\"),\n"
     "                                        grok_live_model_voice=lambda: (\"grok-test\", \"Ara\")),\n"
     "    )\n"),
    # A probe's check of the names it needs, from more than one module: each names its module.
    ("probes/gpt_live_bridge_probe.py",
     '    missing = [name for name in ("_profile_scope", "create_gpt_live_session", "gpt_live_status", "TokenError")\n'
     '               if not hasattr(module, name)]\n',
     '    missing = [f"{part}.{name}" for part, name in (("core", "_profile_scope"), ("core", "TokenError"),\n'
     '                                                   ("gpt_live", "create_gpt_live_session"),\n'
     '                                                   ("gpt_live", "gpt_live_status"))\n'
     '               if not hasattr(getattr(module, part), name)]\n'),
    ("probes/grok_watch_token_probe.py",
     '    missing = [name for name in ("_profile_scope", "grok_live_credentials", "grok_live_model_voice", "TokenError")\n'
     '               if not hasattr(module, name)]\n',
     '    missing = [f"{part}.{name}" for part, name in (("core", "_profile_scope"), ("core", "TokenError"),\n'
     '                                                   ("grok_live", "grok_live_credentials"),\n'
     '                                                   ("grok_live", "grok_live_model_voice"))\n'
     '               if not hasattr(getattr(module, part), name)]\n'),
    # Parametrized names from more than one module: each value names its module.
    ("tests/test_watch_jobs.py",
     '@pytest.mark.parametrize("pattern, valid", [\n'
     '    ("_CALL_WATCH_ID", "0" * 24), ("_WATCH_ID", "a" * 22), ("_WATCH_NONCE", "a" * 16),\n'
     '    ("_WATCH_CT", "a" * 22), ("_ROOM_IMAGE_ID", "room-1"),\n'
     '])\n'
     'def test_ids_with_a_trailing_newline_are_refused(pattern, valid):\n'
     '    assert getattr(api, pattern).match(valid)\n'
     '    assert getattr(api, pattern).match(valid + "\\n") is None\n',
     '@pytest.mark.parametrize("module, pattern, valid", [\n'
     '    ("hermes_calls", "_CALL_WATCH_ID", "0" * 24), ("watch_tools", "_WATCH_ID", "a" * 22),\n'
     '    ("watch_tools", "_WATCH_NONCE", "a" * 16), ("watch_tools", "_WATCH_CT", "a" * 22),\n'
     '    ("rooms", "_ROOM_IMAGE_ID", "room-1"),\n'
     '])\n'
     'def test_ids_with_a_trailing_newline_are_refused(module, pattern, valid):\n'
     '    assert getattr(getattr(api, module), pattern).match(valid)\n'
     '    assert getattr(getattr(api, module), pattern).match(valid + "\\n") is None\n'),
    ("tests/test_limiter_scoping.py",
     '@pytest.mark.parametrize("method, path, limiter", [\n'
     '    ("put", "/conduit-settings", "_conduit_settings_limiter"),\n'
     '    ("get", "/cron/notifications", "_cron_limiter"),\n'
     '    ("get", "/calls", "_calls_limiter"),\n'
     '    ("post", "/health/key", "_health_key_limiter"),\n'
     '    ("post", "/phone/key", "_phone_key_limiter"),\n'
     '    ("post", "/location/key", "_location_key_limiter"),\n'
     '    ("get", "/voice-tools", "_voice_tools_limiter"),\n'
     '    ("post", "/e2e", "_e2e_limiter"),\n'
     '    ("post", "/e2e/rotate", "_e2e_limiter"),\n'
     '])\n'
     'def test_an_unknown_profile_gets_no_rate_limit_bucket(client, hermes_scope, monkeypatch, method, path, limiter):\n'
     '    fresh = api._MintLimiter(1000, 60)\n'
     '    monkeypatch.setattr(api, limiter, fresh)\n',
     '@pytest.mark.parametrize("method, path, module, limiter", [\n'
     '    ("put", "/conduit-settings", "settings", "_conduit_settings_limiter"),\n'
     '    ("get", "/cron/notifications", "notifications", "_cron_limiter"),\n'
     '    ("get", "/calls", "hermes_calls", "_calls_limiter"),\n'
     '    ("post", "/health/key", "phone_data", "_health_key_limiter"),\n'
     '    ("post", "/phone/key", "phone_data", "_phone_key_limiter"),\n'
     '    ("post", "/location/key", "phone_data", "_location_key_limiter"),\n'
     '    ("get", "/voice-tools", "phone_data", "_voice_tools_limiter"),\n'
     '    ("post", "/e2e", "pairing", "_e2e_limiter"),\n'
     '    ("post", "/e2e/rotate", "pairing", "_e2e_limiter"),\n'
     '])\n'
     'def test_an_unknown_profile_gets_no_rate_limit_bucket(client, hermes_scope, monkeypatch, method, path, module,\n'
     '                                                      limiter):\n'
     '    fresh = api._MintLimiter(1000, 60)\n'
     '    monkeypatch.setattr(getattr(api, module), limiter, fresh)\n'),
]

# Calls that take the loaded file without reaching into it, or that name it as the
# file does, beyond a loader's exec_module and this file's own functions: (path, call).
PASSED_ON = {
    ("tests/test_watch_audio_relay_e2e.py", "probe.call"),     # probes/watch_audio_client.py's, which name it api
    ("tests/test_watch_audio_relay_e2e.py", "probe.lookup"),
    ("tests/test_voice_sessions.py", "hermes.stores.get"),     # a lookup's default, only tested for truth
}


# --- Reading the single file ---------------------------------------------------

@dataclass
class Chunk:
    """One top-level statement and the comment lines above it."""
    node: ast.stmt
    section: int
    lead: List[str]          # blank and comment lines above the statement
    body: List[str]          # the statement's own lines
    first: int               # 1-based line of the statement's first line (decorators included)
    first_in_section: bool   # the section header's block is right above it
    names: List[str] = field(default_factory=list)
    module: str = ""


@dataclass
class Section:
    title: str
    header: List[str]        # the header line and the comment block under it
    module: str


@dataclass
class Source:
    text: str
    lines: List[str]
    docstring: str
    imports: List[ast.stmt]
    import_groups: List[int]  # per import: 0 for the standard library's, 1 for those after the blank line
    sections: List[Section]
    chunks: List[Chunk]


def _defined_names(node: ast.stmt) -> List[str]:
    """The module-level names a top-level statement binds."""
    if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
        return [node.name]
    if isinstance(node, ast.AugAssign):
        raise SystemExit(f"line {node.lineno}: a top-level augmented assignment; move it by hand")
    if isinstance(node, (ast.Assign, ast.AnnAssign)):
        targets = node.targets if isinstance(node, ast.Assign) else [node.target]
        return [n.id for t in targets for n in ast.walk(t) if isinstance(n, ast.Name)]
    scope = Scope("module", None)
    Resolver(scope)._collect(scope, [node])
    if scope.bound:
        raise SystemExit(f"line {node.lineno}: a {type(node).__name__} binding {sorted(scope.bound)} at the top "
                         f"level; move it by hand")
    return []


def section_module(title: str) -> str:
    for prefix, module in SECTIONS:
        if prefix and title.startswith(prefix):
            return module
    raise SystemExit(f"No module for the section {title!r}: add it to SECTIONS in {Path(__file__).name}")


def _prefixed(table: Dict[str, str], title: str) -> Optional[str]:
    for prefix, value in table.items():
        if (prefix and title.startswith(prefix)) or prefix == title:
            return value
    return None


def parse(text: str) -> Source:
    lines = text.splitlines(keepends=True)
    tree = ast.parse(text)
    body = list(tree.body)
    doc = body.pop(0)
    if not (isinstance(doc, ast.Expr) and isinstance(doc.value, ast.Constant) and isinstance(doc.value.value, str)):
        raise SystemExit("plugin_api.py doesn't start with its docstring")
    if not any(HEADER.match(line) for line in lines):
        raise SystemExit("plugin_api.py has no sections: it's split already")
    imports: List[ast.stmt] = []
    groups: List[int] = []
    group = 0
    previous = doc.end_lineno
    while body and isinstance(body[0], (ast.Import, ast.ImportFrom)):
        node = body.pop(0)
        if imports and any(not l.strip() for l in lines[previous:node.lineno - 1]):
            group += 1
        previous = node.end_lineno
        if isinstance(node, ast.ImportFrom) and node.module == "__future__":
            if [a.name for a in node.names] != ["annotations"]:
                raise SystemExit("a __future__ import other than annotations")
            continue
        if len(node.names) != 1 and isinstance(node, ast.Import):
            raise SystemExit(f"line {node.lineno}: one module per import, please")
        imports.append(node)
        groups.append(group)
    sections = [Section("", [], SECTIONS[0][1])]
    chunks: List[Chunk] = []
    previous_end = imports[-1].end_lineno if imports else doc.end_lineno
    for node in body:
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            raise SystemExit(f"line {node.lineno}: an import below the top; move it up by hand")
        first = min([node.lineno] + [d.lineno for d in getattr(node, "decorator_list", [])])
        gap = lines[previous_end:first - 1]
        lead = gap
        first_in_section = False
        for i, line in enumerate(gap):
            match = HEADER.match(line)
            if match:
                before, rest = gap[:i], gap[i:]
                if any(l.strip() for l in before):
                    raise SystemExit(f"line {previous_end + 1}: comments above a section header; move them by hand")
                end = 1
                while end < len(rest) and rest[end].startswith("#"):
                    end += 1
                sections.append(Section(match.group("title"), rest[:end], section_module(match.group("title"))))
                lead = rest[end:]
                first_in_section = True
                break
        if any(HEADER.match(l) for l in lead):
            raise SystemExit(f"line {first}: two section headers in a row")
        chunks.append(Chunk(node=node, section=len(sections) - 1, lead=lead, body=lines[first - 1:node.end_lineno],
                            first=first, first_in_section=first_in_section, names=_defined_names(node)))
        previous_end = node.end_lineno
    if any(l.strip() for l in lines[previous_end:]):
        raise SystemExit("text after the last statement; move it by hand")
    return Source(text, lines, doc.value.value, imports, groups, sections, chunks)


def assign_modules(source: Source) -> None:
    for chunk in source.chunks:
        if chunk.names:
            in_core = [n for n in chunk.names if n in CORE]
            if in_core and len(in_core) != len(chunk.names):
                raise SystemExit(f"line {chunk.first}: {chunk.names} are only partly in CORE")
            moved = {MOVES[n] for n in chunk.names if n in MOVES}
            if len(moved) > 1:
                raise SystemExit(f"line {chunk.first}: {chunk.names} move to {sorted(moved)}")
            if in_core:
                chunk.module = "core"
            elif moved:
                chunk.module = moved.pop()
            else:
                chunk.module = source.sections[chunk.section].module
        else:
            text = "".join(chunk.body)
            moved = [m for key, m in MOVES.items() if text.startswith(key)]
            chunk.module = moved[0] if moved else source.sections[chunk.section].module
    unknown = {c.module for c in source.chunks if c.module} - set(MODULE_NAMES)
    if unknown:
        raise SystemExit(f"modules missing from MODULES: {sorted(unknown)}")
    empty = set(MODULE_NAMES) - {c.module for c in source.chunks}
    if empty:
        raise SystemExit(f"modules in MODULES with nothing in them: {sorted(empty)}")


# --- Scopes ----------------------------------------------------------------------
#
# Which names in a statement are the module's own globals: Python's rules,
# enough of them for this file (functions, lambdas, classes, comprehensions,
# global and nonlocal).

@dataclass(eq=False)
class Scope:
    kind: str                    # module, function, class, comprehension
    parent: Optional["Scope"]
    bound: Set[str] = field(default_factory=set)
    declared_global: Set[str] = field(default_factory=set)
    declared_nonlocal: Set[str] = field(default_factory=set)


@dataclass
class NameUse:
    node: ast.Name
    scope: Scope
    annotation: bool


@dataclass
class StringAnnotation:
    node: ast.Constant
    scope: Scope


class Resolver:
    """Walks one top-level statement and records every Name with the scope it's in."""

    def __init__(self, module_scope: Scope) -> None:
        self.module_scope = module_scope
        self.uses: List[NameUse] = []
        self.strings: List[StringAnnotation] = []
        self.globals_statements: List[Tuple[ast.Global, Scope]] = []
        self.functions: Dict[Scope, ast.AST] = {}

    def _bind_target(self, scope: Scope, target: ast.AST) -> None:
        for n in ast.walk(target):
            if isinstance(n, ast.Name):
                scope.bound.add(n.id)

    def _collect(self, scope: Scope, nodes: Iterable[ast.AST]) -> None:
        """Names bound directly in ``scope`` by ``nodes`` (not in nested scopes)."""
        stack = list(nodes)
        while stack:
            n = stack.pop()
            if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                scope.bound.add(n.name)
                stack.extend(self._outer_parts(n))
                continue
            if isinstance(n, ast.Lambda):
                stack.extend(n.args.defaults + [d for d in n.args.kw_defaults if d is not None])
                continue
            if isinstance(n, (ast.ListComp, ast.SetComp, ast.DictComp, ast.GeneratorExp)):
                stack.append(n.generators[0].iter)
                # A walrus in a comprehension binds in the enclosing function.
                for inner in ast.walk(n):
                    if isinstance(inner, ast.NamedExpr):
                        self._bind_target(scope, inner.target)
                continue
            if isinstance(n, ast.Name) and isinstance(n.ctx, (ast.Store, ast.Del)):
                scope.bound.add(n.id)
            elif isinstance(n, ast.Import):
                for a in n.names:
                    scope.bound.add(a.asname or a.name.split(".")[0])
            elif isinstance(n, ast.ImportFrom):
                for a in n.names:
                    if a.name == "*":
                        raise SystemExit(f"line {n.lineno}: a star import")
                    scope.bound.add(a.asname or a.name)
            elif isinstance(n, ast.ExceptHandler) and n.name:
                scope.bound.add(n.name)
            elif isinstance(n, ast.Global):
                scope.declared_global.update(n.names)
            elif isinstance(n, ast.Nonlocal):
                scope.declared_nonlocal.update(n.names)
            elif isinstance(n, (ast.MatchAs, ast.MatchStar)) and n.name:
                scope.bound.add(n.name)
            elif isinstance(n, ast.MatchMapping) and n.rest:
                scope.bound.add(n.rest)
            stack.extend(ast.iter_child_nodes(n))
        scope.bound -= scope.declared_global

    @staticmethod
    def _outer_parts(n: ast.AST) -> List[ast.AST]:
        """The parts of a def or class evaluated in the scope around it."""
        parts: List[ast.AST] = list(getattr(n, "decorator_list", []))
        if isinstance(n, ast.ClassDef):
            parts += n.bases + [k.value for k in n.keywords]
        else:
            a = n.args
            parts += a.defaults + [d for d in a.kw_defaults if d is not None]
        return parts

    @staticmethod
    def _annotations(n: ast.AST) -> List[ast.AST]:
        if isinstance(n, ast.ClassDef):
            return []
        a = n.args
        args = a.posonlyargs + a.args + a.kwonlyargs + [x for x in (a.vararg, a.kwarg) if x is not None]
        return [x.annotation for x in args if x.annotation is not None] + ([n.returns] if n.returns else [])

    def walk_statement(self, node: ast.stmt) -> None:
        self._visit(node, self.module_scope, annotation=False)

    def _visit(self, n: ast.AST, scope: Scope, annotation: bool) -> None:
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)):
            for part in self._outer_parts(n):
                self._visit(part, scope, annotation)
            for ann in self._annotations(n):
                self._visit_annotation(ann, scope)
            inner = Scope("function", scope)
            self.functions[inner] = n
            a = n.args
            for arg in a.posonlyargs + a.args + a.kwonlyargs + [x for x in (a.vararg, a.kwarg) if x is not None]:
                inner.bound.add(arg.arg)
            self._collect(inner, n.body)
            for stmt in n.body:
                self._visit(stmt, inner, False)
            return
        if isinstance(n, ast.Lambda):
            for part in n.args.defaults + [d for d in n.args.kw_defaults if d is not None]:
                self._visit(part, scope, annotation)
            inner = Scope("function", scope)
            a = n.args
            for arg in a.posonlyargs + a.args + a.kwonlyargs + [x for x in (a.vararg, a.kwarg) if x is not None]:
                inner.bound.add(arg.arg)
            self._collect(inner, [n.body])
            self._visit(n.body, inner, False)
            return
        if isinstance(n, ast.ClassDef):
            for part in self._outer_parts(n):
                self._visit(part, scope, annotation)
            inner = Scope("class", scope)
            self._collect(inner, n.body)
            for stmt in n.body:
                self._visit(stmt, inner, False)
            return
        if isinstance(n, (ast.ListComp, ast.SetComp, ast.DictComp, ast.GeneratorExp)):
            self._visit(n.generators[0].iter, scope, annotation)
            inner = Scope("comprehension", scope)
            for gen in n.generators:
                self._bind_target(inner, gen.target)
            for gen in n.generators:
                self._visit(gen.target, inner, False)
                if gen is not n.generators[0]:
                    self._visit(gen.iter, inner, False)
                for cond in gen.ifs:
                    self._visit(cond, inner, False)
            for part in ([n.key, n.value] if isinstance(n, ast.DictComp) else [n.elt]):
                self._visit(part, inner, False)
            return
        if isinstance(n, ast.AnnAssign):
            self._visit(n.target, scope, annotation)
            self._visit_annotation(n.annotation, scope)
            if n.value is not None:
                self._visit(n.value, scope, annotation)
            return
        if isinstance(n, ast.Global):
            self.globals_statements.append((n, scope))
        if isinstance(n, ast.Name):
            self.uses.append(NameUse(n, scope, annotation))
            return
        for child in ast.iter_child_nodes(n):
            self._visit(child, scope, annotation)

    def _visit_annotation(self, ann: ast.AST, scope: Scope) -> None:
        for n in ast.walk(ann):
            if isinstance(n, ast.Constant) and isinstance(n.value, str):
                self.strings.append(StringAnnotation(n, scope))
        self._visit(ann, scope, True)


def binding_scope(name: str, scope: Scope) -> Scope:
    """The scope a name read or written in ``scope`` belongs to."""
    s: Optional[Scope] = scope
    first = True
    while s is not None and s.kind != "module":
        if s.kind != "class" or first:
            if name in s.declared_global:
                break
            if name in s.bound:
                return s
        s = s.parent
        first = False
    while scope.parent is not None:
        scope = scope.parent
    return scope


def resolves_global(name: str, scope: Scope, annotation: bool = False) -> bool:
    """Whether ``name`` read in ``scope`` is the module's global."""
    if annotation:
        # Annotations are strings (``from __future__ import annotations``);
        # get_type_hints and FastAPI read them with the module's globals.
        return True
    return binding_scope(name, scope).kind == "module"


def module_globals_used(tree: ast.Module) -> Tuple[Set[str], Set[str], Dict[str, Set[str]]]:
    """What a module binds at its top level, the globals its code reads (in
    annotation strings too), and the attributes read off each global name."""
    top = Scope("module", None)
    Resolver(top)._collect(top, tree.body)
    used: Set[str] = set()
    attrs: Dict[str, Set[str]] = collections.defaultdict(set)
    for stmt in tree.body:
        r = Resolver(top)
        r.walk_statement(stmt)
        for use in r.uses:
            if resolves_global(use.node.id, use.scope, use.annotation):
                used.add(use.node.id)
        for s in r.strings:
            used.update(_string_annotation_names(s.node.value))
        for n in ast.walk(stmt):
            if isinstance(n, ast.Attribute) and isinstance(n.value, ast.Name):
                attrs[n.value.id].add(n.attr)
    return top.bound, used, attrs


def _string_annotation_names(text: str) -> List[str]:
    try:
        expr = ast.parse(text, mode="eval")
    except SyntaxError:
        return []
    return [n.id for n in ast.walk(expr) if isinstance(n, ast.Name)]


# --- The plan ----------------------------------------------------------------------

@dataclass
class Plan:
    source: Source
    owner: Dict[str, str]                         # top-level name -> module
    imported: Dict[str, List[ast.stmt]]           # name a top import binds -> those imports
    uses: Dict[int, List[NameUse]]                # chunk index -> its global name uses
    strings: Dict[int, List[StringAnnotation]]
    globals_statements: Dict[int, List[Tuple[ast.Global, Scope]]]


def build_plan(source: Source) -> Plan:
    assign_modules(source)
    owner: Dict[str, str] = {}
    for chunk in source.chunks:
        for name in chunk.names:
            if name in owner:
                raise SystemExit(f"line {chunk.first}: {name} is defined twice")
            owner[name] = chunk.module
    imported: Dict[str, List[ast.stmt]] = collections.defaultdict(list)
    for node in source.imports:
        for a in node.names:
            bound = a.asname or (a.name.split(".")[0] if isinstance(node, ast.Import) else a.name)
            imported[bound].append(node)
    clash = sorted(set(owner) & set(imported))
    if clash:
        raise SystemExit(f"names both imported and defined: {clash}")
    taken = sorted(set(MODULE_NAMES) & (set(owner) | set(imported) | set(dir(builtins))))
    if taken:
        raise SystemExit(f"module names the code already uses for something else: {taken}")
    taken = sorted(set(CORE_DEFINED) & (set(owner) | set(imported)))
    if taken:
        raise SystemExit(f"names core.py defines anew that the code already has: {taken}")
    module_scope = Scope("module", None)
    uses: Dict[int, List[NameUse]] = {}
    strings: Dict[int, List[StringAnnotation]] = {}
    globals_statements: Dict[int, List[Tuple[ast.Global, Scope]]] = {}
    for i, chunk in enumerate(source.chunks):
        r = Resolver(module_scope)
        r.walk_statement(chunk.node)
        uses[i] = [u for u in r.uses if resolves_global(u.node.id, u.scope, u.annotation)]
        strings[i] = r.strings
        globals_statements[i] = r.globals_statements
    return Plan(source, owner, dict(imported), uses, strings, globals_statements)


def import_time_references(plan: Plan) -> Dict[str, Set[Tuple[str, str]]]:
    """module -> (name, its module) for names its statements read while the module loads:
    at module level, in class bodies, and in the bodies of what they call then."""
    source = plan.source
    by_name = {n: i for i, c in enumerate(source.chunks) for n in c.names}

    def at_load(i: int) -> List[NameUse]:
        out = []
        for use in plan.uses[i]:
            s: Optional[Scope] = use.scope
            while s is not None and s.kind == "class":
                s = s.parent
            if s is not None and s.kind == "module" and not use.annotation:
                out.append(use)
        return out

    def within(i: int, node: ast.AST) -> List[NameUse]:
        return [u for u in plan.uses[i]
                if (node.lineno, node.col_offset) <= (u.node.lineno, u.node.col_offset)
                and (u.node.end_lineno, u.node.end_col_offset) <= (node.end_lineno, node.end_col_offset)]

    def calls_in(nodes: Iterable[ast.AST], names: Set[str]) -> Set[str]:
        """The file's own functions and classes ``nodes`` call by one of ``names``."""
        out = set()
        for node in nodes:
            for n in ast.walk(node):
                if isinstance(n, ast.Call) and isinstance(n.func, ast.Name) and n.func.id in names \
                        and n.func.id in by_name:
                    out.add(n.func.id)
        return out

    def calls_at_load(i: int) -> Set[str]:
        names = {u.node.id for u in at_load(i)}
        node = source.chunks[i].node
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            parts = Resolver._outer_parts(node)
        elif isinstance(node, ast.ClassDef):
            parts = Resolver._outer_parts(node) + [s for s in node.body
                                                    if not isinstance(s, (ast.FunctionDef, ast.AsyncFunctionDef))]
        else:
            parts = [node]
        return calls_in(parts, names)

    def body_names(name: str, seen: Set[str]) -> Set[str]:
        """The globals a called function reads (for a class, its __init__), and what they call."""
        if name in seen:
            return set()
        seen.add(name)
        i = by_name[name]
        node = source.chunks[i].node
        if isinstance(node, ast.ClassDef):
            bodies = [s for s in node.body if isinstance(s, (ast.FunctionDef, ast.AsyncFunctionDef))
                      and s.name in ("__init__", "__new__", "__post_init__", "__init_subclass__")]
            out: Set[str] = set()
            for base in node.bases:
                if isinstance(base, ast.Name) and base.id in by_name:
                    out |= body_names(base.id, seen)
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            bodies = [node]
            out = set()
        else:
            return set()
        for body in bodies:
            uses = within(i, body)
            out |= {u.node.id for u in uses}
            for other in calls_in([body], {u.node.id for u in uses}):
                out |= body_names(other, seen)
        return out

    result: Dict[str, Set[Tuple[str, str]]] = collections.defaultdict(set)
    for i, chunk in enumerate(source.chunks):
        if not chunk.module:
            continue
        names = {u.node.id for u in at_load(i)}
        for called in calls_at_load(i):
            names |= body_names(called, set())
        for name in names:
            owner = plan.owner.get(name)
            if owner and owner != chunk.module and name not in SHARED:
                result[chunk.module].add((name, owner))
    return result


def load_order_problems(plan: Plan) -> List[str]:
    rank = {m: k for k, m in enumerate(MODULE_NAMES)}
    problems = []
    for module, reads in sorted(import_time_references(plan).items()):
        for name, other in sorted(reads):
            if rank[other] > rank[module]:
                problems.append(f"{module} uses {other}.{name} while it loads, but {other} runs after it: "
                                f"move {module} below {other} in MODULES")
    return problems


# --- Writing the modules -------------------------------------------------------------

class TextFixes:
    def __init__(self, fixes: List[Tuple[str, str, int]]) -> None:
        self.fixes = fixes
        self.counts = collections.Counter()

    def apply(self, text: str, count: bool = True) -> str:
        for old, new, _ in self.fixes:
            if old in text:
                if count:
                    self.counts[old] += text.count(old)
                text = text.replace(old, new)
        return text

    def verify(self) -> None:
        for old, _, expected in self.fixes:
            if self.counts[old] != expected:
                raise SystemExit(f"TEXT_FIXES expects {expected} of {old!r}, found {self.counts[old]}")


def _char_col(line: str, col: int) -> int:
    """An AST column (UTF-8 bytes) as a position in the line."""
    return len(line.encode()[:col].decode())


def _apply(text: str, edits: List[Tuple[int, int, str]]) -> str:
    out, at = [], 0
    for start, end, new in sorted(edits):
        if start < at:
            raise SystemExit(f"overlapping rewrites at {text[start:end]!r}")
        out += [text[at:start], new]
        at = end
    return "".join(out + [text[at:]])


@dataclass
class Rewritten:
    text: str                 # the statement, with its comments above, as it is in its module
    modules: Set[str]         # the other modules it names


def rewrite_chunk(plan: Plan, i: int, fixes: TextFixes) -> Rewritten:
    chunk = plan.source.chunks[i]
    body = "".join(chunk.body)
    starts = [0]
    for line in chunk.body:
        starts.append(starts[-1] + len(line))

    def offset(lineno: int, col: int) -> int:
        k = lineno - chunk.first
        return starts[k] + _char_col(chunk.body[k], col)

    def span(node: ast.AST) -> Tuple[int, int]:
        return offset(node.lineno, node.col_offset), offset(node.end_lineno, node.end_col_offset)

    edits: List[Tuple[int, int, str]] = []
    modules: Set[str] = set()
    mine = chunk.module

    def foreign(name: str) -> Optional[str]:
        owner = plan.owner.get(name)
        if owner and owner != mine and name not in SHARED:
            return owner
        return None

    for use in plan.uses[i]:
        owner = foreign(use.node.id)
        if owner is None:
            continue
        if not isinstance(use.node.ctx, ast.Load):
            raise SystemExit(f"line {use.node.lineno}: {mine} assigns {use.node.id}, which is {owner}'s")
        if not use.annotation and not resolves_global(owner, use.scope):
            raise SystemExit(f"line {use.node.lineno}: {use.node.id} becomes {owner}.{use.node.id}, but {owner} "
                             f"is a local name there; rename the local")
        edits.append((*span(use.node), f"{owner}.{use.node.id}"))
        modules.add(owner)
    for s in plan.strings[i]:
        names = [n for n in _string_annotation_names(s.node.value) if foreign(n)]
        if not names:
            continue
        start, end = span(s.node)
        literal = body[start:end]
        quote = literal[:1]
        if quote not in "\"'" or literal[-1:] != quote or literal[1:-1] != s.node.value or not s.node.value.isascii():
            raise SystemExit(f"line {s.node.lineno}: rewrite the annotation {literal} by hand")
        expr = ast.parse(s.node.value, mode="eval")
        inner = [(n.col_offset, n.end_col_offset, f"{foreign(n.id)}.{n.id}") for n in ast.walk(expr)
                 if isinstance(n, ast.Name) and foreign(n.id)]
        modules.update(foreign(n) for n in names)
        edits.append((start, end, quote + _apply(s.node.value, inner) + quote))
    for node in ast.walk(chunk.node):
        if isinstance(node, ast.Call) and ast.unparse(node) in (PLUGIN_ROOT_EXPR, DASHBOARD_DIR_EXPR):
            name = "_plugin_root()" if ast.unparse(node) == PLUGIN_ROOT_EXPR else "_dashboard_dir()"
            start, end = span(node)
            if any(s <= start and end <= e for s, e, _ in edits):
                continue
            edits = [e for e in edits if not (start <= e[0] and e[1] <= end)]
            edits.append((start, end, name if mine == "core" else f"core.{name}"))
            if mine != "core":
                modules.add("core")
    rewritten = _apply(body, edits)
    for g, _ in plan.globals_statements[i]:
        for name in g.names:
            if plan.owner.get(name, mine) != mine:
                raise SystemExit(f"line {g.lineno}: {mine} declares {name} global, which is {plan.owner[name]}'s")
    if "__file__" in {n.id for n in ast.walk(ast.parse(textwrap.dedent(rewritten))) if isinstance(n, ast.Name)}:
        raise SystemExit(f"line {chunk.first}: __file__ in a form the script doesn't know")
    return Rewritten(fixes.apply("".join(chunk.lead) + rewritten), modules)


def _blank_lines(text: str) -> Tuple[int, str]:
    lines = text.splitlines(keepends=True)
    n = 0
    while n < len(lines) and not lines[n].strip():
        n += 1
    return n, "".join(lines[n:])


def _is_def(node: ast.stmt) -> bool:
    return isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))


def header_line(title: str) -> str:
    line = f"# --- {title} "
    return line + "-" * max(3, 80 - len(line)) + "\n"


def doc_paragraphs(source: Source) -> Dict[str, List[str]]:
    out: Dict[str, List[str]] = collections.defaultdict(list)
    for paragraph in source.docstring.strip("\n").split("\n\n"):
        targets = [m for prefix, m in DOC_PARAGRAPHS if paragraph.startswith(prefix)]
        if len(targets) != 1:
            raise SystemExit(f"no place in DOC_PARAGRAPHS for the docstring paragraph {paragraph[:60]!r}")
        out[targets[0]].append(paragraph)
    return out


def _wrap(text: str, indent: str = "") -> str:
    return "\n".join(textwrap.wrap(text, 79, initial_indent=indent, subsequent_indent=indent,
                                   break_on_hyphens=False))


def module_docstring(name: str, paragraphs: List[str]) -> str:
    summary = dict(MODULES)[name] + "."
    parts = [_wrap(summary)] + paragraphs
    text = "\n\n".join(parts)
    if "\n" not in text and len(text) <= 73:
        return f'"""{text}"""\n'
    return f'"""{text}\n"""\n'


def import_lines(plan: Plan, needed: Set[str], attrs: Dict[str, Set[str]]) -> Tuple[List[str], Set[str]]:
    """The file's imports that ``needed`` takes, in the file's order and groups, and the names they bind."""
    source = plan.source
    kept: Dict[int, str] = {}
    bound: Set[str] = set()
    for name in sorted(needed):
        nodes = plan.imported[name]
        plain = [n for n in nodes if isinstance(n, ast.Import)]
        if len(nodes) > 1 and len(plain) == len(nodes):
            # import a, import a.b, import a.c: the ones this module's a.b and a.c need.
            used = attrs.get(name, set())
            subs = {n: n.names[0].name.split(".")[1] if "." in n.names[0].name else "" for n in nodes}
            keep = [n for n, sub in subs.items() if sub and sub in used]
            if "" in subs.values() and (used - set(subs.values())):
                keep += [n for n, sub in subs.items() if not sub]
            nodes = keep or nodes
        for node in nodes:
            kept[id(node)] = name
            bound.add(name)
    lines: List[List[str]] = [[], []]
    for node, group in zip(source.imports, source.import_groups):
        if id(node) not in kept:
            continue
        if isinstance(node, ast.Import):
            text = ast.get_source_segment(source.text, node)
        else:
            aliases = [a for a in node.names if (a.asname or a.name) in needed]
            if not aliases:
                continue
            names = ", ".join(a.name + (f" as {a.asname}" if a.asname else "") for a in aliases)
            text = f"from {node.module} import {names}"
        lines[group].append(text + "\n")
    return [l for group in lines for l in group + (["\n"] if group else [])], bound


@dataclass
class ModuleOut:
    text: str
    imports: Set[str]         # the names its imports bind
    siblings: Set[str]        # the other modules it imports


def build_modules(plan: Plan, fixes: TextFixes) -> Dict[str, ModuleOut]:
    source = plan.source
    bodies: Dict[str, List[str]] = {m: [] for m in MODULE_NAMES}
    siblings: Dict[str, Set[str]] = {m: set() for m in MODULE_NAMES}
    last_section: Dict[str, int] = {}
    last_node: Dict[str, ast.stmt] = {}
    headed: Set[int] = set()
    for i, chunk in enumerate(source.chunks):
        if not chunk.module:
            continue
        m = chunk.module
        if chunk.names == [LOGGER]:
            if "".join(chunk.body).strip() != LOGGER_LINE:
                raise SystemExit(f"line {chunk.first}: the logger isn't {LOGGER_LINE!r} any more")
            continue
        section = source.sections[chunk.section]
        home = _prefixed(SECTION_HEADERS, section.title) or section.module
        header: List[str] = []
        titled = [CORE_HEADERS[n] for n in chunk.names if n in CORE_HEADERS] if m == "core" else []
        if home == m and chunk.section not in headed and section.header:
            header = [fixes.apply(l) for l in section.header]
            headed.add(chunk.section)
        elif titled:
            header = [header_line(titled[0])]
        elif m == "core" and last_section.get(m) != chunk.section:
            raise SystemExit(f"core takes statements from {section.title!r}: give the first, "
                             f"{chunk.names}, a header in CORE_HEADERS")
        rewritten = rewrite_chunk(plan, i, fixes)
        siblings[m] |= rewritten.modules
        blanks, rest = _blank_lines(rewritten.text)
        if header:
            if not (chunk.first_in_section and home == m):
                blanks = 1
            bodies[m].append("\n\n" + "".join(header))
        elif m not in last_node:
            blanks = 2
        elif _is_def(chunk.node) or _is_def(last_node[m]):
            blanks = 2
        bodies[m].append("\n" * min(blanks, 2) + rest)
        last_section[m] = chunk.section
        last_node[m] = chunk.node
    paragraphs = doc_paragraphs(source)
    out: Dict[str, ModuleOut] = {}
    for m in MODULE_NAMES:
        body = "".join(bodies[m])
        defined, used, _ = module_globals_used(ast.parse(body))
        preamble = ""
        if m == "core":
            preamble = CORE_PREAMBLE
        else:
            for name in SHARED:
                if name in used:
                    preamble += f"{name} = core.{name}\n"
                    siblings[m].add("core")
        full = preamble + body
        defined, used, attrs = module_globals_used(ast.parse(full))
        free = used - defined - set(dir(builtins)) - {"__file__", "__name__", "__package__"}
        sibling_uses = free & set(MODULE_NAMES)
        if sibling_uses != siblings[m]:
            raise SystemExit(f"{m}: names {sorted(sibling_uses ^ siblings[m])} as modules unexpectedly")
        needed = free - sibling_uses
        missing = sorted(n for n in needed if n not in plan.imported)
        if missing:
            raise SystemExit(f"{m} uses names nothing defines or imports: {missing}")
        imports, bound = import_lines(plan, needed, attrs)
        head = module_docstring(m, paragraphs.get(m, [])) + "\nfrom __future__ import annotations\n\n"
        head += "".join(imports)
        if siblings[m]:
            head += f"from . import {', '.join(sorted(siblings[m]))}\n\n"
        text = head + full
        text = text.rstrip("\n") + "\n"
        out[m] = ModuleOut(text, bound, set(siblings[m]))
    return out


def package_init() -> str:
    names = "".join(f'    "{name}",\n' for name in MODULE_NAMES)
    return f'''\
"""The dashboard API's modules, one per feature. plugin_api.py, the file
Hermes loads, loads this package and lists what each module holds."""

from __future__ import annotations

import importlib.util
import os
import sys
from typing import Dict

# In the order they run, which is also the order their routes are tried (the
# first match wins). Every module is created before the first one runs, so
# ``from . import watch_jobs`` at the top of a module only names it: what a
# module's top-level statements use while it loads (a base class, a default
# value, a constant made from another's) has to come from a module above it
# here, while its functions can use any of them.
MODULES = (
{names})


def _load() -> Dict:
    modules = {{}}
    for name in MODULES:
        spec = importlib.util.spec_from_file_location(f"{{__name__}}.{{name}}", os.path.join(__path__[0], f"{{name}}.py"))
        module = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = module
        globals()[name] = module
        modules[name] = module
    for module in modules.values():
        module.__spec__.loader.exec_module(module)
    return modules


# The router Hermes mounts: core's, which every module adds its routes to.
router = _load()["core"].router
'''


def plugin_api_text(source: Source) -> str:
    paragraphs = doc_paragraphs(source)["plugin_api"]
    width = max(len(name) for name in MODULE_NAMES) + 2
    index = []
    for name, summary in MODULES:
        lines = textwrap.wrap(summary, 79 - 4 - width, break_on_hyphens=False)
        index.append(f"    {name:<{width}}{lines[0]}")
        index += [f"    {'':<{width}}{line}" for line in lines[1:]]
    intro = _wrap("Hermes loads this file by path (manifest.json's \"api\"). The routes and the tools behind them "
                  "are in dashboard/api/, one module per feature, in the order they run:")
    rules = _wrap("A module takes what it uses from another by module (``core.TokenError``), never by name, so "
                  "a test that replaces ``plugin_api.core._calls_home`` replaces it for every module. "
                  "CONTRIBUTING.md has the rest of the layout's rules.")
    doc = "\n\n".join([paragraphs[0], intro, "\n".join(index), rules] + paragraphs[1:])
    return f'''\
"""{doc}
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
    name = f"{{__name__}}.api"

    def forget() -> None:
        for key in [key for key in sys.modules if key == name or key.startswith(f"{{name}}.")]:
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
'''


# --- Checking the move ------------------------------------------------------------------

class _Undo(ast.NodeTransformer):
    """Turns a moved statement back into the single file's: ``module.name``
    back into ``name``, and core's folders back into the expressions they were."""

    def __init__(self, owner: Dict[str, str], module: str) -> None:
        self.owner = owner
        self.module = module

    def visit_Attribute(self, node: ast.Attribute) -> ast.AST:
        if isinstance(node.value, ast.Name) and node.value.id in MODULE_NAMES \
                and self.owner.get(node.attr) == node.value.id and node.value.id != self.module:
            return ast.copy_location(ast.Name(id=node.attr, ctx=node.ctx), node)
        return self.generic_visit(node)

    def visit_Call(self, node: ast.Call) -> ast.AST:
        f = node.func
        name = f.id if isinstance(f, ast.Name) and self.module == "core" else \
            f.attr if isinstance(f, ast.Attribute) and isinstance(f.value, ast.Name) and f.value.id == "core" else None
        if name in CORE_DEFINED and not node.args and not node.keywords:
            expr = PLUGIN_ROOT_EXPR if name == "_plugin_root" else DASHBOARD_DIR_EXPR
            return ast.copy_location(ast.parse(expr, mode="eval").body, node)
        return self.generic_visit(node)

    def _annotation(self, node: Optional[ast.AST]) -> Optional[ast.AST]:
        if node is None:
            return None
        node = self.visit(node)
        for n in ast.walk(node):
            if isinstance(n, ast.Constant) and isinstance(n.value, str):
                for module in MODULE_NAMES:
                    n.value = re.sub(rf"\b{module}\.(\w+)",
                                     lambda m, module=module: m.group(1) if self.owner.get(m.group(1)) == module
                                     else m.group(0), n.value)
        return node

    def visit_arg(self, node: ast.arg) -> ast.AST:
        node.annotation = self._annotation(node.annotation)
        return node

    def visit_FunctionDef(self, node: ast.AST) -> ast.AST:
        node.returns = self._annotation(node.returns)
        return self.generic_visit(node)

    visit_AsyncFunctionDef = visit_FunctionDef

    def visit_AnnAssign(self, node: ast.AnnAssign) -> ast.AST:
        node.annotation = self._annotation(node.annotation)
        node.target = self.visit(node.target)
        if node.value is not None:
            node.value = self.visit(node.value)
        return node


def _comments(text: str) -> List[str]:
    return [t.string for t in tokenize.generate_tokens(io.StringIO(text).readline) if t.type == tokenize.COMMENT]


def self_check(plan: Plan, modules: Dict[str, ModuleOut], fixes: TextFixes) -> None:
    """Every statement arrives once, parsing to the single file's once the
    rewrites are undone, and every comment arrives once."""
    source = plan.source
    want_comments: collections.Counter = collections.Counter()
    for section in source.sections:
        want_comments.update(_comments(fixes.apply("".join(section.header), count=False)))
    for chunk in source.chunks:
        if chunk.module and chunk.names != [LOGGER]:
            want_comments.update(_comments(fixes.apply("".join(chunk.lead + chunk.body), count=False)))
    want_comments.update(_comments(CORE_PREAMBLE))
    have_comments: collections.Counter = collections.Counter()
    for m in MODULE_NAMES:
        tree = ast.parse(modules[m].text)
        statements = [s for s in tree.body[1:] if not isinstance(s, (ast.Import, ast.ImportFrom))]
        preamble = len(ast.parse(CORE_PREAMBLE).body) if m == "core" else \
            sum(1 for s in statements[:2] if ast.unparse(s) in [f"{n} = core.{n}" for n in SHARED])
        statements = statements[preamble:]
        chunks = [c for c in source.chunks if c.module == m and c.names != [LOGGER]]
        if len(statements) != len(chunks):
            raise SystemExit(f"{m}: {len(statements)} statements for {len(chunks)} moved ones")
        for stmt, chunk in zip(statements, chunks):
            original = ast.parse(textwrap.dedent(fixes.apply("".join(chunk.body), count=False))).body[0]
            undone = _Undo(plan.owner, m).visit(stmt)
            if ast.dump(undone) != ast.dump(original):
                raise SystemExit(f"{m}: the statement from line {chunk.first} changed beyond the rewrites")
        comments = _comments(modules[m].text)
        have_comments.update(c for c in comments if c + "\n" not in [header_line(t) for t in CORE_HEADERS.values()])
    if have_comments != want_comments:
        lost = want_comments - have_comments
        extra = have_comments - want_comments
        raise SystemExit(f"comments lost: {list(lost)[:5]}, added: {list(extra)[:5]}")


# --- Tests and probes ---------------------------------------------------------------------

STRING_ACCESS = {"setattr", "getattr", "hasattr", "delattr", "object"}  # setattr(api, "x"), patch.object(api, "x")
KEPT_ATTRS = {"router", "__file__", "__name__", "__spec__", "__loader__"} | set(MODULE_NAMES)


def _plugin_api_path(node: ast.AST) -> bool:
    return any(isinstance(n, ast.Constant) and isinstance(n.value, str)
               and (n.value == "plugin_api.py" or n.value.endswith("/plugin_api.py")) for n in ast.walk(node))


def _test_module(node: ast.AST) -> Optional[str]:
    """The test file a call names by path, as its module's name: test_location."""
    for n in ast.walk(node):
        if isinstance(n, ast.Constant) and isinstance(n.value, str):
            match = re.search(r"(?:^|/)(test_\w+)\.py$", n.value)
            if match:
                return match.group(1)
    return None


def _call_name(node: ast.Call) -> str:
    f = node.func
    return f.id if isinstance(f, ast.Name) else f.attr if isinstance(f, ast.Attribute) else ""


@dataclass
class TestFile:
    """A test or probe, and the names in it that hold the loaded plugin_api.py."""
    path: str
    text: str
    tree: ast.Module
    resolver: Resolver
    top: Scope
    scope_of: Dict[ast.Name, Scope]
    parents: Dict[ast.AST, ast.AST]
    holders: Dict[Scope, Set[str]]
    others: Dict[Scope, Set[str]]
    handles: Dict[Scope, Dict[str, str]]  # names holding another test file's module: {"helpers": "test_location"}


def analyze_test(path: str, text: str) -> TestFile:
    """The names that hold plugin_api.py because this file loads it there."""
    tree = ast.parse(text)
    loaders = {n.name for n in tree.body if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
               and _plugin_api_path(n) and any(isinstance(c, ast.Call) and _call_name(c) == "exec_module"
                                               for c in ast.walk(n))}
    top = Scope("module", None)
    resolver = Resolver(top)
    resolver._collect(top, tree.body)
    for stmt in tree.body:
        resolver.walk_statement(stmt)
    scope_of: Dict[ast.Name, Scope] = {u.node: u.scope for u in resolver.uses}
    parents = {child: node for node in ast.walk(tree) for child in ast.iter_child_nodes(node)}
    specs: Dict[Scope, Set[str]] = collections.defaultdict(set)
    test_specs: Dict[Scope, Dict[str, str]] = collections.defaultdict(dict)
    holders: Dict[Scope, Set[str]] = collections.defaultdict(set)
    others: Dict[Scope, Set[str]] = collections.defaultdict(set)
    handles: Dict[Scope, Dict[str, str]] = collections.defaultdict(dict)
    assigns = [n for n in ast.walk(tree) if isinstance(n, ast.Assign) and len(n.targets) == 1
               and isinstance(n.targets[0], ast.Name)]
    for node in assigns:
        v, target = node.value, node.targets[0]
        if isinstance(v, ast.Call) and _call_name(v) == "spec_from_file_location":
            if _plugin_api_path(v):
                specs[binding_scope(target.id, scope_of[target])].add(target.id)
            elif _test_module(v):
                test_specs[binding_scope(target.id, scope_of[target])][target.id] = _test_module(v)
    for node in assigns:
        v, target = node.value, node.targets[0]
        bound = binding_scope(target.id, scope_of[target])
        spec = v.args[0] if isinstance(v, ast.Call) and _call_name(v) == "module_from_spec" and v.args \
            and isinstance(v.args[0], ast.Name) else None
        holds = isinstance(v, ast.Call) and (
            (isinstance(v.func, ast.Name) and v.func.id in loaders)
            or (_plugin_api_path(v) and _call_name(v) != "spec_from_file_location")
            or (spec is not None and spec.id in specs[binding_scope(spec.id, scope_of[spec])]))
        (holders if holds else others)[bound].add(target.id)
        if spec is not None and spec.id in test_specs[binding_scope(spec.id, scope_of[spec])]:
            handles[bound][target.id] = test_specs[binding_scope(spec.id, scope_of[spec])][spec.id]
    # What a loader hands back holds the loaded file in the loader too: a probe's
    # check of the names it needs, before it returns the file.
    for function in tree.body:
        if isinstance(function, (ast.FunctionDef, ast.AsyncFunctionDef)) and function.name in loaders:
            for node in ast.walk(function):
                if isinstance(node, ast.Return) and isinstance(node.value, ast.Name) and node.value in scope_of:
                    bound = binding_scope(node.value.id, scope_of[node.value])
                    holders[bound].add(node.value.id)
                    others[bound].discard(node.value.id)
    return TestFile(path, text, tree, resolver, top, scope_of, parents, holders, others, handles)


def add_imported_holders(test: TestFile, module_holders: Dict[str, Set[str]]) -> bool:
    """``from test_health import api``, or ``api = helpers.api`` with another test
    file's module in ``helpers``: the names another test's module holds it under.
    Whether that added any."""
    functions = {node: scope for scope, node in test.resolver.functions.items()}
    added = False
    for node in ast.walk(test.tree):
        if not (isinstance(node, ast.Assign) and len(node.targets) == 1):
            continue
        target, value = node.targets[0], node.value
        pairs = list(zip(target.elts, value.elts)) if isinstance(target, ast.Tuple) and isinstance(value, ast.Tuple) \
            and len(target.elts) == len(value.elts) else [(target, value)]
        for t, v in pairs:
            if not (isinstance(t, ast.Name) and isinstance(v, ast.Attribute) and isinstance(v.value, ast.Name)):
                continue
            stem = test.handles[binding_scope(v.value.id, test.scope_of[v.value])].get(v.value.id)
            scope = binding_scope(t.id, test.scope_of[t])
            if v.attr in module_holders.get(stem, ()) and t.id not in test.holders[scope]:
                test.holders[scope].add(t.id)
                added = True
    for node in ast.walk(test.tree):
        if not (isinstance(node, ast.ImportFrom) and node.level == 0 and node.module in module_holders):
            continue
        scope, up = test.top, test.parents.get(node)
        while up is not None:
            if isinstance(up, (ast.FunctionDef, ast.AsyncFunctionDef)):
                scope = functions[up]
                break
            if isinstance(up, ast.ClassDef):
                raise SystemExit(f"{test.path}:{node.lineno}: an import in a class body")
            up = test.parents.get(up)
        for a in node.names:
            if a.name in module_holders[node.module] and (a.asname or a.name) not in test.holders[scope]:
                test.holders[scope].add(a.asname or a.name)
                added = True
    return added


def _loop_values(target: ast.AST, iterable: ast.AST, name: str):
    """The strings a loop's ``name`` takes from a tuple of them, or from a column of
    a tuple of tuples (``for part, name in (("core", "TokenError"), ...)``): None
    when they aren't all strings, False when the loop doesn't bind ``name``."""
    names = [target] if isinstance(target, ast.Name) else list(target.elts) if isinstance(target, ast.Tuple) else []
    i = next((i for i, t in enumerate(names) if isinstance(t, ast.Name) and t.id == name), None)
    if i is None:
        return False
    items = iterable.elts if isinstance(iterable, (ast.Tuple, ast.List)) else []
    if isinstance(target, ast.Tuple):
        items = [row.elts[i] if isinstance(row, (ast.Tuple, ast.List)) and len(row.elts) == len(names) else None
                 for row in items]
    return [e.value for e in items] if items and all(
        isinstance(e, ast.Constant) and isinstance(e.value, str) for e in items) else None


def _string_values(test: TestFile, name: ast.Name) -> Optional[List[str]]:
    """The strings a name can hold when a for loop or a comprehension takes it from
    a tuple of them, or it's a parametrized test's argument; None for any other name."""
    up = test.parents.get(name)
    while up is not None:
        loops = [up] if isinstance(up, (ast.For, ast.AsyncFor, ast.comprehension)) else \
            up.generators if isinstance(up, (ast.ListComp, ast.SetComp, ast.DictComp, ast.GeneratorExp)) else []
        for loop in loops:
            values = _loop_values(loop.target, loop.iter, name.id)
            if values is not False:
                return values
        if isinstance(up, (ast.FunctionDef, ast.AsyncFunctionDef)):
            for d in up.decorator_list:
                if not (isinstance(d, ast.Call) and ast.unparse(d.func) == "pytest.mark.parametrize"
                        and len(d.args) >= 2 and isinstance(d.args[0], ast.Constant)
                        and isinstance(d.args[1], (ast.List, ast.Tuple))):
                    continue
                argnames = [a.strip() for a in d.args[0].value.split(",")]
                if name.id not in argnames:
                    continue
                i = argnames.index(name.id)
                rows = [row.elts[i] if len(argnames) > 1 and isinstance(row, ast.Tuple) else
                        row if len(argnames) == 1 else None for row in d.args[1].elts]
                if all(isinstance(v, ast.Constant) and isinstance(v.value, str) for v in rows):
                    return [v.value for v in rows]
            return None
        up = test.parents.get(up)
    return None


def _own_parameter(test: TestFile, call: ast.Call, node: ast.Name) -> Optional[Tuple[Scope, str]]:
    """The parameter of this file's own function that a call hands ``node`` to:
    ``scoped(api, ...)`` or ``asyncio.to_thread(scoped, api, ...)``."""
    func, args = ast.unparse(call.func), list(call.args)
    if func == "asyncio.to_thread" and args and isinstance(args[0], ast.Name):
        func, args = args[0].id, args[1:]
    function = next((n for n in test.tree.body if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
                     and n.name == func), None)
    i = next((i for i, a in enumerate(args) if a is node), None)
    if function is None or i is None:
        return None
    params = function.args.posonlyargs + function.args.args
    if i >= len(params):
        return None
    return next(s for s, f in test.resolver.functions.items() if f is function), params[i].arg


def add_passed_holders(test: TestFile) -> None:
    """A function of this file's that's handed the loaded file holds it too, under
    its parameter's name: def scoped(api, ...), called as scoped(api, ...)."""
    added = True
    while added:
        added = False
        for use in test.resolver.uses:
            node, call = use.node, test.parents.get(use.node)
            if isinstance(node.ctx, ast.Load) and node.id in test.holders[binding_scope(node.id, use.scope)] \
                    and isinstance(call, ast.Call):
                param = _own_parameter(test, call, node)
                if param and param[1] not in test.holders[param[0]]:
                    test.holders[param[0]].add(param[1])
                    added = True


def _handed_on(test: TestFile, call: ast.Call, node: ast.Name) -> bool:
    """Whether a call the loaded file is handed to runs it (exec_module), is one of
    PASSED_ON, or is this file's own function, whose parameter then holds it too."""
    if _call_name(call) == "exec_module" or (test.path, ast.unparse(call.func)) in PASSED_ON:
        return True
    param = _own_parameter(test, call, node)
    return param is not None and param[1] in test.holders[param[0]]


def rewrite_test(test: TestFile, plan: Plan, modules: Dict[str, ModuleOut]) -> str:
    """``api.name`` becomes ``api.<module>.name`` wherever ``api`` holds the loaded plugin_api.py."""
    path, text, holders, parents = test.path, test.text, test.holders, test.parents
    lines = text.splitlines(keepends=True)
    starts = [0]
    for line in lines:
        starts.append(starts[-1] + len(line))

    def span(node: ast.AST) -> Tuple[int, int]:
        return (starts[node.lineno - 1] + _char_col(lines[node.lineno - 1], node.col_offset),
                starts[node.end_lineno - 1] + _char_col(lines[node.end_lineno - 1], node.end_col_offset))

    for scope, names in holders.items():
        if names & test.others[scope]:
            raise SystemExit(f"{path}: {sorted(names & test.others[scope])} hold plugin_api.py and something else")

    counts: collections.Counter = collections.Counter()
    refs: List[Tuple[ast.Name, str]] = []   # (holder name node, the name taken from it)
    for use in test.resolver.uses:
        node = use.node
        if not isinstance(node.ctx, ast.Load) or node.id not in holders[binding_scope(node.id, use.scope)]:
            continue
        parent = parents.get(node)
        where = f"{path}:{node.lineno}: {ast.unparse(parent)[:70]}"
        if isinstance(parent, ast.Attribute) and parent.value is node:
            refs.append((node, parent.attr))
        elif isinstance(parent, ast.Call) and _call_name(parent) in STRING_ACCESS and parent.args \
                and parent.args[0] is node:
            key = parent.args[1] if len(parent.args) >= 2 else None
            if isinstance(key, ast.Constant):
                refs.append((node, key.value))
                continue
            values = _string_values(test, key) if isinstance(key, ast.Name) else None
            if values is None:
                raise SystemExit(f"{where}: no telling which names it takes")
            if all(v in KEPT_ATTRS for v in values):
                continue
            owners = {plan.owner.get(v) for v in values}
            if len(owners) != 1 or None in owners:
                raise SystemExit(f"{where}: takes names from {sorted(map(str, owners))}; "
                                 "a TEST_TEXT_FIXES entry can name each value's module")
            refs.append((node, values[0]))
        elif isinstance(parent, ast.Return):
            continue  # a loader handing it back
        elif isinstance(parent, ast.Call) and node in parent.args:
            if not _handed_on(test, parent, node):
                raise SystemExit(f"{where}: handed to a call that may reach into it under another name")
        else:
            raise SystemExit(f"{where}: plugin_api.py's module used this way")
    for _, name in refs:
        if plan.owner.get(name):
            counts[plan.owner[name]] += 1
    main = counts.most_common(1)[0][0] if counts else "core"
    edits = []
    for node, name in refs:
        if name in KEPT_ATTRS:
            continue
        owner = plan.owner.get(name)
        if owner is None and name in plan.imported:
            owner = main if name in modules[main].imports else \
                next((m for m in MODULE_NAMES if name in modules[m].imports), None)
        if not owner:
            raise SystemExit(f"{path}:{node.lineno}: plugin_api.py has no {name}")
        edits.append((*span(node), f"{node.id}.{owner}"))
    return _apply(text, edits)


def fixed_test_text(path: str, text: str) -> str:
    for fixed_path, old, new in TEST_TEXT_FIXES:
        if fixed_path == path:
            if text.count(old) != 1:
                raise SystemExit(f"{path}: TEST_TEXT_FIXES expects one {old[:60]!r}")
            text = text.replace(old, new)
    return text


def rewrite_tests(ref: Optional[str], plan: Plan, modules: Dict[str, ModuleOut]) -> Dict[str, str]:
    tests = {path: analyze_test(path, fixed_test_text(path, read(path, ref))) for path in rewritten_paths(ref)}
    unknown = sorted({p for p, _, _ in TEST_TEXT_FIXES} - set(tests))
    if unknown:
        raise SystemExit(f"TEST_TEXT_FIXES names {unknown}, which aren't tests or probes")
    by_module = {Path(path).stem: path for path in tests}
    while True:
        module_holders = {stem: tests[path].holders[tests[path].top] for stem, path in by_module.items()}
        if not any([add_imported_holders(test, module_holders) for test in tests.values()]):
            break
    for test in tests.values():
        add_passed_holders(test)
    fixed = {p for p, _, _ in TEST_TEXT_FIXES}
    return {path: rewrite_test(test, plan, modules) for path, test in tests.items()
            if any(test.holders.values()) or path in fixed}


# --- Commands ------------------------------------------------------------------------------

def read(path: str, ref: Optional[str]) -> str:
    if ref is None:
        return (ROOT / path).read_text()
    return subprocess.run(["git", "-C", str(ROOT), "show", f"{ref}:{path}"], check=True, capture_output=True,
                          text=True).stdout


def rewritten_paths(ref: Optional[str]) -> List[str]:
    if ref is None:
        paths = [p.relative_to(ROOT).as_posix() for d in REWRITTEN_DIRS for p in sorted((ROOT / d).rglob("*.py"))]
    else:
        listed = subprocess.run(["git", "-C", str(ROOT), "ls-tree", "-r", "--name-only", ref, "--", *REWRITTEN_DIRS],
                                check=True, capture_output=True, text=True).stdout
        paths = [p for p in listed.splitlines() if p.endswith(".py")]
    return paths


def generate(ref: Optional[str]) -> Dict[str, str]:
    source = parse(read(PLUGIN_API, ref))
    plan = build_plan(source)
    problems = load_order_problems(plan)
    if problems:
        raise SystemExit("\n".join(problems))
    fixes = TextFixes(TEXT_FIXES)
    modules = build_modules(plan, fixes)
    fixes.verify()
    self_check(plan, modules, fixes)
    files = {f"{API_DIR}/{m}.py": out.text for m, out in modules.items()}
    files[f"{API_DIR}/__init__.py"] = package_init()
    files[PLUGIN_API] = plugin_api_text(source)
    files.update(rewrite_tests(ref, plan, modules))
    return files


def analyze(ref: Optional[str]) -> None:
    source = parse(read(PLUGIN_API, ref))
    plan = build_plan(source)
    fixes = TextFixes(TEXT_FIXES)
    modules = build_modules(plan, fixes)
    print(f"{'module':<22}{'lines':>7}{'KB':>7}  imports")
    for m in MODULE_NAMES:
        text = modules[m].text
        print(f"{m:<22}{text.count(chr(10)):>7}{len(text.encode()) / 1024:>7.1f}  {', '.join(sorted(modules[m].siblings))}")
    print()
    reads = import_time_references(plan)
    print("what each module uses from another while it loads:")
    for m in MODULE_NAMES:
        if any(other != "core" for _, other in reads.get(m, ())):
            print(f"  {m:<22} {', '.join(sorted(f'{o}.{n}' for n, o in reads[m] if o != 'core'))}")
    for problem in load_order_problems(plan):
        print("PROBLEM:", problem)


def main(argv: List[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("command", choices=["analyze", "write", "check"])
    parser.add_argument("ref", nargs="?", help="the git ref whose single file to split (default: the working tree)")
    args = parser.parse_args(argv[1:])
    if args.command == "analyze":
        analyze(args.ref)
        return 0
    if args.command == "check" and not args.ref:
        parser.error("check needs the ref the split was made from")
    files = generate(args.ref)
    if args.command == "write":
        for stale in (ROOT / API_DIR).glob("*.py"):
            if stale.relative_to(ROOT).as_posix() not in files:
                stale.unlink()
        for path, text in files.items():
            (ROOT / path).parent.mkdir(parents=True, exist_ok=True)
            (ROOT / path).write_text(text)
        print(f"wrote {len(files)} files")
        return 0
    differ = []
    for path, text in sorted(files.items()):
        have = (ROOT / path).read_text() if (ROOT / path).exists() else ""
        if have != text:
            differ.append(path)
            sys.stdout.writelines(difflib.unified_diff(text.splitlines(True), have.splitlines(True),
                                                       f"split of {args.ref}:{path}", path, n=1))
    extra = sorted(p.relative_to(ROOT).as_posix() for p in (ROOT / API_DIR).glob("*.py")
                   if p.relative_to(ROOT).as_posix() not in files)
    for path in extra:
        print(f"{path}: not one the split makes")
    if differ or extra:
        return 1
    print(f"the {len(files)} files are what the split of {args.ref} makes")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
