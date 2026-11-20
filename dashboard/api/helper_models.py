"""Helper models: the slots Hermes' runtime uses that its dashboard leaves
out of its list (hermes-conduit#2871).

Hermes runs a review after each turn (what goes into memory, which skills to
patch) on its own `auxiliary.background_review` model, with an `enabled`
switch. Its dashboard's slot list (``web_server_config._AUX_TASK_SLOTS``)
doesn't name it, so `GET /api/model/auxiliary` doesn't list it and
`POST /api/model/set` refuses it (NousResearch/hermes-agent#157839), and the
model can only be changed in config.yaml. Until Hermes lists it, this adds
it to that list in the dashboard's process, after the built-ins, so both
routes take it as Hermes' fix will: a built-in row, which Conduit names. A
Hermes that lists it already is left as it is.
"""

from __future__ import annotations

import sys
from typing import List

from . import core

logger = core.logger

MISSING_SLOTS = ("background_review",)


def _add_missing_slots() -> List[str]:
    """Adds the slots Hermes' list lacks; the ones it added. Fail-soft: a
    Hermes laid out otherwise keeps its own list."""
    try:
        from hermes_cli import web_server_config
    except ImportError:
        logger.debug("conduit_push: no hermes_cli.web_server_config; helper model slots left as they are")
        return []
    slots = getattr(web_server_config, "_AUX_TASK_SLOTS", None)
    if not isinstance(slots, tuple):
        logger.debug("conduit_push: Hermes' _AUX_TASK_SLOTS is %s, not a tuple; helper model slots left as they are",
                     type(slots).__name__)
        return []
    missing = tuple(slot for slot in MISSING_SLOTS if slot not in slots)
    if not missing:
        return []
    widened = slots + missing
    # The writes and the plugin rows read this module's list; the slot list
    # route took the list by name when its module loaded, so it gets it too
    # (a route module that loads later takes the widened one).
    web_server_config._AUX_TASK_SLOTS = widened
    models = sys.modules.get("hermes_cli.web_routers.models")
    if models is not None and getattr(models, "_AUX_TASK_SLOTS", None) is slots:
        models._AUX_TASK_SLOTS = widened
    elif models is not None:
        # Hermes' slot list route reads its own copy: it won't list them.
        logger.debug("conduit_push: web_routers.models holds another slot list; GET /api/model/auxiliary won't "
                     "list %s", ", ".join(missing))
    logger.info("conduit_push: listed Hermes' helper model slots %s on the dashboard", ", ".join(missing))
    return list(missing)


if "hermes_cli.web_server" in sys.modules:
    # Loaded by the dashboard (not a test or the CLI).
    try:
        _add_missing_slots()
    except Exception:  # noqa: BLE001 — the helper model routes keep Hermes' own list
        logger.debug("conduit_push: couldn't list Hermes' missing helper model slots", exc_info=True)
