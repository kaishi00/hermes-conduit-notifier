"""Hermes asks the user to OK something on their phone: the confirm prompts
``conduit_present`` shows and how the user answered them.

Before an action the user should approve (sending a message, buying,
deleting), Hermes keeps a prompt here: a title, a Markdown preview of what
will happen and the label of the button that does it. The relay wakes the
phone (``phone.changes``), Conduit fetches the waiting prompts with an
exchange, shows its confirm panel and reports the answer on its next
exchange: confirmed, declined or edit, by tap or by voice during a call.

The phone is where the user answers, not the gate: nothing here acts on an
answer. The agent reads it with the tool and does (or doesn't do) the action
itself. An answer the agent hasn't seen yet stays unreported until the tool
returns it once.

The same exchange carries the cards ``conduit_show`` puts on the screen of
a voice call (a table, steps, a chart): short-lived, shown once by a phone
that says it can (feature ``show-cards``), never answered. A card can carry
rich cards (maps and places, ``card_schema``) beside its Markdown; a phone
that doesn't draw them shows the Markdown. ``fallback`` marks Markdown that
is only the cards' own text, which a phone that draws them leaves out.

State lives in the profile's ``conduit-present.json`` beside the pairing
state. The tool (agent process) and the dashboard route (dashboard process)
both use this module: the dashboard loads it by path, so it imports nothing
from the plugin package.
"""

from __future__ import annotations

import importlib.util
import json
import os
import re
import secrets
import tempfile
import threading
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Callable, Iterator

try:
    import fcntl
except ImportError:  # Windows
    fcntl = None  # type: ignore[assignment]
try:
    import msvcrt
except ImportError:  # everything but Windows
    msvcrt = None  # type: ignore[assignment]

FILE_NAME = "conduit-present.json"
VERSION = 1

STATES = ("waiting", "confirmed", "declined", "edit", "expired", "cancelled")
# What a phone reports for a prompt the user answered, and how.
OUTCOMES = ("confirmed", "declined", "edit")
# "auto": Conduit opened Apple Maps for a navigate prompt itself, because the
# user was in that chat with Conduit open.
VIAS = ("tap", "voice", "auto")
# What a navigate prompt (conduit_maps navigate) carries: where to and how.
# Conduit opens Apple Maps with these when the user says go.
MAPS_MODES = ("driving", "walking", "transit", "cycling")
MAX_MAPS_TO_CHARS = 200
MAX_FEATURES = 16

MAX_TITLE_CHARS = 80
MAX_PREVIEW_CHARS = 2000
MAX_ACTION_LABEL_CHARS = 24
MAX_SPOKEN_YES = 5
MAX_SPOKEN_YES_CHARS = 40
MAX_EDIT_TEXT_CHARS = 2000
MAX_DEVICE_NAME_CHARS = 60
MAX_DEVICES = 8
MAX_WAITING = 20
MAX_PROMPTS = 50
MAX_RESULTS = 40
# Cards conduit_show puts on a call's screen: how long a phone may still pick
# one up, how many are kept, and how many Hermes may show an hour.
CARD_TTL_S = 10 * 60
MAX_CARDS = 20
CARDS_PER_HOUR = 60
MAX_CARD_TITLE_CHARS = 80
MAX_CARD_MARKDOWN_CHARS = 8000
# What a phone that shows cards says it can do, and how recently it must have
# exchanged for Hermes to count on it (a phone in a voice conversation
# exchanges at least every reply).
SHOW_FEATURE = "show-cards"
# A phone that draws stats, charts, forecasts, comparisons and steps.
INFO_CARDS_FEATURE = "info-cards"
SHOW_DEVICE_RECENT_S = 6 * 3600
# How often an unchanged phone's last-seen time is refreshed.
SEEN_REFRESH_S = 60
DEFAULT_TTL_MINUTES = 30
MAX_TTL_MINUTES = 24 * 60
# Answered, expired and cancelled prompts are kept a week so the next chat can
# still tell what happened.
FINISHED_TTL_S = 7 * 24 * 3600
# Prompts Hermes may show per hour, as settings changes. Advisory: the
# dashboard's auth and the route's own limiter are the boundary.
PER_HOUR = 20
HOUR_S = 3600
# A device not seen for this long is forgotten.
DEVICE_TTL_S = 60 * 24 * 3600

