"""Tests for _update_plan_quota: read_only=False fix, 429 backoff, error isolation.

The function lives in conduit_push/__init__.py and calls:
  - agent.account_usage_cache.cached_account_usage
  - agent.account_usage_cache.snapshot_is_stale
  - agent.account_usage.fetch_account_usage
  - live_activity.reporter.plan_quota_updated
  - live_activity.reporter.plan_reset_updated

These are all monkeypatched for isolation.
"""

from __future__ import annotations

import importlib.util
import os
import sys
import types
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Optional
from unittest.mock import MagicMock, call, patch

import pytest

ROOT = __import__("pathlib").Path(__file__).resolve().parents[1]

# ── stub minimal hermes_constants so the plugin loads ─────────────────────────
if "hermes_constants" not in sys.modules:
    _hc = types.ModuleType("hermes_constants")
    _hc.get_hermes_home = lambda: __import__("pathlib").Path(os.environ["HERMES_HOME"])
    sys.modules["hermes_constants"] = _hc
if "conduit_push" not in sys.modules:
    _pkg = types.ModuleType("conduit_push")
    _pkg.__path__ = [str(ROOT)]
    sys.modules["conduit_push"] = _pkg


def _load(name, path):
    if name in sys.modules:
        return sys.modules[name]
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


plugin = _load("conduit_push.__init__", ROOT / "__init__.py")


# ── minimal stubs for account_usage types ─────────────────────────────────────

@dataclass
class _Window:
    label: str
    used_percent: Optional[float] = None
    reset_at: Optional[datetime] = None


@dataclass
class _Snapshot:
    windows: tuple = field(default_factory=tuple)
    unavailable_reason: Optional[str] = None


def _make_snapshot(used_pct: float = 60.0, label: str = "Current session",
                   reset_at: Optional[datetime] = None) -> _Snapshot:
    window = _Window(label=label, used_percent=used_pct, reset_at=reset_at)
    return _Snapshot(windows=(window,))


# ── fixtures ──────────────────────────────────────────────────────────────────

@pytest.fixture(autouse=True)
def reset_backoff():
    """Reset the module-level 429 sentinel before and after each test."""
    plugin._plan_quota_backoff_until = 0.0
    yield
    plugin._plan_quota_backoff_until = 0.0


# ── tests ─────────────────────────────────────────────────────────────────────

def test_anthropic_path_calls_plan_quota_updated_with_non_null_plan_left(monkeypatch):
    """Happy-path: stale cache → fetch → plan_quota_updated with non-null plan_left."""
    snapshot = _make_snapshot(used_pct=40.0)  # 40% used → 60% left → plan_left = 60

    monkeypatch.setitem(sys.modules, "agent.account_usage_cache", types.SimpleNamespace(
        cached_account_usage=lambda slug: None,
        snapshot_is_stale=lambda s: True,
    ))
    monkeypatch.setitem(sys.modules, "agent.account_usage", types.SimpleNamespace(
        fetch_account_usage=lambda provider, read_only=False: snapshot,
    ))

    plan_quota_calls = []
    plan_reset_calls = []
    monkeypatch.setattr(plugin.live_activity.reporter, "plan_quota_updated",
                        lambda sid, frac: plan_quota_calls.append((sid, frac)))
    monkeypatch.setattr(plugin.live_activity.reporter, "plan_reset_updated",
                        lambda sid, ms: plan_reset_calls.append((sid, ms)))

    plugin._update_plan_quota("s-1")

    assert len(plan_quota_calls) == 1, "plan_quota_updated should be called once"
    sid, left_fraction = plan_quota_calls[0]
    assert sid == "s-1"
    assert 0.0 < left_fraction <= 1.0, f"plan_left fraction should be non-null and in (0,1]: {left_fraction}"
    assert abs(left_fraction - 0.60) < 0.01, f"expected ~0.60, got {left_fraction}"
    assert len(plan_reset_calls) == 1


