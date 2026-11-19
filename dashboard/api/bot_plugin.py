"""A bot's Conduit plugin: whether a profile has it on, and turning it on there
(hermes-conduit#2803)."""

from __future__ import annotations

import asyncio
import os
import re
import subprocess
import sys
from pathlib import Path
from typing import Any, Dict, Optional

from fastapi import HTTPException, Request, Response

from . import core, self_update

logger = core.logger
router = core.router


# --- A bot's Conduit plugin ------------------------------------------------------
#
# Hermes answers every plugin request for a profile that hasn't turned the
# plugin on with "Plugin not found", and a profile made fresh (New Bot without
# Start From, `hermes profile create` without --clone) hasn't. Hermes' own
# plugin switches act on the dashboard's profile only, and `hermes -p <name>
# plugins enable` finds only a plugin in that profile's own plugins/ folder,
# which a fresh profile doesn't have. So Conduit asks this dashboard, its own
# profile's copy of the plugin, to do what the README tells a host to: link the
# profile's plugins/conduit_push to the main install (profile_copies.py), then
# run `hermes -p <name> plugins enable conduit_push`, Hermes' own enable, which
# writes the profile's plugins.enabled and has the running gateway load it.
# New Bot calls it for every bot it makes, Edit Bot's Turn On for one that has
# it off. GET says which.

BOT_PLUGIN_ENABLE_TIMEOUT_S = 90.0
# Hermes' profile ids (hermes_constants.PROFILE_ID_RE): checked here too, so a
# name never reaches the command line as anything but a profile id.
_PROFILE_ID = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")


def _profile_home(name: str) -> Path:
    """An existing profile's home, or HTTPException."""
    name = (name or "").strip()
    if not _PROFILE_ID.match(name) or name == "default":
        raise HTTPException(status_code=400, detail="Name a bot's profile", headers={"Cache-Control": "no-store"})
    try:
        home = core._profile_dir(name)
    except Exception:  # noqa: BLE001 — Hermes can't place the name
        home = None
    if home is None or not Path(home).is_dir():
        raise HTTPException(status_code=404, detail=f"No profile named {name}", headers={"Cache-Control": "no-store"})
    return Path(home)


def _refuse_other_ticket(request: Request, name: str) -> None:
    """A Hermes Desktop ticket is one profile's principal: it may turn the plugin
    on for its own profile, never another's."""
    ticket_home = core._native_ticket_home(request)
    if ticket_home is not None and not core._job_profile_on_home(name, ticket_home):
        raise HTTPException(status_code=403, detail=core.CONDUIT_PROFILE_TICKET_REASON,
                            headers={"Cache-Control": "no-store"})


def _hermes_root() -> Optional[Path]:
    """The Hermes root that holds every profile, None on a Hermes without profiles."""
    try:
        from hermes_constants import get_default_hermes_root
    except (ImportError, AttributeError):  # an older Hermes: one home, no profiles
        return None
    return Path(os.path.abspath(get_default_hermes_root()))


def _main_install() -> Optional[Path]:
    """The main install this dashboard runs, when it is one (not a profile's own copy)."""
    root = _hermes_root()
    shared = Path(os.path.abspath(core._plugin_root()))
    return shared if root is not None and shared.parent == root / "plugins" else None


async def bot_plugin_state(name: str, home: Path) -> Dict[str, Any]:
    """``on``: Hermes serves the plugin's routes for the profile (its own
    consent check) and its chats have a copy to load. ``can_turn_on``: this
    dashboard can turn it on there (it runs the main install)."""
    root = _hermes_root()
    copies = self_update.profile_copies_module()
    # The profile's copy is counted against the main install wherever this dashboard runs from.
    holds = bool(copies and root is not None and copies.holds_plugin(home, root / "plugins" / core.PLUGIN_NAME))
    # Hermes failing to check (503) or not knowing the profile (404) is said
    # as such, never read as the plugin being off.
    allowed = not await core._plugin_refused_on(name)
    return {"profile": name, "on": allowed and holds, "allowed": allowed, "linked": holds,
            "can_turn_on": copies is not None and _main_install() is not None}


def _enable_command(name: str) -> list:
    return [sys.executable, "-m", "hermes_cli.main", "-p", name, "plugins", "enable", core.PLUGIN_NAME]


def turn_on(name: str, home: Path) -> None:
    """Links the profile to the main install, then runs Hermes' own enable for
    it. Raises core.TokenError with what went wrong."""
    shared = _main_install()
    copies = self_update.profile_copies_module() if shared is not None else None
    if copies is None:
        raise core.TokenError(501, "This dashboard runs a profile's own copy of the plugin, so it can't share it")
    root = shared.parent.parent
    try:
        changes = copies.link_profile(root, shared, home)
    except OSError as exc:
        logger.warning("Conduit plugin: couldn't link profile %s to the main install", name, exc_info=True)
        raise core.TokenError(500, f"Couldn't give the profile the plugin ({type(exc).__name__})")
    if changes is None:
        raise core.TokenError(500, f"This install's plugin.yaml doesn't name {core.PLUGIN_NAME}")
    if not copies.holds_plugin(home, shared):
        raise core.TokenError(409, f"Profile {name} holds something else under plugins/{shared.name}")
    # -p names the profile under the root; a HERMES_HOME naming a profile's home must not outrank it.
    env = {**os.environ, "HERMES_HOME": str(root)}
    try:
        done = subprocess.run(_enable_command(name), env=env,
                              cwd=self_update._hermes_root(sys.modules.get("hermes_cli")) or None,
                              stdin=subprocess.DEVNULL, capture_output=True, text=True, errors="replace",
                              timeout=BOT_PLUGIN_ENABLE_TIMEOUT_S, start_new_session=True)
    except subprocess.TimeoutExpired:
        raise core.TokenError(504, "Hermes didn't finish turning the plugin on in time")
    except OSError as exc:
        raise core.TokenError(500, f"Couldn't run Hermes to turn the plugin on ({type(exc).__name__})")
    if done.returncode != 0:
        tail = [line.strip() for line in (done.stderr or done.stdout or "").splitlines() if line.strip()]
        logger.warning("Conduit plugin: `hermes -p %s plugins enable` failed (%s): %s", name, done.returncode,
                       "\n".join(tail[-10:]))
        raise core.TokenError(502, f"Hermes refused to turn the plugin on: {tail[-1][:300] if tail else done.returncode}")
    logger.info("Conduit plugin: turned on for profile %s", name)


@router.get("/profiles/plugin")
async def get_bot_plugin(request: Request, response: Response, name: str = "") -> Dict[str, Any]:
    response.headers["Cache-Control"] = "no-store"
    _refuse_other_ticket(request, name.strip())
    home = _profile_home(name)
    return await bot_plugin_state(name.strip(), home)


@router.post("/profiles/plugin")
async def post_bot_plugin(request: Request, response: Response) -> Dict[str, Any]:
    response.headers["Cache-Control"] = "no-store"
    body = await core._read_json_body(request)
    name = str((body or {}).get("name") or "").strip() if isinstance(body, dict) else ""
    _refuse_other_ticket(request, name)
    home = _profile_home(name)
    try:
        await asyncio.get_running_loop().run_in_executor(None, turn_on, name, home)
    except core.TokenError as exc:
        raise HTTPException(status_code=exc.status, detail=str(exc), headers={"Cache-Control": "no-store"})
    except Exception as exc:
        raise core._unexpected("turn on", exc, feature="Bot plugin")
    return {"ok": True, **await bot_plugin_state(name, home)}
