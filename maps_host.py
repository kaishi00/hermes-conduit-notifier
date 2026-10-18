"""conduit_maps' host engine: Apple Maps Server API, called from this host.

Lookups that don't need the user's iPhone (or when it can't be reached) are
answered here. The relay hands paired hosts a short-lived Apple Maps access
token (``POST /v1/maps/token``, one token shared by every host, so it doesn't
say which host used it); this module calls ``maps-api.apple.com`` itself, so
the relay never sees a lookup. Answers have the same shapes as the phone's
(designs/maps-v1-contract.md sections 4 and 7).

The host knows no saved place's coordinates (they live on the phone) and
the server API gives transit times but not transit routes: those lookups
raise ``NeedsPhone``. "me" is the host's last fix, at the detail it was
shared at.
"""

from __future__ import annotations

import json
import math
import os
import re
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable

from . import client

APPLE = "https://maps-api.apple.com/v1"
DAILY_CALLS = 200
USAGE_FILE = "conduit-maps-usage.json"
# After the relay says it has no Maps key, don't ask again for this long.
UNAVAILABLE_RETRY_S = 3600
TIMEOUT_S = 10.0
TRANSPORT = {"driving": "Automobile", "walking": "Walking", "cycling": "Cycling", "transit": "Transit"}
MAX_LOOKUP = 5


class NeedsPhone(Exception):
    """This lookup can only be answered by the phone."""


class HostUnavailable(Exception):
    """The host engine can't answer now (no token, quota, Apple refused)."""

    def __init__(self, error: str, note: str) -> None:
        super().__init__(note)
        self.error = error


# The token cache and the usage counter have their own locks, so a slow
# token exchange doesn't hold up counting.
# A token is a header value: printable ASCII only.
_TOKEN = re.compile(r"[\x21-\x7e]{1,4096}")


def _lifetime(value: Any) -> bool:
    """A token lifetime worth caching: a finite number of seconds over a minute."""
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value) and value > 60
_lock = threading.Lock()
_usage_lock = threading.Lock()
_token: dict[str, Any] = {}
_unavailable_until = 0.0


def configured() -> bool:
    """Whether this host is paired with a relay (that may hand out tokens)."""
    try:
        return client.load_state() is not None and time.time() >= _unavailable_until
    except Exception:  # noqa: BLE001
        return False


def _access_token(refresh: bool = False) -> str:
    global _unavailable_until
    with _lock:
        now = time.time()
        if not refresh and _token.get("value") and _token.get("expires_at", 0) - now > 60:
            return _token["value"]
        if now < _unavailable_until:
            raise HostUnavailable("unavailable", "Apple Maps isn't available from this Hermes host.")
        state = client.load_state()
        if state is None:
            raise HostUnavailable("unavailable", "This Hermes isn't paired with Conduit's relay.")
        try:
            answer = client.request_json(f"{state['relay_url'].rstrip('/')}/v1/maps/token", method="POST",
                                         payload={}, credential=state["credential"], timeout=TIMEOUT_S)
        except client.RelayRejected as error:
            if error.status == 404:
                _unavailable_until = now + UNAVAILABLE_RETRY_S
                raise HostUnavailable("unavailable", "The notification relay doesn't offer Apple Maps.") from None
            raise HostUnavailable("unavailable", "Apple Maps couldn't be reached just now.") from None
        except Exception:  # noqa: BLE001 — network
            raise HostUnavailable("unavailable", "Apple Maps couldn't be reached just now.") from None
        value, lifetime = answer.get("access_token"), answer.get("expires_in")
        if not isinstance(value, str) or not _TOKEN.fullmatch(value) or not _lifetime(lifetime):
            raise HostUnavailable("unavailable", "Apple Maps couldn't be reached just now.")
        _token.update(value=value, expires_at=now + float(lifetime))
        return value


def _spend(home: Path) -> None:
    """One Apple call against this host's daily share of Conduit's quota.

    The count is read and rewritten in place under a file lock, so gateway
    and dashboard processes sharing a home count together. The file is
    owner-only from the first byte and never followed through a symlink.
    """
    path = Path(home) / USAGE_FILE
    flags = os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0)
    try:
        with _usage_lock, os.fdopen(os.open(path, flags, 0o600), "r+b") as handle, client._exclusive(handle):
            _count_one(handle)
    except OSError:
        raise HostUnavailable("unavailable", "This Hermes host can't keep count of its Apple Maps lookups.") from None


