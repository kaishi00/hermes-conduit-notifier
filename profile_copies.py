"""Every profile that turns the plugin on runs the main install's code
(hermes-conduit#1538).

Hermes loads a profile's chat-turn plugins from that profile's own
``plugins/`` folder and nowhere else, while its dashboard falls back to the
main home's. So a profile cloned before an update (``hermes profile create
--clone`` copies ``plugins/``) kept its old copy for good: Conduit updates the
main install, reads the main install's version, and the profile's replies went
out from July's code, with no chat title and nothing the phone could open. A
profile turned on with ``hermes -p <name> plugins enable conduit_push`` and no
copy at all paired fine and sent nothing from its turns.

``sweep`` makes each such profile's ``plugins/<name>`` a link to the main
install: one copy of the code, so an update reaches every profile. A copy the
profile had of its own is moved to ``plugins-backup/`` beside it (never
deleted), with its version in the name. Where the host can't make a link
(Windows without the right to), the main install is copied in instead, marked
as ours, and copied again whenever its version falls behind.

A profile counts when its config.yaml lists the plugin in ``plugins.enabled``
and not in ``plugins.disabled``, or has no ``plugins.enabled`` at all (Hermes
then loads every installed plugin) and holds a copy already. A profile that
doesn't turn the plugin on is left alone, and so is anything in its folder that
isn't this plugin.

The dashboard runs it when it starts and after an update pulls the new
version, before the gateway restarts; the plugin runs it when it registers
under the main home. A change applies to a process when it next loads its
plugins. Never raises: a profile it can't fix is logged and skipped.

Standalone (no imports from the plugin package): the dashboard loads it from
its file, the plugin imports it.
"""

from __future__ import annotations

import logging
import os
import shutil
from pathlib import Path
from typing import Any, Callable, List, Optional

logger = logging.getLogger("hermes.plugins.conduit_push")

PLUGIN_NAME = "conduit_push"
BACKUP_DIR = "plugins-backup"
# In a copy made where no link could be: the copy is ours to replace.
COPY_MARKER = ".conduit-shared-copy"
_COPY_IGNORED = shutil.ignore_patterns(".git", "__pycache__", "*.pyc", COPY_MARKER)


def _manifest(directory: Path) -> dict:
    """``name`` and ``version`` from a plugin folder's plugin.yaml, {} when it has none."""
    values: dict = {}
    for filename in ("plugin.yaml", "plugin.yml"):
        try:
            text = (directory / filename).read_text(encoding="utf-8-sig")
        except (OSError, UnicodeDecodeError):
            continue
        for line in text.splitlines():
            key, sep, value = line.partition(":")
            # Top-level keys only: a nested `name:` belongs to something else.
            if sep and key == key.lstrip() and key.strip() in ("name", "version") and key.strip() not in values:
                values[key.strip()] = value.split("#", 1)[0].strip().strip("\"'")
        break
    return values


def _config(home: Path) -> dict:
    try:
        text = (home / "config.yaml").read_text(encoding="utf-8-sig")
    except (OSError, UnicodeDecodeError):
        return {}
    try:
        try:
            import hermes_yaml as yaml
        except ImportError:
            import yaml
        data = yaml.safe_load(text)
    except Exception:  # noqa: BLE001 — an unreadable config turns nothing on
        logger.info("Conduit plugin: couldn't read %s; leaving its plugins alone", home / "config.yaml")
        return {}
    return data if isinstance(data, dict) else {}


def _turns_on(home: Path, has_copy: bool) -> bool:
    plugins = _config(home).get("plugins")
    plugins = plugins if isinstance(plugins, dict) else {}
    disabled = plugins.get("disabled")
    if isinstance(disabled, list) and PLUGIN_NAME in disabled:
        return False
    enabled = plugins.get("enabled")
    if isinstance(enabled, list):
        return PLUGIN_NAME in enabled
    # No allow-list: Hermes loads what's installed, so only a profile with a copy runs it.
    return has_copy


