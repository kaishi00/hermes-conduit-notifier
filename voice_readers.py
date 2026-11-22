"""Which live voice modes the user lets read each kind of data (#ADD).

Conduit asks, per voice mode, before a call may read the user's location,
health or calendar and phone status: what the call reads goes to the mode's
company (Google, Alibaba, OpenAI, xAI), or to the host's own fast model for
Local Live. The phone keeps that choice and, from plugins that store it, sends
it in each store's sealed sharing as ``voice_readers``, so the host can answer
a call that is only signed in to the dashboard, such as Conduit for desktop's,
for those modes and no others. A phone that leaves it out lets no signed-in
call read anything; its own calls still read through the sealed /voice-tools.
"""

from __future__ import annotations

from typing import Any, Callable

# Conduit's ids for the live modes (hermes-conduit LiveVoiceModeName.storageID).
VOICE_READERS = ("gemini-live", "qwen-live", "gpt-live", "grok-live", "local-live")


def parse(value: Any, error: Callable[[str], Exception], field: str = "sharing.voice_readers") -> list[str]:
    """The modes a batch lets read, known ones only (a later phone's new mode
    is ignored here rather than refused); missing is none."""
    if value is None:
        return []
    if not isinstance(value, list):
        raise error(f"{field} must be a list")
    return sorted({item for item in value if isinstance(item, str) and item in VOICE_READERS})


def lets(sharing: Any, reader: str) -> bool:
    """Whether stored sharing lets ``reader`` read."""
    readers = sharing.get("voice_readers") if isinstance(sharing, dict) else None
    return isinstance(readers, list) and reader in readers
