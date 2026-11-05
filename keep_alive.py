"""Keeps HTTPS connections to the relay open between requests (#562).

urllib opens a new TCP and TLS connection for every request and closes it
after the answer. Pushes, Live Activity updates and a parked question's
polls to the relay come in bursts and every 2 s, so each paid a handshake
(one to two round trips) before the request went out. This handler is a drop-in
for urllib's own HTTPS handler: the opener, its redirect refusal and its
error handling stay as they were, and only the connection outlives the
request.

- A connection goes back to the pool only once its answer was read to the
  end and the relay didn't ask to close it; one read part way is closed.
- A pooled connection the relay (or what's in front of it) closed while it
  sat idle fails on the next send. That request is sent again once on a new
  connection when it failed while being written, or when asking twice
  changes nothing (GET, HEAD, PUT, DELETE, OPTIONS; urllib3's rule): a POST
  the relay answered-or-not after taking it whole, a pairing claim say, is
  not replayed. One that failed mid-write is, as urllib3 does: a claim the
  relay did take answers 404 the second time (the code is gone), and
  events are deduplicated.
- Through an HTTP(S) proxy, urllib's own handler runs (a tunnel each time).
- A forked process starts with an empty pool, and a connection that was in
  flight across the fork is closed rather than pooled: it never shares a
  socket with its parent.
"""

from __future__ import annotations

import http.client
import os
import socket
import ssl
import threading
import time
import urllib.error
import urllib.request
from typing import Any

# Kept at most this long between requests, and at most this many per relay.
IDLE_SECONDS = 60.0
MAX_IDLE_PER_ORIGIN = 4

# Asking these again changes nothing on the relay.
_IDEMPOTENT = frozenset({"GET", "HEAD", "DELETE", "PUT", "OPTIONS"})

# A pooled connection that fails like this was closed while it sat idle.
_STALE = (http.client.RemoteDisconnected, ConnectionResetError, BrokenPipeError, ConnectionAbortedError,
          ssl.SSLEOFError, ssl.SSLZeroReturnError)


class _PooledResponse(http.client.HTTPResponse):
    """Hands its connection back to the pool once it is closed, read whole."""

    _release: Any = None

    def close(self) -> None:
        # http.client drops fp once the whole body was read.
        whole = self.fp is None
        super().close()
        release, self._release = self._release, None
        if release is not None:
            release(whole and not self.will_close)


class KeepAliveHTTPSHandler(urllib.request.HTTPSHandler):
    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self._lock = threading.Lock()
        self._idle: dict[str, list[tuple[http.client.HTTPSConnection, float]]] = {}
        self._pid = os.getpid()
        self.connects = 0  # new connections made; tests count them

    def https_open(self, req: urllib.request.Request) -> Any:
        if req.has_proxy() or getattr(req, "_tunnel_host", None):
            return super().https_open(req)
        origin = req.host
        connection = self._take(origin)
        reused = connection is not None
        if connection is None:
            connection = self._connect(origin, req.timeout)
        written: list[bool] = []
        try:
            response = self._send(connection, req, written)
        except _STALE as error:
            connection.close()
            # Sent again only when the relay can't have acted on it: it
            # failed while being written, or asking again changes nothing
            # (a pairing claim must never be replayed).
            if not reused or (written and req.get_method() not in _IDEMPOTENT):
                raise urllib.error.URLError(error) from error
            connection = self._connect(origin, req.timeout)
            try:
                response = self._send(connection, req, [])
            except OSError as retry_error:
                connection.close()
                raise urllib.error.URLError(retry_error) from retry_error
        except OSError as error:
            connection.close()
            raise urllib.error.URLError(error) from error
        except BaseException:
            connection.close()
            raise

        opened_in = os.getpid()

        def release(reusable: bool) -> None:
            # Across a fork both processes hold this socket: neither pools it.
            if reusable and os.getpid() == opened_in:
                self._give_back(origin, connection)
            else:
                connection.close()

        response._release = release
        response.url = req.get_full_url()
        response.msg = response.reason
        return response

    def _connect(self, origin: str, timeout: Any) -> http.client.HTTPSConnection:
        self.connects += 1
        # http.client splits "relay.example:8443" itself.
        connection = http.client.HTTPSConnection(origin, timeout=timeout, context=self._context)
        connection.response_class = _PooledResponse
        return connection

    @staticmethod
    def _send(connection: http.client.HTTPSConnection, req: urllib.request.Request,
              written: list[bool]) -> http.client.HTTPResponse:
        # The request's timeout covers this request, a reused connection too.
        connection.timeout = req.timeout
        if connection.sock is not None and req.timeout is not socket._GLOBAL_DEFAULT_TIMEOUT:
            connection.sock.settimeout(req.timeout)
        # As urllib's own do_open, without its "Connection: close".
        headers = dict(req.unredirected_hdrs)
        headers.update({key: value for key, value in req.headers.items() if key not in headers})
        headers = {name.title(): value for name, value in headers.items()}
        connection.request(req.get_method(), req.selector, req.data, headers,
                           encode_chunked=req.has_header("Transfer-encoding"))
        written.append(True)
        return connection.getresponse()

    def _take(self, origin: str) -> http.client.HTTPSConnection | None:
        now = time.monotonic()
        stale: list[http.client.HTTPSConnection] = []
        found = None
        with self._lock:
            self._forget_if_forked()
            idle = self._idle.get(origin, [])
            while idle:
                connection, since = idle.pop()
                if now - since <= IDLE_SECONDS and connection.sock is not None:
                    found = connection
                    break
                stale.append(connection)
        for connection in stale:
            connection.close()
        return found

    def _give_back(self, origin: str, connection: http.client.HTTPSConnection) -> None:
        with self._lock:
            self._forget_if_forked()
            idle = self._idle.setdefault(origin, [])
            if len(idle) < MAX_IDLE_PER_ORIGIN:
                idle.append((connection, time.monotonic()))
                return
        connection.close()

    def _forget_if_forked(self) -> None:
        # Under the lock. The parent's sockets stay the parent's.
        if self._pid != os.getpid():
            self._pid = os.getpid()
            self._idle = {}

    def close_idle(self) -> None:
        with self._lock:
            idle, self._idle = self._idle, {}
        for connections in idle.values():
            for connection, _since in connections:
                connection.close()
