"""A cron job delivered to a Bot Chat just shows its report there (hermes-conduit FEATURE_MAP "Cron jobs delivered to a bot").

Hermes delivers ``deliver: bot-chat[:<profile>]`` as a real inbound turn
(cron/scheduler_delivery.py ``_deliver_to_bot_chat``): the bot reads the
report, acts on anything that needs action and summarizes it, a model call
for every run. Many people only want the report in the chat they read. So
each job has a choice, kept in the job's profile (cron_store ``act``):

- Just show it (the default): the report is written into the bot's Bot Chat
  as a note, the way Hermes writes a cron result into a local chat
  (gateway/session_cron.py ``deliver_to_local_chat``): no turn, no model
  call, and the bot sees it as context on its next turn.
- Let the bot act on it: Hermes' own delivery, a turn, as before.

The plugin replaces Hermes' ``_deliver_to_bot_chat`` in the process that runs
the jobs, and a job that lets the bot act goes to Hermes' own. A Hermes
without the local-chat note keeps its own delivery for every job.
"""

from __future__ import annotations

import functools
import importlib
import logging
import re
import threading
import time
from pathlib import Path
from typing import Any, Callable, Optional

from .cron_store import CronStore
from .events import _FENCE, _MEDIA_DIRECTIVE, _MEDIA_TAG, _TRAILING

logger = logging.getLogger(__name__)

DELIVERY = "cron.scheduler_delivery"
# Where Hermes writes a cron result into a local chat as a note: newer than
# v0.22.5, so reached by name and checked by supported().
LOCAL_NOTE = "gateway.session_cron"
METHOD = "_deliver_to_bot_chat"
_GUARD_FLAG = "__conduit_cron_bot_chat__"
# The note's first line. Conduit reads the job's name from it and shows the
# rest as the report; the bot, on its next turn, reads that nobody asked it
# to act.
HEADER = '[Cron job "{name}" report: shown in the chat, no reply asked for]'
# A turn running in the Bot Chat holds its transcript: a note written from
# another process waits for it, as long as Hermes waits on its own owner.
NOTE_WAIT_S = 60.0
NOTE_RETRY_S = 5.0

# The report in place of one Hermes couldn't redact.
WITHHELD = "(The report was withheld: it couldn't be checked for secrets. It's in the job's own run history.)"

_lock = threading.Lock()
_installed: Optional[tuple[Any, Callable[..., Any], Callable[..., Any]]] = None


# The note is a user row of the transcript, and the chat's viewers read user
# rows by their reference vocabulary: a whole-line `@image:` directive lifts
# into the bubble's attachments row and a `@file:` one renders as a chip
# (Hermes Desktop's chat-messages hydration and user-message; Conduit's
# CronReportCard). A raw MEDIA tag is neither: there it reads as the host's
# path, so a report's picture shows as words where Hermes' own turn delivery
# sent the file itself (issue 581). Its whole-line tags therefore become
# references. The image extensions are artifacts_index's, kept equal by
# tests/test_cron_bot_chat.py.
_IMAGE_REFERENCE = frozenset("png jpg jpeg gif webp heic heif bmp svg tif tiff".split())


def _quoted(path: str) -> str:
    """A path as Hermes' format_reference_value quotes one: with the first
    quote mark it lacks when it holds a space, a bracket or a quote mark."""
    if not re.search(r"[\s\[\](){}<>'\"`]", path):
        return path
    quote = next(mark for mark in "`\"'" if mark not in path)
    return quote + path + quote


# What may stand before a whole-line tag: indentation and a list item's
# marker. They go, so the reference is a line of its own, the one a user row
# lifts into its attachments. A quoted line (`>`) is an example and stays.
_LINE_LEAD = re.compile(r"^\s*(?:(?:[-*+]|\d{1,9}[.)])\s+)?")