_INSTALLATION = re.compile(r"^[A-Za-z0-9_-]{8,64}$")
_PROMPT_ID = re.compile(r"^[A-Za-z0-9_-]{8,64}$")
# What the phone needs to show a prompt; the rest stays on this host.
_PUBLIC = ("id", "title", "preview", "action_label", "spoken_yes", "session_id", "created_at", "expires_at")

_thread_lock = threading.Lock()


class PresentError(ValueError):
    """A prompt this store won't keep; ``code`` is a short machine word."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


class PresentStore:
    def __init__(self, home: Path, clock: Callable[[], float] = time.time) -> None:
        self.path = Path(home) / FILE_NAME
        self.clock = clock

    # --- The phone's side ----------------------------------------------------

    def exchange(self, body: Any) -> dict[str, Any]:
        """A phone's exchange: what the user answered, then the prompts still
        waiting. Answers for prompts already answered, expired or unknown are
        ignored: the first answer wins."""
        if not isinstance(body, dict):
            raise PresentError("invalid", "Expected a JSON object")
        installation = body.get("installation_id")
        if not isinstance(installation, str) or not _INSTALLATION.match(installation):
            raise PresentError("invalid", "installation_id must be a plain id")
        results = body.get("results", [])
        if results is None:
            results = []
        if not isinstance(results, list) or len(results) > MAX_RESULTS:
            raise PresentError("invalid", "results must be a short list")
        with self._locked() as state:
            now = self.clock()
            device = {
                "name": _line(body.get("device"), MAX_DEVICE_NAME_CHARS) or "Conduit",
                "app_version": _line(body.get("app_version"), 40),
                "features": _features(body.get("features")),
                "seen_at": now,
            }
            # A phone polls often: an unchanged device is only re-stamped once
            # a minute, so a quiet exchange doesn't rewrite the file.
            known = state["devices"].get(installation)
            if (isinstance(known, dict) and known.get("name") == device["name"]
                    and known.get("app_version") == device["app_version"]
                    and known.get("features", []) == device["features"]
                    and isinstance(known.get("seen_at"), (int, float))
                    and 0 <= now - known["seen_at"] < SEEN_REFRESH_S):
                device["seen_at"] = known["seen_at"]
            state["devices"][installation] = device
            _trim_devices(state)
            for result in results:
                _take_result(state, installation, result, now)
            # The newest, within a cap only a hand-edited store can exceed.
            waiting = [_public(prompt) for prompt in state["prompts"] if prompt["status"] == "waiting"][-MAX_WAITING:]
            # Expired cards leave the file; the rest go only to a phone that
            # shows them.
            state["cards"] = [card for card in state["cards"] if now < card["expires_at"]]
            cards = ([_for_device(card, device["features"]) for card in state["cards"]]
                     if SHOW_FEATURE in device["features"] else [])
        # Only a host with cards to show says so.
        return {"prompts": waiting, **({"cards": cards} if cards else {})}

    # --- Hermes' side --------------------------------------------------------

    def has_device(self) -> bool:
        """Whether a Conduit that shows prompts has exchanged with this
        profile."""
        return bool(self._load()["devices"])

    def can_show(self) -> bool:
        """Whether a Conduit that shows cards exchanged with this profile
        recently."""
        return _shows_cards(self._load(), self.clock())

    def has_feature(self, feature: str) -> bool:
        """Whether a Conduit that exchanged with this profile says it can
        do ``feature`` (like "maps-navigate")."""
        return any(feature in device.get("features", []) for device in self._load()["devices"].values())

    def confirm(self, title: Any, preview: Any, action_label: Any = None, *, spoken_yes: Any = None,
                session_id: Any = None, ttl_minutes: Any = None, maps: Any = None) -> dict[str, Any]:
        """Keeps a confirm prompt for the phone. Raises PresentError when it
        won't."""
        clean_title = _line(title, MAX_TITLE_CHARS + 1)
        if not clean_title:
            raise PresentError("no_title", "Give a short title, like \"Send to Mei?\".")
        if len(clean_title) > MAX_TITLE_CHARS:
            raise PresentError("too_long", f"title is at most {MAX_TITLE_CHARS} characters.")
        clean_preview = _text(preview, MAX_PREVIEW_CHARS + 1)
        if not clean_preview:
            raise PresentError("no_preview", "Give a preview of exactly what will happen (Markdown).")
        if len(clean_preview) > MAX_PREVIEW_CHARS:
            raise PresentError("too_long", f"preview is at most {MAX_PREVIEW_CHARS} characters.")
        label = _line(action_label, MAX_ACTION_LABEL_CHARS + 1) if action_label is not None else "Confirm"
        if not label:
            raise PresentError("invalid", "action_label is a short button label, like \"Send\".")
        if len(label) > MAX_ACTION_LABEL_CHARS:
            raise PresentError("too_long", f"action_label is at most {MAX_ACTION_LABEL_CHARS} characters.")
        phrases = _spoken_yes(spoken_yes)
        if ttl_minutes is None:
            ttl_minutes = DEFAULT_TTL_MINUTES
        if isinstance(ttl_minutes, bool) or not isinstance(ttl_minutes, (int, float)) \
                or not 1 <= ttl_minutes <= MAX_TTL_MINUTES:
            raise PresentError("invalid", f"ttl_minutes is 1 to {MAX_TTL_MINUTES}.")
        session = _line(session_id, 180)
        navigate = None
        if maps is not None:
            navigate = _maps(maps)
            if navigate is None:
                raise PresentError("invalid", "maps is {to, mode} with a place and a travel mode.")
        with self._locked() as state:
            now = self.clock()
            if not state["devices"]:
                raise PresentError("no_device", "No Conduit that shows confirm prompts has connected to this "
                                                "Hermes profile yet.")
            recent = [at for at in state["asked"] if now - at < HOUR_S]
            if len(recent) >= PER_HOUR:
                raise PresentError("rate_limited", "Too many confirm prompts this hour. Ask the user in chat instead.")
            if sum(prompt["status"] == "waiting" for prompt in state["prompts"]) >= MAX_WAITING:
                raise PresentError("too_many", "Too many prompts are still waiting. Cancel ones you no longer need.")
            created = int(now)
            prompt = {
                "id": secrets.token_urlsafe(12), "title": clean_title, "preview": clean_preview,
                "action_label": label, "spoken_yes": phrases, "session_id": session,
                "created_at": created, "expires_at": created + int(ttl_minutes * 60),
                "status": "waiting", "reported": False,
            }
            if navigate is not None:
                prompt["maps"] = navigate
            state["prompts"].append(prompt)
            _trim_prompts(state)
            state["asked"] = (recent + [now])[-PER_HOUR:]
            return _shown(prompt)

    def show(self, title: Any, markdown: Any, *, session_id: Any = None, cards: Any = None,
             markdown_is_fallback: bool = False) -> dict[str, Any]:
        """Keeps a card for the screen of the user's voice call. ``cards``
        are rich cards, already normalized; ``markdown`` is what shows where
        they can't be drawn (``markdown_is_fallback``: only there, it's the
        cards' own text). Raises PresentError when it won't."""
        clean_title = _line(title, MAX_CARD_TITLE_CHARS + 1)
        if not clean_title:
            raise PresentError("no_title", "Give a short title, a few words.")
        if len(clean_title) > MAX_CARD_TITLE_CHARS:
            raise PresentError("too_long", f"title is at most {MAX_CARD_TITLE_CHARS} characters.")
        body = _text(markdown, MAX_CARD_MARKDOWN_CHARS + 1)
        if not body:
            raise PresentError("no_markdown", "Give what to show, as Markdown.")
        if len(body) > MAX_CARD_MARKDOWN_CHARS:
            raise PresentError("too_long", f"markdown is at most {MAX_CARD_MARKDOWN_CHARS} characters.")
        schema = _card_schema()
        rich = schema.normalize_cards(cards) if schema else []
        with self._locked() as state:
            now = self.clock()
            if not _shows_cards(state, now):
                raise PresentError("no_device", "No Conduit that shows cards has connected to this Hermes profile "
                                                "yet.")
            recent = [at for at in state["shown"] if now - at < HOUR_S]
            if len(recent) >= CARDS_PER_HOUR:
                raise PresentError("rate_limited", "Too many cards this hour. Say it instead.")
            created = int(now)
            card = {"id": secrets.token_urlsafe(12), "title": clean_title, "markdown": body,
                    "session_id": _line(session_id, 180), "created_at": created, "expires_at": created + CARD_TTL_S}
            if rich:
                card["cards"] = rich
                if markdown_is_fallback:
                    card["fallback"] = True
            state["cards"] = (state["cards"] + [card])[-MAX_CARDS:]
            state["shown"] = (recent + [now])[-CARDS_PER_HOUR:]
            return dict(card)

    def get(self, prompt_id: Any) -> dict[str, Any] | None:
        """One prompt as it stands, without marking it reported (for polling)."""
        prompt = _find(self._load(), prompt_id)
        return _shown(prompt) if prompt else None

    def status(self, prompt_id: Any = None) -> list[dict[str, Any]]:
        """One prompt (any state) or, without an id, the waiting prompts and
        every outcome the agent hasn't seen. Outcomes returned are marked
        reported."""
        with self._locked() as state:
            if prompt_id is not None:
                prompt = _find(state, prompt_id)
                found = [prompt] if prompt else []
            else:
                found = [prompt for prompt in state["prompts"]
                         if prompt["status"] == "waiting" or not prompt["reported"]]
            shown = [_shown(prompt) for prompt in found]
            for prompt in found:
                if prompt["status"] != "waiting":
                    prompt["reported"] = True
            return shown

    def unreported_outcomes(self) -> list[dict[str, Any]]:
        """Outcomes the agent hasn't seen yet, oldest first, marked seen."""
        with self._locked() as state:
            found = [prompt for prompt in state["prompts"] if prompt["status"] != "waiting" and not prompt["reported"]]
            for prompt in found:
                prompt["reported"] = True
            return [_shown(prompt) for prompt in found]

    def cancel(self, prompt_id: Any) -> dict[str, Any] | None:
        """Takes back a waiting prompt; the prompt as it stands after (an
        answered one keeps its answer), or None when there's no such prompt."""
        with self._locked() as state:
            prompt = _find(state, prompt_id)
            if prompt is None:
                return None
            if prompt["status"] == "waiting":
                prompt.update(status="cancelled", finished_at=int(self.clock()))
            prompt["reported"] = True
            return _shown(prompt)

    # --- Storage -------------------------------------------------------------

    def _load(self, *, for_write: bool = False) -> dict[str, Any]:
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except (FileNotFoundError, ValueError, RecursionError):
            raw = None
        except OSError:
            # Unreadable for now (not missing, not corrupt): a write would
            # replace the prompts it couldn't read, so it fails instead.
            if for_write:
                raise
            raw = None
        return _normalized(raw, self.clock())

    @contextmanager
    def _locked(self) -> Iterator[dict[str, Any]]:
        """Load, let the caller change, then save: all under the file lock."""
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        lock_path = self.path.with_name(f".{self.path.name}.lock")
        with _thread_lock, open(lock_path, "a+b") as handle, _exclusive(handle):
            state = self._load(for_write=True)
            before = _encoded(state)
            yield state
            after = _encoded(state)
            if after != before or not self.path.exists():
                self._save(after)

    def _save(self, encoded: str) -> None:
        # A fresh, unguessable file (mkstemp opens it O_EXCL, mode 0600).
        descriptor, name = tempfile.mkstemp(prefix=f".{self.path.name}.", suffix=".tmp", dir=self.path.parent)
        temporary = Path(name)
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
                handle.write(encoded)
            temporary.replace(self.path)
        except BaseException:
            temporary.unlink(missing_ok=True)
            raise
        self.path.chmod(0o600)


