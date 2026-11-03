"""Read aloud, streamed (notifier#?): the first sentence plays while the rest is
synthesized. These hold the three promises the route makes — a host without a
chunked provider says so instead of downgrading the voice, a bad request is
refused before any synthesis starts, and chunked PCM is re-encoded to MP3 as it
arrives rather than buffered whole."""

import importlib.util
from pathlib import Path

import pytest

from fastapi import FastAPI
from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parents[1]


def _load_plugin_api():
    # The dashboard imports plugin_api.py by file path, not as a package.
    spec = importlib.util.spec_from_file_location("conduit_plugin_api_read_aloud", ROOT / "dashboard" / "plugin_api.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


api = _load_plugin_api()
read_aloud = api.read_aloud

SILENCE = b"\x00\x00" * 2400  # 0.1 s of 24 kHz mono int16


@pytest.fixture(autouse=True)
def _scoped_runs_inline(monkeypatch):
    """core._run_scoped reaches Hermes' profile machinery; the limiter is not what
    these tests are about, so run the scoped call inline instead."""
    async def scoped(profile, fn, executor=None):
        return fn()
    monkeypatch.setattr(api.core, "_run_scoped", scoped)


def _client() -> TestClient:
    app = FastAPI()
    app.include_router(api.router)
    return TestClient(app)


class _Streamer:
    """A TTS provider that opted into raw PCM, as a plugin one would."""

    sample_rate = 24000

    def __init__(self, chunks):
        self._chunks = chunks

    def stream(self, text, voice=None):
        assert isinstance(text, str) and text
        for chunk in self._chunks:
            yield chunk


def test_status_reports_no_streamer_without_downgrading(monkeypatch):
    monkeypatch.setattr(read_aloud, "_tts_config", lambda: {"provider": "edge"})
    monkeypatch.setattr(read_aloud, "_streamer", lambda cfg: None)
    body = _client().get("/read-aloud/status").json()
    assert body["ok"] is True
    assert body["streaming"] is False
    assert body["reason"] == "provider_has_no_chunked_api"
    assert body["provider"] == "edge"


def test_status_reports_the_rate_it_would_stream(monkeypatch):
    monkeypatch.setattr(read_aloud, "_tts_config", lambda: {"provider": "elio"})
    monkeypatch.setattr(read_aloud, "_streamer", lambda cfg: _Streamer([SILENCE]))
    body = _client().get("/read-aloud/status").json()
    assert body["streaming"] is True
    assert body["sample_rate"] == 24000
    assert body["formats"] == ["mp3", "pcm"]


def test_stream_refuses_empty_text(monkeypatch):
    monkeypatch.setattr(read_aloud, "_tts_config", lambda: {"provider": "elio"})
    monkeypatch.setattr(read_aloud, "_streamer", lambda cfg: _Streamer([SILENCE]))
    body = _client().post("/read-aloud/stream", json={"text": "   "}).json()
    assert body["ok"] is False
    assert "text is required" in body["error"]


def test_stream_refuses_unknown_format(monkeypatch):
    monkeypatch.setattr(read_aloud, "_tts_config", lambda: {"provider": "elio"})
    monkeypatch.setattr(read_aloud, "_streamer", lambda cfg: _Streamer([SILENCE]))
    body = _client().post("/read-aloud/stream", json={"text": "hello", "format": "wav"}).json()
    assert body["ok"] is False
    assert "format must be one of" in body["error"]


def test_stream_says_so_when_the_host_cannot_stream(monkeypatch):
    monkeypatch.setattr(read_aloud, "_tts_config", lambda: {"provider": "edge"})
    monkeypatch.setattr(read_aloud, "_streamer", lambda cfg: None)
    body = _client().post("/read-aloud/stream", json={"text": "hello"}).json()
    assert body["ok"] is False
    assert body["reason"] == "provider_has_no_chunked_api"


def test_stream_returns_chunked_mp3(monkeypatch):
    monkeypatch.setattr(read_aloud, "_tts_config", lambda: {"provider": "elio"})
    monkeypatch.setattr(read_aloud, "_streamer", lambda cfg: _Streamer([SILENCE, SILENCE]))
    response = _client().post("/read-aloud/stream", json={"text": "hello there"})
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("audio/mpeg")
    assert response.headers["cache-control"] == "no-store"
    assert len(response.content) > 0


def test_pcm_format_carries_its_own_rate(monkeypatch):
    monkeypatch.setattr(read_aloud, "_tts_config", lambda: {"provider": "elio"})
    monkeypatch.setattr(read_aloud, "_streamer", lambda cfg: _Streamer([SILENCE]))
    response = _client().post("/read-aloud/stream", json={"text": "hello", "format": "pcm"})
    assert response.status_code == 200
    assert response.headers["x-audio-sample-rate"] == "24000"
    assert response.content == SILENCE