def reference_tags(text: str) -> str:
    """A report's whole-line MEDIA tags as the references its viewers
    render: an image as an `@image:` attachment, any other file as a
    `@file:` chip, on a line of its own without a list marker. A line with
    more than one tag, or a path holding all three quote marks, stays as
    written; a tag inside prose stays prose, and one in fenced code stays
    code, as events reads them. Hermes' delivery directives
    (`[[as_document]]`) go, as media_free drops them."""
    if "MEDIA:" not in text:
        return text
    lines, fenced = [], False
    for line in text.splitlines():
        if _FENCE.match(line):
            fenced = not fenced
        body = _MEDIA_DIRECTIVE.sub("", line[_LINE_LEAD.match(line).end():]).strip()
        match = (_MEDIA_TAG.fullmatch(body)
                 if not fenced and not _FENCE.match(line) and body.count("MEDIA:") == 1 else None)
        if match is None:
            lines.append(line)
            continue
        path = match.group("path").strip().strip("`\"'").rstrip(_TRAILING + "~")
        if not path or all(mark in path for mark in "`\"'"):
            lines.append(line)
            continue
        name = re.split(r"[/\\]", path.rstrip("/\\"))[-1]
        image = "." in name and name.rsplit(".", 1)[1].lower() in _IMAGE_REFERENCE
        # Quoted as Hermes quotes a reference (format_reference_value), so a
        # bracket, quote mark or space doesn't end it early.
        lines.append(f"{'@image' if image else '@file'}:{_quoted(path)}")
    return "\n".join(lines)


def note_text(job: Any, content: Any) -> str:
    job = job if isinstance(job, dict) else {}
    name = str(job.get("name") or job.get("id") or "?")
    text = content if isinstance(content, str) else str(content or "")
    # Redacted as Hermes' own delivery redacts it, or not written at all:
    # the bot reads the note on its next turn, and a report can hold a key.
    redact = getattr(_delivery_module(), "_redact_cron_payload", None)
    try:
        if not callable(redact):
            raise LookupError("no _redact_cron_payload")
        name, text = redact(name, "job name"), redact(text, "bot-chat payload")
    except Exception:  # noqa: BLE001 - withheld rather than shown unredacted
        logger.warning("conduit_push: cron report not redactable, withheld from the Bot Chat", exc_info=True)
        name = str(job.get("id") or "?")
        text = WITHHELD
    name = " ".join(name.replace('"', "'").split()) or "?"
    return HEADER.format(name=name) + "\n\n" + reference_tags(text)


def lets_act(job: Any) -> bool:
    """Whether the user picked "Let the bot act on it" for this job. The
    setting is the job's own profile's (this process's home while it runs)."""
    job_id = job.get("id") if isinstance(job, dict) else None
    try:
        from hermes_constants import get_hermes_home

        return CronStore(get_hermes_home()).lets_act(job_id)
    except Exception:  # noqa: BLE001 - unreadable: the report just shows, the default
        logger.debug("conduit_push: cron Bot Chat setting unreadable", exc_info=True)
        return False


def _delivery_module() -> Any:
    try:
        return importlib.import_module(DELIVERY)
    except Exception:  # noqa: BLE001
        return None


def _target_home(profile: str) -> Path:
    from hermes_constants import get_hermes_home

    if not profile:
        return Path(get_hermes_home()).resolve()
    from hermes_cli.profiles import get_profile_dir

    return Path(get_profile_dir(profile)).resolve()


def _bot_chat(home: Path) -> Optional[str]:
    from tools.bot_live_delivery import _bot_chat_tip

    return _bot_chat_tip(home)


def _suppressed(home: Path, for_failure: bool) -> bool:
    """A failure notice for a profile that hides warning notifications, as
    Hermes' own delivery decides it."""
    if not for_failure:
        return False
    from gateway.warning_notifications import warning_notifications_enabled
    from hermes_cli.config_effective import load_user_config_effective

    platform = getattr(_delivery_module(), "BOT_CHAT_POLICY_PLATFORM", "tui")
    return not warning_notifications_enabled(platform, load_user_config_effective(home / "config.yaml"))