# --- Helpers ---------------------------------------------------------------------


def _encoded(state: dict[str, Any]) -> str:
    return json.dumps(state, separators=(",", ":"), ensure_ascii=False) + "\n"


def _line(value: Any, limit: int) -> str:
    """One line of printable text, trimmed; empty when ``value`` isn't text."""
    if not isinstance(value, str):
        return ""
    # Line breaks, tabs and other controls become spaces, not glue.
    text = " ".join("".join(char if char.isprintable() else " " for char in value).split())
    return text[:limit]


def _text(value: Any, limit: int) -> str:
    """Multi-line printable text (Markdown keeps its line breaks), trimmed."""
    if not isinstance(value, str):
        return ""
    lines = value.replace("\r\n", "\n").replace("\r", "\n").split("\n")
    kept = ["".join(char if char.isprintable() else " " for char in line).rstrip() for line in lines]
    return "\n".join(kept).strip("\n").strip()[:limit]


def first_line(preview: str) -> str:
    """The preview's first non-empty line, trimmed: the wake's body."""
    return next((line.strip() for line in preview.split("\n") if line.strip()), "")


def _spoken_yes(value: Any) -> list[str]:
    if value is None:
        return []
    if not isinstance(value, list) or len(value) > MAX_SPOKEN_YES:
        raise PresentError("invalid", f"spoken_yes is a list of up to {MAX_SPOKEN_YES} short phrases.")
    phrases: list[str] = []
    for item in value:
        phrase = _line(item, MAX_SPOKEN_YES_CHARS + 1)
        if not phrase or len(phrase) > MAX_SPOKEN_YES_CHARS:
            raise PresentError("invalid", f"Each spoken_yes phrase is short text (up to {MAX_SPOKEN_YES_CHARS} "
                                          "characters), like \"send it\".")
        if phrase.casefold() not in (kept.casefold() for kept in phrases):
            phrases.append(phrase)
    return phrases


