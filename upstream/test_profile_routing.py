"""Which profile a plugin request reaches, on a dashboard serving several (hermes-conduit#1336).

Every test here checks where the request landed (the speech server URL path
of the profile whose settings were used), not only that it was answered: a
socket opened in the wrong profile is worse than one refused.
"""

from __future__ import annotations

import pytest

# The sockets that relay to the profile's speech server, and so show where they landed.
SPEECH_SOCKETS = ("local-live/socket", "speech-server/socket")


# Upstream compatibility: Hermes' own profile routing. These report a known
# limitation as xfail (contract.yml known_failures) and fail when it's fixed.

@pytest.mark.parametrize("route", SPEECH_SOCKETS)
def test_upstream_profile_socket_reaches_profile(dashboard, known_failure, route):
    result = dashboard.open_socket(route, profile="second")
    known_failure(result.dialed == ["/second"], result.signature, repr(result))


def test_upstream_profile_http_reaches_profile(dashboard):
    status, body = dashboard.get("local-live/status", profile="second")
    assert status == 200, body
    assert (body.get("speech_server") or {}).get("url") == dashboard.speech.url("second"), body


# Conduit compatibility: the conduit_profile workaround (notifier #412). Must
# pass on every supported Hermes.

def test_capability_lists_dashboard_profiles(dashboard):
    status, body = dashboard.get("capabilities")
    assert status == 200, body
    assert "dashboard-profiles" in str(body), body


@pytest.mark.parametrize("route", SPEECH_SOCKETS)
def test_launch_socket_reaches_launch_profile(dashboard, route):
    result = dashboard.open_socket(route)
    assert result.dialed == ["/launch"], result


@pytest.mark.parametrize("route", SPEECH_SOCKETS)
def test_conduit_profile_socket_reaches_profile(dashboard, route):
    result = dashboard.open_socket(route, conduit_profile="second")
    assert result.dialed == ["/second"], result


def test_conduit_profile_http_reaches_profile(dashboard):
    status, body = dashboard.get("local-live/status", conduit_profile="second")
    assert status == 200, body
    assert (body.get("speech_server") or {}).get("url") == dashboard.speech.url("second"), body
    assert body.get("api_key"), "Local Live gave no key on the secondary profile"


@pytest.mark.parametrize("route", SPEECH_SOCKETS)
def test_conduit_profile_socket_needs_sign_in(dashboard, route):
    result = dashboard.open_socket(route, token=False, conduit_profile="second")
    assert result.dialed == [], result
    assert result.first is None, result


@pytest.mark.parametrize("route", SPEECH_SOCKETS)
def test_conduit_profile_socket_unknown_profile_goes_nowhere(dashboard, route):
    result = dashboard.open_socket(route, conduit_profile="nobody")
    assert result.dialed == [], result


def _hermes_gates_profile_consent(dashboard) -> bool:
    """Whether Hermes itself refuses ?profile= for a profile that didn't enable the plugin.

    Per-profile plugin consent came with Hermes 0.22.3; before it, ?profile=
    had no gate either, and conduit_profile matches Hermes there.
    """
    status, _ = dashboard.get("local-live/status", profile="disabled")
    return status == 404


@pytest.mark.parametrize("route", SPEECH_SOCKETS)
def test_conduit_profile_socket_respects_profile_consent(dashboard, route):
    # "disabled" has the plugin installed but not enabled.
    result = dashboard.open_socket(route, conduit_profile="disabled")
    assert "/launch" not in result.dialed, result
    if not _hermes_gates_profile_consent(dashboard):
        pytest.skip("this Hermes has no per-profile plugin consent")
    assert result.dialed == [], result


def test_conduit_profile_http_respects_profile_consent(dashboard):
    status, body = dashboard.get("local-live/status", conduit_profile="disabled")
    if not _hermes_gates_profile_consent(dashboard):
        pytest.skip("this Hermes has no per-profile plugin consent")
    assert status == 404, body


@pytest.mark.parametrize("route", SPEECH_SOCKETS)
def test_conduit_profile_mismatch_is_refused(dashboard, route):
    result = dashboard.open_socket(route, conduit_profile="second", profile="disabled")
    assert result.dialed == [], result


def test_profile_without_sign_in_is_refused(dashboard):
    status, body = dashboard.get("local-live/status", auth=False, conduit_profile="second")
    assert status in (401, 403), body
