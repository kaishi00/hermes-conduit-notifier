"""Memory and Personality: what this host's Hermes remembers, and the profile's
SOUL.md.

Memory: Gemini Live can see what this host's Hermes remembers without Conduit
knowing the backend: the built-in MEMORY.md / USER.md snapshot plus whatever
external provider `memory.provider` names (Honcho, Mem0, …). Read-only.

Personality: the profile's SOUL.md as Hermes loads it, so voice keeps the
agent's personality. Read-only.
"""

from __future__ import annotations

import asyncio
import atexit
import contextvars
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Callable, Dict, Optional

from fastapi import HTTPException, Request, Response

from . import core

logger = core.logger
router = core.router


# --- Memory -----------------------------------------------------------------
#
# Read-only view of the host's memory for Gemini Live: never sync_turn, never
# a write. The built-in store is read fresh per request (it is a couple of
# small files); an external provider is initialized once per profile and kept,
# since initializing one can open network clients.

MEMORY_CONTEXT_MAX_CHARS = 8000
MEMORY_RECALL_MAX_CHARS = 4000
MEMORY_MAX_QUERY_CHARS = 500
# A voice turn recalls at most once; cap a looping client well above that.
MEMORY_RECALL_LIMIT = 30
MEMORY_RECALL_WINDOW_S = 60.0
# The context route starts the provider and reads files: generous, but bounded.
MEMORY_CONTEXT_LIMIT = 30
MEMORY_CONTEXT_WINDOW_S = 60.0
# Budget for one request, provider start included. Each provider call runs on
# its own thread, so a hung call is abandoned rather than holding a worker.
MEMORY_TIMEOUT_S = 10.0
MEMORY_WORKERS = 8
# Requests one profile may have running or queued at once, so a slow backend
# can't fill the shared pool for every profile.
MEMORY_MAX_IN_FLIGHT = 2
# How long provider shutdown may take when the process exits.
MEMORY_SHUTDOWN_TIMEOUT_S = 5.0
MEMORY_MAX_BODY_BYTES = 16 * 1024
# A stable session, so providers that key recall state per session reuse it.
MEMORY_SESSION_ID = "conduit-voice"
MEMORY_PLATFORM = "conduit_voice"
# An unavailable or failed provider is retried after this long, so fixing its
# credentials doesn't need a dashboard restart.
MEMORY_PROVIDER_RETRY_S = 60.0
# Same defaults Hermes' agent_init uses for the built-in store.
_BUILTIN_MEMORY_CHAR_LIMIT = 2200
_BUILTIN_USER_CHAR_LIMIT = 1375
_MEMORY_FAILURE = "The memory provider failed; the Hermes log has the details"

_MISSING_MEMORY_MODULES = frozenset({
    "hermes_cli", "hermes_cli.config", "hermes_constants",
    "tools", "tools.memory_tool", "tools.memory_tool_store",
    "agent", "agent.memory_provider", "plugins", "plugins.memory",
})


def _is_missing_memory_modules(exc: ImportError) -> bool:
    # A Hermes without the memory modules, or an older one whose modules lack
    # a name this imports (ImportError.name is then that module). A broken
    # import deeper inside Hermes is a real failure.
    return exc.name in _MISSING_MEMORY_MODULES


def _clip_block(value: Any, limit: int) -> str:
    """Like _clip, but keeps line structure: memory blocks are formatted text."""
    text = str(value or "").strip()
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


def _hermes_memory_config() -> Dict[str, Any]:
    """The active profile's Hermes config (profile-scoped by _run_scoped)."""
    from hermes_cli.config import load_config

    config = load_config()
    return config if isinstance(config, dict) else {}


def _int_or(value: Any, default: int) -> int:
    try:
        return max(int(value), 0)
    except (TypeError, ValueError, OverflowError):
        return default


def _hermes_builtin_memory(config: Dict[str, Any]) -> str:
    """MEMORY.md + USER.md exactly as Hermes renders them into its system prompt."""
    from tools.memory_tool import (
        MemoryStore, get_builtin_memory_config, get_builtin_memory_store_flags, get_memory_dir)

    section = get_builtin_memory_config(config)
    section = section if isinstance(section, dict) else {}
    memory_on, user_on = get_builtin_memory_store_flags(config)
    # load_from_disk creates the memories dir; nothing stored means nothing to read.
    if not (memory_on or user_on) or not get_memory_dir().is_dir():
        return ""
    store = MemoryStore(
        _int_or(section.get("memory_char_limit"), _BUILTIN_MEMORY_CHAR_LIMIT),
        _int_or(section.get("user_char_limit"), _BUILTIN_USER_CHAR_LIMIT),
        memory_enabled=memory_on,
        user_profile_enabled=user_on,
    )
    store.load_from_disk()
    blocks = [store.format_for_system_prompt(kind) for on, kind in ((memory_on, "memory"), (user_on, "user")) if on]
    return "\n\n".join(b for b in blocks if b)


