"""Pages from the host: a dev server on the Hermes host, for the coding workspace's Browser tabs."""

from __future__ import annotations

import asyncio
import contextlib
import http.client
import json
import socket
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Dict, Iterator, List, Optional, Tuple

from fastapi import HTTPException, Request
from starlette.responses import StreamingResponse

from . import core
from . import terminals
from . import workspace

logger = core.logger
router = core.router


# --- Pages from the host ---------------------------------------------------------
#
# Conduit for desktop's workbench (issue 451) shows a page a dev server on the
# Hermes host serves (`npm run dev` in a terminal, or the bot's), as if the
# desktop were the host: a Browser tab's localhost:5173 is the host's. The
# desktop serves the page from a scheme of its own and sends each of the
# page's requests here, wrapped, so nothing of the page's mixes with the
# dashboard's own requests:
#
# ANY /workspace/preview/{port}/{path}?{query}
#     X-Conduit-Page-Headers: JSON [[name, value], ...], the page's own headers
#     body: the page's own body
#  -> 200, X-Conduit-Page-Status: the dev server's status
#          X-Conduit-Page-Headers: JSON [[name, value], ...], its headers
#     body: its body, as it sent it
#
# The dashboard's own credential, cookies and proxy headers never reach the
# dev server, and its Set-Cookie, redirects and 401s never reach the
# dashboard's sign-in: the dashboard answers 200 for any answer it got, and
# its own refusals (a port it won't reach, pages turned off) are the usual
# JSON errors. A page reaches only this host's loopback, never another
# machine. It follows the terminals switch (Settings > Allow terminals),
# since a terminal reaches the same servers, and is off on a hosted Hermes.

