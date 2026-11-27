"""What each running chat is doing now, for Conduit's bots lists (Eric, 2026-11-26).

Hermes streams a turn's tool progress only to the client attached to that
chat, and its live registry (``session.active_list``) says a chat is working
but not on what. The plugin's hooks see every turn of every chat, so they
note each running turn's step here: its phase (thinking, a tool, waiting on
an approval or on the user's answer) and, for a tool, its category, the same
coarse one the Lock Screen shows (live_activity.tool_category: never a tool's
name, arguments or output). Conduit and Conduit for desktop read it with GET
/live-steps and show the step under the bot's name ("Running a command").

The hooks run in the gateway; the route runs in the dashboard. So the steps
are published per Hermes home in ``conduit-live-steps.json``: the running
turns' steps by session id, and when the file was written. An app shows a
step only for a chat the live registry also says is working, so a file left
behind by a gateway that stopped mid-turn shows nothing; a reader drops a
file older than STALE_S too.

A turn's delegate agents ride its step (``agents``): each one ``delegate_task``
started while the turn runs, with its goal and its own step, from the hooks
of the child's own calls, until it stops. A turn that waits on them shows
nothing else, and Hermes streams their progress only to the client that
started the turn, so this is how a chat opened elsewhere (an API chat's turn,
another app's) sees them working (Eric, 2026-11-27). The bots lists
read only their count; a chat's own read (GET /sessions/live-work) reads them.

Standalone (no imports from the plugin package): the dashboard loads it from
its file, the plugin imports it.
"""

from __future__ import annotations

import json
import math
import os
import tempfile
import threading
import time
from pathlib import Path
from typing import Any, Callable

FILE_NAME = "conduit-live-steps.json"
# A file this old is from a gateway that stopped: nothing in it is running.
STALE_S = 12 * 60 * 60.0
# At most this many running chats per home; the oldest step goes first.
MAX_SESSIONS = 64
PHASES = ("thinking", "tool", "approval", "input")
# At most this many delegate agents per turn; the oldest goes first.
MAX_AGENTS = 8
# A delegate agent's goal, as the user reads it in their chat: its first line, this long at most.
GOAL_MAX = 200

_lock = threading.Lock()
# home -> session id -> {"phase", "tool", "since"}; session id -> home.
_steps: dict[str, dict[str, dict[str, Any]]] = {}
_home_of: dict[str, str] = {}
# What each home's file says now, so an unchanged step writes nothing.
_written: dict[str, str] = {}
# A running turn's delegate agent (its child session id) -> the turn's session id.
_parent_of: dict[str, str] = {}

# Which noted turn a session id continues through compactions: the noted id,
# False when none, None when it can't tell yet (live_activity.compacted_from,
# set by the plugin). A compaction mid-turn gives the turn a new session id,
# and its step moves to that id, the one the live registry then lists.
lineage: Callable[[str, set[str]], str | bool | None] | None = None
# Ids that continue no noted turn (a session's lineage never changes), and
# ids the store couldn't place yet -> when to look again.
MAX_LINEAGE_IDS = 1024
UNSETTLED_RETRY_S = 30.0
_not_moved: set[str] = set()
_unsettled: dict[str, float] = {}


def _moved(session_id: str, recheck: bool = False) -> str | None:
    """The home of the noted turn ``session_id`` continues through a compaction,
    with the turn's step moved to ``session_id``; None when it continues none.
    ``recheck`` asks the store even inside the retry wait: a turn's end has no
    later hook to put it right."""
    with _lock:
        if session_id in _home_of or session_id in _not_moved or not _home_of:
            return None
        if not recheck and time.monotonic() < _unsettled.get(session_id, 0.0):
            return None
        running = set(_home_of)
        find = lineage
    if find is None:
        return None
    try:
        found = find(session_id, running)
    except Exception:  # noqa: BLE001 — never into the turn
        found = None  # a locked or broken store: asked again later, not on every hook
    with _lock:
        if found is False:
            _unsettled.pop(session_id, None)
            if len(_not_moved) >= MAX_LINEAGE_IDS:
                _not_moved.clear()
            _not_moved.add(session_id)
            return None
        if not isinstance(found, str) or not found:
            if len(_unsettled) >= MAX_LINEAGE_IDS:
                _unsettled.pop(next(iter(_unsettled)))
            _unsettled[session_id] = time.monotonic() + UNSETTLED_RETRY_S
            return None
        key = _home_of.get(found)
        if key is None or session_id in _home_of:
            return None
        steps = _steps.get(key, {})
        entry = steps.pop(found, None)
        _home_of.pop(found, None)
        if entry is None:
            return None
        steps[session_id] = entry
        _home_of[session_id] = key
        # Its delegate agents go with it: their steps and stops find the turn under its new id.
        for child in entry.get("agents") or {}:
            if _parent_of.get(child) == found:
                _parent_of[child] = session_id
        return key