def _find(state: dict[str, Any], prompt_id: Any) -> dict[str, Any] | None:
    if not isinstance(prompt_id, str):
        return None
    return next((prompt for prompt in state["prompts"] if prompt["id"] == prompt_id), None)


def _public(prompt: dict[str, Any]) -> dict[str, Any]:
    """A waiting prompt as the phone shows it."""
    public = {key: prompt[key] for key in _PUBLIC}
    if prompt.get("maps"):
        public["maps"] = prompt["maps"]
    return public


def _maps(value: Any) -> dict[str, str] | None:
    """Where a navigate prompt goes, cleaned, or None when it's malformed."""
    if not isinstance(value, dict):
        return None
    to = _line(value.get("to"), MAX_MAPS_TO_CHARS + 1)
    mode = value.get("mode", "driving")
    if not to or len(to) > MAX_MAPS_TO_CHARS or mode not in MAPS_MODES:
        return None
    return {"to": to, "mode": mode}


def _features(value: Any) -> list[str]:
    """What a phone says it can do: short plain names, sorted."""
    if not isinstance(value, list):
        return []
    names = {_line(item, 40) for item in value[:MAX_FEATURES] if isinstance(item, str)}
    return sorted(name for name in names if name)


def _shown(prompt: dict[str, Any]) -> dict[str, Any]:
    """A prompt as the agent reads it."""
    shown = {key: prompt[key] for key in ("id", "title", "action_label", "status", "created_at", "expires_at")}
    for key in ("via", "edit_text", "device", "finished_at"):
        if prompt.get(key) not in (None, ""):
            shown[key] = prompt[key]
    return shown