PREVIEW_LIMIT = 6000
PREVIEW_WINDOW_S = 60.0
# A dev server compiles a page on its first request, which can take a while.
PREVIEW_TIMEOUT_S = 120.0
PREVIEW_MAX_BODY_BYTES = 16 * 1024 * 1024
PREVIEW_MAX_HEADERS_CHARS = 64 * 1024
PREVIEW_CHUNK = 64 * 1024
PAGE_HEADERS = "X-Conduit-Page-Headers"
PAGE_STATUS = "X-Conduit-Page-Status"
METHODS = ["GET", "HEAD", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"]
# Headers about this hop, which the dev server and the page each set for their own.
HOP_HEADERS = frozenset({
    "connection", "keep-alive", "proxy-connection", "proxy-authenticate", "proxy-authorization",
    "te", "trailer", "transfer-encoding", "upgrade", "host", "content-length",
})
_preview_limiter = core._MintLimiter(PREVIEW_LIMIT, PREVIEW_WINDOW_S,
                                     message="Too many page requests; try again shortly")
_preview_executor = ThreadPoolExecutor(max_workers=8, thread_name_prefix="conduit-preview")


class _Unreachable(Exception):
    """Nothing answers on that port."""


def _why_not() -> Optional[Tuple[int, str]]:
    """(status, reason) when pages from this host are off, else None."""
    if not terminals.terminals_enabled():
        return 403, "Terminals and pages from this host are turned off on this Hermes"
    try:
        boundary = workspace._managed_boundary()
    except Exception:
        return 503, "Hermes couldn't read which folder its files are kept in"
    if boundary is not None:
        return 403, "Pages from the host are off on a hosted Hermes"
    return None


def _page_headers(raw: Optional[str]) -> List[Tuple[str, str]]:
    """The page's own headers, as the desktop wrapped them; hop headers dropped."""
    if not raw:
        return []
    if len(raw) > PREVIEW_MAX_HEADERS_CHARS:
        raise HTTPException(status_code=431, detail="The page's headers are too long")
    try:
        pairs = json.loads(raw)
    except ValueError:
        raise HTTPException(status_code=400, detail=f"{PAGE_HEADERS} must be JSON")
    if not isinstance(pairs, list):
        raise HTTPException(status_code=400, detail=f"{PAGE_HEADERS} must be a list")
    headers: List[Tuple[str, str]] = []
    for pair in pairs:
        if (not isinstance(pair, list) or len(pair) != 2 or not all(isinstance(p, str) for p in pair)
                or any(c in pair[0] + pair[1] for c in "\r\n\0") or not pair[0].strip()):
            raise HTTPException(status_code=400, detail=f"{PAGE_HEADERS} must hold [name, value] pairs")
        if pair[0].strip().lower() in HOP_HEADERS:
            continue
        headers.append((pair[0].strip(), pair[1]))
    return headers


def _target(request: Request, port: int) -> str:
    """The path and query the page asked for, as it sent them (not decoded)."""
    raw = request.scope.get("raw_path") or request.url.path.encode("utf-8")
    path = raw.decode("latin-1")
    marker = f"/workspace/preview/{port}"
    at = path.find(marker)
    rest = path[at + len(marker):] if at >= 0 else "/"
    if not rest.startswith("/"):
        rest = "/" + rest
    query = request.scope.get("query_string") or b""
    return rest + ("?" + query.decode("latin-1") if query else "")


def _open(port: int, method: str, target: str, headers: List[Tuple[str, str]],
          body: bytes) -> Tuple[http.client.HTTPConnection, http.client.HTTPResponse]:
    """The dev server's answer, from this host's loopback: 127.0.0.1, then ::1
    (a server on "localhost" may listen on either)."""
    refused: Optional[Exception] = None
    for address in ("127.0.0.1", "::1"):
        conn = http.client.HTTPConnection(address, port, timeout=PREVIEW_TIMEOUT_S)
        try:
            conn.putrequest(method, target, skip_host=True, skip_accept_encoding=True)
            conn.putheader("Host", f"localhost:{port}")
            for name, value in headers:
                if name.lower() == "accept-encoding":
                    continue
                conn.putheader(name, value)
            # As it sent it, so the desktop hands the page bytes it can read.
            conn.putheader("Accept-Encoding", "identity")
            if body or method in ("POST", "PUT", "PATCH"):
                conn.putheader("Content-Length", str(len(body)))
            conn.endheaders(body or None)
            return conn, conn.getresponse()
        except (ConnectionRefusedError, socket.gaierror, OSError) as exc:
            conn.close()
            if isinstance(exc, (socket.timeout, TimeoutError)):
                raise
            refused = exc
            continue
    raise _Unreachable(str(refused) if refused else "refused")


def _chunks(conn: http.client.HTTPConnection, answer: http.client.HTTPResponse) -> Iterator[bytes]:
    try:
        while True:
            chunk = answer.read1(PREVIEW_CHUNK) if hasattr(answer, "read1") else answer.read(PREVIEW_CHUNK)
            if not chunk:
                return
            yield chunk
    except (OSError, http.client.HTTPException):
        # The dev server stopped mid-answer: the page gets what came.
        return
    finally:
        with contextlib.suppress(Exception):
            conn.close()


async def _body(request: Request) -> bytes:
    """The page's body, refused past the cap without buffering it all."""
    too_large = HTTPException(status_code=413, detail="The page's request is too large",
                              headers={"Cache-Control": "no-store"})
    try:
        declared = int(request.headers.get("content-length") or 0)
    except ValueError:
        declared = 0
    if declared > PREVIEW_MAX_BODY_BYTES:
        raise too_large
    raw = bytearray()
    async for chunk in request.stream():
        raw.extend(chunk)
        if len(raw) > PREVIEW_MAX_BODY_BYTES:
            raise too_large
    return bytes(raw)


def _answer_headers(answer: http.client.HTTPResponse) -> List[List[str]]:
    return [[name, value] for name, value in answer.getheaders() if name.lower() not in HOP_HEADERS]


@router.api_route("/workspace/preview/{port}/{path:path}", methods=METHODS)
async def workspace_preview(request: Request, port: str, path: str = "") -> Any:  # noqa: ARG001 — read raw
    headers_out = {"Cache-Control": "no-store"}
    try:
        number = int(port)
    except ValueError:
        number = 0
    if not 1 <= number <= 65535 or str(number) != port:
        raise HTTPException(status_code=400, detail="port must be a number from 1 to 65535", headers=headers_out)
    server = request.scope.get("server")
    if isinstance(server, (tuple, list)) and len(server) > 1 and server[1] == number:
        raise HTTPException(status_code=403, detail="That port is the dashboard's own", headers=headers_out)
    await core._scoped_acquire(_preview_limiter, None)
    refused = await asyncio.get_running_loop().run_in_executor(_preview_executor, _why_not)
    if refused is not None:
        raise HTTPException(status_code=refused[0], detail=refused[1], headers=headers_out)
    page_headers = _page_headers(request.headers.get(PAGE_HEADERS))
    body = await _body(request) if request.method not in ("GET", "HEAD") else b""
    target = _target(request, number)
    loop = asyncio.get_running_loop()
    try:
        conn, answer = await asyncio.wait_for(
            loop.run_in_executor(_preview_executor, _open, number, request.method, target, page_headers, body),
            timeout=PREVIEW_TIMEOUT_S + 5)
    except _Unreachable:
        raise HTTPException(status_code=502, detail=f"Nothing on this host answers on port {number}",
                            headers=headers_out)
    except (asyncio.TimeoutError, socket.timeout, TimeoutError):
        raise HTTPException(status_code=504, detail=f"The server on port {number} took too long to answer",
                            headers=headers_out)
    except (OSError, http.client.HTTPException) as exc:
        logger.info("Conduit preview of port %s failed: %s", number, exc)
        raise HTTPException(status_code=502, detail=f"The server on port {number} didn't answer properly",
                            headers=headers_out)
    headers_out[PAGE_STATUS] = str(answer.status)
    headers_out[PAGE_HEADERS] = json.dumps(_answer_headers(answer), ensure_ascii=True)
    if request.method == "HEAD":
        conn.close()
        return StreamingResponse(iter(()), status_code=200, headers=headers_out,
                                 media_type="application/octet-stream")
    return StreamingResponse(_chunks(conn, answer), status_code=200, headers=headers_out,
                             media_type="application/octet-stream")