def _count_one(handle: Any) -> None:
    # A crash mid-write leaves a partial record, read back as a fresh day:
    # at worst one day's count restarts.
    if hasattr(os, "fchmod"):
        os.fchmod(handle.fileno(), 0o600)
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    handle.seek(0)
    try:
        usage = json.loads(handle.read(4096) or b"{}")
    except ValueError:
        usage = {}
    if not isinstance(usage, dict):
        usage = {}
    count = usage.get("count", 0) if usage.get("day") == today else 0
    if not isinstance(count, int) or isinstance(count, bool) or count < 0:
        count = 0
    if count >= DAILY_CALLS:
        raise HostUnavailable("daily_limit", "Today's Apple Maps lookups from this host are used up.")
    handle.seek(0)
    handle.truncate()
    handle.write(json.dumps({"day": today, "count": count + 1}).encode())
    handle.flush()
    os.fsync(handle.fileno())


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args: Any, **kwargs: Any) -> None:
        return None


_OPENER = urllib.request.build_opener(_NoRedirect)
MAX_ANSWER_BYTES = 512 * 1024


class Engine:
    def __init__(self, home: Path, fetch: Callable[[str, str], Any] | None = None, zone: Any = timezone.utc,
                 clock: Callable[[], float] = time.time) -> None:
        self.home = home
        self.fetch = fetch or self._fetch
        self.zone = zone
        self.clock = clock

    # --- Apple ------------------------------------------------------------------------

    def _fetch(self, url: str, token: str) -> Any:
        request = urllib.request.Request(url, headers={"Authorization": f"Bearer {token}",
                                                       "Accept": "application/json"})
        # No redirects (the token stays with Apple) and a capped body.
        with _OPENER.open(request, timeout=TIMEOUT_S) as response:
            body = response.read(MAX_ANSWER_BYTES + 1)
        if len(body) > MAX_ANSWER_BYTES:
            raise HostUnavailable("failed", "Apple Maps sent more than this host reads.")
        return json.loads(body or b"{}")

    def _get(self, path: str, params: dict[str, Any]) -> Any:
        query = urllib.parse.urlencode({key: value for key, value in params.items() if value is not None})
        url = f"{APPLE}/{path}" + (f"?{query}" if query else "")
        for attempt in (0, 1):
            token = _access_token(refresh=attempt == 1)
            if attempt == 0:
                # Counted once Apple will be asked, not when the relay can't
                # give a token. A retry with a fresh token after a 401 isn't
                # counted again: Apple refused the first one's token.
                _spend(self.home)
            try:
                return self.fetch(url, token)
            except urllib.error.HTTPError as error:
                if error.code == 401 and attempt == 0:
                    continue
                if error.code == 429:
                    raise HostUnavailable("throttled", "Apple Maps is busy; try again shortly.") from None
                if error.code == 404:
                    raise HostUnavailable("not_found", "Apple Maps found nothing for that.") from None
                raise HostUnavailable("failed", "Apple Maps couldn't answer just now.") from None
            except HostUnavailable:
                raise
            except Exception:  # noqa: BLE001 — network
                raise HostUnavailable("failed", "Apple Maps couldn't be reached just now.") from None
        raise HostUnavailable("failed", "Apple Maps refused this host's token.")

    # --- Lookups ----------------------------------------------------------------------

    def answer(self, request: dict[str, Any], me: dict[str, Any] | None) -> dict[str, Any]:
        """``me``: {"lat", "lon", "detail", "label"} from the host's last fix,
        or None when the request doesn't use it."""
        action = request["action"]
        if action == "search":
            return self._search(request, me)
        if action == "lookup":
            return self._lookup(request, me)
        if action == "reverse_geocode":
            return self._reverse(request, me)
        if action == "directions":
            return self._directions(request, me)
        return self._travel_times(request, me)

    def _point(self, ref: dict[str, Any], me: dict[str, Any] | None, hint: tuple[float, float] | None
               ) -> tuple[tuple[float, float], dict[str, Any]]:
        """A ref as (lat, lon) and the place to show for it."""
        if ref.get("me"):
            assert me is not None
            return (me["lat"], me["lon"]), _me_place(me)
        if "saved" in ref:
            raise NeedsPhone()
        if "lat" in ref:
            point = (ref["lat"], ref["lon"])
            return point, {"name": f"{point[0]:.5f}, {point[1]:.5f}", "lat": point[0], "lon": point[1]}
        if "place_id" in ref:
            place = self._get(f"place/{urllib.parse.quote(ref['place_id'], safe='')}", {})
            return self._located(place, None)
        results = (self._get("search", {"q": ref["text"], "searchLocation": _pair(hint)}) or {}).get("results") or []
        if not results:
            raise HostUnavailable("not_found", f"Apple Maps found nothing for \"{ref['text']}\".")
        return self._located(results[0], hint)

    def _located(self, place: Any, centre: tuple[float, float] | None) -> tuple[tuple[float, float], dict[str, Any]]:
        shown = _place(place, centre, None)
        if "lat" not in shown:
            raise HostUnavailable("not_found", "Apple Maps gave a place without a location.")
        return (shown["lat"], shown["lon"]), shown

    def _search(self, request: dict[str, Any], me: dict[str, Any] | None) -> dict[str, Any]:
        centre, _ = self._point(request["near"], me, _mine(me))
        found = (self._get("search", {"q": request["query"], "searchLocation": _pair(centre)}) or {}).get("results")
        detail = me["detail"] if me and request["near"].get("me") else "precise"
        places = [_place(item, centre, detail) for item in (found or [])[:request.get("limit", 10)]]
        if not places:
            raise HostUnavailable("not_found", "Apple Maps found nothing for that.")
        return {"places": places}

    def _lookup(self, request: dict[str, Any], me: dict[str, Any] | None) -> dict[str, Any]:
        hint = None
        if request.get("near"):
            hint, _ = self._point(request["near"], me, _mine(me))
        elif me:
            hint = _mine(me)
        found = (self._get("search", {"q": request["query"], "searchLocation": _pair(hint)}) or {}).get("results")
        places = [_place(item, None, None) for item in (found or [])[:MAX_LOOKUP]]
        if not places:
            raise HostUnavailable("not_found", "Apple Maps found nothing for that.")
        return {"places": places}

    def _reverse(self, request: dict[str, Any], me: dict[str, Any] | None) -> dict[str, Any]:
        if request["near"].get("me") and me and me["detail"] != "precise":
            # Around a 1 km cell, the area name is the honest answer.
            return {"places": [_me_place(me)]}
        point, _ = self._point(request["near"], me, None)
        found = (self._get("reverseGeocode", {"loc": _pair(point)}) or {}).get("results") or []
        if not found:
            raise HostUnavailable("not_found", "Apple Maps has no address there.")
        return {"places": [_place(item, None, None) for item in found[:MAX_LOOKUP]]}

    def _when(self, request: dict[str, Any]) -> dict[str, str]:
        for field, param in (("arrive_by", "arrivalDate"), ("depart_at", "departureDate")):
            if request.get(field):
                return {param: _utc(request[field], self.zone)}
        return {}

    def _directions(self, request: dict[str, Any], me: dict[str, Any] | None) -> dict[str, Any]:
        if request["mode"] == "transit":
            raise NeedsPhone()
        origin, origin_place = self._point(request["from"], me, _mine(me))
        destination, destination_place = self._point(request["to"][0], me, origin)
        answer = self._get("directions", {
            "origin": _pair(origin), "destination": _pair(destination), "transportType": TRANSPORT[request["mode"]],
            "requestsAlternateRoutes": "true" if request.get("alternatives", True) else "false",
            **self._when(request)}) or {}
        steps = answer.get("steps") or []
        hide_steps = bool(request["from"].get("me") and me and me["detail"] != "precise") or not request.get("steps", True)
        routes = []
        for route in (answer.get("routes") or [])[:3 if request.get("alternatives", True) else 1]:
            shown: dict[str, Any] = {"name": route.get("name"), "duration_s": route.get("durationSeconds"),
                                     "distance_m": route.get("distanceMeters"), "has_tolls": route.get("hasTolls")}
            if not hide_steps:
                shown["steps"] = [{"text": steps[index].get("instructions"), "distance_m": steps[index].get("distanceMeters")}
                                  for index in route.get("stepIndexes") or []
                                  if isinstance(index, int) and 0 <= index < len(steps) and steps[index].get("instructions")]
            routes.append(shown)
        if not routes:
            raise HostUnavailable("no_route", "Apple Maps has no route for that.")
        result = {"from": origin_place, "to": destination_place, "mode": request["mode"], "routes": routes}
        result.update(self._times(request, routes[0].get("duration_s")))
        return result

    def _travel_times(self, request: dict[str, Any], me: dict[str, Any] | None) -> dict[str, Any]:
        origin, origin_place = self._point(request["from"], me, _mine(me))
        targets: list[tuple[tuple[float, float] | None, dict[str, Any]]] = []
        for ref in request["to"]:
            try:
                targets.append(self._point(ref, me, origin))
            except HostUnavailable as error:
                if error.error != "not_found":
                    raise
                targets.append((None, {"name": ref.get("text") or ref.get("place_id") or "?"}))
        wanted = [point for point, _ in targets if point is not None]
        etas: list[Any] = []
        if wanted:
            etas = (self._get("etas", {"origin": _pair(origin), "destinations": "|".join(_pair(p) for p in wanted),
                                       "transportType": TRANSPORT[request["mode"]], **self._when(request)}) or {}
                    ).get("etas") or []
        results, cursor = [], 0
        for point, place in targets:
            if point is None:
                results.append({"to": place, "error": "not_found"})
                continue
            eta = etas[cursor] if cursor < len(etas) and isinstance(etas[cursor], dict) else None
            cursor += 1
            if eta is None or eta.get("expectedTravelTimeSeconds") is None:
                results.append({"to": place, "error": "no_route"})
                continue
            item = {"to": place, "duration_s": eta.get("expectedTravelTimeSeconds"),
                    "typical_duration_s": eta.get("staticTravelTimeSeconds"), "distance_m": eta.get("distanceMeters")}
            item.update({key: value for key, value in self._times(request, item["duration_s"]).items()
                         if key in ("arrive_at", "leave_by")})
            results.append(item)
        return {"from": origin_place, "mode": request["mode"], "results": results}

    def _times(self, request: dict[str, Any], seconds: Any) -> dict[str, str]:
        if not isinstance(seconds, (int, float)):
            return {}
        if request.get("arrive_by"):
            arrive = _local(request["arrive_by"], self.zone)
            return {"arrive_at": _show(arrive), "leave_by": _show(arrive - timedelta(seconds=seconds))}
        depart = _local(request["depart_at"], self.zone) if request.get("depart_at") else \
            datetime.fromtimestamp(self.clock(), self.zone)
        return {"depart_at": _show(depart), "arrive_at": _show(depart + timedelta(seconds=seconds))}