def started(home: Any, session_id: str, *, now: float | None = None) -> None:
    """A turn of ``session_id`` started in ``home``: it's thinking."""
    if not session_id or home is None:
        return
    key = str(home)
    with _lock:
        old = _home_of.get(session_id)
        if old is not None and old != key:
            _steps.get(old, {}).pop(session_id, None)
        _home_of[session_id] = key
        steps = _steps.setdefault(key, {})
        _drop_agents(steps.pop(session_id, None))
        steps[session_id] = {"phase": "thinking", "tool": None, "since": time.time() if now is None else now}
        while len(steps) > MAX_SESSIONS:
            oldest = next(iter(steps))
            _drop_agents(steps.pop(oldest))
            _home_of.pop(oldest, None)
        homes = {key} | ({old} if old is not None and old != key else set())
    for name in homes:
        _publish(name)


def step(session_id: str, phase: str, tool: str | None = None, *, now: float | None = None) -> None:
    """A running turn moved on: thinking, a tool (by category), or waiting on an approval or an answer.
    A delegate agent's step goes to its turn's ``agents``; any other session with no turn noted here
    (a fork) is left out."""
    if not session_id or phase not in PHASES:
        return
    if agent_step(session_id, phase, tool, now=now):
        return
    moved = _moved(session_id)
    with _lock:
        key = _home_of.get(session_id)
        entry = _steps.get(key or "", {}).get(session_id)
        if entry is None:
            return
        tool = tool if phase == "tool" else None
        if entry["phase"] == phase and entry["tool"] == tool:
            if moved is None:
                return
        else:
            entry.update(phase=phase, tool=tool, since=time.time() if now is None else now)
    # Outside the lock: _publish takes it. A step moved to its compacted id is published even when unchanged.
    _publish(key)


def ended(session_id: str) -> None:
    """The turn of ``session_id`` is over, however it ended."""
    if not session_id:
        return
    _moved(session_id, recheck=True)
    with _lock:
        key = _home_of.pop(session_id, None)
        entry = _steps.get(key or "", {}).pop(session_id, None)
        if key is None or entry is None:
            return
        _drop_agents(entry)
    _publish(key)


def _drop_agents(entry: dict[str, Any] | None) -> None:
    """Forgets the parent links of a turn's delegate agents; the caller holds the lock."""
    for child in (entry or {}).get("agents", {}):
        _parent_of.pop(child, None)


def _goal(value: Any) -> str:
    lines = [line.strip() for line in str(value or "").splitlines() if line.strip()]
    text = lines[0] if lines else ""
    return text if len(text) <= GOAL_MAX else text[:GOAL_MAX - 1].rstrip() + "…"


def agent_started(parent_session_id: str, child_session_id: str, goal: Any = None, *,
                  now: float | None = None) -> None:
    """``delegate_task`` started a delegate agent for the running turn of ``parent_session_id``.
    One started for a session with no turn noted here (a background agent outliving its turn,
    a nested one) is left out: the chat's own roster shows those."""
    if not parent_session_id or not child_session_id:
        return
    _moved(parent_session_id)  # a turn a compaction moved to this id
    with _lock:
        key = _home_of.get(parent_session_id)
        entry = _steps.get(key or "", {}).get(parent_session_id)
        if entry is None:
            return
        at = time.time() if now is None else now
        agents = entry.setdefault("agents", {})
        agents.pop(child_session_id, None)
        agents[child_session_id] = {"goal": _goal(goal), "phase": "thinking", "tool": None,
                                    "since": at, "started": at}
        _parent_of[child_session_id] = parent_session_id
        while len(agents) > MAX_AGENTS:
            oldest = next(iter(agents))
            agents.pop(oldest)
            _parent_of.pop(oldest, None)
    _publish(key)


def agent_step(child_session_id: str, phase: str, tool: str | None = None, *, now: float | None = None) -> bool:
    """A running turn's delegate agent moved on; False when ``child_session_id`` is none of them."""
    with _lock:
        parent = _parent_of.get(child_session_id)
        if parent is None:
            return False
        key = _home_of.get(parent)
        agent = _steps.get(key or "", {}).get(parent, {}).get("agents", {}).get(child_session_id)
        if agent is None:
            _parent_of.pop(child_session_id, None)
            return False
        tool = tool if phase == "tool" else None
        if agent["phase"] == phase and agent["tool"] == tool:
            return True
        agent.update(phase=phase, tool=tool, since=time.time() if now is None else now)
    _publish(key)
    return True


