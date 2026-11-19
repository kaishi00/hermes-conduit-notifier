"""A model the bot names for one workstream at its start (app issue 2788).

The bot's Workstream Model (Edit Bot > Work) stays the default. ``work_start``
may name another model for that one workstream, when the user's own words call
for it (what they said, their SOUL.md, notes); Conduit never picks one itself.
Here the name is checked as Hermes' own ``/model`` checks it, in the bot's
profile: its providers, their catalogs and credentials, aliases such as
``opus``. A name Hermes refuses starts nothing, and its reason, with the
closest names it found, goes back to the bot.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any, Optional

logger = logging.getLogger(__name__)

# Hermes' levels (hermes_constants.VALID_REASONING_EFFORTS) and "none" for off.
REASONING = ("none", "minimal", "low", "medium", "high", "xhigh", "max", "ultra")
MAX_CHARS = 200


class Refused(ValueError):
    """The name or level can't be run; ``str()`` is the note for the bot."""


def _line(value: Any) -> str:
    return " ".join(str(value).split()) if isinstance(value, str) else ""


def requested(args: dict) -> dict:
    """The bot's ``model``, ``provider`` and ``reasoning_effort`` from work_start's
    arguments, as given ({} when it named no model). ``model`` may also be an
    object carrying the three."""
    raw = args.get("model")
    given = raw if isinstance(raw, dict) else {"model": raw}
    picked = {key: _line(given.get(key) if key in given else args.get(key))
              for key in ("model", "provider", "reasoning_effort")}
    return {key: value for key, value in picked.items() if value} if picked["model"] else {}


def check(home: Path, profile: str, asked: dict) -> dict:
    """``asked`` (from ``requested``) as Hermes would run it: the model's own id
    and its provider, with the reasoning level. Raises Refused when Hermes
    can't run it or the level isn't one of Hermes'."""
    model, provider = asked.get("model", ""), asked.get("provider", "")
    effort = asked.get("reasoning_effort", "").lower()
    if any(len(value) > MAX_CHARS for value in (model, provider)):
        raise Refused("That model name is too long to be one. Name a model from the bot's providers.")
    if effort and effort not in REASONING:
        raise Refused(f"Reasoning {effort} isn't a level Hermes has. Use one of: {', '.join(REASONING)}, "
                      "or leave it out for the model's own.")
    resolved = _switch(home, profile, model, provider)
    picked = {"model": resolved.get("model") or model}
    if resolved.get("provider") or provider:
        picked["provider"] = resolved.get("provider") or provider
    if effort:
        picked["reasoning_effort"] = effort
    return picked


def _switch(home: Path, profile: str, model: str, provider: str) -> dict:
    """Hermes' /model pipeline (``switch_model``) in ``profile``'s scope; nothing
    is saved. {} when this Hermes can't check (too old, or the check itself
    broke): the name goes to Hermes as given, whose session.create still
    refuses a model its provider can't serve."""
    try:
        from hermes_cli.inventory import load_picker_context
        from hermes_cli.model_switch import switch_model
        from hermes_cli.models_validate import static_model_provider_conflict
        from hermes_cli.providers import resolve_provider_full
    except ImportError:
        return {}
    from . import room_setup

    try:
        with room_setup.profile_scope(profile):
            ctx = load_picker_context()
            if not provider:
                # ``anthropic:claude-opus-5-5``: /model takes the provider apart (--provider), so a
                # leading name that is one of Hermes' providers is taken as it, the longest first
                # (``custom:fake:model``); a model id with a colon of its own (``llama3:8b``) stays whole.
                cuts = [index for index, char in enumerate(model) if char == ":"]
                for index in reversed(cuts):
                    head, rest = model[:index].strip(), model[index + 1:].strip()
                    if head and rest and resolve_provider_full(head, ctx.user_providers, ctx.custom_providers):
                        provider, model = head, rest
                        break
            result = switch_model(model, ctx.current_provider, ctx.current_model,
                                  current_base_url=ctx.current_base_url, explicit_provider=provider,
                                  user_providers=ctx.user_providers, custom_providers=ctx.custom_providers)
            reason = " ".join(str(getattr(result, "error_message", "") or "").split())
            picked = {"model": str(getattr(result, "new_model", "") or ""),
                      "provider": str(getattr(result, "target_provider", "") or "")}
            if getattr(result, "success", False) and picked["model"]:
                # What session.create would refuse anyway (a model its provider can't serve), said
                # before a row is kept for it.
                conflict = static_model_provider_conflict(picked["model"], picked["provider"] or None)
                if conflict:
                    result, reason = None, " ".join(str(conflict.get("message") or "").split())
    except Exception:  # noqa: BLE001 — Hermes' own start still checks it
        logger.warning("conduit_push: checking a workstream's model failed", exc_info=True)
        return {}
    if not getattr(result, "success", False):
        raise Refused(f"Hermes can't run {model}" + (f" here: {reason}" if reason else " here.")
                      + " Nothing was started. Use a name from that, or leave model out to run it on the "
                        "Workstream Model.")
    return picked


def label(picked: Optional[dict]) -> str:
    """``model`` with its reasoning, as the note and the card name it."""
    if not picked or not picked.get("model"):
        return ""
    effort = picked.get("reasoning_effort")
    return picked["model"] + (f" ({effort} reasoning)" if effort else "")