def test_fetch_uses_read_only_false(monkeypatch):
    """Verify fetch_account_usage is called with read_only=False, NOT read_only=True."""
    snapshot = _make_snapshot()
    fetch_calls = []

    def mock_fetch(provider, *, read_only=False):
        fetch_calls.append({"provider": provider, "read_only": read_only})
        return snapshot

    monkeypatch.setitem(sys.modules, "agent.account_usage_cache", types.SimpleNamespace(
        cached_account_usage=lambda slug: None,
        snapshot_is_stale=lambda s: True,
    ))
    monkeypatch.setitem(sys.modules, "agent.account_usage", types.SimpleNamespace(
        fetch_account_usage=mock_fetch,
    ))
    monkeypatch.setattr(plugin.live_activity.reporter, "plan_quota_updated", lambda *a: None)
    monkeypatch.setattr(plugin.live_activity.reporter, "plan_reset_updated", lambda *a: None)

    plugin._update_plan_quota("s-1")

    assert len(fetch_calls) == 1
    assert fetch_calls[0]["provider"] == "anthropic"
    assert fetch_calls[0]["read_only"] is False, "Must use read_only=False for Anthropic"


def test_plan_reset_updated_called_with_epoch_ms_when_reset_at_set(monkeypatch):
    """plan_reset_updated receives a non-null millisecond epoch when reset_at is set."""
    # 2026-01-15 12:00:00 UTC
    reset_dt = datetime(2026, 1, 15, 12, 0, 0, tzinfo=timezone.utc)
    snapshot = _make_snapshot(used_pct=20.0, reset_at=reset_dt)

    monkeypatch.setitem(sys.modules, "agent.account_usage_cache", types.SimpleNamespace(
        cached_account_usage=lambda slug: None,
        snapshot_is_stale=lambda s: True,
    ))
    monkeypatch.setitem(sys.modules, "agent.account_usage", types.SimpleNamespace(
        fetch_account_usage=lambda provider, read_only=False: snapshot,
    ))

    plan_reset_calls = []
    monkeypatch.setattr(plugin.live_activity.reporter, "plan_quota_updated", lambda *a: None)
    monkeypatch.setattr(plugin.live_activity.reporter, "plan_reset_updated",
                        lambda sid, ms: plan_reset_calls.append(ms))

    plugin._update_plan_quota("s-1")

    assert len(plan_reset_calls) == 1
    ms = plan_reset_calls[0]
    assert ms is not None, "plan_reset_ms should not be None when reset_at is set"
    assert isinstance(ms, int)
    assert ms > 0


def test_429_httpx_exception_sets_backoff_and_skips_next_call(monkeypatch):
    """HTTP 429 from the usage endpoint → 5-min backoff; next call within window skips fetch."""
    import importlib
    # Provide a stub httpx that raises HTTPStatusError on fetch
    _httpx = types.ModuleType("httpx")

    class _FakeResponse:
        status_code = 429

    class _HTTPStatusError(Exception):
        def __init__(self):
            self.response = _FakeResponse()

    _httpx.HTTPStatusError = _HTTPStatusError

    def _fetch_raises(provider, *, read_only=False):
        raise _HTTPStatusError()

    monkeypatch.setitem(sys.modules, "httpx", _httpx)
    monkeypatch.setitem(sys.modules, "agent.account_usage_cache", types.SimpleNamespace(
        cached_account_usage=lambda slug: None,
        snapshot_is_stale=lambda s: True,
    ))
    monkeypatch.setitem(sys.modules, "agent.account_usage", types.SimpleNamespace(
        fetch_account_usage=_fetch_raises,
    ))

    plan_quota_calls = []
    monkeypatch.setattr(plugin.live_activity.reporter, "plan_quota_updated",
                        lambda *a: plan_quota_calls.append(a))
    monkeypatch.setattr(plugin.live_activity.reporter, "plan_reset_updated", lambda *a: None)

    # First call: should hit the 429, set backoff, return without calling plan_quota_updated
    plugin._update_plan_quota("s-1")
    assert len(plan_quota_calls) == 0, "plan_quota_updated must not be called on 429"

    # Backoff sentinel must be set in the future
    import time
    assert plugin._plan_quota_backoff_until > time.monotonic(), \
        "_plan_quota_backoff_until must be in the future after 429"

    # Second call within the backoff window: fetch must NOT be called
    fetch_call_count = []

    def _fetch_tracking(provider, *, read_only=False):
        fetch_call_count.append(1)
        return _make_snapshot()

    monkeypatch.setitem(sys.modules, "agent.account_usage", types.SimpleNamespace(
        fetch_account_usage=_fetch_tracking,
    ))

    plugin._update_plan_quota("s-1")
    assert len(fetch_call_count) == 0, "fetch must be skipped while in 429 backoff window"
    assert len(plan_quota_calls) == 0, "plan_quota_updated must not be called in backoff window"


