"""Provider dispatch: resolve current backend + uniform chat/text/json calls."""
from __future__ import annotations

import json
import logging
import random
import time
from typing import Callable, Iterator, TypeVar

import anthropic
import httpx

logger = logging.getLogger(__name__)

T = TypeVar("T")

# ---- Transient-failure retry -----------------------------------------------
# A 429 from a hosted provider, or a 503 while Ollama loads a model, used to
# surface to the user as a hard error. Only the httpx paths are wrapped:
# the Anthropic SDK does its own retrying (see MAX_ATTEMPTS below), and stacking
# ours on top would multiply the attempts.

MAX_ATTEMPTS = 3
_BACKOFF_BASE = 0.5          # seconds; doubled per attempt
_BACKOFF_CAP = 8.0
_RETRY_AFTER_CAP = 30.0      # ignore absurd Retry-After values rather than hang

# Retried: rate limits, and the 5xx family that means "try again", plus 408/409/425.
# Never retried: 400/401/403/404 — those are configuration or auth problems, and
# retrying just makes a wrong API key look like a hang.
_RETRY_STATUSES = frozenset({408, 409, 425, 429, 500, 502, 503, 504})


def _retry_after_seconds(exc: httpx.HTTPStatusError) -> float | None:
    """Retry-After as seconds, if the server sent a sane one. Date form is ignored."""
    raw = exc.response.headers.get("retry-after") if exc.response is not None else None
    if not raw:
        return None
    try:
        secs = float(raw)
    except (TypeError, ValueError):
        return None
    if secs < 0:
        return None
    return min(secs, _RETRY_AFTER_CAP)


def _transient_delay(exc: Exception, attempt: int) -> float | None:
    """Seconds to wait before retrying `exc`, or None if it isn't worth retrying."""
    if isinstance(exc, httpx.HTTPStatusError):
        if exc.response is None or exc.response.status_code not in _RETRY_STATUSES:
            return None
        after = _retry_after_seconds(exc)
        if after is not None:
            return after
    elif not isinstance(exc, (httpx.TimeoutException, httpx.TransportError)):
        return None
    # Exponential backoff with jitter, so several callers retrying at once don't
    # march in lockstep into the same rate limit. The cap is applied after jitter,
    # otherwise the 1.5x upper end of the jitter would push past it.
    delay = min(_BACKOFF_BASE * (2 ** attempt), _BACKOFF_CAP) * (0.5 + random.random())
    return min(delay, _BACKOFF_CAP)


def _with_retries(call: Callable[[], T], *, what: str) -> T:
    """Run `call`, retrying transient HTTP failures with backoff."""
    for attempt in range(MAX_ATTEMPTS):
        try:
            return call()
        except Exception as e:
            delay = _transient_delay(e, attempt)
            if delay is None or attempt == MAX_ATTEMPTS - 1:
                raise
            logger.warning("%s: %s — retrying in %.1fs (attempt %d/%d)",
                           what, type(e).__name__, delay, attempt + 2, MAX_ATTEMPTS)
            time.sleep(delay)
    raise AssertionError("unreachable")


def _stream_with_retries(make_stream: Callable[[], Iterator[str]], *, what: str) -> Iterator[str]:
    """Same, for streams — but only until the first token.

    Once a delta has reached the caller it's already on the user's screen, so a
    retry would duplicate text. After that point failures propagate.
    """
    for attempt in range(MAX_ATTEMPTS):
        produced = False
        try:
            for chunk in make_stream():
                produced = True
                yield chunk
            return
        except Exception as e:
            delay = _transient_delay(e, attempt)
            if produced or delay is None or attempt == MAX_ATTEMPTS - 1:
                raise
            logger.warning("%s: %s before first token — retrying in %.1fs (attempt %d/%d)",
                           what, type(e).__name__, delay, attempt + 2, MAX_ATTEMPTS)
            time.sleep(delay)


# ---- Provider resolution ---------------------------------------------------

async def resolve_provider() -> tuple[str | None, dict]:
    """Return (provider, cfg). cfg is provider-specific; provider None when unset."""
    from app.core import ai  # late: tests monkeypatch ai.setting

    chosen = (await ai.setting("ai.provider")).strip().lower() or "auto"
    timeout = float(await ai.setting("ai.timeout_seconds") or 120)

    anth_key = await ai.setting("ai.anthropic_key")
    open_key = await ai.setting("ai.openai_key")
    ollama_url = await ai.setting("ai.ollama_url")

    if chosen == "anthropic" or (chosen == "auto" and anth_key):
        if not anth_key:
            return None, {}
        return "anthropic", {
            "api_key": anth_key,
            "model": await ai.setting("ai.anthropic_model"),
            "timeout": timeout,
        }
    if chosen == "openai" or (chosen == "auto" and open_key):
        if not open_key:
            return None, {}
        return "openai", {
            "api_key": open_key,
            "model": await ai.setting("ai.openai_model"),
            "base_url": (await ai.setting("ai.openai_base_url")).rstrip("/"),
            "timeout": timeout,
        }
    if chosen == "ollama" or (chosen == "auto" and ollama_url):
        if not ollama_url:
            return None, {}
        return "ollama", {
            "url": ollama_url.rstrip("/"),
            "model": await ai.setting("ai.ollama_model"),
            "timeout": timeout,
            "keep_alive": (await ai.setting("ai.ollama_keep_alive")) or "30m",
        }
    return None, {}