def _configured_memory_provider(config: Dict[str, Any]) -> Optional[str]:
    """The external provider ``memory.provider`` names, or None for the built-in store."""
    section = config.get("memory") if isinstance(config, dict) else None
    name = str((section or {}).get("provider") or "").strip() if isinstance(section, dict) else ""
    if not name:
        return None
    try:
        from agent.memory_provider import is_core_memory_provider
    except ImportError as exc:
        if not _is_missing_memory_modules(exc):
            raise
        return None  # a Hermes without external providers
    return None if is_core_memory_provider(name) else name


def _hermes_load_memory_provider(name: str) -> Any:
    from plugins.memory import load_memory_provider

    return load_memory_provider(name)


def _memory_provider_init_kwargs() -> Dict[str, Any]:
    """What agent_init's _memory_provider_init_kwargs gives a provider, minus the agent."""
    from hermes_constants import get_hermes_home

    kwargs: Dict[str, Any] = {
        "platform": MEMORY_PLATFORM,
        "hermes_home": str(get_hermes_home()),
        # Some providers skip recall as well as writes outside "primary". The
        # recall provider never writes (no sync_turn, no memory tool), so it is
        # safe there; a call's end needs it to write the call to memory.
        "agent_context": "primary",
    }
    try:
        from hermes_cli.profiles import get_active_profile_name

        kwargs["agent_identity"] = get_active_profile_name()
        kwargs["agent_workspace"] = "hermes"
    except Exception:  # noqa: BLE001 — identity is optional scoping
        logger.debug("Could not name the active profile for the memory provider", exc_info=True)
    return kwargs


def _start_memory_provider(name: str) -> Any:
    """A loaded, available, initialized provider, or None. Never raises."""
    try:
        provider = _hermes_load_memory_provider(name)
        if provider is None:
            logger.info("Memory provider %r for Conduit voice is not installed", name)
            return None
        if not provider.is_available():
            logger.info("Memory provider %r for Conduit voice is not available", name)
            return None
        provider.initialize(session_id=MEMORY_SESSION_ID, **_memory_provider_init_kwargs())
        return provider
    except Exception:  # noqa: BLE001 — a provider must never break the route
        logger.warning("Memory provider %r failed to start for Conduit voice", name, exc_info=True)
        return None


def _memory_cache_key(profile: Optional[str]) -> str:
    """The provider cache key: the profile's resolved Hermes home, exactly.

    Runs inside _run_scoped. Unlike _limiter_key it doesn't fold case, so on a
    case-sensitive host two profiles never share a provider.
    """
    try:
        from hermes_constants import get_hermes_home

        return str(get_hermes_home().resolve())
    except ImportError as exc:
        if not _is_missing_memory_modules(exc):
            raise
    name = (profile or "").strip()
    return "" if name.lower() in ("", "current") else name


_MEMORY_BUSY = "The memory provider is not responding; try again shortly"


class _ProviderEntry:
    """One profile's provider. Every call into it (initialize, prefetch,
    shutdown) holds ``call_lock``, so they run one at a time, and runs on its
    own thread with a deadline, so a hung call is abandoned, not waited on."""

    def __init__(self, name: str) -> None:
        self.name = name
        self.provider: Any = None
        self.started = False
        self.started_at = 0.0
        self.call_lock = threading.Lock()
        self._state = threading.Lock()
        self._running = False
        self.stuck = False  # a call outlived its deadline and still holds call_lock

    def call(self, fn: Callable[[], Any], deadline: float, what: str) -> Any:
        if self.stuck:
            raise core.TokenError(503, _MEMORY_BUSY)
        if not self.call_lock.acquire(timeout=max(deadline - time.monotonic(), 0.0)):
            raise core.TokenError(503 if self.stuck else 504, _MEMORY_BUSY if self.stuck else f"Memory {what} timed out")
        box: Dict[str, Any] = {}
        done = threading.Event()
        # The profile scope lives in contextvars; the call thread must see it.
        ctx = contextvars.copy_context()

        def run() -> None:
            try:
                box["value"] = ctx.run(fn)
            except BaseException as exc:  # noqa: BLE001 — re-raised on the caller's thread
                box["error"] = exc
            finally:
                with self._state:
                    self._running = False
                    self.stuck = False
                    done.set()
                self.call_lock.release()

        with self._state:
            self._running = True
        try:
            threading.Thread(target=run, name=f"conduit-memory-{what}", daemon=True).start()
        except BaseException:
            with self._state:
                self._running = False
            self.call_lock.release()
            raise
        if not done.wait(max(deadline - time.monotonic(), 0.0)):
            with self._state:
                completed = done.is_set()
                if not completed and self._running:
                    self.stuck = True
            if not completed:
                logger.warning("Memory provider %r %s for Conduit timed out; abandoning the call", self.name, what)
                raise core.TokenError(504, f"Memory {what} timed out")
        if "error" in box:
            raise box["error"]
        return box.get("value")


