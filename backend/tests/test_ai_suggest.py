"""Tests for AI remediation (suggest_fix) — no network/keys, provider mocked."""
from __future__ import annotations

import pytest

from app.core import ai


def _fake_setting(values: dict):
    async def _get(key: str) -> str:
        return str(values.get(key, ""))
    return _get


_OPENAI = {"ai.provider": "openai", "ai.openai_key": "k", "ai.openai_model": "m",
           "ai.openai_base_url": "https://x/v1"}


def test_provider_json_unknown_provider_raises():
    with pytest.raises(RuntimeError, match="unknown provider"):
        ai._provider_json("nope", "sys", "user", {})


async def test_suggest_fix_returns_structured_patch(monkeypatch):
    monkeypatch.setattr(ai, "setting", _fake_setting(_OPENAI))
    monkeypatch.setattr(ai, "validate_text", lambda t: {"confidence": "high", "unknown_modules": []})
    monkeypatch.setattr(ai, "_provider_json", lambda p, s, u, c, **k: {
        "root_cause": "task needs privilege escalation",
        "fix_summary": "add become: true to the play",
        "target_path": "playbooks/site.yml",
        "new_content": "- hosts: all\n  become: true\n  tasks: []\n",
        "manual_steps": [],
    })
    out = await ai.suggest_fix({"host": "h", "task": "t"},
                               playbook_path="playbooks/site.yml",
                               playbook_content="- hosts: all\n  tasks: []\n")
    assert out["root_cause"].startswith("task needs")
    assert out["target_path"] == "playbooks/site.yml"
    assert "become: true" in out["new_content"]
    assert out["validation"]["confidence"] == "high"


async def test_suggest_fix_environmental_has_no_file_patch(monkeypatch):
    monkeypatch.setattr(ai, "setting", _fake_setting(_OPENAI))
    monkeypatch.setattr(ai, "validate_text", lambda t: {})
    monkeypatch.setattr(ai, "_provider_json", lambda p, s, u, c, **k: {
        "root_cause": "ssh auth denied",
        "new_content": None,
        "manual_steps": ["add the deploy key to the host's authorized_keys"],
    })
    out = await ai.suggest_fix({"host": "h"}, playbook_path="p.yml")
    assert out["new_content"] is None
    assert out["manual_steps"] == ["add the deploy key to the host's authorized_keys"]
    assert out["target_path"] == "p.yml"  # falls back to the playbook path


async def test_suggest_fix_blank_new_content_becomes_none(monkeypatch):
    monkeypatch.setattr(ai, "setting", _fake_setting(_OPENAI))
    monkeypatch.setattr(ai, "validate_text", lambda t: {})
    monkeypatch.setattr(ai, "_provider_json", lambda p, s, u, c, **k: {"new_content": "   "})
    out = await ai.suggest_fix({"host": "h"})
    assert out["new_content"] is None


async def test_suggest_fix_no_provider_raises(monkeypatch):
    monkeypatch.setattr(ai, "setting", _fake_setting({"ai.provider": "auto"}))
    with pytest.raises(RuntimeError, match="no AI provider"):
        await ai.suggest_fix({"host": "h"})
