"""probes/grok_watch_token_probe.py against a local stand-in for xAI's realtime socket."""

import asyncio
import base64
import contextlib
import importlib.util
import json
import pathlib
import time
import types

ROOT = pathlib.Path(__file__).resolve().parents[1]


def _load_probe():
    spec = importlib.util.spec_from_file_location("conduit_grok_watch_token_probe", ROOT / "probes" / "grok_watch_token_probe.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


probe = _load_probe()


@contextlib.asynccontextmanager
async def fake_xai(audio_to_done_s=0.0, header_auth=True):
    """A realtime socket that answers each response.create with audio at once
    and response.done `audio_to_done_s` later. Without `header_auth` it refuses
    an Authorization header (401) and only takes the xai-client-secret
    subprotocol."""
    from websockets.asyncio.server import serve

    opened = []

    def process_request(connection, request):
        if request.headers.get("Authorization"):
            if not header_auth:
                return connection.respond(401, "header auth not supported\n")
            opened.append("header")
        elif request.headers.get("Sec-WebSocket-Protocol", "").startswith("xai-client-secret."):
            opened.append("subprotocol")
        else:
            return connection.respond(401, "no token\n")
        return None

    def select_subprotocol(connection, offered):
        return offered[0] if offered else None

    async def handler(connection):
        async for message in connection:
            kind = json.loads(message).get("type")
            if kind == "session.update":
                await connection.send(json.dumps({"type": "session.updated"}))
            elif kind == "response.create":
                await connection.send(json.dumps({"type": "response.output_audio.delta",
                                                  "delta": base64.b64encode(b"\1" * 4800).decode()}))
                await asyncio.sleep(audio_to_done_s)
                await connection.send(json.dumps({"type": "response.done"}))

    async with serve(handler, "127.0.0.1", 0, process_request=process_request,
                     select_subprotocol=select_subprotocol) as server:
        port = server.sockets[0].getsockname()[1]
        yield f"http://127.0.0.1:{port}", opened


def test_first_audio_is_timed_when_the_audio_arrives_not_when_the_response_ends():
    async def scenario():
        async with fake_xai(audio_to_done_s=1.5) as (base, _):
            socket, refused = await probe.open_socket(f"{base.replace('http', 'ws', 1)}/realtime", "token", "header")
            assert refused is None
            call = probe.Call(socket, time.monotonic())
            try:
                return await call.turn("hello")
            finally:
                await call.close()

    turn = asyncio.run(scenario())
    assert turn["ok"]
    assert turn["first_audio_s"] is not None and turn["first_audio_s"] < 0.75, turn


def test_token_reuse_is_checked_with_the_method_that_opened_the_call(monkeypatch):
    class TokenError(Exception):
        pass

    api = types.SimpleNamespace(
        _profile_scope=lambda profile: contextlib.nullcontext(),
        grok_live_credentials=lambda: ("bearer", "test sign-in"),
        grok_live_model_voice=lambda: ("grok-test", "Ara"),
        TokenError=TokenError,
    )
    monkeypatch.setattr(probe, "mint", lambda base, bearer, seconds: {"status": 200, "ms": 1, "token": "token"})

    async def scenario():
        async with fake_xai(header_auth=False) as (base, opened):
            # -20 cancels the hold past expiry (expires + 20 s): the stand-in
            # never expires the token, so there is nothing to wait for.
            args = types.SimpleNamespace(profile=None, expires=-20, api_base=base)
            return await probe.run(args, api), opened

    summary, opened = asyncio.run(scenario())
    assert summary["connect_header"] == 401
    assert summary["connect_subprotocol"] == "ok"
    assert summary["reuse_while_open"] == "accepted"
    assert summary["reconnect_after_expiry"] == "accepted"
    assert opened == ["subprotocol"] * 3


def test_only_a_plain_loopback_url_counts_as_a_local_test_server():
    assert probe.local_test_server("http://127.0.0.1:8080")
    assert probe.local_test_server("http://127.0.0.1:8080/v1")
    for url in ("http://127.0.0.1:80@example.com", "http://user@127.0.0.1:8080", "http://127.0.0.1",
                "https://127.0.0.1:8080", "http://127.0.0.1:8080/elsewhere", "http://127.0.0.1:8080?x=1",
                "http://127.0.0.1:99999", "http://127.0.0.1.example.com:8080"):
        assert not probe.local_test_server(url), url
