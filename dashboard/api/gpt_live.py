"""GPT-Live on a ChatGPT/Codex subscription, or on this host's OpenAI key.

GPT-Live: exchanges Conduit's WebRTC offer for a GPT-Live answer using this
host's Codex sign-in, so live voice bills the ChatGPT subscription. The OAuth
token and account id stay on the host (hermes-agent#108940, host half).
Conduit can instead ask for a session billed to this host's OpenAI API key
(``billing: "api"``), only when the profile turns that on; the two never
stand in for each other.
"""

from __future__ import annotations

import asyncio
import http.client
import inspect
import json
import re
import threading
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Callable, Dict, Optional

from fastapi import HTTPException, Request, Response

from . import core, gemini_qwen_live

logger = core.logger
router = core.router


# --- GPT-Live on a ChatGPT/Codex subscription --------------------------------
#
# Backport of hermes-agent#108940's host half: exchange a WebRTC SDP offer for
# a GPT-Live answer using this host's Codex OAuth sign-in (`hermes auth` ->
# OpenAI Codex), so the voice layer bills the ChatGPT subscription instead of
# an API key. The bearer and its account id never leave the host. There is no
# fallback: a sign-in, entitlement or transport failure is an error, never an
# API-billed session.
#
# Once Hermes ships the same exchange (tools.voice_live grows
# _create_subscription_session), this route hands the request to Hermes and
# keeps only its URL, so Conduit needs no change when upstream lands.
#
# GPT-Live on an API key is a separate path Conduit must ask for by name
# (``billing: "api"``): the public GPT-Live API (POST /v1/live/sessions, the
# one Hermes' desktop voice mode uses) on this host's OpenAI key, billed per
# minute. A request without ``billing`` is a subscription request, so an older
# Conduit can never start an API-billed call. Each path fails on its own and
# never falls back to the other, the response's ``auth`` names the path that
# was used, and the profile must turn the API path on
# (voice.gpt_live.conduit_api_billing: true): an OpenAI key that is here for
# speech-to-text or TTS is not enough on its own.

GPT_LIVE_URL = "https://chatgpt.com/backend-api/codex/realtime/calls?intent=quicksilver&architecture=avas"
GPT_LIVE_DEFAULT_MODEL = "gpt-live-1-codex"
GPT_LIVE_DEFAULT_VOICE = "cove"
GPT_LIVE_TIMEOUT_S = 30.0
# A call starts once per conversation; cap a looping client well above that.
GPT_LIVE_LIMIT = 10
GPT_LIVE_WINDOW_S = 60.0
GPT_LIVE_WORKERS = 2
# An SDP offer is a few KB; the rest is room for the recent-history items.
# Room for a briefing at its character limit in 4-byte UTF-8, JSON-escaped, plus
# the SDP offer and history.
GPT_LIVE_MAX_BODY_BYTES = 640 * 1024
GPT_LIVE_MAX_HISTORY_ITEMS = 40
GPT_LIVE_MAX_ANSWER_BYTES = 64 * 1024
# Bounds how long Conduit waits (a 504), above the socket timeout. It can't
# interrupt a Hermes call already running on a worker (a token refresh, the
# upstream exchange), so a Hermes call that never returns holds its worker
# until the process restarts. Status has its own pool: a stuck session never
# blocks the readiness check.
GPT_LIVE_REQUEST_TIMEOUT_S = GPT_LIVE_TIMEOUT_S + 2
GPT_LIVE_NO_FALLBACK = "No API fallback was used."
_GPT_LIVE_VOICE = re.compile(r"[a-z][a-z0-9_-]{0,31}")
_GPT_LIVE_CALL_ID = re.compile(r"rtc_[A-Za-z0-9_-]+|[0-9a-fA-F-]{36}")

# The two ways a GPT-Live call is paid for. A request names one; a request
# that names none is a subscription request.
GPT_LIVE_BILLINGS = ("subscription", "api")
# The public GPT-Live API, on this host's OpenAI key (Hermes' tools.voice_live
# defaults, read from voice.gpt_live's model / voice / api_key / base_url).
GPT_LIVE_API_BASE_URL = "https://api.openai.com/v1"
GPT_LIVE_API_DEFAULT_MODEL = "gpt-live-1"
GPT_LIVE_API_DEFAULT_VOICE = "marin"
# The profile's own switch for API-billed calls from Conduit.
GPT_LIVE_API_SWITCH = "conduit_api_billing"
# API-billed calls one profile may start per day unless voice.gpt_live
# .conduit_api_daily_calls says otherwise. Counted in memory: a restart of the
# dashboard starts the count again.
GPT_LIVE_API_DAILY_CALLS = 20
GPT_LIVE_API_DAILY_KEY = "conduit_api_daily_calls"
GPT_LIVE_API_HOST = "api.openai.com"
GPT_LIVE_DAY_S = 24 * 60 * 60
GPT_LIVE_NO_SUBSCRIPTION = "The ChatGPT subscription was not used."
# Loose on purpose: the public API's id format is unverified and the id is only
# echoed back to Conduit, never put in a URL or header.
GPT_LIVE_API_SESSION_ID_MAX = 256

