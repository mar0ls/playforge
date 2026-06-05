"""Tests for natural-language → playbook generation.

No network/keys: the per-provider backend is swapped for a fake that returns a
parsed spec, so we exercise the transform + build + validation pipeline.
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


# --- _parse_spec ------------------------------------------------------------

def test_parse_spec_strips_fences():
    assert ai._parse_spec('```json\n{"name":"x","tasks":[]}\n```')["name"] == "x"


def test_parse_spec_invalid_raises():
    with pytest.raises(RuntimeError, match="valid JSON"):
        ai._parse_spec("not json at all")


def test_parse_spec_non_object_raises():
    with pytest.raises(RuntimeError, match="not an object"):
        ai._parse_spec("[1, 2, 3]")


# --- _to_builder_spec -------------------------------------------------------

def test_to_builder_spec_converts_args_to_yaml():
    parsed = {
        "name": "Install nginx", "hosts": "web", "become": True, "gather_facts": False,
        "tasks": [{"name": "install", "module": "ansible.builtin.apt",
                   "args": {"name": "nginx", "state": "present"}, "tags": ["web"]}],
    }
    spec = ai._to_builder_spec(parsed, "")
    assert spec["hosts"] == "web"
    assert spec["become"] is True
    assert spec["gather_facts"] is False
    task = spec["tasks"][0]
    assert task["module"] == "ansible.builtin.apt"
    assert "name: nginx" in task["args_yaml"]
    assert "state: present" in task["args_yaml"]
    assert task["tags"] == ["web"]


def test_to_builder_spec_defaults_and_gather_facts_true_omitted():
    spec = ai._to_builder_spec({"tasks": [], "gather_facts": True}, "all")
    assert spec["name"] == "Generated play"
    assert spec["hosts"] == "all"
    assert spec["gather_facts"] is None  # only an explicit False is kept


# --- generate_playbook orchestration ---------------------------------------

async def test_generate_builds_and_validates(monkeypatch):
    monkeypatch.setattr(ai, "setting", _fake_setting(_OPENAI))
    monkeypatch.setattr(ai, "validate_text", lambda t: {"confidence": "high", "unknown_modules": []})
    monkeypatch.setitem(ai._GENERATE_BACKENDS, "openai", lambda d, h, c: {
        "name": "Ping", "hosts": "all", "become": False,
        "tasks": [{"name": "ping", "module": "ansible.builtin.ping", "args": {}}]})
    out = await ai.generate_playbook("ping all hosts", hosts="all")
    assert "ansible.builtin.ping" in out["yaml"]
    assert out["spec"]["tasks"][0]["module"] == "ansible.builtin.ping"
    assert out["validation"]["confidence"] == "high"


async def test_generate_invalid_spec_raises_builder_error(monkeypatch):
    monkeypatch.setattr(ai, "setting", _fake_setting(_OPENAI))
    monkeypatch.setattr(ai, "validate_text", lambda t: {})
    # Task with no module → build_playbook rejects it.
    monkeypatch.setitem(ai._GENERATE_BACKENDS, "openai",
                        lambda d, h, c: {"name": "x", "tasks": [{"name": "t", "args": {}}]})
    with pytest.raises(ai.BuilderError):
        await ai.generate_playbook("do something")


async def test_generate_without_provider_raises(monkeypatch):
    monkeypatch.setattr(ai, "setting", _fake_setting({"ai.provider": "auto"}))
    with pytest.raises(RuntimeError, match="no AI provider"):
        await ai.generate_playbook("x")


async def test_generate_empty_description_raises():
    with pytest.raises(RuntimeError, match="description is required"):
        await ai.generate_playbook("   ")