# --- Helpers -------------------------------------------------------------------------


def _pair(point: tuple[float, float] | None) -> str | None:
    return None if point is None else f"{point[0]:.6f},{point[1]:.6f}"


def _mine(me: dict[str, Any] | None) -> tuple[float, float] | None:
    return None if me is None else (me["lat"], me["lon"])


def _me_place(me: dict[str, Any]) -> dict[str, Any]:
    # The user's own position is never echoed back as coordinates or an address.
    return {"name": me.get("label") or "the user's position"}


def _place(item: Any, centre: tuple[float, float] | None, detail: str | None) -> dict[str, Any]:
    item = item if isinstance(item, dict) else {}
    shown: dict[str, Any] = {"name": item.get("name") or ", ".join((item.get("formattedAddressLines") or [])[:1]) or "?"}
    lines = item.get("formattedAddressLines")
    if isinstance(lines, list) and lines:
        shown["address"] = ", ".join(str(line) for line in lines[:4])
    if item.get("poiCategory"):
        shown["category"] = item["poiCategory"]
    coordinate = item.get("coordinate") or {}
    lat, lon = coordinate.get("latitude"), coordinate.get("longitude")
    if isinstance(lat, (int, float)) and isinstance(lon, (int, float)):
        shown["lat"], shown["lon"] = lat, lon
        if centre is not None:
            metres = _metres_between(centre, (lat, lon))
            shown["distance_m"] = round(metres, -2) if detail == "area" else round(metres)
    if item.get("id"):
        shown["place_id"] = item["id"]
    return shown


def _metres_between(a: tuple[float, float], b: tuple[float, float]) -> float:
    lat1, lon1, lat2, lon2 = map(math.radians, (a[0], a[1], b[0], b[1]))
    h = math.sin((lat2 - lat1) / 2) ** 2 + math.cos(lat1) * math.cos(lat2) * math.sin((lon2 - lon1) / 2) ** 2
    return 2 * 6_371_000 * math.asin(math.sqrt(h))


def _local(value: str, zone: Any) -> datetime:
    parsed = datetime.fromisoformat(value[:-1] + "+00:00" if value.endswith("Z") else value)
    return parsed.replace(tzinfo=zone) if parsed.tzinfo is None else parsed.astimezone(zone)


def _utc(value: str, zone: Any) -> str:
    return _local(value, zone).astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _show(moment: datetime) -> str:
    return moment.isoformat(timespec="minutes")
