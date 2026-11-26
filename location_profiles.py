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

A profile reads its own store while a device shares location into it
(talking or background on) and hasn't since moved on to another paired
profile's store, as the calendar and health do (profile_sharing.
reading_profile, notifier issue 1078). If several profiles share with the
others, the first one wins: the default profile, then named ones
alphabetically.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Callable

from . import profile_sharing
from .location_store import FILE_NAME, LocationStore, _synced_devices
# The profile lookup is shared with the calendar and health (profile_sharing).
from .profile_sharing import _UNUSABLE_NAME, PAIRING_FILE, _installation, label, profile_homes  # noqa: F401


def store_for(home: str | Path, make: Callable[[Any], LocationStore] = LocationStore) -> tuple[LocationStore, str | None]:
    """The store this profile reads, and the name of the profile it belongs
    to when that's another one (None for its own). ``make`` opens a store.

    A device's row counts only where the device still syncs: when the user
    moves location sharing to another profile, the old profile keeps the
    device's row as it was, sharing on, and read as live it served the
    frozen fix there and to every other profile, past the new profile's
    own choice about sharing with the others (notifier issue 1078)."""
    own = make(home)
    if own.exists():
        # A store an older plugin wrote is brought up to date first, as a read of it would.
        own._upgrade()
    found = profile_sharing.reading_profile(home, lambda path: _synced_devices(Path(path) / FILE_NAME))
    if found is None:
        return own, None
    name, path = found
    store = make(path)
    # Only its devices that share with this host's other profiles answer
    # here, each by its own word, as phone and health do.
    store.shared_only = True
    return store, name