# Hermes' own voice-layer persona (tools/voice_live.py), for a Hermes that
# predates that module. Short on purpose: the live model delegates real work.
GPT_LIVE_PERSONA = (
    "You are Hermes, a calm and friendly voice assistant. Speak naturally at an unhurried pace. "
    "Be clear and direct, not overly cheerful. If the user is frustrated, acknowledge it briefly "
    "and focus on the next helpful step.\n\n"
    "Backchannel policy: Use moderate backchannels. Acknowledge naturally without competing with "
    "the main response.\n\n"
    "Interruption policy: Stop speaking when the user interrupts. Listen to what they say.\n\n"
    "Delegation policy:\n"
    "Backend tools:\n"
    "- Hermes agent: a full AI agent with tools — it can run commands, read and edit files, "
    "browse the web, search, remember things across sessions, schedule tasks, and reason "
    "carefully about anything. It is the one who actually does work and knows facts.\n\n"
    "Delegate to the backend when:\n"
    "- The user asks a question that needs facts, current information, or careful reasoning.\n"
    "- The user asks you to do, check, find, make, fix, run or remember anything.\n"
    "- A correction changes work already requested.\n\n"
    "Do not delegate to the backend when:\n"
    "- The user greets you, makes small talk, or asks you to repeat a result already provided.\n"
    "- You need a brief clarification to understand the request.\n\n"
    "Delegate before giving an answer that depends on backend work. Do not guess the result "
    "while waiting; say briefly that you are checking, then wait for the result."
)

_gpt_live_executor = ThreadPoolExecutor(max_workers=GPT_LIVE_WORKERS, thread_name_prefix="conduit-gpt-live")
GPT_LIVE_STATUS_WORKERS = 2
_gpt_live_status_executor = ThreadPoolExecutor(max_workers=GPT_LIVE_STATUS_WORKERS,
                                               thread_name_prefix="conduit-gpt-live-status")
# A timed-out status read keeps its worker until Hermes answers, so a check
# finding every worker busy is turned away (503) rather than queued behind them.
_gpt_live_status_slots = threading.BoundedSemaphore(GPT_LIVE_STATUS_WORKERS)
_gpt_live_limiter = core._MintLimiter(GPT_LIVE_LIMIT, GPT_LIVE_WINDOW_S,
                                 message="Too many GPT-Live session requests; try again shortly")
_gpt_live_api_daily = core._MintLimiter(GPT_LIVE_API_DAILY_CALLS, GPT_LIVE_DAY_S,
                                   message="This profile reached its daily limit of GPT-Live calls on the API key "
                                           "(voice.gpt_live.conduit_api_daily_calls). Nothing was billed.")


def _upstream_voice_live() -> Any:
    """Hermes' tools.voice_live when it carries the subscription exchange, else None."""
    try:
        from tools import voice_live
    except ImportError:
        return None
    exchange = getattr(voice_live, "_create_subscription_session", None)
    build = getattr(voice_live, "build_session_config", None)
    if not callable(exchange) or not callable(build):
        return None
    try:
        build_params = inspect.signature(build).parameters
        exchange_params = inspect.signature(exchange).parameters
    except (TypeError, ValueError):
        return None
    # The shape #108940 ships; anything else means Hermes moved on and the plugin keeps serving.
    if "live" not in build_params or len(exchange_params) != 2:
        logger.info("Hermes' GPT-Live subscription exchange has a different shape; using the plugin's own")
        return None
    return voice_live


def _gpt_live_settings() -> Dict[str, Any]:
    """The profile's ``voice.gpt_live`` block (profile-scoped by _run_scoped), or {}."""
    try:
        from hermes_cli.config import load_config
        config = load_config()
    except ImportError:
        return {}
    except Exception as exc:
        logger.warning("GPT-Live could not read the Hermes config: %s", type(exc).__name__)
        return {}
    voice = config.get("voice") if isinstance(config, dict) else None
    live = voice.get("gpt_live") if isinstance(voice, dict) else None
    return live if isinstance(live, dict) else {}


def _gpt_live_model_voice(live: Dict[str, Any]) -> tuple:
    return (str(live.get("subscription_model") or "").strip() or GPT_LIVE_DEFAULT_MODEL,
            str(live.get("subscription_voice") or "").strip() or GPT_LIVE_DEFAULT_VOICE)


def _gpt_live_instructions(live: Dict[str, Any]) -> str:
    try:
        from tools.voice_live import LIVE_PERSONA as persona
    except ImportError:
        persona = GPT_LIVE_PERSONA
    extra = str(live.get("instructions") or "").strip()
    return f"{persona}\n\n{extra}" if extra else persona


