"""Transient provider failures are retried; permanent ones aren't.

Drives the real `_openai_chat` / `_ollama_chat` / stream functions through a
stubbed httpx transport, so the retry decision, backoff and Retry-After parsing
are exercised as they run in production rather than mocked away.

`time.sleep` is patched out — these assert the retry policy, not wall-clock.
"""
from __future__ import annotations

import httpx
import pytest

from app.core.ai import providers


@pytest.fixture(autouse=True)
def _no_sleep(monkeypatch):
    slept: list[float] = []
    monkeypatch.setattr(providers.time, "sleep", lambda s: slept.append(s))
    return slept


@pytest.fixture
def openai_cfg():
    return {"model": "gpt-x", "base_url": "https://api.example/v1",
            "api_key": "k", "timeout": 5}


@pytest.fixture
def ollama_cfg():
    return {"model": "llama", "url": "http://ollama:11434", "timeout": 5}


def _transport(monkeypatch, responses):
    """Serve `responses` (status, json, headers) in order to any request."""
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        i = min(calls["n"], len(responses) - 1)
        calls["n"] += 1
        status, payload, headers = responses[i]
        return httpx.Response(status, json=payload, headers=headers or {})

    real_client = httpx.Client

    def fake_client(*a, **kw):
        return real_client(*a, **{**kw, "transport": httpx.MockTransport(handler)})

    monkeypatch.setattr(providers.httpx, "Client", fake_client)
    return calls


_OK = {"choices": [{"message": {"content": "hi"}}], "message": {"content": "hi"}}


# --- retried failures --------------------------------------------------------

def test_openai_retries_429_then_succeeds(monkeypatch, openai_cfg, _no_sleep):
    calls = _transport(monkeypatch, [(429, {"error": "slow down"}, None), (200, _OK, None)])

    out = providers._openai_chat([{"role": "user", "content": "x"}], openai_cfg)

    assert out["choices"][0]["message"]["content"] == "hi"
    assert calls["n"] == 2
    assert len(_no_sleep) == 1


@pytest.mark.parametrize("status", [408, 409, 425, 429, 500, 502, 503, 504])
def test_every_transient_status_is_retried(monkeypatch, openai_cfg, status, _no_sleep):
    calls = _transport(monkeypatch, [(status, {}, None), (200, _OK, None)])

    providers._openai_chat([{"role": "user", "content": "x"}], openai_cfg)

    assert calls["n"] == 2, f"status {status} should have been retried"


def test_ollama_retries_503_while_model_loads(monkeypatch, ollama_cfg, _no_sleep):
    calls = _transport(monkeypatch, [(503, {}, None), (200, _OK, None)])

    out = providers._ollama_chat(ollama_cfg, [{"role": "user", "content": "x"}])

    assert out["message"]["content"] == "hi"
    assert calls["n"] == 2


def test_gives_up_after_max_attempts(monkeypatch, openai_cfg, _no_sleep):
    calls = _transport(monkeypatch, [(503, {}, None)])

    with pytest.raises(httpx.HTTPStatusError):
        providers._openai_chat([{"role": "user", "content": "x"}], openai_cfg)

    assert calls["n"] == providers.MAX_ATTEMPTS
    assert len(_no_sleep) == providers.MAX_ATTEMPTS - 1


# --- failures that must NOT be retried ---------------------------------------

@pytest.mark.parametrize("status", [400, 401, 403, 404, 422])
def test_permanent_errors_are_not_retried(monkeypatch, openai_cfg, status, _no_sleep):
    """A wrong API key must fail immediately, not look like a hang."""
    calls = _transport(monkeypatch, [(status, {"error": "nope"}, None)])

    with pytest.raises(httpx.HTTPStatusError):
        providers._openai_chat([{"role": "user", "content": "x"}], openai_cfg)

    assert calls["n"] == 1
    assert _no_sleep == []


# --- Retry-After -------------------------------------------------------------

def test_retry_after_header_is_honoured(monkeypatch, openai_cfg, _no_sleep):
    _transport(monkeypatch, [(429, {}, {"retry-after": "7"}), (200, _OK, None)])

    providers._openai_chat([{"role": "user", "content": "x"}], openai_cfg)

    assert _no_sleep == [7.0]


def test_absurd_retry_after_is_capped(monkeypatch, openai_cfg, _no_sleep):
    _transport(monkeypatch, [(429, {}, {"retry-after": "99999"}), (200, _OK, None)])

    providers._openai_chat([{"role": "user", "content": "x"}], openai_cfg)

    assert _no_sleep == [providers._RETRY_AFTER_CAP]


def test_garbage_retry_after_falls_back_to_backoff(monkeypatch, openai_cfg, _no_sleep):
    """HTTP-date form and junk both fall through to exponential backoff."""
    _transport(monkeypatch, [(429, {}, {"retry-after": "Wed, 21 Oct 2026 07:28:00 GMT"}),
                             (200, _OK, None)])

    providers._openai_chat([{"role": "user", "content": "x"}], openai_cfg)

    assert len(_no_sleep) == 1
    assert 0 < _no_sleep[0] <= providers._BACKOFF_CAP


# --- backoff shape -----------------------------------------------------------

def test_backoff_grows_and_is_capped():
    delays = [providers._transient_delay(httpx.ConnectError("boom"), a) for a in range(8)]

    assert all(d is not None and d > 0 for d in delays)
    assert all(d <= providers._BACKOFF_CAP for d in delays)  # jitter never exceeds the cap
    assert delays[3] > delays[0], "backoff should grow with attempts"


def test_transport_errors_are_transient():
    assert providers._transient_delay(httpx.ConnectError("refused"), 0) is not None
    assert providers._transient_delay(httpx.ReadTimeout("slow"), 0) is not None


def test_unrelated_exceptions_are_not_retried():
    assert providers._transient_delay(ValueError("bug in our code"), 0) is None


# --- streaming ---------------------------------------------------------------

def test_stream_retries_before_first_token():
    attempts = {"n": 0}

    def make_stream():
        attempts["n"] += 1
        if attempts["n"] == 1:
            raise httpx.ConnectError("refused")
        yield "hello"
        yield " world"

    out = list(providers._stream_with_retries(make_stream, what="test"))

    assert out == ["hello", " world"]
    assert attempts["n"] == 2


def test_stream_does_not_retry_after_a_token_reached_the_user():
    """Retrying mid-stream would duplicate text already on screen."""
    attempts = {"n": 0}

    def make_stream():
        attempts["n"] += 1
        yield "partial"
        raise httpx.ConnectError("dropped")

    got = []
    with pytest.raises(httpx.ConnectError):
        for chunk in providers._stream_with_retries(make_stream, what="test"):
            got.append(chunk)

    assert got == ["partial"]
    assert attempts["n"] == 1, "must not restart a stream that already emitted"


def test_stream_gives_up_after_max_attempts():
    attempts = {"n": 0}

    def make_stream():
        attempts["n"] += 1
        raise httpx.ConnectError("refused")
        yield  # pragma: no cover - unreachable, marks this a generator

    with pytest.raises(httpx.ConnectError):
        list(providers._stream_with_retries(make_stream, what="test"))

    assert attempts["n"] == providers.MAX_ATTEMPTS
