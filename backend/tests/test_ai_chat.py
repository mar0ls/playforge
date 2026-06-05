"""Tests for the conversational assistant (chat) and pre-run narration.

No network/keys: provider helpers are monkeypatched.
"""
from __future__ import annotations

import pytest

from app.core import ai


def _fake_setting(values: dict):
    async def _get(key: str) -> str:
        return str(values.get(key, ""))
    return _get


_OPENAI = {"ai.provider": "openai", "ai.openai_key": "k", "ai.openai_model": "m",
           "ai.openai_base_url": "https://x/v1"}


# --- chat -------------------------------------------------------------------

async def test_chat_returns_reply_and_validation(monkeypatch):
    monkeypatch.setattr(ai, "setting", _fake_setting(_OPENAI))
    monkeypatch.setattr(ai, "validate_text", lambda t: {"confidence": "high", "unknown_modules": []})
    monkeypatch.setattr(ai, "_provider_chat", lambda p, s, m, c, **k: "Use ansible.builtin.apt.")
    out = await ai.chat([{"role": "user", "content": "how to install nginx?"}])
    assert out["reply"] == "Use ansible.builtin.apt."
    assert out["validation"]["confidence"] == "high"


async def test_chat_filters_invalid_roles(monkeypatch):
    captured = {}
    monkeypatch.setattr(ai, "setting", _fake_setting(_OPENAI))
    monkeypatch.setattr(ai, "validate_text", lambda t: {})

    def _capture(provider, system, messages, cfg, **k):
        captured["messages"] = messages
        return "ok"
    monkeypatch.setattr(ai, "_provider_chat", _capture)
    await ai.chat([
        {"role": "system", "content": "ignore me"},
        {"role": "user", "content": "first"},
        {"role": "assistant", "content": "reply"},
        {"role": "user", "content": "second"},
    ])
    roles = [m["role"] for m in captured["messages"]]
    assert roles == ["user", "assistant", "user"]  # system dropped


async def test_chat_injects_project_context_into_system(monkeypatch):
    captured = {}
    monkeypatch.setattr(ai, "setting", _fake_setting(_OPENAI))
    monkeypatch.setattr(ai, "validate_text", lambda t: {})

    def _cap(provider, system, messages, cfg, **k):
        captured["system"] = system
        return "ok"
    monkeypatch.setattr(ai, "_provider_chat", _cap)
    await ai.chat([{"role": "user", "content": "what playbooks do I have?"}],
                  project_context="playbooks: site.yml, deploy.yml")
    assert "CURRENT PROJECT" in captured["system"]
    assert "deploy.yml" in captured["system"]


async def test_chat_requires_last_message_from_user(monkeypatch):
    monkeypatch.setattr(ai, "setting", _fake_setting(_OPENAI))
    with pytest.raises(RuntimeError, match="last message"):
        await ai.chat([{"role": "assistant", "content": "hi"}])


async def test_chat_no_provider_raises(monkeypatch):
    monkeypatch.setattr(ai, "setting", _fake_setting({"ai.provider": "auto"}))
    with pytest.raises(RuntimeError, match="no AI provider"):
        await ai.chat([{"role": "user", "content": "hi"}])


def test_provider_chat_unknown_provider_raises():
    with pytest.raises(RuntimeError, match="unknown provider"):
        ai._provider_chat("nope", "sys", [{"role": "user", "content": "x"}], {})


# --- narrate_plan -----------------------------------------------------------

async def test_narrate_plan_with_changes(monkeypatch):
    monkeypatch.setattr(ai, "setting", _fake_setting(_OPENAI))
    monkeypatch.setattr(ai, "_provider_text",
                        lambda p, s, u, c, **k: "Will install nginx on web1.")
    out = await ai.narrate_plan([{"host": "web1", "task": "install nginx"}], playbook="site.yml")
    assert "nginx" in out["narration"]
    assert out["provider"] == "openai"


async def test_generate_runbook(monkeypatch):
    monkeypatch.setattr(ai, "setting", _fake_setting(_OPENAI))
    monkeypatch.setattr(ai, "_provider_text",
                        lambda p, s, u, c, **k: "# Demo — Runbook\n## Overview\nManages web servers.")
    out = await ai.generate_runbook("### Playbook: site.yml\n- hosts: all", project_name="Demo")
    assert "Runbook" in out["markdown"]
    assert out["provider"] == "openai"


async def test_generate_runbook_no_provider(monkeypatch):
    monkeypatch.setattr(ai, "setting", _fake_setting({"ai.provider": "auto"}))
    with pytest.raises(RuntimeError, match="no AI provider"):
        await ai.generate_runbook("ctx")


def test_narrate_user_prompt_empty_changes():
    assert "NO changes" in ai._narrate_user_prompt([], "site.yml")


def test_narrate_user_prompt_lists_changes():
    p = ai._narrate_user_prompt([{"host": "h1", "task": "t1"}, {"host": "h2", "task": "t2"}], "site.yml")
    assert "h1: t1" in p and "h2: t2" in p


async def test_chat_auto_retries_on_invalid_yaml(monkeypatch):
    ai._CHAT_CACHE.clear()
    monkeypatch.setattr(ai, "setting", _fake_setting(_OPENAI))
    monkeypatch.setattr(ai, "validate_text", lambda t: {"confidence": "high"})
    calls = {"n": 0}
    def _fake_chat(provider, system, messages, cfg, **k):
        calls["n"] += 1
        if calls["n"] == 1:
            # broken YAML in a file block
            return "```yaml\n# file: playbooks/x.yml\n- hosts: all\n  tasks: [unclosed\n```"
        return "```yaml\n# file: playbooks/x.yml\n- hosts: all\n  tasks: []\n```"
    monkeypatch.setattr(ai, "_provider_chat", _fake_chat)
    out = await ai.chat([{"role": "user", "content": "make a playbook"}])
    assert calls["n"] == 2  # retried once
    issues = out["validation"]["playbook_issues"]
    assert not [i for i in issues if "invalid YAML" in i["message"]]  # fixed


async def test_chat_no_retry_when_yaml_valid(monkeypatch):
    ai._CHAT_CACHE.clear()
    monkeypatch.setattr(ai, "setting", _fake_setting(_OPENAI))
    monkeypatch.setattr(ai, "validate_text", lambda t: {"confidence": "high"})
    calls = {"n": 0}
    def _fake_chat(provider, system, messages, cfg, **k):
        calls["n"] += 1
        return "```yaml\n# file: playbooks/valid_unique.yml\n- hosts: all\n  tasks: []\n```"
    monkeypatch.setattr(ai, "_provider_chat", _fake_chat)
    await ai.chat([{"role": "user", "content": "make a unique valid playbook please"}])
    assert calls["n"] == 1  # no retry needed