def _note_from_here(home: Path, chat: str, text: str, for_failure: bool) -> Optional[str]:
    """The note through the chat's owner in this process, in its queue
    order, as Hermes writes a cron result into a local chat. None once
    admitted, else Hermes' error."""
    # By module, as install() checked it: Hermes v0.22.5 has none, and its
    # canary's test (upstream/test_cron_bot_chat.py) holds it.
    cron = importlib.import_module(LOCAL_NOTE)
    return cron.deliver_to_local_chat(home, chat, text, diagnostic=for_failure)


def _note_from_elsewhere(home: Path, chat: str, text: str, key: str, for_failure: bool,
                         sleep: Optional[Callable[[float], None]] = None,
                         clock: Optional[Callable[[], float]] = None) -> Optional[str]:
    """The note written straight into the profile's history when its owner
    runs in another process: once per run (keyed by it), and never while
    a turn holds the chat, so it waits for that turn."""
    from hermes_state import SessionDB

    metadata = {"completion_note": [key], "conduit_cron_report": True}
    if for_failure:
        metadata["notification_category"] = "diagnostic"
    sleep, clock = sleep or time.sleep, clock or time.monotonic
    deadline = clock() + NOTE_WAIT_S
    while True:
        db = None
        try:
            db = SessionDB(db_path=home / "state.db")
            db.append_completion_note(chat, key, text, "internal_notification", metadata)
            return None
        except Exception as exc:  # noqa: BLE001 - a running turn refuses it, a locked database too; anything else once the wait is up
            if clock() + NOTE_RETRY_S > deadline:
                return f"bot-chat report not written: {exc!r}"
        finally:
            if db is not None:
                db.close()
        sleep(NOTE_RETRY_S)


def show(job: dict, content: str, profile: str, *, for_failure: bool = False,
         in_process: Optional[Callable[[Path], bool]] = None) -> Optional[str]:
    """Just show it: the run's report as a note in the Bot Chat, no turn.
    None once it's written (or a hidden failure notice is dropped), else
    why it isn't, as Hermes' own delivery reports it."""
    target = f"bot-chat:{profile or '(own)'}"
    try:
        home = _target_home(profile)
        if _suppressed(home, for_failure):
            job.setdefault("_bot_chat_delivery_receipts", {})[target] = {"status": "suppressed"}
            job["_notification_all_targets_suppressed"] = True
            return None
        chat = _bot_chat(home)
        if not chat:
            return f"{target}: no Bot Chat yet, so the report has nowhere to show"
        text = note_text(job, content)
        key = f"conduit-cron:{job.get('id', '?')}:{job.get('execution_id') or job.setdefault('_bot_chat_run_id', time.time_ns())}"
        here = in_process(home) if in_process is not None else _owned_here(home)
        error = _note_from_here(home, chat, text, for_failure) if here else "its owner runs elsewhere"
        if error is not None:
            # The owner here doesn't hold the chat as a local one (a Bot
            # Chat Hermes' own door hasn't adopted yet refuses), or it runs
            # in another process: the note goes straight into the history.
            if here:
                logger.info("conduit_push: the Bot Chat's owner refused the report (%s); writing it into the "
                            "history", error)
            error = _note_from_elsewhere(home, chat, text, key, for_failure)
        if error is None:
            job.setdefault("_bot_chat_delivery_receipts", {})[target] = {"status": "shown", "delivery_id": key}
            logger.info("Job '%s': report shown in the %s Bot Chat (no turn)", job.get("id", "?"), profile or "own")
        return error
    except Exception as exc:  # noqa: BLE001 - reported to Hermes as its own delivery reports a failure
        logger.warning("conduit_push: cron report to %s failed", target, exc_info=True)
        return f"{target} report not shown: {exc!r}"


def _owned_here(home: Path) -> bool:
    return importlib.import_module(LOCAL_NOTE).owner_for_home(home) is not None


