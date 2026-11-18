"""Profile-local push pairings, shared by the plugin and dashboard."""
from __future__ import annotations

import hashlib
import json
import os
import stat
from pathlib import Path
from typing import Any

MAX_DEVICES = 16


def valid_installation_id(value: Any) -> bool:
    return isinstance(value, str) and 0 < len(value) <= 256 and bool(value.strip()) and value.isprintable()


def load(path: Path) -> dict[str, Any] | None:
    """Read credentials only from a regular file, never a pairing symlink."""
    path = Path(path)
    if path.parent.is_symlink():
        return None
    try:
        fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0))
        with os.fdopen(fd, "r", encoding="utf-8") as handle:
            if not stat.S_ISREG(os.fstat(handle.fileno()).st_mode) or path.is_symlink():
                return None
            value = json.load(handle)
    except (OSError, ValueError):
        return None
    if not isinstance(value, dict) or not isinstance(value.get("credential"), str) or not value["credential"]:
        return None
    return value


def for_installation(primary: Path, installation_id: str) -> Path:
    """Locate one installation without creating its directory on a read."""
    if not valid_installation_id(installation_id):
        raise ValueError("installation_id must be a nonempty printable string of at most 256 characters")
    primary = Path(primary)
    state = load(primary)
    if state and state.get("installation_id") == installation_id:
        return primary
    name = hashlib.sha256(installation_id.encode("utf-8")).hexdigest() + ".json"
    return primary.parent / "conduit-push-devices" / name


def paths(primary: Path) -> list[Path]:
    """Valid primary and sorted sidecars, once per installation."""
    primary = Path(primary)
    result = []
    seen = set()
    state = load(primary)
    if state:
        result.append(primary)
        installation = state.get("installation_id")
        if isinstance(installation, str):
            seen.add(installation)
    directory = primary.parent / "conduit-push-devices"
    if directory.is_symlink():
        return result
    for path in sorted(directory.glob("*.json")):
        state = load(path)
        installation = state.get("installation_id") if state else None
        if not valid_installation_id(installation) or installation in seen:
            continue
        if path.name != hashlib.sha256(installation.encode("utf-8")).hexdigest() + ".json":
            continue
        result.append(path)
        seen.add(installation)
    return result
