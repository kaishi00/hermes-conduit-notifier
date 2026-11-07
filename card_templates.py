"""Saved card templates: a composed card Hermes saved once under a name and
fills by name afterwards (conduit_show's ``save_templates``, and a card of
type ``template``), so a card it shows often costs a few values, not its
whole layout (Eric picked saved templates with composed cards, 2026-11-07,
issue 1946).

A template is a composed card whose text holds ``{{slot}}`` placeholders. A
section with ``repeat`` names a list slot and is drawn once per item, its
placeholders written ``{{slot.key}}``. Filling happens here, before the card
is normalized, so Conduit and the Desktop half only ever see a composed
card. Templates are kept per profile.
"""

from __future__ import annotations

import copy
import json
import os
import re
import tempfile
import threading
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Callable, Iterator

from . import card_schema
from .checklist_store import _exclusive, _sync_directory

FILE_NAME = "conduit-card-templates.json"
MAX_TEMPLATES = 50
MAX_SAVES_PER_CALL = 4
MAX_TEMPLATE_BYTES = 16 * 1024
MAX_ABOUT_CHARS = 160
MAX_REPEAT_ITEMS = 20
MAX_FILL_CHARS = 2000
NAME = re.compile(r"^[a-z0-9][a-z0-9-]{0,39}$")
_SLOT = re.compile(r"\{\{\s*([A-Za-z0-9_]+(?:\.[A-Za-z0-9_]+)?)\s*\}\}")

SAVE_SCHEMA: dict[str, Any] = {
    "type": "array",
    "maxItems": MAX_SAVES_PER_CALL,
    "description": ("Save composed cards you'll show again as templates, then show one by name with a card of type "
                    "template. Save one when the user wants the same kind of card again (a daily agenda, an email "
                    "draft, a delivery). Saved templates stay on this host for every chat; the drawing rule lists "
                    "them."),
    "items": {
        "type": "object",
        "properties": {
            "name": {"type": "string", "maxLength": 40,
                     "description": "Lowercase letters, digits and dashes, like email-draft."},
            "about": {"type": "string", "maxLength": MAX_ABOUT_CHARS, "description": "What it's for, in a line."},
            "card": {"type": "object",
                     "description": ("A composed card as in cards, with {{slot}} wherever a value goes, like "
                                     "{\"type\":\"composed\",\"sections\":[{\"items\":[{\"kind\":\"text\","
                                     "\"text\":\"{{subject}}\"}]}]}. A section with \"repeat\": \"events\" is "
                                     "drawn once per item of the list slot events, written {{events.time}}. "
                                     "Leave card out to delete the template.")},
        },
        "required": ["name"],
    },
}

TEMPLATE_INPUT: dict[str, Any] = {
    "type": "object",
    "description": "template only: a saved template and the values for its slots.",
    "properties": {
        "name": {"type": "string", "maxLength": 40, "description": "The template's name."},
        "fill": {"type": "object",
                 "description": ("Each slot's value: text, a number, or for a repeat section's slot a list of "
                                 "objects, like {\"subject\": \"Friday\", \"events\": [{\"time\": \"9:00\"}]}.")},
    },
    "required": ["name"],
}

_lock = threading.Lock()


class TemplateError(ValueError):
    pass