def _codex_credentials(refresh_if_expiring: bool = True) -> tuple:
    """(bearer, ChatGPT account id) from Hermes' Codex sign-in; TokenError(503) when unusable."""
    try:
        from hermes_cli.auth_codex import resolve_codex_runtime_credentials
        from hermes_cli.auth_constants import AuthError, _decode_jwt_claims
    except ImportError:
        raise core.TokenError(503, "This Hermes version has no Codex sign-in. " + GPT_LIVE_NO_FALLBACK)
    try:
        credentials = resolve_codex_runtime_credentials(refresh_if_expiring=refresh_if_expiring)
    except AuthError:
        raise core.TokenError(503, "GPT-Live needs a working Codex sign-in on the Hermes host. "
                              "Run `hermes auth` and choose OpenAI Codex. " + GPT_LIVE_NO_FALLBACK)
    except Exception as exc:
        # A refresh that failed on the network, say: the type only, since the text could carry detail.
        logger.warning("GPT-Live could not read the Codex sign-in: %s", type(exc).__name__)
        raise core.TokenError(503, "GPT-Live could not read the Codex sign-in on the Hermes host. "
                              + GPT_LIVE_NO_FALLBACK)
    token = str(credentials.get("api_key") or "").strip() if isinstance(credentials, dict) else ""
    try:
        claims = _decode_jwt_claims(token)
    except Exception:
        claims = None
    claims = claims.get("https://api.openai.com/auth") if isinstance(claims, dict) else None
    account = claims.get("chatgpt_account_id") if isinstance(claims, dict) else None
    if (not token or not isinstance(account, str) or not account.strip()
            or any(c in token + account for c in "\r\n")):
        raise core.TokenError(503, "GPT-Live needs a Codex sign-in with a ChatGPT account. " + GPT_LIVE_NO_FALLBACK)
    return token, account.strip()


def gpt_live_status() -> Dict[str, Any]:
    """Credential readiness only; the voice entitlement is checked when a call starts."""
    live = _gpt_live_settings()
    model, voice = _gpt_live_model_voice(live)
    status: Dict[str, Any] = {"auth": "subscription", "model": model, "voice": voice,
                              "source": "hermes" if _upstream_voice_live() else "plugin"}
    # The API path's readiness rides along under its own key: the top-level
    # fields stay the subscription's, which is all an older Conduit reads.
    status["api"] = gpt_live_api_status(live)
    try:
        _codex_credentials(refresh_if_expiring=False)
    except core.TokenError as exc:
        # A readiness reason, not a call failure: without the "no fallback" sentence.
        return {**status, "available": False, "reason": str(exc).replace(" " + GPT_LIVE_NO_FALLBACK, "")}
    return {**status, "available": True, "reason": None}


GPT_LIVE_API_OFF = ("GPT-Live on the API key is off for this profile. To allow it, set "
                    "voice.gpt_live.conduit_api_billing: true in the Hermes config.")
GPT_LIVE_API_NO_CALLS = ("GPT-Live on the API key allows no calls for this profile "
                         "(voice.gpt_live.conduit_api_daily_calls is 0).")


def _gpt_live_api_enabled(live: Dict[str, Any]) -> bool:
    # Only a real true: "yes", 1 or a typo never turns on billing.
    return live.get(GPT_LIVE_API_SWITCH) is True


def _gpt_live_api_daily_calls(live: Dict[str, Any]) -> int:
    value = live.get(GPT_LIVE_API_DAILY_KEY)
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        return GPT_LIVE_API_DAILY_CALLS
    return value


def _gpt_live_api_model_voice(live: Dict[str, Any]) -> tuple:
    return (str(live.get("model") or "").strip() or GPT_LIVE_API_DEFAULT_MODEL,
            str(live.get("voice") or "").strip() or GPT_LIVE_API_DEFAULT_VOICE)


def _gpt_live_api_base_url(live: Dict[str, Any]) -> tuple:
    """(base URL, its host) for the API path; TokenError(503) for one that isn't an https URL with a host."""
    base_url = str(live.get("base_url") or GPT_LIVE_API_BASE_URL).strip().rstrip("/")
    try:
        parsed = urllib.parse.urlparse(base_url)
        host = parsed.hostname or ""
    except ValueError:
        parsed, host = None, ""
    if (parsed is None or parsed.scheme != "https" or not host or parsed.query or parsed.fragment
            or any(c in base_url for c in "\r\n\t ")):
        raise core.TokenError(503, "voice.gpt_live.base_url must be an https URL with a host and no query or "
                              "fragment. " + GPT_LIVE_NO_SUBSCRIPTION)
    try:
        port = parsed.port
    except ValueError:
        raise core.TokenError(503, "voice.gpt_live.base_url has an invalid port. " + GPT_LIVE_NO_SUBSCRIPTION)
    # The shared key's gate below compares the host; a non-standard port is another service.
    if port not in (None, 443):
        host = f"{host}:{port}"
    return base_url, host