def _take_result(state: dict[str, Any], installation: str, result: Any, now: float) -> None:
    """A phone's answer to a waiting prompt; anything else is ignored."""
    if not isinstance(result, dict):
        return
    prompt_id = result.get("id")
    outcome = result.get("outcome")
    via = result.get("via", "tap")
    if not isinstance(prompt_id, str) or not _PROMPT_ID.match(prompt_id) or outcome not in OUTCOMES \
            or via not in VIAS:
        return
    prompt = _find(state, prompt_id)
    if prompt is None or prompt["status"] != "waiting":
        return
    # Conduit opens Maps by itself, and only as a yes to a Go prompt.
    if via == "auto" and (outcome != "confirmed" or not prompt.get("maps")):
        return
    prompt.update(status=outcome, via=via, device=installation, finished_at=int(now), reported=False)
    if outcome == "edit":
        edit_text = _text(result.get("edit_text"), MAX_EDIT_TEXT_CHARS)
        if edit_text:
            prompt["edit_text"] = edit_text


def _trim_devices(state: dict[str, Any]) -> None:
    devices = state["devices"]
    if len(devices) <= MAX_DEVICES:
        return
    for installation in sorted(devices, key=lambda item: devices[item]["seen_at"])[:len(devices) - MAX_DEVICES]:
        del devices[installation]


def _trim_prompts(state: dict[str, Any]) -> None:
    """Keeps the newest prompts; finished ones go first, never a waiting one."""
    prompts = state["prompts"]
    extra = len(prompts) - MAX_PROMPTS
    if extra <= 0:
        return
    finished = [prompt["id"] for prompt in prompts if prompt["status"] != "waiting"][:extra]
    state["prompts"] = [prompt for prompt in prompts if prompt["id"] not in finished]


