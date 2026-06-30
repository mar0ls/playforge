"""Provider dispatch: resolve current backend + uniform chat/text/json calls."""
from __future__ import annotations

import json
from typing import Iterator

import anthropic
import httpx


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
    with httpx.Client(timeout=cfg["timeout"]) as c:
        r = c.post(f"{cfg['base_url']}/chat/completions",
                   headers={"Authorization": f"Bearer {cfg['api_key']}"}, json=body)
        r.raise_for_status()
        return r.json()


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
    with httpx.Client(timeout=cfg["timeout"]) as c:
        r = c.post(f"{cfg['url']}/api/chat", json=payload)
        r.raise_for_status()
        return r.json()


# ---- Streaming (token-by-token) --------------------------------------------
# Sync generators (httpx sync client). The async chat layer bridges them to the
# event loop via a worker thread + queue.

def _ollama_chat_stream(cfg: dict, messages: list, *, num_predict: int = 1200) -> Iterator[str]:
    payload = {
        "model": cfg["model"], "stream": True,
        "keep_alive": cfg.get("keep_alive", "30m"), "messages": messages,
        "options": {"temperature": 0.3, "num_predict": num_predict},
    }
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


def _openai_chat_stream(cfg: dict, messages: list, *, max_tokens: int = 1200) -> Iterator[str]:
    body = {"model": cfg["model"], "messages": messages, "max_tokens": max_tokens,
            "temperature": 0.3, "stream": True}
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


def _anthropic_chat_stream(cfg: dict, system: str, messages: list, *, max_tokens: int = 1200) -> Iterator[str]:
    client = anthropic.Anthropic(api_key=cfg["api_key"], timeout=cfg["timeout"])
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
        client = anthropic.Anthropic(api_key=cfg["api_key"], timeout=cfg["timeout"])
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
        client = anthropic.Anthropic(api_key=cfg["api_key"], timeout=cfg["timeout"])
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
        client = anthropic.Anthropic(api_key=cfg["api_key"], timeout=cfg["timeout"])
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
    client = anthropic.Anthropic(api_key=api_key, timeout=timeout)
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