def _gpt_live_api_credentials(live: Dict[str, Any]) -> tuple:
    """(api key, base URL, host) for the API path, the way Hermes' desktop voice
    mode resolves them: voice.gpt_live.api_key, else the OpenAI audio key chain
    (VOICE_TOOLS_OPENAI_KEY, OPENAI_API_KEY). The shared chain only ever goes to
    OpenAI itself: a custom base_url needs its own voice.gpt_live.api_key, so the
    host's general key never reaches another service. TokenError(503) otherwise."""
    base_url, host = _gpt_live_api_base_url(live)
    key = str(live.get("api_key") or "").strip()
    if not key:
        if host != GPT_LIVE_API_HOST:
            raise core.TokenError(503, f"voice.gpt_live.base_url points at {host}, so GPT-Live needs its own key there "
                                  "(voice.gpt_live.api_key); the host's OPENAI_API_KEY only goes to OpenAI. "
                                  + GPT_LIVE_NO_SUBSCRIPTION)
        try:
            import tools.tool_backend_helpers as helpers
        except ImportError as exc:
            # Only a Hermes without the helper falls back to the environment; a
            # broken import inside it is a failure, not an absent helper.
            if exc.name not in ("tools", "tools.tool_backend_helpers"):
                logger.warning("GPT-Live could not load Hermes' OpenAI key lookup: %s", type(exc).__name__)
                raise core.TokenError(503, "GPT-Live could not read the OpenAI key on the Hermes host. " + GPT_LIVE_NO_SUBSCRIPTION)
            helpers = None
        resolve_openai_audio_api_key = getattr(helpers, "resolve_openai_audio_api_key", None)
        if not callable(resolve_openai_audio_api_key):
            # The profile's own .env where Hermes can say, as its key chain reads it.
            try:
                key = str(gemini_qwen_live._env_value("VOICE_TOOLS_OPENAI_KEY") or gemini_qwen_live._env_value("OPENAI_API_KEY") or "").strip()
            except Exception as exc:
                logger.warning("GPT-Live could not read the OpenAI key: %s", type(exc).__name__)
                key = ""
        else:
            try:
                key = str(resolve_openai_audio_api_key() or "").strip()
            except Exception as exc:
                logger.warning("GPT-Live could not read the OpenAI key: %s", type(exc).__name__)
                key = ""
    if not key or any(c in key for c in "\r\n"):
        raise core.TokenError(503, "GPT-Live on the API key needs an OpenAI API key on the Hermes host "
                              "(OPENAI_API_KEY or voice.gpt_live.api_key). " + GPT_LIVE_NO_SUBSCRIPTION)
    return key, base_url, host


def gpt_live_api_status(live: Dict[str, Any]) -> Dict[str, Any]:
    """Readiness of the API path: the profile's switch, then a key. Never returns the key."""
    model, voice = _gpt_live_api_model_voice(live)
    status: Dict[str, Any] = {"auth": "api", "model": model, "voice": voice, "enabled": _gpt_live_api_enabled(live),
                              "daily_limit": _gpt_live_api_daily_calls(live)}
    if not status["enabled"]:
        return {**status, "available": False, "reason": GPT_LIVE_API_OFF}
    if status["daily_limit"] == 0:
        return {**status, "available": False, "reason": GPT_LIVE_API_NO_CALLS}
    try:
        _key, _base_url, host = _gpt_live_api_credentials(live)
    except core.TokenError as exc:
        return {**status, "available": False, "reason": str(exc).replace(" " + GPT_LIVE_NO_SUBSCRIPTION, "")}
    # Which OpenAI-compatible service bills the call; the host only, no path or key.
    return {**status, "available": True, "reason": None, "host": host}


def gpt_live_session_config(history: Optional[list], live: Dict[str, Any]) -> Dict[str, Any]:
    model, voice = _gpt_live_model_voice(live)
    config: Dict[str, Any] = {
        "model": model,
        "instructions": _gpt_live_instructions(live),
        "audio": {"output": {"voice": voice}},
        "delegation": {"type": "client"},
    }
    if history:
        # The Codex frameless contract names seeded history initial_items.
        config["initial_items"] = history
    return config


class _GPTLiveAnsweredError(core.TokenError):
    """OpenAI answered 2xx but its answer couldn't be read: a session may have started."""


def _post_sdp(url: str, headers: Dict[str, str], body: Dict[str, Any]) -> tuple:
    """POST the offer; returns (status, answer text, Location). No redirects: the bearer must not follow one."""
    request = urllib.request.Request(url, data=json.dumps(body).encode("utf-8"), method="POST",
                                     headers={**headers, "Content-Type": "application/json"})
    answered = False

    def failed(message: str) -> core.TokenError:
        # Once OpenAI answered 2xx, a session may have started: every later failure says so.
        return (_GPTLiveAnsweredError if answered else core.TokenError)(502, message + GPT_LIVE_NO_FALLBACK)

    try:
        with gemini_qwen_live._opener.open(request, timeout=GPT_LIVE_TIMEOUT_S) as response:
            answered = 200 <= response.status < 300
            # An SDP answer is a few KB; one byte over the cap is refused, not buffered.
            answer = response.read(GPT_LIVE_MAX_ANSWER_BYTES + 1)
            if len(answer) > GPT_LIVE_MAX_ANSWER_BYTES:
                raise failed("GPT-Live returned an invalid WebRTC answer. ")
            return response.status, answer.decode("utf-8", "replace"), response.headers.get("Location", "")
    except urllib.error.HTTPError as exc:
        # The body can echo account details; only the status goes back to Conduit.
        status = exc.code
        exc.close()
        return status, "", ""
    except (urllib.error.URLError, TimeoutError, OSError, http.client.HTTPException) as exc:
        # http.client.HTTPException covers a chunked body cut off mid-read (IncompleteRead).
        if answered:
            logger.warning("GPT-Live could not read OpenAI's answer after a 2xx: %s", type(exc).__name__)
        else:
            logger.warning("GPT-Live session request could not reach OpenAI: %s", getattr(exc, "reason", exc))
        raise failed("GPT-Live connection failed. ")
    except ValueError as exc:
        # A header value the request can't carry (a non-Latin-1 token, say): the type only, never the value.
        logger.warning("GPT-Live could not build its session request: %s", type(exc).__name__)
        raise failed("GPT-Live connection failed. ")