async def ai_enabled() -> bool:
    provider, _ = await resolve_provider()
    return provider is not None


# ---- Per-provider HTTP -----------------------------------------------------

def _openai_chat(messages: list, cfg: dict, *, json_mode: bool = False, max_tokens: int = 512) -> dict:
    body: dict = {
        "model": cfg["model"], "messages": messages,
        "max_tokens": max_tokens, "temperature": 0.3,
    }
    if json_mode:
        body["response_format"] = {"type": "json_object"}

    def _call() -> dict:
        with httpx.Client(timeout=cfg["timeout"]) as c:
            r = c.post(f"{cfg['base_url']}/chat/completions",
                       headers={"Authorization": f"Bearer {cfg['api_key']}"}, json=body)
            r.raise_for_status()
            return r.json()

    return _with_retries(_call, what="openai chat")


def _ollama_chat(cfg: dict, messages: list, *, fmt: str | None = None,
                 temperature: float = 0.3, num_predict: int = 512) -> dict:
    # keep_alive: keep model resident; a cold reload is ~15s on big quantised models.
    payload: dict = {
        "model": cfg["model"], "stream": False,
        "keep_alive": cfg.get("keep_alive", "30m"),
        "messages": messages,
        "options": {"temperature": temperature, "num_predict": num_predict},
    }
    if fmt:
        payload["format"] = fmt

    def _call() -> dict:
        with httpx.Client(timeout=cfg["timeout"]) as c:
            r = c.post(f"{cfg['url']}/api/chat", json=payload)
            r.raise_for_status()
            return r.json()

    return _with_retries(_call, what="ollama chat")


# ---- Streaming (token-by-token) --------------------------------------------
# Sync generators (httpx sync client). The async chat layer bridges them to the
# event loop via a worker thread + queue.

def _ollama_chat_stream(cfg: dict, messages: list, *, num_predict: int = 1200) -> Iterator[str]:
    payload = {
        "model": cfg["model"], "stream": True,
        "keep_alive": cfg.get("keep_alive", "30m"), "messages": messages,
        "options": {"temperature": 0.3, "num_predict": num_predict},
    }
    def _open() -> Iterator[str]:
        with httpx.Client(timeout=cfg["timeout"]) as c:
            with c.stream("POST", f"{cfg['url']}/api/chat", json=payload) as r:
                r.raise_for_status()
                for line in r.iter_lines():
                    if not line:
                        continue
                    obj = json.loads(line)
                    delta = (obj.get("message") or {}).get("content", "")
                    if delta:
                        yield delta
                    if obj.get("done"):
                        break

    yield from _stream_with_retries(_open, what="ollama stream")


def _openai_chat_stream(cfg: dict, messages: list, *, max_tokens: int = 1200) -> Iterator[str]:
    body = {"model": cfg["model"], "messages": messages, "max_tokens": max_tokens,
            "temperature": 0.3, "stream": True}
    def _open() -> Iterator[str]:
        with httpx.Client(timeout=cfg["timeout"]) as c:
            with c.stream("POST", f"{cfg['base_url']}/chat/completions",
                          headers={"Authorization": f"Bearer {cfg['api_key']}"}, json=body) as r:
                r.raise_for_status()
                for line in r.iter_lines():
                    if not line or not line.startswith("data:"):
                        continue
                    data = line[len("data:"):].strip()
                    if data == "[DONE]":
                        break
                    delta = (json.loads(data)["choices"][0].get("delta") or {}).get("content")
                    if delta:
                        yield delta

    yield from _stream_with_retries(_open, what="openai stream")


def _anthropic_chat_stream(cfg: dict, system: str, messages: list, *, max_tokens: int = 1200) -> Iterator[str]:
    client = anthropic.Anthropic(api_key=cfg["api_key"], timeout=cfg["timeout"],
                                 max_retries=MAX_ATTEMPTS - 1)
    with client.messages.stream(model=cfg["model"], max_tokens=max_tokens,
                                system=system, messages=messages) as stream:
        for text in stream.text_stream:
            yield text


