"""Which turns are cron runs, and whether a run's push should go out.

Hermes runs every cron job as an agent turn with ``task_id``
``cron:<job id>:<execution>`` (cron/scheduler.py ``_CronRunScope``) and, on
the classic path, session id ``cron_<job id>_<YYYYmmdd_HHMMSS>``. A gateway-
owned run keeps the task id but has a ``local-…`` session id, so the task id
comes first.

A run stays quiet when:
- the user turned the job's notifications off in Conduit (cron_store), or
- its output is Hermes' silence marker by the cron rule: ``[SILENT]`` (or
  SILENT / NO_REPLY) as the whole reply, on its own first or last line, or
  opening the reply (``[SILENT] No changes``). Hermes doesn't deliver such a
  run anywhere else either. Chats keep Hermes' interactive rule, the marker
  as the whole reply (events.is_silent_response, the exact ``[SILENT]`` on
  a Hermes without it).
- on a delivered leg (the reply or the failure push, not a background
  task's), the job delivers to a Bot Chat: the report or failure notice
  reaches the user through the Bot Chat (the note's push from
  cron_bot_chat.show, or the Bot Chat's own turn's reply when the bot acts
  on the report), so the run chat's push would be the second one
  (Eric, 2026-11-23, issue 1047: one notification per run, wherever the
  job delivers).
"""

from __future__ import annotations

import logging
import re
from typing import Any

from hermes_constants import get_hermes_home

from .cron_store import CronStore, valid_job_id
from .events import is_silent_response

logger = logging.getLogger(__name__)

_TASK = re.compile(r"^cron:([^:]+):")
_SESSION = re.compile(r"^cron_(.+)_\d{8}_\d{6}\Z")
_FALLBACK_MARKERS = ("[silent]", "silent", "no_reply", "no reply")


def job_id(session_id: Any = None, task_id: Any = None) -> str | None:
    """The cron job a turn runs, or None for any other turn."""
    for pattern, value in ((_TASK, task_id), (_SESSION, session_id)):
        match = pattern.match(value) if isinstance(value, str) else None
        if match and valid_job_id(match.group(1)):
            return match.group(1)
    return None


def is_silent(response: Any) -> bool:
    """Hermes' cron silence rule, from Hermes itself when it has one."""
    try:
        from gateway.response_filters import is_autonomous_silence_response
    except Exception:  # noqa: BLE001 — an older Hermes (the same rule, kept here), or a broken one
        return _fallback_silent(response)
    try:
        return bool(is_autonomous_silence_response(response))
    except Exception:  # noqa: BLE001 — a Hermes change must not cost the push
        logger.warning("conduit_push: Hermes silence check failed; using the plugin's copy", exc_info=True)
        return _fallback_silent(response)


def _fallback_silent(response: Any) -> bool:
    if is_silent_response(response):
        return True
    stripped = response.strip() if isinstance(response, str) else ""
    if not stripped:
        return False
    if stripped.casefold().startswith("[silent]"):
        return True
    lines = [line for line in stripped.splitlines() if line.strip()]
    return any(" ".join(line.split()).casefold() in _FALLBACK_MARKERS for line in (lines[0], lines[-1]))


def muted(job: str) -> bool:
    try:
        return CronStore(get_hermes_home()).is_muted(job)
    except Exception:  # noqa: BLE001 — unreadable: the push goes out
        logger.debug("conduit_push: cron notification setting unreadable", exc_info=True)
        return False


def _bot_chat_targets(deliver: Any) -> list[str]:
    """Every profile the delivery names for a Bot Chat ('' for the job's
    own), in its order; empty when the job doesn't deliver to a Bot Chat.
    All of them: Hermes' delivery loop reaches each, and the quiet rule
    asks whether any lands in this load's home."""
    parts = deliver if isinstance(deliver, (list, tuple)) else str(deliver or "").split(",")
    found = []
    for part in parts:
        text = str(part).strip()
        low = text.lower()
        if low == "bot-chat":
            found.append("")
        elif low.startswith("bot-chat:"):
            found.append(text[len("bot-chat:"):].strip())
    return found


def _job_deliver(job: str, failure: bool) -> Any:
    """The delivery targets Hermes holds for the job: its failure_deliver
    when one is set and the failure notice is on its way, else deliver.
    None when Hermes' job reader can't say (an older Hermes, a deleted
    job)."""
    try:
        from cron.jobs import get_job

        record = get_job(job)
    except Exception:  # noqa: BLE001 — a Hermes without the reader: pushes as before
        return None
    if not isinstance(record, dict):
        return None
    deliver = record.get("deliver")
    if failure:
        return record.get("failure_deliver") or deliver
    return deliver


def quiet(session_id: Any, task_id: Any, response: Any = None, *,
          delivered: bool = False, failure: bool = False) -> bool:
    """Whether this turn is a cron run whose push should not go out.

    A run stays quiet when the user turned its job's notifications off
    (cron_store), or its output is Hermes' silence marker by the cron rule
    (``[SILENT]``, SILENT / NO_REPLY, "…[SILENT] No changes").

    On a delivered leg (`delivered`, the reply or the failure push — never
    a background task's, which reports on its own), a run delivered to a
    Bot Chat is quiet too (Eric, 2026-11-23, issue 1047: one notification
    per run, wherever the job delivers): the report or failure notice
    reaches the user through the Bot Chat, so the run chat's push would be
    the second one — the Bot Chat's own turn's reply push when the bot acts
    on the report (Let the bot act on it, any target profile), else the
    note's own push from cron_bot_chat.show, sent by the load whose home is
    the delivery's (cron_bot_chat._own_target — the same predicate the note
    push fires on, so the two can't disagree). A delivery to another
    profile's Bot Chat keeps the run chat's push: a push that opens that
    chat seals with that profile's pairing, which the run's load doesn't
    hold.
    """
    job = job_id(session_id, task_id)
    if job is None:
        return False
    if muted(job) or (response is not None and is_silent(response)):
        return True
    if not delivered:
        return False
    targets = _bot_chat_targets(_job_deliver(job, failure))
    if not targets:
        return False
    try:
        acting = CronStore(get_hermes_home()).lets_act(job)
    except Exception:  # noqa: BLE001 — unreadable: the report just shows, the default
        acting = False
    if acting:
        # The Bot Chat's own turn is the delivery; its reply push opens it.
        return True
    # Just show it: the note's push opens the Bot Chat, from the load that
    # holds its pairing — any listed target this home serves.
    from .cron_bot_chat import _own_target

    return any(_own_target(target) for target in targets)
