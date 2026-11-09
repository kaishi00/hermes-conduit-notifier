"""conduit_show calls a model wrote out as text in its reply instead of
making them.

Hermes puts a plugin's tools behind its tool-search bridge, so a card is a
call inside a call: ``tool_call`` naming ``conduit_show``, whose arguments
hold the cards. A weak model writes that out as text in its reply
(``<tool_call><function=conduit_show><args><title>…``) and the user gets
markup instead of a card (Eric, 2026-11-09: Furina's three website concepts).
The plugin finds such a call in the reply, shows it as conduit_show would
have, and the reply keeps the board's line (``::conduit-board{id="cb-…"}``)
in its place, which Conduit draws (show_tool, room_boards.DesktopBoards).

Reads the shapes these models write: ``<function=NAME>`` with
``<parameter=KEY>`` values (Qwen's) or an ``<args>`` element with one
element per argument, and a JSON object with ``name`` and ``arguments``
(Hermes'), each inside ``<tool_call>`` and, when the reply ends first,
unclosed. A call through the bridge (``tool_call`` with ``name`` and
``arguments``, or ``calls``) counts as the call it makes. Imports nothing
from the plugin package.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any

SHOW = "conduit_show"
BRIDGE = "tool_call"

# A block runs to its closing tag, or to the end of a reply cut short (with
# any half-written closing tag at the end).
_BLOCK = re.compile(r"<tool_call>(?P<body>.*?)(?:</tool_call>|</?[a-z_]{0,12}\s*\Z|\Z)", re.DOTALL)
_FUNCTION = re.compile(r"<function=(?P<name>[\w.-]{1,64})>(?P<body>.*?)(?:</function>|\Z)", re.DOTALL)
_PARAMETER = re.compile(r"<parameter=(?P<key>[\w.-]{1,64})>(?P<value>.*?)(?:</parameter>|(?=<parameter=)|\Z)",
                        re.DOTALL)
_ARGS = re.compile(r"<(?:args|arguments)>(?P<body>.*?)(?:</(?:args|arguments)>|\Z)", re.DOTALL)
# A fenced example in a reply (how to call it) is text, never a call.
_FENCE = re.compile(r"^[ \t]*(?P<fence>```|~~~).*?(?:^[ \t]*(?P=fence)|\Z)", re.DOTALL | re.MULTILINE)
# Arguments that are text: markup in them (HTML in Markdown) stays as written.
TEXT_ARGS = frozenset({"title", "subtitle", "markdown"})
# Where a block with no </tool_call> ends: after the call's last closing tag,
# so prose the model wrote after it stays the reply's.
_CALL_CLOSE = re.compile(r"</(?:function|args|arguments|parameter)>")
_CLOSING_TAIL = re.compile(r"(?:\s*</?[a-z_]{0,12}>?)*\s*\Z")
MAX_REPLY = 200_000


@dataclass
class Leak:
    """A call written out in a reply: where it is, and what it calls."""
    start: int
    end: int
    name: str
    args: dict[str, Any] = field(default_factory=dict)


def shows(text: Any) -> list[Leak]:
    """The conduit_show calls ``text`` writes out, in order."""
    if not isinstance(text, str) or "<tool_call>" not in text or len(text) > MAX_REPLY:
        return []
    fenced = [(match.start(), match.end()) for match in _FENCE.finditer(text)]
    found = []
    for match in _BLOCK.finditer(text):
        if any(start <= match.start() < end for start, end in fenced):
            continue
        call = _call(match.group("body"))
        if call is None:
            continue
        name, args = _unbridged(*call)
        if name == SHOW:
            found.append(Leak(match.start(), _end(text, match), name, args))
    return found


def _end(text: str, match: re.Match) -> int:
    """Where a block ends: its closing tag, or, left unclosed, its call's
    last closing tag and any closing tags after it."""
    block = match.group(0)
    if block.endswith("</tool_call>"):
        return match.end()
    closes = list(_CALL_CLOSE.finditer(block))
    if not closes:
        return match.end()
    after = closes[-1].end()
    if "<parameter=" in block[after:]:
        return match.end()  # a parameter left unfinished is the call's
    tail = _CLOSING_TAIL.match(block, after)
    return match.start() + (tail.end() if tail else after)


def _call(body: str) -> tuple[str, dict[str, Any]] | None:
    stripped = body.strip()
    if stripped.startswith("{"):
        parsed = _json(stripped)
        if isinstance(parsed, dict) and isinstance(parsed.get("name"), str):
            return parsed["name"], _arguments(parsed.get("arguments"))
        return None
    function = _FUNCTION.search(body)
    if function is None:
        return None
    inner = function.group("body")
    if "<parameter=" in inner:
        return function.group("name"), {m.group("key"): (m.group("value").strip() if m.group("key") in TEXT_ARGS
                                                         else _value(m.group("value")))
                                        for m in _PARAMETER.finditer(inner)}
    args = _ARGS.search(inner)
    if args is not None:
        return function.group("name"), _elements(args.group("body"))
    parsed = _json(inner.strip())
    return function.group("name"), parsed if isinstance(parsed, dict) else {}


def _unbridged(name: str, args: dict[str, Any]) -> tuple[str, dict[str, Any]]:
    """A call through Hermes' bridge as the call it makes (its first)."""
    if name != BRIDGE:
        return name, args
    calls = args.get("calls")
    if isinstance(calls, str):
        calls = _json(calls)
    if isinstance(calls, dict):
        calls = [calls]
    first = calls[0] if isinstance(calls, list) and calls and isinstance(calls[0], dict) else args
    inner = first.get("name")
    return (inner, _arguments(first.get("arguments"))) if isinstance(inner, str) else (name, args)


