"""Location shared with the other profiles on this host (#287).

Conduit sends the user's location to one profile: the one sharing is bound
to, which keeps the store, the agreed key and the place rules. With "Share
with this host's other profiles" on in Conduit (``sharing.other_profiles``),
every other profile on the same host that is paired with the same iPhone
(the same relay installation) reads that store as its own: ``now``,
``places``, ``history``, ``distance_to``, ``locate`` and maps lookups. A
locate goes through the asking profile's own pairing, naming the sharing
profile, so the phone answers it like one of that profile's. Place rules
stay with the sharing profile.

A profile that shares location itself (talking or background on) always
reads its own store. If several profiles share with the others, the first
one wins: the default profile, then named ones alphabetically.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Callable

from .location_store import LocationStore
# The profile lookup is shared with the calendar and health (profile_sharing).
from .profile_sharing import _UNUSABLE_NAME, PAIRING_FILE, _installation, label, profile_homes  # noqa: F401


def store_for(home: str | Path, make: Callable[[Any], LocationStore] = LocationStore) -> tuple[LocationStore, str | None]:
    """The store this profile reads, and the name of the profile it belongs
    to when that's another one (None for its own). ``make`` opens a store."""
    own = make(home)
    if _sharing_on(own):
        return own, None
    installation = _installation(Path(home))
    if installation:
        for name, path in profile_homes(Path(home)):
            if len(name) > 64 or _UNUSABLE_NAME.search(name) or _installation(path) != installation:
                continue
            store = make(path)
            if not store.exists():
                continue
            view = store.snapshot(upgrade=False)  # never another profile's write lock
            if view is not None and view["sharing"].get("other_profiles") is True and _on(view["sharing"]):
                return store, name
    return own, None


def _sharing_on(store: LocationStore) -> bool:
    if not store.exists():
        return False
    view = store.snapshot()
    return view is not None and _on(view["sharing"])


def _on(sharing: dict[str, Any]) -> bool:
    return bool(sharing.get("talking") or sharing.get("background"))