def _valid_answer(sdp: Any, call_id: Any) -> bool:
    return (isinstance(sdp, str) and sdp.startswith("v=0")
            and isinstance(call_id, str) and _GPT_LIVE_CALL_ID.fullmatch(call_id) is not None)


def _plugin_gpt_live_session(sdp: str, config: Dict[str, Any], post: Callable[..., tuple]) -> Dict[str, Any]:
    token, account = _codex_credentials()
    status, answer, location = post(GPT_LIVE_URL, {
        "Authorization": f"Bearer {token}",
        "ChatGPT-Account-Id": account,
        "OpenAI-Alpha": "quicksilver=v2",
    }, {"sdp": sdp, "session": config})
    if not 200 <= status < 300:
        logger.warning("GPT-Live session was rejected: HTTP %s", status)
        raise core.TokenError(502, f"GPT-Live session was rejected (HTTP {status}); check the Codex sign-in "
                              "and the account's voice access. " + GPT_LIVE_NO_FALLBACK)
    call_id = urllib.parse.urlparse(location).path.rstrip("/").rsplit("/", 1)[-1]
    if not _valid_answer(answer, call_id):
        raise core.TokenError(502, "GPT-Live returned an invalid WebRTC answer. " + GPT_LIVE_NO_FALLBACK)
    return {"auth": "subscription", "session": {"id": call_id}, "transport": {"type": "webrtc", "sdp": answer}}


def _clean_billing(billing: Any) -> str:
    """The path Conduit asked to be billed on. None is the subscription: only a
    Conduit that names the API path by name can start an API-billed call."""
    if billing is None:
        return "subscription"
    if not isinstance(billing, str) or billing not in GPT_LIVE_BILLINGS:
        raise core.TokenError(400, "billing must be subscription or api")
    return billing


def gpt_live_api_session_config(history: Optional[list], live: Dict[str, Any]) -> Dict[str, Any]:
    """The ``session`` object for POST /v1/live/sessions (client delegation, as Hermes' own)."""
    model, voice = _gpt_live_api_model_voice(live)
    config: Dict[str, Any] = {
        "model": model,
        "instructions": _gpt_live_instructions(live),
        "audio": {"output": {"voice": voice}},
        "delegation": {"type": "client"},
    }
    if history:
        # The public API names seeded history input.
        config["input"] = history
    return config


def _api_gpt_live_session(sdp: str, history: Optional[list], live: Dict[str, Any], limiter_key: Optional[str],
                          post: Callable[..., tuple], *, briefing_applied: bool, greeting_applied: bool) -> Dict[str, Any]:
    """A GPT-Live call billed to this host's OpenAI key. Never the subscription:
    every failure here is an error, and nothing else is tried."""
    if not _gpt_live_api_enabled(live):
        raise core.TokenError(403, GPT_LIVE_API_OFF + " " + GPT_LIVE_NO_SUBSCRIPTION)
    daily_limit = _gpt_live_api_daily_calls(live)
    if daily_limit == 0:
        raise core.TokenError(403, GPT_LIVE_API_NO_CALLS + " " + GPT_LIVE_NO_SUBSCRIPTION)
    key, base_url, _host = _gpt_live_api_credentials(live)
    # Counted only once the call can really be billed, so a missing key doesn't use up the day,
    # and given back below when OpenAI doesn't start it.
    daily_key = limiter_key or ""
    mint = _gpt_live_api_daily.acquire(daily_key, limit=daily_limit)
    try:
        return _api_gpt_live_exchange(sdp, history, live, key, base_url, post,
                                      briefing_applied=briefing_applied, greeting_applied=greeting_applied)
    except _GPTLiveAPIAnsweredError:
        # OpenAI said yes, so a billed session may exist: the slot stays counted.
        raise
    except BaseException:
        _gpt_live_api_daily.release(daily_key, mint)
        raise


class _GPTLiveAPIAnsweredError(core.TokenError):
    """A failure after OpenAI answered 2xx: a session may have started, so its daily slot stays used."""