def agent_ended(child_session_id: str) -> None:
    """A delegate agent stopped, however it ended: it leaves its turn's step."""
    if not child_session_id:
        return
    with _lock:
        parent = _parent_of.pop(child_session_id, None)
        key = _home_of.get(parent or "")
        entry = _steps.get(key or "", {}).get(parent or "")
        agents = (entry or {}).get("agents", {})
        if agents.pop(child_session_id, None) is None:
            return
        if not agents:
            entry.pop("agents", None)
    _publish(key)


def forget() -> None:
    """Forgets every step (the plugin was unloaded); the files go when the next turn writes."""
    with _lock:
        _steps.clear()
        _home_of.clear()
        _written.clear()
        _not_moved.clear()
        _unsettled.clear()
        _parent_of.clear()


def _publish(key: str | None) -> None:
    if key is None:
        return
    with _lock:
        steps = dict(_steps.get(key, {}))
        body = json.dumps({"steps": steps, "at": time.time()}, separators=(",", ":"), sort_keys=True)
        signature = json.dumps(steps, sort_keys=True)
        if _written.get(key) == signature:
            return
        _written[key] = signature
    path = Path(key) / FILE_NAME
    if not steps:
        try:
            path.unlink()
        except FileNotFoundError:
            pass
        return
    # A fresh, private (0600) temp file of its own, never one already there.
    descriptor, temporary = tempfile.mkstemp(dir=path.parent, prefix=f".{FILE_NAME}.", suffix=".tmp")
    try:
        try:
            handle = os.fdopen(descriptor, "w", encoding="utf-8")
        except BaseException:
            os.close(descriptor)
            raise
        with handle:
            handle.write(body)
        os.replace(temporary, path)
    except BaseException:
        with _lock:
            _written.pop(key, None)  # the next change tries again
        try:
            os.unlink(temporary)
        except OSError:
            pass
        raise


def _number(value: Any) -> float | None:
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        return None
    try:
        return float(value) if math.isfinite(value) else None
    except OverflowError:
        return None


def _agents(value: Any) -> list[dict[str, Any]]:
    """A step's delegate agents, oldest first: ``id`` (the child's session), ``goal``, ``phase``,
    ``tool``, ``since`` (when its step began) and ``started``."""
    if not isinstance(value, dict):
        return []
    found = []
    for child, agent in list(value.items()):
        if not isinstance(child, str) or not child or not isinstance(agent, dict):
            continue
        phase = agent.get("phase")
        if phase not in PHASES:
            continue
        tool = agent.get("tool")
        goal = agent.get("goal")
        found.append({"id": child, "goal": goal if isinstance(goal, str) else "", "phase": phase,
                      "tool": tool if phase == "tool" and isinstance(tool, str) else None,
                      "since": _number(agent.get("since")), "started": _number(agent.get("started"))})
    # The file keeps them in id order (sort_keys): oldest first by when each started.
    found.sort(key=lambda agent: agent["started"] if agent["started"] is not None else float("inf"))
    return found[:MAX_AGENTS]


def read(home: Any, *, now: float | None = None, agents: bool = False) -> dict[str, dict[str, Any]]:
    """The running chats' steps in ``home`` by session id: ``phase``, ``tool`` (a category, for a
    tool) and ``since`` (when the step began), and ``agents`` when the turn has delegate agents
    running: how many, or with ``agents=True`` each one (``_agents``). Anything unreadable, or a
    stale file, is none."""
    try:
        raw = json.loads((Path(home) / FILE_NAME).read_text(encoding="utf-8"))
    except (OSError, ValueError, RecursionError):
        return {}
    if not isinstance(raw, dict) or not isinstance(raw.get("steps"), dict):
        return {}
    at = _number(raw.get("at"))
    now = time.time() if now is None else now
    if at is None or not 0 <= now - at <= STALE_S:
        return {}
    answer: dict[str, dict[str, Any]] = {}
    for session_id, entry in list(raw["steps"].items())[:MAX_SESSIONS]:
        if not isinstance(session_id, str) or not session_id or not isinstance(entry, dict):
            continue
        phase = entry.get("phase")
        if phase not in PHASES:
            continue
        tool = entry.get("tool")
        answer[session_id] = {"phase": phase, "tool": tool if phase == "tool" and isinstance(tool, str) else None,
                              "since": _number(entry.get("since"))}
        running = _agents(entry.get("agents"))
        if running:
            answer[session_id]["agents"] = running if agents else len(running)
    return answer
