"""Unit tests for AI provider resolution and dispatch.

No network and no API keys: we swap `ai.setting` (the settings-store accessor)
for a dict-backed async stub, and replace the per-provider backend callables with
fakes. This exercises the orchestration in `explain_failure` and the provider
selection in `resolve_provider` deterministically.
"""
from __future__ import annotations

import pytest

from app.core import ai


def _fake_setting(values: dict):
    async def _get(key: str) -> str:
        return str(values.get(key, ""))
    return _get


# --- resolve_provider -------------------------------------------------------

async def test_auto_prefers_anthropic_when_key_present(monkeypatch):
    monkeypatch.setattr(ai, "setting", _fake_setting({
        "ai.provider": "auto",
        "ai.anthropic_key": "sk-ant-x",
        "ai.anthropic_model": "claude-opus-4-7",
        "ai.timeout_seconds": "90",
    }))
    provider, cfg = await ai.resolve_provider()
    assert provider == "anthropic"
    assert cfg["api_key"] == "sk-ant-x"
    assert cfg["model"] == "claude-opus-4-7"
    assert cfg["timeout"] == 90.0


async def test_auto_falls_back_to_openai(monkeypatch):
    monkeypatch.setattr(ai, "setting", _fake_setting({
        "ai.provider": "auto",
        "ai.openai_key": "sk-oai",
        "ai.openai_model": "gpt-4o-mini",
        "ai.openai_base_url": "https://api.openai.com/v1/",
    }))
    provider, cfg = await ai.resolve_provider()
    assert provider == "openai"
    assert cfg["base_url"] == "https://api.openai.com/v1"  # trailing slash stripped


async def test_auto_falls_back_to_ollama(monkeypatch):
    monkeypatch.setattr(ai, "setting", _fake_setting({
        "ai.provider": "auto",
        "ai.ollama_url": "http://ollama:11434/",
        "ai.ollama_model": "llama3.1",
    }))
    provider, cfg = await ai.resolve_provider()
    assert provider == "ollama"
    assert cfg["url"] == "http://ollama:11434"


async def test_explicit_provider_without_credentials_is_none(monkeypatch):
    monkeypatch.setattr(ai, "setting", _fake_setting({"ai.provider": "openai"}))
    provider, cfg = await ai.resolve_provider()
    assert provider is None
    assert cfg == {}


async def test_nothing_configured_is_none(monkeypatch):
    monkeypatch.setattr(ai, "setting", _fake_setting({"ai.provider": "auto"}))
    provider, _ = await ai.resolve_provider()
    assert provider is None


async def test_ai_enabled_reflects_resolution(monkeypatch):
    monkeypatch.setattr(ai, "setting", _fake_setting({"ai.provider": "auto"}))
    assert await ai.ai_enabled() is False


# --- explain_failure orchestration ------------------------------------------

async def test_explain_failure_dispatches_and_attaches_validation(monkeypatch):
    monkeypatch.setattr(ai, "setting", _fake_setting({
        "ai.provider": "openai",
        "ai.openai_key": "sk-oai",
        "ai.openai_model": "gpt-4o-mini",
        "ai.openai_base_url": "https://api.openai.com/v1",
        "ai.validate_responses": "0",  # skip the second LLM pass
    }))

    def fake_explain(failure, playbook, cfg):
        assert cfg["api_key"] == "sk-oai"
        return {"provider": "openai", "model": "gpt-4o-mini",
                "explanation": "The host refused the SSH connection; check the port."}

    monkeypatch.setitem(ai._EXPLAIN_BACKENDS, "openai", fake_explain)

    out = await ai.explain_failure({"host": "web1", "task": "ping"}, playbook="site.yml")
    assert out["provider"] == "openai"
    assert "validation" in out
    assert out["validation"]["confidence"] == "high"
    assert "self_critique" not in out["validation"]  # disabled


async def test_explain_failure_self_critique_downgrades_confidence(monkeypatch):
    monkeypatch.setattr(ai, "setting", _fake_setting({
        "ai.provider": "openai",
        "ai.openai_key": "sk-oai",
        "ai.openai_model": "gpt-4o-mini",
        "ai.openai_base_url": "https://api.openai.com/v1",
        "ai.validate_responses": "1",
    }))
    monkeypatch.setitem(ai._EXPLAIN_BACKENDS, "openai",
                        lambda f, p, c: {"provider": "openai", "model": "m",
                                         "explanation": "Plain text, no modules."})
    monkeypatch.setitem(ai._CRITIQUE_BACKENDS, "openai",
                        lambda f, e, c: {"unsupported_claims": ["claim a", "claim b"]})

    out = await ai.explain_failure({"host": "web1"})
    assert out["validation"]["self_critique"]["unsupported_claims"] == ["claim a", "claim b"]
    assert out["validation"]["confidence"] == "low"


async def test_explain_failure_raises_when_unconfigured(monkeypatch):
    monkeypatch.setattr(ai, "setting", _fake_setting({"ai.provider": "auto"}))
    with pytest.raises(RuntimeError, match="no AI provider"):
        await ai.explain_failure({"host": "x"})


# --- _parse_critique --------------------------------------------------------

def test_parse_critique_plain_json():
    out = ai._parse_critique('{"unsupported_claims": ["a", "b"]}')
    assert out["unsupported_claims"] == ["a", "b"]


def test_parse_critique_fenced_json():
    out = ai._parse_critique('```json\n{"unsupported_claims": ["x"]}\n```')
    assert out["unsupported_claims"] == ["x"]


def test_parse_critique_invalid_marks_error():
    out = ai._parse_critique("not json at all")
    assert out["unsupported_claims"] == []
    assert out["_parse_error"] is True


def test_parse_critique_non_dict_marks_error():
    out = ai._parse_critique("[1, 2, 3]")
    assert out["_parse_error"] is True


# --- per-provider self-critique default -------------------------------------

def test_should_self_critique_tri_state():
    from app.core.ai import _should_self_critique
    # forced on / off regardless of provider
    assert _should_self_critique("ollama", "1") is True
    assert _should_self_critique("anthropic", "0") is False
    # auto (default/empty/unknown): on for cloud, off for local
    for v in ("auto", "", "anything"):
        assert _should_self_critique("anthropic", v) is True
        assert _should_self_critique("openai", v) is True
        assert _should_self_critique("ollama", v) is False