def _api_gpt_live_exchange(sdp: str, history: Optional[list], live: Dict[str, Any], key: str, base_url: str,
                           post: Callable[..., tuple], *, briefing_applied: bool, greeting_applied: bool) -> Dict[str, Any]:
    config = gpt_live_api_session_config(history, live)
    try:
        status, text, _location = post(f"{base_url}/live/sessions", {"Authorization": f"Bearer {key}"},
                                       {"session": config, "transport": {"type": "webrtc", "sdp": sdp}})
    except core.TokenError as exc:
        # Said for this path, whatever the shared transport's own wording. An answer
        # that failed after a 2xx keeps its daily slot, like an invalid one below.
        raise (_GPTLiveAPIAnsweredError if isinstance(exc, _GPTLiveAnsweredError) else core.TokenError)(
            exc.status, "GPT-Live on the API key could not connect, or its answer was invalid. "
                        + GPT_LIVE_NO_SUBSCRIPTION)
    if not 200 <= status < 300:
        logger.warning("GPT-Live API session was rejected: HTTP %s", status)
        if status == 401:
            raise core.TokenError(502, "OpenAI rejected the API key (HTTP 401); check voice.gpt_live.api_key or "
                                  "OPENAI_API_KEY on the Hermes host. " + GPT_LIVE_NO_SUBSCRIPTION)
        if status == 429:
            raise core.TokenError(429, "OpenAI is rate-limiting or out of quota for this API key (HTTP 429); try "
                                  "again later. " + GPT_LIVE_NO_SUBSCRIPTION)
        raise core.TokenError(502, f"GPT-Live on the API key was rejected (HTTP {status}); check the OpenAI key "
                              "and its access to GPT-Live. " + GPT_LIVE_NO_SUBSCRIPTION)
    try:
        answer = json.loads(text)
    except ValueError:
        answer = None
    session = answer.get("session") if isinstance(answer, dict) else None
    transport = answer.get("transport") if isinstance(answer, dict) else None
    session_id = session.get("id") if isinstance(session, dict) else None
    answer_sdp = transport.get("sdp") if isinstance(transport, dict) else None
    if (not isinstance(answer_sdp, str) or not answer_sdp.startswith("v=0")
            or transport.get("type") != "webrtc"
            or not isinstance(session_id, str) or not session_id or len(session_id) > GPT_LIVE_API_SESSION_ID_MAX
            or not session_id.isprintable()):
        raise _GPTLiveAPIAnsweredError(502, "GPT-Live returned an invalid WebRTC answer. " + GPT_LIVE_NO_SUBSCRIPTION)
    model, voice = _gpt_live_api_model_voice(live)
    # Only the fields Conduit reads; "auth" names the path that was billed. The
    # public API's answer doesn't echo the session, so briefing_applied,
    # greeting_applied and voice say what was sent, not what OpenAI confirmed.
    return {"auth": "api", "session": {"id": session_id}, "transport": {"type": "webrtc", "sdp": answer_sdp},
            "source": "plugin", "model": model, "voice": voice,
            "briefing_applied": briefing_applied, "greeting_applied": greeting_applied}


def _clean_history(history: Any) -> Optional[list]:
    if history is None:
        return None
    if not isinstance(history, list) or not all(isinstance(item, dict) for item in history):
        raise core.TokenError(400, "history must be a list of conversation items")
    return history[-GPT_LIVE_MAX_HISTORY_ITEMS:] or None


def _clean_voice(voice: Any) -> Optional[str]:
    """The voice Conduit asked for, or None to keep the profile's configured one."""
    if voice is None:
        return None
    if not isinstance(voice, str):
        raise core.TokenError(400, "voice must be a voice name such as cove")
    voice = voice.strip().lower()
    if not voice:
        return None
    if _GPT_LIVE_VOICE.fullmatch(voice) is None:
        raise core.TokenError(400, "voice must be a voice name such as cove")
    return voice


# Conduit's rules plus the user's persona and memory. 32 KB turned away
# calls for users with a long memory ("briefing is too long",
# kaishi00/hermes-conduit#1077); Conduit 2.2.0 build 219 doesn't trim it.
GPT_LIVE_MAX_BRIEFING_CHARS = 64 * 1024
# Last, so a chatty persona doesn't talk the model into an opening greeting.
GPT_LIVE_WAIT_FOR_USER = (
    "Opening policy: do not speak first. When the call starts, stay silent until the user "
    "speaks, then answer what they said. This applies whatever the persona above says."
)
GPT_LIVE_MAX_GREETING_CHARS = 200
# A call Hermes placed: the user answered and is waiting to hear why it rang
# (kaishi00/hermes-conduit#544). Conduit's briefing says what to open with.
GPT_LIVE_CALL_OPENING = (
    "Opening policy: speak first. You placed this call and the user just answered, so as soon "
    "as it starts, tell them why you called, as the part about this call above says, without "
    "waiting for them to speak. Then wait for them. This applies whatever the persona above says."
)
# The same call without Conduit's briefing (a Watch call whose briefing couldn't be
# read): nothing says why it rang, so the model mustn't guess a reason.
GPT_LIVE_CALL_OPENING_UNBRIEFED = (
    "Opening policy: speak first. You placed this call and the user just answered, so as soon "
    "as it starts, say in one short sentence that it's you calling, without guessing why. Then "
    "wait for them. This applies whatever the persona above says."
)


def _greet_first(greeting: str) -> str:
    """The opening policy for a user who asked to be greeted when a call connects."""
    say = f' Say: "{greeting}"' if greeting else ""
    return ("Opening policy: speak first. As soon as the call starts, greet the user in one short "
            f"sentence so they know you're there and listening.{say} Then wait for them. "
            "This applies whatever the persona above says.")