def _prompt(raw: Any, now: float) -> dict[str, Any] | None:
    """A well-formed prompt from disk, expired when its time ran out, or None
    when it's malformed or too old to keep."""
    if not isinstance(raw, dict):
        return None
    prompt_id, status = raw.get("id"), raw.get("status")
    created, expires = raw.get("created_at"), raw.get("expires_at")
    if (not isinstance(prompt_id, str) or not _PROMPT_ID.match(prompt_id) or status not in STATES
            or not isinstance(raw.get("title"), str) or not isinstance(raw.get("preview"), str)
            or not isinstance(raw.get("action_label"), str)
            or not all(isinstance(value, int) and not isinstance(value, bool) for value in (created, expires))):
        return None
    spoken = raw.get("spoken_yes")
    prompt: dict[str, Any] = {
        # Cleaned again on the way out, as on the way in: the file is text
        # anyone with the account could edit.
        "id": prompt_id, "title": _line(raw["title"], MAX_TITLE_CHARS),
        "preview": _text(raw["preview"], MAX_PREVIEW_CHARS),
        "action_label": _line(raw["action_label"], MAX_ACTION_LABEL_CHARS) or "Confirm",
        "spoken_yes": [phrase for phrase in (_line(item, MAX_SPOKEN_YES_CHARS) for item in spoken) if phrase][
            :MAX_SPOKEN_YES] if isinstance(spoken, list) else [],
        "session_id": _line(raw.get("session_id"), 180),
        "created_at": created, "expires_at": expires, "status": status, "reported": raw.get("reported") is True,
    }
    if raw.get("maps") is not None:
        navigate = _maps(raw.get("maps"))
        if navigate is not None:
            prompt["maps"] = navigate
    if raw.get("via") in VIAS:
        prompt["via"] = raw["via"]
    edit_text = _text(raw.get("edit_text"), MAX_EDIT_TEXT_CHARS)
    if edit_text:
        prompt["edit_text"] = edit_text
    device = raw.get("device")
    if isinstance(device, str) and _INSTALLATION.match(device):
        prompt["device"] = device
    finished = raw.get("finished_at")
    if isinstance(finished, int) and not isinstance(finished, bool):
        prompt["finished_at"] = finished
    if status == "waiting" and now >= expires:
        # Nobody answered in time: the agent learns it as a no.
        prompt.update(status="expired", finished_at=expires, reported=False)
    if prompt["status"] != "waiting" and now - prompt.get("finished_at", expires) > FINISHED_TTL_S:
        return None
    return prompt


def _normalized(raw: Any, now: float) -> dict[str, Any]:
    """A well-formed state from whatever is on disk, with waiting prompts past
    their time expired and old entries dropped."""
    state: dict[str, Any] = {"version": VERSION, "devices": {}, "prompts": [], "asked": [], "cards": [],
                             "shown": []}
    if not isinstance(raw, dict):
        return state
    devices = raw.get("devices")
    for installation, device in devices.items() if isinstance(devices, dict) else ():
        if not isinstance(installation, str) or not _INSTALLATION.match(installation) or not isinstance(device, dict):
            continue
        seen = device.get("seen_at")
        if not isinstance(seen, (int, float)) or isinstance(seen, bool) or now - seen > DEVICE_TTL_S:
            continue
        state["devices"][installation] = {"name": _line(device.get("name"), MAX_DEVICE_NAME_CHARS) or "Conduit",
                                          "app_version": _line(device.get("app_version"), 40),
                                          "features": _features(device.get("features")),
                                          "seen_at": float(seen)}
    prompts = raw.get("prompts")
    seen_ids: set[str] = set()
    for item in prompts if isinstance(prompts, list) else []:
        prompt = _prompt(item, now)
        if prompt is not None and prompt["id"] not in seen_ids:
            seen_ids.add(prompt["id"])
            state["prompts"].append(prompt)
    _trim_prompts(state)
    asked = raw.get("asked")
    if isinstance(asked, list):
        state["asked"] = [at for at in asked if isinstance(at, (int, float)) and not isinstance(at, bool)
                          and now - at < HOUR_S][-PER_HOUR:]
    cards = raw.get("cards")
    card_ids: set[str] = set()
    for item in cards if isinstance(cards, list) else []:
        card = _card(item, now)
        if card is not None and card["id"] not in card_ids:
            card_ids.add(card["id"])
            state["cards"].append(card)
    state["cards"] = state["cards"][-MAX_CARDS:]
    shown = raw.get("shown")
    if isinstance(shown, list):
        state["shown"] = [at for at in shown if isinstance(at, (int, float)) and not isinstance(at, bool)
                          and now - at < HOUR_S][-CARDS_PER_HOUR:]
    return state


