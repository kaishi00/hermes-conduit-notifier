"""CONDUIT_PUSH_PROXY: a proxy for this plugin's own HTTPS traffic only.

A host on a network without direct internet access can send this plugin's
requests (the relay, the Watch's relay socket, the rooms mailbox, Apple Maps,
the Live voice providers) through an HTTP(S) proxy without routing the rest of
Hermes through it, which a process-wide HTTPS_PROXY would (notifier #746,
proposed by aminrpg).

The rule is simple: the setting applies wherever this plugin would follow
HTTPS_PROXY, and only there. HTTPS and WSS requests go through it; plain HTTP
and WS (loopback, a LAN or Tailscale pairing claim, a speech server on the
LAN) never do, and a host NO_PROXY names is reached directly. Openers that
never take a proxy (the room bridge's replay to this Hermes' own API server)
stay that way.

Unset or empty changes nothing: every caller gets the opener and connection
arguments it used before, so a normal setup is untouched. A value that isn't
an http:// or https:// proxy URL is logged once and ignored (requests go out
as they would without it), and `hermes conduit-push status` says so, rather
than failing every request.

stdlib only, no package-relative imports: the dashboard loads this file by
path, as it does relay_urls.py.
"""

from __future__ import annotations

import logging
import os
import threading
import urllib.parse
import urllib.request
from typing import Any, Callable, Optional

ENV = "CONDUIT_PUSH_PROXY"

logger = logging.getLogger("hermes.plugins.conduit_push")

_lock = threading.Lock()
_parsed: tuple[str, Optional[str], Optional[str]] | None = None  # (raw, url, problem)
_openers: dict[tuple[Any, str], Any] = {}


def _check(raw: str) -> tuple[Optional[str], Optional[str]]:
    """(the proxy URL, None) for a usable value, else (None, why it isn't).
    The reason never echoes the value, which may hold a password."""
    try:
        parts = urllib.parse.urlsplit(raw)
        parts.port  # raises on a port that isn't a number in range
    except ValueError:
        return None, "it isn't a URL"
    if parts.scheme.lower() not in ("http", "https"):
        return None, "it must start with http:// or https:// (SOCKS isn't supported)"
    if not parts.hostname:
        return None, "it has no host"
    if parts.path not in ("", "/") or parts.query or parts.fragment:
        return None, "it must be just the proxy's address, like http://proxy.example:3128"
    return raw, None


def _read() -> tuple[Optional[str], Optional[str]]:
    global _parsed
    raw = (os.environ.get(ENV) or "").strip()
    if not raw:
        return None, None
    with _lock:
        if _parsed is None or _parsed[0] != raw:
            url, problem = _check(raw)
            if problem:
                logger.warning("%s is ignored: %s. This plugin's requests go out without it.", ENV, problem)
            _parsed = (raw, url, problem)
        return _parsed[1], _parsed[2]


def proxy_url() -> Optional[str]:
    """The proxy this plugin's HTTPS requests use, or None (unset or unusable)."""
    return _read()[0]


def status() -> Optional[str]:
    """One line for `hermes conduit-push status`, or None when it's unset.
    Shows the proxy's host and port, never its credentials."""
    raw = (os.environ.get(ENV) or "").strip()
    if not raw:
        return None
    url, problem = _read()
    if problem or url is None:
        return f"ignored ({problem})"
    parts = urllib.parse.urlsplit(url)
    host = parts.hostname or ""
    if ":" in host:
        host = f"[{host}]"
    return f"{parts.scheme.lower()}://{host}" + (f":{parts.port}" if parts.port else "") + " (HTTPS requests only)"


def opener(default: Any, build: Callable[[urllib.request.ProxyHandler], Any]) -> Any:
    """The opener for an HTTPS request: `default` when the setting is unset or
    unusable, else one `build` made around a ProxyHandler for the proxy, kept
    while the setting stays the same. `build` must be the same function each
    call (a module-level one), since openers are kept per builder."""
    url = proxy_url()
    if url is None:
        return default
    key = (build, url)
    with _lock:
        made = _openers.get(key)
        if made is None:
            # Only https: plain HTTP never goes through it. urllib still
            # skips the proxy for a host NO_PROXY names.
            made = build(urllib.request.ProxyHandler({"https": url}))
            _openers[key] = made
        return made


def proxies(scheme: str, host: str) -> bool:
    """Whether a request with this scheme to this host goes through the setting."""
    if scheme.lower() not in ("https", "wss") or proxy_url() is None:
        return False
    try:
        return not urllib.request.proxy_bypass(host)
    except Exception:  # noqa: BLE001 - platform settings it can't read: proxied
        return True


_ws_proxy_support: Optional[bool] = None


def websocket_kwargs(url: str, connect: Any) -> dict[str, Any]:
    """Extra arguments for a `websockets` connect to `url`: {} when the
    setting is unset or doesn't apply (ws://, a NO_PROXY host), so the call
    is the one it always was; else {"proxy": url} (websockets 15+)."""
    global _ws_proxy_support
    parts = urllib.parse.urlsplit(url)
    if not proxies(parts.scheme, parts.hostname or ""):
        return {}
    if _ws_proxy_support is None:
        try:
            import inspect

            _ws_proxy_support = "proxy" in inspect.signature(connect).parameters
        except (TypeError, ValueError):
            _ws_proxy_support = False
        if not _ws_proxy_support:
            logger.warning("%s doesn't reach WebSocket connections on this host: its websockets package is "
                           "older than 15. They go out without the proxy.", ENV)
    return {"proxy": proxy_url()} if _ws_proxy_support else {}