def _clean_greeting(greeting: Any) -> Optional[str]:
    """None keeps the call silent until the user speaks; text (or "" for a default
    greeting) makes the model greet first. Kept to one printable line without
    double quotes so it reads as the line to say. It is the user's own setting
    for their own call, not untrusted input: nothing here stops it from reading
    as an instruction."""
    if greeting is None:
        return None
    if not isinstance(greeting, str):
        raise core.TokenError(400, "greeting must be text")
    if len(greeting) > GPT_LIVE_MAX_GREETING_CHARS * 4:
        raise core.TokenError(400, "greeting is too long")
    greeting = "".join(ch if ch.isprintable() else " " for ch in greeting.replace('"', "'"))
    greeting = " ".join(greeting.split())
    if len(greeting) > GPT_LIVE_MAX_GREETING_CHARS:
        raise core.TokenError(400, "greeting is too long")
    return greeting


def _clean_call_opening(call_opening: Any) -> bool:
    """True when Conduit opens a call Hermes placed: the model speaks first with
    the briefing's opening instead of a greeting or staying silent."""
    if call_opening is None:
        return False
    if not isinstance(call_opening, bool):
        raise core.TokenError(400, "call_opening must be true or false")
    return call_opening


def _clean_briefing(briefing: Any) -> Optional[str]:
    """Conduit's rules/persona/memory for this call, or None when it sent none."""
    if briefing is None:
        return None
    if not isinstance(briefing, str):
        raise core.TokenError(400, "briefing must be text")
    briefing = briefing.strip()
    if len(briefing) > GPT_LIVE_MAX_BRIEFING_CHARS:
        raise core.TokenError(400, "briefing is too long")
    return briefing or None


def _built_session(config: Any) -> Dict[str, Any]:
    """The session block of a Hermes-built config, which may or may not wrap it in "session"."""
    if not isinstance(config, dict):
        return {}
    inner = config.get("session")
    return inner if isinstance(inner, dict) else config


def _built_voice(config: Any) -> Optional[str]:
    audio = _built_session(config).get("audio")
    output = audio.get("output") if isinstance(audio, dict) else None
    voice = output.get("voice") if isinstance(output, dict) else None
    if not isinstance(voice, str) or _GPT_LIVE_VOICE.fullmatch(voice.strip().lower()) is None:
        return None
    return voice.strip().lower()


def _built_has_instructions(config: Any, text: Optional[str]) -> bool:
    """True only when Hermes' config really carries the text (the briefing or the
    opening policy), so Conduit never drops it unsent."""
    if not text:
        return False
    instructions = _built_session(config).get("instructions")
    return isinstance(instructions, str) and text in instructions


def create_gpt_live_session(
    sdp: Any,
    history: Any = None,
    limiter_key: Optional[str] = None,
    post: Optional[Callable[..., tuple]] = None,
    *,
    voice: Any = None,
    briefing: Any = None,
    greeting: Any = None,
    call_opening: Any = None,
    billing: Any = None,
) -> Dict[str, Any]:
    if not isinstance(sdp, str) or not sdp.startswith("v=0"):
        raise core.TokenError(400, "sdp must be a WebRTC SDP offer")
    billing = _clean_billing(billing)
    history = _clean_history(history)
    voice = _clean_voice(voice)
    briefing = _clean_briefing(briefing)
    greeting = _clean_greeting(greeting)
    call_opening = _clean_call_opening(call_opening)
    if limiter_key is not None:
        _gpt_live_limiter.acquire(limiter_key)
    live = {**_gpt_live_settings(), "auth": billing}
    if voice:
        live["voice" if billing == "api" else "subscription_voice"] = voice
    if briefing:
        # Part of the session's instructions, so the model has it before the first word:
        # sent as context appends after the call starts, it answers each chunk out loud.
        extra = str(live.get("instructions") or "").strip()
        live["instructions"] = f"{extra}\n\n{briefing}" if extra else briefing
    if call_opening:
        opening = GPT_LIVE_CALL_OPENING if briefing else GPT_LIVE_CALL_OPENING_UNBRIEFED
    else:
        opening = GPT_LIVE_WAIT_FOR_USER if greeting is None else _greet_first(greeting)
    # Conduit sends its own opening turn when the model won't speak first.
    speaks_first = call_opening or greeting is not None
    extra = str(live.get("instructions") or "").strip()
    live["instructions"] = f"{extra}\n\n{opening}" if extra else opening
    if billing == "api":
        return _api_gpt_live_session(sdp, history, live, limiter_key, post or _post_sdp,
                                     briefing_applied=bool(briefing), greeting_applied=speaks_first)
    # Echoed back so Conduit can tell a host that applied the chosen voice from one that ignored it.
    applied_voice = _gpt_live_model_voice(live)[1]
    upstream = _upstream_voice_live()
    if upstream is not None:
        try:
            config = upstream.build_session_config(history, live=live)
            result = upstream._create_subscription_session(sdp, config)
        except Exception as exc:
            # Only the type is logged: Hermes' text could quote a provider response.
            logger.warning("Hermes' GPT-Live subscription exchange failed: %s", type(exc).__name__)
            raise core.TokenError(502, "GPT-Live session could not be started; check the Codex sign-in "
                                  "and the account's voice access. " + GPT_LIVE_NO_FALLBACK)
        session, transport = (result.get("session"), result.get("transport")) if isinstance(result, dict) else (None, None)
        if (not isinstance(session, dict) or not isinstance(transport, dict)
                or not _valid_answer(transport.get("sdp"), session.get("id"))):
            raise core.TokenError(502, "GPT-Live returned an invalid WebRTC answer. " + GPT_LIVE_NO_FALLBACK)
        # Prefer the voice Hermes put in the session over the one we asked for.
        built = _built_voice(config)
        if built:
            applied_voice = built
        # Only the fields Conduit reads: nothing else Hermes returns leaves the host.
        return {"auth": "subscription", "session": {"id": session["id"]},
                "transport": {"type": "webrtc", "sdp": transport["sdp"]}, "source": "hermes",
                "voice": applied_voice, "briefing_applied": _built_has_instructions(config, briefing),
                "greeting_applied": speaks_first and _built_has_instructions(config, opening)}
    config = gpt_live_session_config(history, live)
    return {**_plugin_gpt_live_session(sdp, config, post or _post_sdp), "source": "plugin", "voice": applied_voice,
            "briefing_applied": bool(briefing), "greeting_applied": speaks_first}