def _for_device(card: dict[str, Any], features: list[str]) -> dict[str, Any]:
    """A copy of the card for one phone: one that doesn't draw information
    cards gets the rest, with the Markdown shown (it covers what was left
    out), or just the Markdown when nothing else is left."""
    card = dict(card)
    schema = _card_schema()
    if INFO_CARDS_FEATURE in features or not card.get("cards") or schema is None:
        return card
    kept = [rich for rich in card["cards"] if rich.get("type") not in schema.INFO_TYPES]
    if len(kept) == len(card["cards"]):
        return card
    if kept:
        card["cards"] = kept
    else:
        card.pop("cards")
    card.pop("fallback", None)
    return card


def _shows_cards(state: dict[str, Any], now: float) -> bool:
    return any(SHOW_FEATURE in device["features"] and now - device["seen_at"] < SHOW_DEVICE_RECENT_S
               for device in state["devices"].values())


def _card(raw: Any, now: float) -> dict[str, Any] | None:
    """A well-formed card from disk, cleaned again, or None when it's
    malformed or past its time."""
    if not isinstance(raw, dict):
        return None
    card_id, created, expires = raw.get("id"), raw.get("created_at"), raw.get("expires_at")
    if (not isinstance(card_id, str) or not _PROMPT_ID.match(card_id)
            or not all(isinstance(value, int) and not isinstance(value, bool) for value in (created, expires))
            # No longer-lived card than the tool makes, even hand-edited.
            or not 0 < expires - created <= CARD_TTL_S or now >= expires):
        return None
    title = _line(raw.get("title"), MAX_CARD_TITLE_CHARS)
    markdown = _text(raw.get("markdown"), MAX_CARD_MARKDOWN_CHARS)
    if not title or not markdown:
        return None
    card = {"id": card_id, "title": title, "markdown": markdown, "session_id": _line(raw.get("session_id"), 180),
            "created_at": created, "expires_at": expires}
    schema = _card_schema()
    rich = schema.normalize_cards(raw.get("cards")) if schema else []
    if rich:
        card["cards"] = rich
        if raw.get("fallback") is True:
            card["fallback"] = True
    return card


_card_schema_module: Any = None
_card_schema_lock = threading.Lock()


_CARD_SCHEMA_MISSING = object()


def _card_schema() -> Any:
    """card_schema.py beside this file, loaded by path once: the dashboard
    loads this module by path too, so it can't import the package. None in
    an install that lost it or its schema, where cards are left out and
    everything else here still works."""
    global _card_schema_module
    with _card_schema_lock:
        if _card_schema_module is None:
            path = Path(__file__).resolve().parent / "card_schema.py"
            spec = importlib.util.spec_from_file_location("conduit_push_present_card_schema", path)
            try:
                if spec is None or spec.loader is None:
                    raise ImportError("card_schema.py is missing")
                module = importlib.util.module_from_spec(spec)
                spec.loader.exec_module(module)
            except Exception:  # noqa: BLE001 — a broken install draws no cards
                module = _CARD_SCHEMA_MISSING
            _card_schema_module = module
        return None if _card_schema_module is _CARD_SCHEMA_MISSING else _card_schema_module


@contextmanager
def _exclusive(handle: Any) -> Iterator[None]:
    # The same locking as calls_store: flock where there is one, a
    # first-byte lock on Windows.
    if fcntl is not None:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
    elif msvcrt is not None:
        handle.seek(0)
        for attempt in range(6):  # about a minute
            try:
                msvcrt.locking(handle.fileno(), msvcrt.LK_LOCK, 1)
                break
            except OSError:
                if attempt == 5:
                    raise
        try:
            yield
        finally:
            handle.seek(0)
            msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
    else:
        yield