def _arguments(value: Any) -> dict[str, Any]:
    if isinstance(value, str):
        value = _json(value)
    return value if isinstance(value, dict) else {}


def _elements(body: str) -> dict[str, Any]:
    """An ``<args>`` element's arguments, one element each."""
    value = _tree(body, text_keys=TEXT_ARGS)
    return value if isinstance(value, dict) else {}


def _value(raw: str) -> Any:
    """An argument's value: JSON where it reads as an object or a list
    (cards), elements as what they hold (``<cards><item>…`` a list of cards),
    otherwise its text."""
    text = raw.strip()
    if text[:1] in ("{", "["):
        parsed = _json(text)
        if parsed is not None:
            return parsed
    if text.startswith("<") and _TAG.match(text):
        tree = _tree(text)
        if tree not in ({}, [], ""):
            return tree
    return text


_TAG = re.compile(r"<(?P<close>/?)(?P<name>[A-Za-z_][\w-]{0,63})>")
MAX_DEPTH = 12


def _tree(body: str, text_keys: frozenset[str] = frozenset()) -> Any:
    """Elements as values: an element holding only text is its text (JSON
    where it reads as JSON), one holding elements a dict of them, and one
    whose elements share a name (``<item>``) a list. One holding text and
    elements both (HTML in Markdown) is its text as written, and so is a
    top-level element named in ``text_keys``. A tag left open closes where
    its parent does; other markup in text stays text."""
    root: list[Any] = ["", [], [], 0, len(body)]  # name, children, text, where its inside starts and ends
    stack = [root]
    at = 0
    for match in _TAG.finditer(body):
        stack[-1][2].append(body[at:match.start()])
        at = match.end()
        name = match.group("name")
        if match.group("close"):
            if any(node[0] == name for node in stack[1:]):
                while stack[-1][0] != name:
                    stack.pop()[4] = match.start()
                stack.pop()[4] = match.start()
            else:
                stack[-1][2].append(match.group(0))  # a stray closing tag is text
        elif len(stack) <= MAX_DEPTH:
            node = [name, [], [], match.end(), len(body)]
            stack[-1][1].append(node)
            stack.append(node)
        else:
            stack[-1][2].append(match.group(0))
    stack[-1][2].append(body[at:])
    if text_keys:
        for child in root[1]:
            if child[0] in text_keys:
                child[1], child[2] = [], [body[child[3]:child[4]]]
    return _node_value(root, body)


def _node_value(node: list[Any], body: str) -> Any:
    children = node[1]
    if children and "".join(node[2]).strip():
        return body[node[3]:node[4]].strip()  # text with markup in it
    if not children:
        text = "".join(node[2]).strip()
        parsed = _json(text) if text[:1] in ("{", "[") else None
        return parsed if parsed is not None else text
    names = [child[0] for child in children]
    if len(children) > 1 and len(set(names)) == 1 or names == ["item"]:
        return [_node_value(child, body) for child in children]
    value: dict[str, Any] = {}
    for child in children:
        item = _node_value(child, body)
        if child[0] in value:
            value[child[0]] = (value[child[0]] if isinstance(value[child[0]], list) else [value[child[0]]]) + [item]
        else:
            value[child[0]] = item
    return value


def _json(text: str) -> Any:
    try:
        return json.loads(text)
    except ValueError:
        return None