def _name(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    name = card_schema.clean_text(value).lower()
    return name if NAME.match(name) else None


class TemplateStore:
    def __init__(self, home: Any, clock: Callable[[], float] = time.time):
        self.path = Path(home) / FILE_NAME
        self.clock = clock

    def _load(self) -> list[dict[str, Any]]:
        try:
            state = json.loads(self.path.read_text(encoding="utf-8"))
        except (FileNotFoundError, ValueError):
            return []
        templates = state.get("templates") if isinstance(state, dict) else None
        if not isinstance(templates, list):
            return []
        return [entry for entry in templates if isinstance(entry, dict) and _name(entry.get("name"))
                and isinstance(entry.get("about"), str) and isinstance(entry.get("card"), dict)]

    def all(self) -> list[dict[str, Any]]:
        return self._load()

    def names(self) -> list[str]:
        return [entry["name"] for entry in self._load()]

    def get(self, name: Any) -> dict[str, Any] | None:
        wanted = _name(name)
        return next((entry for entry in self._load() if entry["name"] == wanted), None) if wanted else None

    def save(self, given: Any, notes: list[str]) -> list[str]:
        """Saves (or deletes) the templates given; answers what was saved
        or deleted, as lines for the model, and adds problems to notes."""
        if not isinstance(given, list):
            notes.append("save_templates: not a list, nothing saved")
            return []
        changes: list[tuple[str, dict[str, Any] | None]] = []
        done: list[str] = []
        for index, entry in enumerate(given[:MAX_SAVES_PER_CALL]):
            path = f"save_templates[{index}]"
            if not isinstance(entry, dict):
                notes.append(f"{path}: not an object, not saved")
                continue
            name = _name(entry.get("name"))
            if not name:
                notes.append(f"{path}.name: use lowercase letters, digits and dashes (up to 40), not saved")
                continue
            card = entry.get("card")
            if card is None:
                changes.append((name, None))
                continue
            problem = _template_problem(card)
            if problem:
                notes.append(f"{path} {name}: {problem}, not saved")
                continue
            # Checked filled with sample text, so its mistakes show now.
            checks: list[str] = []
            sample = expand_template(card, _sample_fill(card), checks, f"{path}.card")
            if sample is None or not card_schema.normalize_cards([sample], checks):
                notes.extend(checks[:10])
                notes.append(f"{path} {name}: no composed card is left with sample text in its slots, not saved")
                continue
            notes.extend(f"{line} (with sample text in its slots)" for line in checks[:10])
            about = card_schema.clean_text(entry["about"])[:MAX_ABOUT_CHARS] \
                if isinstance(entry.get("about"), str) else ""
            changes.append((name, {"name": name, "about": about, "card": card}))
        if len(given) > MAX_SAVES_PER_CALL:
            notes.append(f"save_templates: at most {MAX_SAVES_PER_CALL} at once, the rest weren't saved")
        if not changes:
            return done
        with _locked(self.path):
            templates = self._load()
            for name, template in changes:
                had = any(entry["name"] == name for entry in templates)
                templates = [entry for entry in templates if entry["name"] != name]
                if template is None:
                    done.append(f"{name}: deleted" if had else f"{name}: there was no such template")
                    continue
                templates.append({**template, "saved_at": self.clock()})
                done.append(f"{name}: saved")
            if len(templates) > MAX_TEMPLATES:
                dropped = [entry["name"] for entry in templates[:-MAX_TEMPLATES]]
                notes.append(f"save_templates: at most {MAX_TEMPLATES} are kept, so the oldest went: "
                             f"{', '.join(dropped)}")
                templates = templates[-MAX_TEMPLATES:]
            _write(self.path, {"templates": templates})
        return done

    def fill(self, raw: dict[str, Any], notes: list[str], path: str) -> dict[str, Any] | None:
        """A card of type template as the composed card it fills, with the
        call's own title, live name, source and actions over the template's."""
        given = raw.get("template")
        if not isinstance(given, dict):
            notes.append(f"{path}.template: needs the template's name and fill, dropped")
            return None
        template = self.get(given.get("name"))
        if template is None:
            saved = self.names()
            notes.append(f"{path}.template.name: no saved template is called that "
                         f"({'saved: ' + ', '.join(saved) if saved else 'none is saved yet'}), dropped")
            return None
        fill = given.get("fill") if isinstance(given.get("fill"), dict) else {}
        card = expand_template(template["card"], fill, notes, f"{path}.template")
        if card is None:
            return None
        for key in ("title", "live", "source", "actions"):
            if raw.get(key) is not None:
                card[key] = raw[key]
        return card


def _template_problem(card: Any) -> str | None:
    if not isinstance(card, dict) or not isinstance(card.get("type"), str) or \
            card_schema.clean_text(card["type"]).lower() != "composed":
        return "a template is a composed card"
    try:
        size = len(json.dumps(card, ensure_ascii=False).encode("utf-8"))
    except (TypeError, ValueError):
        return "not JSON"
    if size > MAX_TEMPLATE_BYTES:
        return f"over {MAX_TEMPLATE_BYTES // 1024} KB"
    return None


# Fields a whole slot fills with a number.
_NUMBER_KEYS = ("progress", "min", "max")


def _slots(value: Any, found: set[str], numbers: set[str], key: str = "") -> None:
    if isinstance(value, str):
        found.update(match.group(1) for match in _SLOT.finditer(value))
        whole = _SLOT.fullmatch(value.strip())
        if whole and key in _NUMBER_KEYS:
            numbers.add(whole.group(1))
    elif isinstance(value, dict):
        for inner_key, inner in value.items():
            _slots(inner, found, numbers, inner_key)
    elif isinstance(value, list):
        for inner in value:
            _slots(inner, found, numbers, key)


def _sample_fill(card: dict[str, Any]) -> dict[str, Any]:
    found: set[str] = set()
    numbers: set[str] = set()
    _slots(card, found, numbers)
    fill: dict[str, Any] = {}
    for slot in sorted(found):
        head, _, key = slot.partition(".")
        sample = 0 if slot in numbers else "Sample"
        if key:
            items = fill.setdefault(head, [{}])
            if isinstance(items, list):
                items[0][key] = sample
        else:
            fill.setdefault(head, sample)
    return fill


def expand_template(card: dict[str, Any], fill: dict[str, Any], notes: list[str], path: str) -> dict[str, Any] | None:
    """The template with its slots filled and its repeat sections drawn once
    per item; a slot the fill doesn't give becomes empty text."""
    card = copy.deepcopy(card)
    missing: dict[str, str] = {}
    sections = card.get("sections")
    if isinstance(sections, list):
        drawn = []
        for section in sections:
            if isinstance(section, dict) and isinstance(section.get("repeat"), str):
                slot = section.pop("repeat")
                items = fill.get(slot)
                if not isinstance(items, list):
                    notes.append(f"{path}.fill.{slot}: not given, its section left out")
                    continue
                if len(items) > MAX_REPEAT_ITEMS:
                    notes.append(f"{path}.fill.{slot}: at most {MAX_REPEAT_ITEMS} items, the rest were left out")
                for item in items[:MAX_REPEAT_ITEMS]:
                    scope = {f"{slot}.{key}": value for key, value in item.items()} if isinstance(item, dict) else {}
                    drawn.append(_filled(section, scope, missing, prefix=f"{slot}."))
            else:
                drawn.append(section)
        card["sections"] = drawn
    card = _filled(card, fill, missing)
    for slot in sorted(missing):
        notes.append(f"{path}.fill.{slot}: {missing[slot]}, left empty (an element left with nothing is left out)")
    return card


def _a_kind(value: Any) -> str:
    return "a list" if isinstance(value, list) else "an object" if isinstance(value, dict) else "a value"


def _filled(value: Any, fill: dict[str, Any], missing: dict[str, str], prefix: str = "", key: str = "") -> Any:
    if isinstance(value, str):
        whole = _SLOT.fullmatch(value.strip())
        if whole and key in _NUMBER_KEYS and (not prefix or whole.group(1).startswith(prefix)):
            given = fill.get(whole.group(1))
            # A slot that is the whole value takes a number or true/false as
            # it is (a progress, a min), so it stays one.
            if isinstance(given, (int, float)) and not isinstance(given, bool):
                return given
        def put(match: re.Match[str]) -> str:
            slot = match.group(1)
            if prefix and not slot.startswith(prefix):
                return match.group(0)
            given = fill.get(slot)
            if isinstance(given, bool):
                return "on" if given else "off"
            if isinstance(given, (int, float)):
                return card_schema._cell(given)
            if isinstance(given, str):
                return given[:MAX_FILL_CHARS]
            # A list or object given for a text slot is the model's mistake, not a gap.
            missing[slot] = "not given" if given is None else f"{_a_kind(given)}, which this slot can't take"
            return ""
        return _SLOT.sub(put, value)
    if isinstance(value, dict):
        return {inner_key: _filled(inner, fill, missing, prefix, inner_key) for inner_key, inner in value.items()}
    if isinstance(value, list):
        return [_filled(inner, fill, missing, prefix, key) for inner in value]
    return value


@contextmanager
def _locked(path: Path) -> Iterator[None]:
    """Held while the file is read and written: two agents may save at once."""
    path.parent.mkdir(parents=True, exist_ok=True)
    lock_path = path.with_name(f".{path.name}.lock")
    with _lock, open(lock_path, "a+b") as handle, _exclusive(handle):
        yield


def _write(path: Path, state: dict[str, Any]) -> None:
    handle, temporary = tempfile.mkstemp(dir=path.parent, prefix=".conduit-card-templates-")
    try:
        with os.fdopen(handle, "w", encoding="utf-8") as file:
            json.dump(state, file, ensure_ascii=False)
            file.flush()
            os.fsync(file.fileno())
        os.chmod(temporary, 0o600)
        os.replace(temporary, path)
        _sync_directory(path.parent)
    except BaseException:
        try:
            os.unlink(temporary)
        except OSError:
            pass
        raise