def _stop_memory_provider(entry: _ProviderEntry, timeout: float = MEMORY_SHUTDOWN_TIMEOUT_S) -> None:
    if entry.provider is None:
        return
    try:
        entry.call(entry.provider.shutdown, time.monotonic() + timeout, "shutdown")
    except Exception:  # noqa: BLE001
        logger.warning("Memory provider %r shutdown failed", entry.name, exc_info=True)


class _MemoryProviders:
    """One initialized provider per profile, rebuilt when memory.provider changes.

    The registry lock only guards the dict; it is never held across provider
    I/O. The first request for a profile starts its provider under that
    entry's call lock, and concurrent requests wait on it with their deadline.
    """

    def __init__(self, clock: Callable[[], float] = time.monotonic) -> None:
        self.clock = clock
        self._entries: Dict[str, _ProviderEntry] = {}
        self._in_flight: Dict[str, int] = {}
        self._lock = threading.Lock()

    def _drop(self, entry: _ProviderEntry) -> None:
        # Shut down off the request path: it waits for the entry's calls.
        threading.Thread(target=_stop_memory_provider, args=(entry,), name="conduit-memory-stop", daemon=True).start()

    def get(self, key: str, name: Optional[str], deadline: Optional[float] = None,
            start: Optional[Callable[[str], Any]] = None) -> Optional[_ProviderEntry]:
        """The started entry for ``key``, or None when no provider is usable."""
        deadline = time.monotonic() + MEMORY_TIMEOUT_S if deadline is None else deadline
        with self._lock:
            entry = self._entries.get(key)
            if entry is not None and (entry.name != name or (
                    entry.started and entry.provider is None
                    and self.clock() - entry.started_at >= MEMORY_PROVIDER_RETRY_S)):
                del self._entries[key]
                self._drop(entry)
                entry = None
            if name is None:
                return None
            if entry is None:
                entry = self._entries[key] = _ProviderEntry(name)
        if not entry.started:
            def begin() -> None:
                if not entry.started:  # another request may have started it meanwhile
                    entry.provider = (start or _start_memory_provider)(name)
                    entry.started_at = self.clock()
                    entry.started = True

            entry.call(begin, deadline, "start")
        return entry if entry.provider is not None else None

    def enter(self, key: str) -> None:
        with self._lock:
            if self._in_flight.get(key, 0) >= MEMORY_MAX_IN_FLIGHT:
                raise core.TokenError(503, _MEMORY_BUSY)
            self._in_flight[key] = self._in_flight.get(key, 0) + 1

    def leave(self, key: str) -> None:
        with self._lock:
            count = self._in_flight.get(key, 0) - 1
            if count > 0:
                self._in_flight[key] = count
            else:
                self._in_flight.pop(key, None)

    def clear(self) -> None:
        with self._lock:
            entries, self._entries = list(self._entries.values()), {}
        for entry in entries:
            _stop_memory_provider(entry)


_memory_providers = _MemoryProviders()
_memory_limiter = core._MintLimiter(MEMORY_RECALL_LIMIT, MEMORY_RECALL_WINDOW_S,
                               message="Too many memory lookups; try again shortly")
_memory_context_limiter = core._MintLimiter(MEMORY_CONTEXT_LIMIT, MEMORY_CONTEXT_WINDOW_S,
                                       message="Too many memory requests; try again shortly")
_memory_executor = ThreadPoolExecutor(max_workers=MEMORY_WORKERS, thread_name_prefix="conduit-memory")
# Let providers flush and close their clients when the dashboard exits.
atexit.register(lambda: _memory_providers.clear())


