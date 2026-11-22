"""Optional flags an older Hermes route doesn't take are left out of the call
(logged once) instead of failing every call with a TypeError."""

import importlib.util
import logging
import pathlib

ROOT = pathlib.Path(__file__).resolve().parents[1]
_spec = importlib.util.spec_from_file_location("conduit_plugin_api_flags", ROOT / "dashboard" / "plugin_api.py")
_api = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_api)
core = _api.core


async def _old(session_id, profile=None, limit=None, offset=0, order=None, include_compacted=False):
    return {}


async def _new(session_id, profile=None, include_compacted=False, inline_images=True):
    return {}


async def _open(session_id, **kwargs):
    return {}


def test_a_flag_the_route_lacks_is_dropped_and_logged_once(caplog):
    core._unknown_flags_logged.clear()
    flags = {"include_compacted": True, "inline_images": False}
    with caplog.at_level(logging.WARNING):
        assert core.supported_flags(_old, flags) == {"include_compacted": True}
        assert core.supported_flags(_old, flags) == {"include_compacted": True}
    assert caplog.text.count("has no inline_images") == 1


def test_a_route_that_takes_the_flags_gets_them_all():
    flags = {"include_compacted": True, "inline_images": False}
    assert core.supported_flags(_new, flags) == flags
    assert core.supported_flags(_open, flags) == flags
