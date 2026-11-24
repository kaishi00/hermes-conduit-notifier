"""Phone data shared with the other profiles on this host.

Conduit shares each kind of phone data with one profile: the one sharing is
turned on for, which keeps the store and the agreed key. With "Share with
this host's other profiles" on in Conduit, every other profile on the same
host that is paired with the same iPhone (the same relay installation)
reads that store as its own. Location has it (location_profiles, #287), and
so do the calendar and phone (phone_store's ``sharing.other_profiles``) and
health (a health batch's ``other_profiles``).

A profile with data of that kind of its own always reads its own store: a
device that shares something into it, and hasn't since moved on to another
paired profile's store (reading_profile). If several profiles share with the
others, the first one wins: the default profile, then named ones
alphabetically.

Only the standard library here: the dashboard loads the stores as a private
package (``conduit_push_dashboard_pkg``), and they import this.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Callable, List, Tuple

PAIRING_FILE = "conduit-push.json"
# A folder name goes into notes the agent reads and into the access log, and
# is sent to the phone as is, so one that would need cleaning isn't used.
_UNUSABLE_NAME = re.compile("[\x00-\x1f\x7f-\x9f\u200b-\u200f\u2028-\u202e\u2060-\u206f\ufeff]")


# One device's row in a profile's store, as store_for's readers give it:
# (device id, last synced, shares something with its own profile, shares it
# with this host's other profiles too).
Device = Tuple[str, float, bool, bool]


def reading_profile(home: str | Path, devices: Callable[[Path], List[Device]]) -> tuple[str, Path] | None:
    """The other profile whose store this one reads, as (name, home), or
    None for its own. ``devices(home)`` lists the devices that have synced
    into a profile's store; it must never take that store's write lock.

    A profile reads its own store while a device shares something into it,
    else the first other profile, paired with the same iPhone, where a
    device shares with the others. A device's row counts only where the
    device still syncs (issue 1021): Conduit binds a kind of phone data to
    one profile, and when the user moves it, the old profile keeps the
    device's row, turned off or just left as it was. So a row that shares
    nothing isn't the profile's own data, and one the same device has since
    passed by syncing into another paired profile's store is left behind:
    read as live, it pinned the old profile to an empty or frozen store and
    could stand in front of the live one for every other profile."""
    home = Path(home)
    own = _devices(devices, home)
    installation = _installation(home)
    if not installation:
        return None
    others = []
    for name, path in profile_homes(home):
        if len(name) > 64 or _UNUSABLE_NAME.search(name) or _installation(path) != installation:
            continue
        others.append((name, path, _devices(devices, path)))
    stores = [own] + [rows for _, _, rows in others]

    def current(rows: List[Device]) -> List[Device]:
        """The rows that share something and that no other store has a later sharing sync of."""
        return [row for row in rows if row[2] and not any(
            other is not rows and any(theirs[0] == row[0] and theirs[2] and theirs[1] > row[1] for theirs in other)
            for other in stores)]

    if current(own):
        return None
    for name, path, rows in others:
        if any(row[3] for row in current(rows)):
            return name, path
    return None


def _devices(devices: Callable[[Path], List[Device]], home: Path) -> List[Device]:
    try:
        return list(devices(home))
    except Exception:  # noqa: BLE001 — another profile's broken store is never this one's problem
        return []


def profile_homes(home: Path) -> list[tuple[str, Path]]:
    """Every other Hermes profile on this host, as (name, home): the default
    profile is the root, named ones live in its profiles/ folder."""
    root = home.parent.parent if home.parent.name == "profiles" else home
    try:
        named = sorted(path for path in (root / "profiles").iterdir() if path.is_dir())
    except OSError:
        named = []
    try:
        own = home.resolve()
    except (OSError, RuntimeError):  # RuntimeError: a symlink loop
        return []
    found, seen = [], {own}
    for name, path in [("default", root)] + [(path.name, path) for path in named if path.name != "default"]:
        try:
            resolved = path.resolve()
        except (OSError, RuntimeError):
            continue
        if resolved not in seen:
            seen.add(resolved)
            found.append((name, path))
    return found


def label(name: str) -> str:
    """A profile's name as it may go into another profile's access log."""
    return _UNUSABLE_NAME.sub("", name)[:64]


def _installation(home: Path) -> str:
    """The relay installation (the iPhone) this profile is paired with, or ""."""
    try:
        value = json.loads((home / PAIRING_FILE).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return ""
    installation = value.get("installation_id") if isinstance(value, dict) else None
    credential = value.get("credential") if isinstance(value, dict) else None
    return installation if isinstance(installation, str) and isinstance(credential, str) and credential else ""