def memory_context(key: str = "", limiter_key: Optional[str] = None) -> Dict[str, Any]:
    """What Hermes injects into its own system prompt from memory, for Gemini Live."""
    deadline = time.monotonic() + MEMORY_TIMEOUT_S
    if limiter_key is not None:
        _memory_context_limiter.acquire(limiter_key)
    try:
        config = _hermes_memory_config()
        builtin = _hermes_builtin_memory(config)
        name = _configured_memory_provider(config)
    except ImportError as exc:
        if not _is_missing_memory_modules(exc):
            raise
        return {"available": False, "reason": "unsupported", "provider": None, "recall": False, "context": ""}
    entry = _memory_providers.get(key, name, deadline)
    # Not the provider's system_prompt_block(): that is mostly instructions
    # for its own tools, which the voice model can't call. Its memory is
    # reached through recall instead.
    context = _clip_block(builtin, MEMORY_CONTEXT_MAX_CHARS) if builtin else ""
    recall = entry is not None
    label = name if recall else ("builtin" if builtin else None)
    result: Dict[str, Any] = {"available": bool(context) or recall, "provider": label, "recall": recall, "context": context}
    if not result["available"]:
        result["reason"] = "disabled"
    return result


def run_memory_recall(query: Any, key: str = "", limiter_key: Optional[str] = None) -> Dict[str, Any]:
    """The external provider's recall for ``query``; built-in memory is already in the context."""
    deadline = time.monotonic() + MEMORY_TIMEOUT_S
    raw_query = str(query or "")
    # Bounded before normalizing, so a huge body isn't tokenized first.
    if len(raw_query) > MEMORY_MAX_QUERY_CHARS * 4:
        raise core.TokenError(400, f"query is longer than {MEMORY_MAX_QUERY_CHARS} characters")
    query = " ".join(raw_query.split())
    if not query:
        raise core.TokenError(400, "query is required")
    if len(query) > MEMORY_MAX_QUERY_CHARS:
        raise core.TokenError(400, f"query is longer than {MEMORY_MAX_QUERY_CHARS} characters")
    if limiter_key is not None:
        _memory_limiter.acquire(limiter_key)
    try:
        name = _configured_memory_provider(_hermes_memory_config())
    except ImportError as exc:
        if not _is_missing_memory_modules(exc):
            raise
        name = None
    entry = _memory_providers.get(key, name, deadline)
    if entry is None:
        return {"available": False, "results": ""}
    provider = entry.provider
    try:
        text = entry.call(lambda: provider.prefetch(query, session_id=MEMORY_SESSION_ID), deadline, "recall")
    except core.TokenError:
        raise
    except Exception:  # noqa: BLE001 — its text can carry hosts or keys
        logger.warning("Memory provider %r recall for Conduit failed", name, exc_info=True)
        raise core.TokenError(502, _MEMORY_FAILURE)
    return {"available": True, "results": _clip_block(text, MEMORY_RECALL_MAX_CHARS)}


def _memory_job(profile: Optional[str], job: Callable[[str, str], Dict[str, Any]]) -> Callable[[], Dict[str, Any]]:
    """Wrap ``job(cache_key, limiter_key)`` with the per-profile in-flight cap."""
    def run() -> Dict[str, Any]:
        key = _memory_cache_key(profile)
        _memory_providers.enter(key)
        try:
            return job(key, core._limiter_key(profile))
        finally:
            _memory_providers.leave(key)

    return run


async def _run_memory(profile: Optional[str], fn: Callable[[], Dict[str, Any]], what: str) -> Dict[str, Any]:
    try:
        # A backstop: the provider calls enforce MEMORY_TIMEOUT_S themselves.
        return await asyncio.wait_for(core._run_scoped(profile, fn, _memory_executor), timeout=MEMORY_TIMEOUT_S + 2.0)
    except asyncio.TimeoutError:
        logger.warning("Memory %s for Conduit timed out after %ss", what, MEMORY_TIMEOUT_S)
        raise HTTPException(status_code=504, detail=f"Memory {what} timed out", headers={"Cache-Control": "no-store"})
    except core.TokenError as exc:
        logger.warning("Memory %s for Conduit failed: %s", what, exc)
        raise HTTPException(status_code=exc.status, detail=str(exc), headers={"Cache-Control": "no-store"})
    except HTTPException as exc:
        exc.headers = {**(exc.headers or {}), "Cache-Control": "no-store"}
        raise
    except Exception as exc:
        raise core._unexpected(what, exc, feature="Memory")


