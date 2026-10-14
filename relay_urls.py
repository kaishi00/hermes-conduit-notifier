"""Which relay URLs this host will send a pairing code or credential to.

Shared by `hermes conduit-push pair` (client.py) and the dashboard's pairing
route, so both pairing paths apply the same rules:

- Every request that carries a credential goes to an HTTPS relay. Plain HTTP
  is allowed only to this machine (loopback), where nothing crosses a network.
- A pairing claim may also use plain HTTP to a private address (LAN, Tailscale,
  link-local). Self-hosters pair against their relay that way; the relay then
  answers with its HTTPS PUBLIC_URL, which is what gets saved.
- The saved relay is the one the relay names when it is a valid HTTPS URL on
  the same origin as the claim, or on any origin when the claim went to a
  private address. Otherwise the claimed URL itself is kept, if it is fit to
  carry a credential.

No URL with credentials, a query or a fragment is ever used. Error messages
never echo the URL, which may hold a secret.
"""

from __future__ import annotations

import ipaddress
import urllib.parse

_CGNAT = ipaddress.ip_network("100.64.0.0/10")  # Tailscale addresses
_PRIVATE_SUFFIXES = (".ts.net", ".local", ".localhost", ".internal", ".lan", ".home.arpa")


class RelayURLError(ValueError):
    """A relay URL this host won't use. The message is safe to show."""


def _split(url: object) -> urllib.parse.SplitResult:
    if not isinstance(url, str) or not url or len(url) > 2048 \
            or any(not 0x21 <= ord(c) <= 0x7E for c in url) or "\\" in url:
        raise RelayURLError("The relay URL must be an http(s) address.")
    try:
        parts = urllib.parse.urlsplit(url)
        host, _ = parts.hostname, parts.port  # .port raises on a bad port
    except ValueError:
        raise RelayURLError("The relay URL must be an http(s) address.") from None
    if parts.scheme not in ("https", "http") or not host:
        raise RelayURLError("The relay URL must be an http(s) address.")
    if parts.username is not None or parts.password is not None or parts.fragment or "#" in url:
        raise RelayURLError("The relay URL must not contain credentials or a fragment.")
    return parts


def _ip(host: str) -> ipaddress.IPv4Address | ipaddress.IPv6Address | None:
    try:
        return ipaddress.ip_address(host.split("%", 1)[0])
    except ValueError:
        return None


def is_loopback(host: str) -> bool:
    host = host.lower().rstrip(".")
    ip = _ip(host)
    return ip.is_loopback if ip is not None else host == "localhost" or host.endswith(".localhost")


def is_private(host: str) -> bool:
    """Loopback, RFC 1918, unique-local, link-local, Tailscale or a local-only name."""
    host = host.lower().rstrip(".")
    ip = _ip(host)
    if ip is not None:
        if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped is not None:
            ip = ip.ipv4_mapped
        return ip.is_loopback or ip.is_private or ip.is_link_local or ip in _CGNAT
    return host == "localhost" or host.endswith(_PRIVATE_SUFFIXES)


def _origin(parts: urllib.parse.SplitResult) -> tuple[str, str, int]:
    return parts.scheme, (parts.hostname or "").lower(), parts.port or (443 if parts.scheme == "https" else 80)


def check_request_url(url: object) -> str:
    """A URL fit to carry a relay credential: HTTPS, or HTTP to this machine."""
    parts = _split(url)
    if parts.query or "?" in url:  # type: ignore[operator]
        raise RelayURLError("The relay URL must not contain a query.")
    if parts.scheme != "https" and not is_loopback(parts.hostname or ""):
        raise RelayURLError("The relay URL must use https://.")
    return url  # type: ignore[return-value]


def check_claim_base(url: object) -> str:
    """The relay base to claim a pairing code at, without trailing slashes.

    HTTPS anywhere, or plain HTTP to a private address. No query.
    """
    parts = _split(url)
    if parts.query or "?" in url:  # type: ignore[operator]
        raise RelayURLError("The relay URL must not contain a query.")
    if parts.scheme != "https" and not is_private(parts.hostname or ""):
        raise RelayURLError("The relay URL must use https:// (plain http:// only works for a LAN or Tailscale address).")
    return url.rstrip("/")  # type: ignore[union-attr]


def paired_relay_url(returned: object, claimed: str) -> str:
    """The relay URL to save after claiming at `claimed` (see the module doc)."""
    claimed_parts = _split(claimed)
    if returned:
        try:
            check_request_url(returned)
            parts = _split(returned)
            if is_private(claimed_parts.hostname or "") or _origin(parts) == _origin(claimed_parts):
                return returned.rstrip("/")  # type: ignore[union-attr]
        except RelayURLError:
            pass
    try:
        return check_request_url(claimed).rstrip("/")
    except RelayURLError:
        raise RelayURLError("The relay didn't return an https:// address to use after pairing. "
                            "Set its PUBLIC_URL to an https:// URL and pair again.") from None