def _provider_chat_stream(provider: str, system: str, messages: list[dict], cfg: dict,
                          *, max_tokens: int = 1200) -> Iterator[str]:
    """Yield reply text deltas from the active provider as they arrive."""
    if provider == "anthropic":
        yield from _anthropic_chat_stream(cfg, system, messages, max_tokens=max_tokens)
    elif provider == "openai":
        yield from _openai_chat_stream(cfg, [{"role": "system", "content": system}, *messages],
                                       max_tokens=max_tokens)
    elif provider == "ollama":
        yield from _ollama_chat_stream(cfg, [{"role": "system", "content": system}, *messages],
                                       num_predict=max_tokens)
    else:
        raise RuntimeError(f"unknown provider: {provider}")


# ---- Uniform dispatchers ---------------------------------------------------

def _provider_chat(provider: str, system: str, messages: list[dict], cfg: dict,
                   *, max_tokens: int = 1200) -> str:
    if provider == "anthropic":
        client = anthropic.Anthropic(api_key=cfg["api_key"], timeout=cfg["timeout"],
                                 max_retries=MAX_ATTEMPTS - 1)
        resp = client.messages.create(model=cfg["model"], max_tokens=max_tokens,
                                      system=system, messages=messages)  # type: ignore[arg-type]
        return next((b.text for b in resp.content if b.type == "text"), "")
    if provider == "openai":
        data = _openai_chat([{"role": "system", "content": system}, *messages], cfg, max_tokens=max_tokens)
        return data["choices"][0]["message"]["content"]
    if provider == "ollama":
        data = _ollama_chat(cfg, [{"role": "system", "content": system}, *messages],
                            temperature=0.3, num_predict=max_tokens)
        return (data.get("message") or {}).get("content", "")
    raise RuntimeError(f"unknown provider: {provider}")


def _provider_text(provider: str, system: str, user: str, cfg: dict, *, max_tokens: int = 600) -> str:
    if provider == "anthropic":
        client = anthropic.Anthropic(api_key=cfg["api_key"], timeout=cfg["timeout"],
                                 max_retries=MAX_ATTEMPTS - 1)
        resp = client.messages.create(model=cfg["model"], max_tokens=max_tokens, system=system,
                                      messages=[{"role": "user", "content": user}])
        return next((b.text for b in resp.content if b.type == "text"), "")
    if provider == "openai":
        data = _openai_chat([{"role": "system", "content": system},
                             {"role": "user", "content": user}], cfg, max_tokens=max_tokens)
        return data["choices"][0]["message"]["content"]
    if provider == "ollama":
        data = _ollama_chat(cfg, [{"role": "system", "content": system}, {"role": "user", "content": user}],
                            temperature=0.3, num_predict=max_tokens)
        return (data.get("message") or {}).get("content", "")
    raise RuntimeError(f"unknown provider: {provider}")


def _provider_json(provider: str, system: str, user: str, cfg: dict, *, max_tokens: int = 2000) -> dict:
    from .generate import _parse_spec

    if provider == "anthropic":
        client = anthropic.Anthropic(api_key=cfg["api_key"], timeout=cfg["timeout"],
                                 max_retries=MAX_ATTEMPTS - 1)
        resp = client.messages.create(model=cfg["model"], max_tokens=max_tokens, system=system,
                                      messages=[{"role": "user", "content": user}])
        return _parse_spec(next((b.text for b in resp.content if b.type == "text"), ""))
    if provider == "openai":
        data = _openai_chat([{"role": "system", "content": system},
                             {"role": "user", "content": user}], cfg, json_mode=True, max_tokens=max_tokens)
        return _parse_spec(data["choices"][0]["message"]["content"])
    if provider == "ollama":
        data = _ollama_chat(cfg, [{"role": "system", "content": system}, {"role": "user", "content": user}],
                            fmt="json", temperature=0.2, num_predict=max_tokens)
        return _parse_spec((data.get("message") or {}).get("content", ""))
    raise RuntimeError(f"unknown provider: {provider}")


# ---- Probing (Settings page) -----------------------------------------------

def probe_anthropic(api_key: str, *, timeout: float = 30.0) -> list[dict]:
    client = anthropic.Anthropic(api_key=api_key, timeout=timeout,
                                 max_retries=MAX_ATTEMPTS - 1)
    return [{"id": m.id, "display_name": getattr(m, "display_name", m.id)}
            for m in client.models.list()]


def probe_openai(api_key: str, base_url: str, *, timeout: float = 30.0) -> list[dict]:
    base = base_url.rstrip("/")
    with httpx.Client(timeout=timeout) as c:
        r = c.get(f"{base}/models", headers={"Authorization": f"Bearer {api_key}"})
        r.raise_for_status()
        data = r.json()
    models = data.get("data") or []
    return [{"id": m["id"], "display_name": m["id"]} for m in models if "id" in m]


def probe_ollama(url: str, *, timeout: float = 30.0) -> list[dict]:
    base = url.rstrip("/")
    with httpx.Client(timeout=timeout) as c:
        r = c.get(f"{base}/api/tags")
        r.raise_for_status()
        data = r.json()
    return [{"id": m["name"], "display_name": m["name"]} for m in (data.get("models") or [])]