@router.get("/memory/context")
async def get_memory_context(response: Response, profile: Optional[str] = None) -> Dict[str, Any]:
    response.headers["Cache-Control"] = "no-store"
    job = _memory_job(profile, lambda key, limiter_key: memory_context(key, limiter_key=limiter_key))
    return {"ok": True, **(await _run_memory(profile, job, "context"))}


@router.post("/memory/recall")
async def post_memory_recall(request: Request, response: Response, profile: Optional[str] = None) -> Dict[str, Any]:
    response.headers["Cache-Control"] = "no-store"
    body = await core._read_json_body(request, MEMORY_MAX_BODY_BYTES)
    query = body.get("query") if isinstance(body, dict) else None
    job = _memory_job(profile, lambda key, limiter_key: run_memory_recall(query, key, limiter_key=limiter_key))
    return {"ok": True, **(await _run_memory(profile, job, "recall"))}


# --- Personality ------------------------------------------------------------
#
# The profile's SOUL.md as Hermes itself loads it (injection scan, legacy
# protocol stripped), so a voice model can keep the agent's personality.
# Read-only; the voice-side rules against stage directions live in the
# pre_llm_call hook and in Conduit's own voice prompt.
PERSONALITY_MAX_CHARS = 8000
PERSONALITY_LIMIT = 30
PERSONALITY_WINDOW_S = 60.0
PERSONALITY_TIMEOUT_S = 10.0
PERSONALITY_WORKERS = 2

_MISSING_PERSONALITY_MODULES = frozenset({"agent", "agent.prompt_builder", "hermes_constants"})

# Its own small pool: a hung filesystem read can't pin the default executor
# or the memory and search pools.
_personality_executor = ThreadPoolExecutor(max_workers=PERSONALITY_WORKERS, thread_name_prefix="conduit-personality")
_personality_limiter = core._MintLimiter(PERSONALITY_LIMIT, PERSONALITY_WINDOW_S,
                                    message="Too many personality requests; try again shortly")


def _hermes_soul_md() -> Optional[str]:
    """SOUL.md for the active profile (profile-scoped by _run_scoped), or None."""
    from agent.prompt_builder import load_soul_md
    from hermes_constants import get_hermes_home

    # Pin the home explicitly: this runs on a worker thread, and Hermes'
    # ambient lookup there could fall back to the launch profile.
    try:
        return load_soul_md(home_override=get_hermes_home())
    except TypeError:
        # A Hermes whose load_soul_md predates home_override: the profile
        # scope _run_scoped entered is what its ambient lookup reads.
        return load_soul_md()


def personality(limiter_key: Optional[str] = None) -> Dict[str, Any]:
    """The agent's SOUL.md for Conduit voice; ``available`` is false when there is none."""
    if limiter_key is not None:
        _personality_limiter.acquire(limiter_key)
    try:
        soul = _hermes_soul_md()
    except ImportError as exc:
        # An older Hermes without load_soul_md (or without Hermes at all).
        if exc.name not in _MISSING_PERSONALITY_MODULES:
            raise
        return {"available": False, "text": ""}
    text = _clip_block(soul, PERSONALITY_MAX_CHARS) if soul else ""
    return {"available": bool(text), "text": text}


@router.get("/personality")
async def get_personality(response: Response, profile: Optional[str] = None) -> Dict[str, Any]:
    response.headers["Cache-Control"] = "no-store"
    try:
        result = await asyncio.wait_for(
            core._run_scoped(profile, lambda: personality(limiter_key=core._limiter_key(profile)), _personality_executor),
            timeout=PERSONALITY_TIMEOUT_S,
        )
    except asyncio.TimeoutError:
        logger.warning("Personality for Conduit timed out after %ss", PERSONALITY_TIMEOUT_S)
        raise HTTPException(status_code=504, detail="Personality timed out", headers={"Cache-Control": "no-store"})
    except core.TokenError as exc:
        logger.warning("Personality for Conduit failed: %s", exc)
        raise HTTPException(status_code=exc.status, detail=str(exc), headers={"Cache-Control": "no-store"})
    except HTTPException as exc:
        exc.headers = {**(exc.headers or {}), "Cache-Control": "no-store"}
        raise
    except Exception as exc:
        raise core._unexpected("request", exc, feature="Personality")
    return {"ok": True, **result}
