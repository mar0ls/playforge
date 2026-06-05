"""Probe should fall back to the saved key/url when the client doesn't send one."""
from __future__ import annotations

import pytest

pytest.importorskip("aiosqlite")
pytest.importorskip("cryptography")

from app.api import ai as ai_api
from app.api.ai import ProbeIn, probe
from app.core import ai as ai_core
from app.core import settings_store
from app.models.db import init_db


@pytest.fixture(autouse=True)
async def _db():
    await init_db()
    yield


async def test_probe_uses_saved_ollama_url(monkeypatch):
    await settings_store.set("ai.ollama_url", "http://saved-ollama:11434")
    captured = {}
    monkeypatch.setattr(ai_core, "probe_ollama",
                        lambda url, timeout=30: (captured.__setitem__("url", url), [{"id": "m1"}])[1])
    # client sends no url → must use the saved one
    out = await probe(ProbeIn(provider="ollama"))
    assert captured["url"] == "http://saved-ollama:11434"
    assert out["models"] == [{"id": "m1"}]


async def test_probe_prefers_client_url(monkeypatch):
    await settings_store.set("ai.ollama_url", "http://saved:11434")
    captured = {}
    monkeypatch.setattr(ai_core, "probe_ollama",
                        lambda url, timeout=30: (captured.__setitem__("url", url), [])[1])
    await probe(ProbeIn(provider="ollama", url="http://typed:11434"))
    assert captured["url"] == "http://typed:11434"  # typed wins over saved


async def test_probe_uses_saved_openai_key(monkeypatch):
    await settings_store.set("ai.openai_key", "sk-saved-123")
    captured = {}
    monkeypatch.setattr(ai_core, "probe_openai",
                        lambda key, base, timeout=30: (captured.__setitem__("key", key), [{"id": "gpt"}])[1])
    out = await probe(ProbeIn(provider="openai"))
    assert captured["key"] == "sk-saved-123"
    assert out["models"]


async def test_probe_errors_when_nothing_saved(monkeypatch):
    from fastapi import HTTPException
    await settings_store.set("ai.openai_key", "")
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    with pytest.raises(HTTPException) as exc:
        await probe(ProbeIn(provider="openai"))
    assert exc.value.status_code == 400