def _backup_target(home: Path, name: str, version: str) -> Path:
    base = home / BACKUP_DIR / (f"{name}-{version}" if version else name)
    target, n = base, 2
    while target.exists() or target.is_symlink():
        target = base.with_name(f"{base.name}-{n}")
        n += 1
    return target


def _links_to(path: Path, shared: Path) -> bool:
    try:
        return path.is_symlink() and path.resolve() == shared.resolve()
    except OSError:
        return False


def sweep(root: Path, shared: Path, *, link: Callable[[Path, Path], None] = os.symlink) -> List[str]:
    """Points every profile under ``root`` that turns the plugin on at
    ``shared``, the main install's folder. Returns what it changed, one line
    each, for the log and the tests."""
    changes: List[str] = []
    shared, root = Path(os.path.abspath(shared)), Path(os.path.abspath(root))
    # Only the main install is shared: a dashboard running a profile's own copy changes nothing.
    if shared.parent != root / "plugins" or _manifest(shared).get("name") != PLUGIN_NAME:
        return changes
    version = _manifest(shared).get("version", "")
    try:
        profiles = sorted(p for p in (root / "profiles").iterdir() if p.is_dir() and not p.is_symlink())
    except OSError:
        return changes
    for home in profiles:
        try:
            changes.extend(_point_profile(home, shared, version, link))
        except Exception as error:  # noqa: BLE001 — one profile mustn't stop the others
            logger.warning("Conduit plugin: couldn't point profile %s at the main install: %s", home.name, error)
    for line in changes:
        logger.info("Conduit plugin: %s", line)
    return changes


def _point_profile(home: Path, shared: Path, version: str, link: Callable[[Path, Path], None]) -> List[str]:
    changes: List[str] = []
    plugins = home / "plugins"
    target = plugins / shared.name
    ours: Optional[Path] = None
    copies: List[Path] = []
    try:
        children = sorted(plugins.iterdir()) if plugins.is_dir() else []
    except OSError:
        children = []
    for child in children:
        if _links_to(child, shared):
            ours = child
        elif child.is_dir() and not child.is_symlink() and _manifest(child).get("name") == PLUGIN_NAME:
            copies.append(child)
    if not _turns_on(home, bool(ours or copies)):
        return changes
    for copy in copies:
        if (copy / COPY_MARKER).exists():
            if copy == target and _manifest(copy).get("version") == version:
                ours = copy  # our copy, current
                continue
            shutil.rmtree(copy)
            changes.append(f"profile {home.name}: removed its outdated copy of the main install")
            continue
        backup = _backup_target(home, copy.name, _manifest(copy).get("version", ""))
        backup.parent.mkdir(parents=True, exist_ok=True)
        copy.rename(backup)
        changes.append(f"profile {home.name}: moved its own copy ({_manifest(backup).get('version') or 'no version'}) "
                       f"to {BACKUP_DIR}/{backup.name}")
    if ours is not None:
        return changes
    if target.exists() or target.is_symlink():
        # Something else holds the name (not this plugin, or a link elsewhere): not ours to replace.
        logger.warning("Conduit plugin: profile %s has %s, which isn't the main install; leaving it", home.name, target)
        return changes
    plugins.mkdir(parents=True, exist_ok=True)
    try:
        link(shared, target)
        changes.append(f"profile {home.name}: linked to the main install ({version})")
    except OSError:
        shutil.copytree(shared, target, ignore=_COPY_IGNORED)
        (target / COPY_MARKER).write_text("Copied from the main install by the Conduit plugin; replaced on update.\n",
                                          encoding="utf-8")
        changes.append(f"profile {home.name}: copied the main install ({version}), as no link could be made")
    return changes


def sweep_quietly(root: Any, shared: Any) -> List[str]:
    """sweep, for a caller that must never fail because of it."""
    try:
        return sweep(Path(root), Path(shared))
    except Exception:  # noqa: BLE001
        logger.warning("Conduit plugin: couldn't point profiles at the main install", exc_info=True)
        return []