@router.get("/gpt-live/status")
async def get_gpt_live_status(response: Response, profile: Optional[str] = None) -> Dict[str, Any]:
    response.headers["Cache-Control"] = "no-store"
    # The slot is freed exactly once: by the read when it ran, else by this
    # request (a read it gave up on before it started never runs).
    state = {"started": False, "abandoned": False}
    state_lock = threading.Lock()

    def status() -> Dict[str, Any]:
        with state_lock:
            if state["abandoned"]:
                return {}
            state["started"] = True
        try:
            return gpt_live_status()
        finally:
            _gpt_live_status_slots.release()

    if not _gpt_live_status_slots.acquire(blocking=False):
        raise HTTPException(status_code=503, detail="GPT-Live status is still waiting on Hermes; try again shortly",
                            headers={"Cache-Control": "no-store"})
    try:
        try:
            result = await asyncio.wait_for(core._run_scoped(profile, status, _gpt_live_status_executor),
                                            timeout=GPT_LIVE_REQUEST_TIMEOUT_S)
        except BaseException:
            with state_lock:
                if not state["started"]:
                    state["abandoned"] = True
                    _gpt_live_status_slots.release()
            raise
        return {"ok": True, **result}
    except asyncio.TimeoutError:
        logger.warning("GPT-Live status for Conduit timed out after %ss", GPT_LIVE_REQUEST_TIMEOUT_S)
        raise HTTPException(status_code=504, detail="GPT-Live status timed out", headers={"Cache-Control": "no-store"})
    except core.TokenError as exc:
        raise HTTPException(status_code=exc.status, detail=str(exc), headers={"Cache-Control": "no-store"})
    except HTTPException as exc:
        exc.headers = {**(exc.headers or {}), "Cache-Control": "no-store"}
        raise
    except Exception as exc:
        raise core._unexpected("status", exc, feature="GPT-Live")


@router.post("/gpt-live/session")
async def post_gpt_live_session(request: Request, response: Response, profile: Optional[str] = None) -> Dict[str, Any]:
    response.headers["Cache-Control"] = "no-store"
    body = await core._read_json_body(request, GPT_LIVE_MAX_BODY_BYTES)
    if not isinstance(body, dict):
        raise HTTPException(status_code=400, detail="Request body must be a JSON object",
                            headers={"Cache-Control": "no-store"})
    try:
        result = await asyncio.wait_for(
            core._run_scoped(
                profile,
                lambda: create_gpt_live_session(body.get("sdp"), body.get("history"), limiter_key=core._limiter_key(profile), voice=body.get("voice"),
                                                briefing=body.get("briefing"), greeting=body.get("greeting"),
                                                call_opening=body.get("call_opening"), billing=body.get("billing")),
                _gpt_live_executor,
            ),
            timeout=GPT_LIVE_REQUEST_TIMEOUT_S,
        )
    except asyncio.TimeoutError:
        logger.warning("GPT-Live session for Conduit timed out after %ss", GPT_LIVE_REQUEST_TIMEOUT_S)
        raise HTTPException(status_code=504, detail="GPT-Live session timed out", headers={"Cache-Control": "no-store"})
    except core.TokenError as exc:
        logger.warning("GPT-Live session for Conduit failed: %s", exc)
        raise HTTPException(status_code=exc.status, detail=str(exc), headers={"Cache-Control": "no-store"})
    except HTTPException as exc:
        exc.headers = {**(exc.headers or {}), "Cache-Control": "no-store"}
        raise
    except Exception as exc:
        raise core._unexpected("session", exc, feature="GPT-Live")
    return {"ok": True, **result}