CRON_TOOL = "cronjob_manage"
SHOWS = ("With Conduit, each run's report just shows in the Bot Chat as it is, as a note: no turn, and you "
         "see it as context on your next turn. You can't change that; the user picks \"Let the bot act on it\" "
         "for this job in Conduit's job editor if you should act on each report. Tell the user which it is.")
ACTS = ("With Conduit, this job is set to \"Let the bot act on it\": each report comes in as a turn for you to "
        "act on and summarize. The user can switch it to \"Just show it\" in Conduit's job editor.")


def _delivers_to_bot_chat(deliver: Any) -> bool:
    parts = deliver if isinstance(deliver, (list, tuple)) else str(deliver or "").split(",")
    return any(str(p).strip().lower() == "bot-chat" or str(p).strip().lower().startswith("bot-chat:") for p in parts)


def annotate(tool_name: Any, result: Any) -> Optional[str]:
    """A cron tool result for a job delivered to a Bot Chat, with what
    Conduit does with its reports (Hermes' own text says each is a turn).
    None leaves any other result as it is."""
    if tool_name != CRON_TOOL or _installed is None or not isinstance(result, str):
        return None
    try:
        import json

        parsed = json.loads(result)
    except ValueError:
        return None
    if not isinstance(parsed, dict) or parsed.get("success") is not True or not parsed.get("job_id"):
        return None
    if not _delivers_to_bot_chat(parsed.get("deliver")):
        return None
    parsed["conduit_bot_chat"] = ACTS if lets_act({"id": parsed["job_id"]}) else SHOWS
    return json.dumps(parsed, ensure_ascii=False)


def _replacement(original: Callable[..., Any]) -> Callable[..., Any]:
    def _deliver_to_bot_chat(job: dict, content: str, profile: str, *args: Any, **kwargs: Any) -> Any:
        if lets_act(job):
            return original(job, content, profile, *args, **kwargs)
        # Hermes passes it by keyword; positionally, it's the next one.
        for_failure = kwargs.get("for_failure", args[0] if args else False)
        return show(job, content, profile, for_failure=bool(for_failure))

    functools.update_wrapper(_deliver_to_bot_chat, original)
    setattr(_deliver_to_bot_chat, _GUARD_FLAG, True)
    return _deliver_to_bot_chat


def supported() -> bool:
    """Whether this Hermes can write a cron result into a chat as a note."""
    try:
        cron = importlib.import_module(LOCAL_NOTE)
        state = importlib.import_module("hermes_state")
    except Exception:  # noqa: BLE001
        return False
    return (callable(getattr(cron, "deliver_to_local_chat", None))
            and callable(getattr(cron, "owner_for_home", None))
            and callable(getattr(getattr(state, "SessionDB", None), "append_completion_note", None)))


def install() -> bool:
    """Puts the report-only delivery in front of Hermes' own, once per
    process. False when this Hermes has no Bot Chat delivery or can't write
    a note, and every job then keeps Hermes' turn."""
    global _installed
    with _lock:
        if _installed is not None:
            return True
        module = _delivery_module()
        own = getattr(module, METHOD, None) if module is not None else None
        # A replacement an earlier load of the plugin left behind wraps Hermes' own.
        while callable(own) and getattr(own, _GUARD_FLAG, False):
            own = getattr(own, "__wrapped__", None)
        if not callable(own) or not supported():
            logger.info("conduit_push: this Hermes can't show a cron report in a Bot Chat without a turn; "
                        "jobs delivered to a Bot Chat start the bot's turn")
            return False
        replacement = _replacement(own)
        setattr(module, METHOD, replacement)
        _installed = (module, own, replacement)
        return True


def uninstall() -> None:
    """Puts Hermes' own delivery back (the plugin unloads). A replacement
    another load has set since is left to that load."""
    global _installed
    with _lock:
        installed, _installed = _installed, None
        if installed is not None:
            module, own, replacement = installed
            if getattr(module, METHOD, None) is replacement:
                setattr(module, METHOD, own)