def test_429_backoff_expires_after_window(monkeypatch):
    """After the 5-min window expires, fetch is attempted again."""
    import time

    # Set backoff to expired (in the past)
    plugin._plan_quota_backoff_until = time.monotonic() - 1.0  # already expired

    snapshot = _make_snapshot(used_pct=30.0)
    fetch_calls = []

    def _fetch(provider, *, read_only=False):
        fetch_calls.append(1)
        return snapshot

    monkeypatch.setitem(sys.modules, "agent.account_usage_cache", types.SimpleNamespace(
        cached_account_usage=lambda slug: None,
        snapshot_is_stale=lambda s: True,
    ))
    monkeypatch.setitem(sys.modules, "agent.account_usage", types.SimpleNamespace(
        fetch_account_usage=_fetch,
    ))

    plan_quota_calls = []
    monkeypatch.setattr(plugin.live_activity.reporter, "plan_quota_updated",
                        lambda *a: plan_quota_calls.append(a))
    monkeypatch.setattr(plugin.live_activity.reporter, "plan_reset_updated", lambda *a: None)

    plugin._update_plan_quota("s-1")

    assert len(fetch_calls) == 1, "fetch should be called after backoff expired"
    assert len(plan_quota_calls) == 1, "plan_quota_updated should be called after backoff expired"


def test_error_isolation_exception_does_not_propagate(monkeypatch):
    """Any exception inside _update_plan_quota must not propagate — the turn is never broken."""
    monkeypatch.setitem(sys.modules, "agent.account_usage_cache", types.SimpleNamespace(
        cached_account_usage=lambda slug: None,
        snapshot_is_stale=lambda s: True,
    ))
    monkeypatch.setitem(sys.modules, "agent.account_usage", types.SimpleNamespace(
        fetch_account_usage=lambda *a, **kw: (_ for _ in ()).throw(RuntimeError("network is down")),
    ))

    plan_quota_calls = []
    monkeypatch.setattr(plugin.live_activity.reporter, "plan_quota_updated",
                        lambda *a: plan_quota_calls.append(a))

    # Must not raise
    plugin._update_plan_quota("s-1")

    assert len(plan_quota_calls) == 0, "plan_quota_updated must not be called when fetch raises"


def test_none_snapshot_does_not_call_plan_quota_updated(monkeypatch):
    """When fetch returns None (e.g. token missing), plan_quota_updated is NOT called."""
    monkeypatch.setitem(sys.modules, "agent.account_usage_cache", types.SimpleNamespace(
        cached_account_usage=lambda slug: None,
        snapshot_is_stale=lambda s: True,
    ))
    monkeypatch.setitem(sys.modules, "agent.account_usage", types.SimpleNamespace(
        fetch_account_usage=lambda *a, **kw: None,
    ))

    plan_quota_calls = []
    monkeypatch.setattr(plugin.live_activity.reporter, "plan_quota_updated",
                        lambda *a: plan_quota_calls.append(a))

    plugin._update_plan_quota("s-1")
    assert len(plan_quota_calls) == 0


def test_cache_hit_skips_fetch(monkeypatch):
    """When the cache is fresh, fetch_account_usage is NOT called."""
    snapshot = _make_snapshot(used_pct=50.0)
    fetch_calls = []

    monkeypatch.setitem(sys.modules, "agent.account_usage_cache", types.SimpleNamespace(
        cached_account_usage=lambda slug: snapshot,
        snapshot_is_stale=lambda s: False,  # cache is fresh
    ))
    monkeypatch.setitem(sys.modules, "agent.account_usage", types.SimpleNamespace(
        fetch_account_usage=lambda *a, **kw: fetch_calls.append(1) or snapshot,
    ))

    plan_quota_calls = []
    monkeypatch.setattr(plugin.live_activity.reporter, "plan_quota_updated",
                        lambda *a: plan_quota_calls.append(a))
    monkeypatch.setattr(plugin.live_activity.reporter, "plan_reset_updated", lambda *a: None)

    plugin._update_plan_quota("s-1")

    assert len(fetch_calls) == 0, "fetch must not be called when cache is fresh"
    assert len(plan_quota_calls) == 1, "plan_quota_updated must still be called from cached snapshot"
