"""Integration tests for the runtime settings store (DB + Fernet encryption).

Uses a real aiosqlite DB under the temp data dir set up in conftest. Needs
`cryptography` and `aiosqlite`; both ship in the image.
"""
from __future__ import annotations

import os

import pytest

pytest.importorskip("cryptography")
pytest.importorskip("aiosqlite")

from app.core import settings_store
from app.models.db import AppSetting, SessionLocal, init_db


@pytest.fixture(autouse=True)
async def _db():
    await init_db()
    yield


async def test_plain_setting_roundtrip():
    await settings_store.set("ai.openai_base_url", "https://proxy.local/v1")
    assert await settings_store.get("ai.openai_base_url") == "https://proxy.local/v1"


async def test_default_returned_when_unset():
    # ai.openai_model has a spec default of gpt-4o-mini and no env override here.
    os.environ.pop("OPENAI_API_KEY", None)
    assert await settings_store.get("ai.openai_model") == "gpt-4o-mini"


async def test_secret_is_encrypted_at_rest():
    await settings_store.set("ai.openai_key", "sk-super-secret")
    # Plaintext round-trips through the API...
    assert await settings_store.get("ai.openai_key") == "sk-super-secret"
    # ...but the stored row is a Fernet token, not the plaintext.
    async with SessionLocal() as session:
        row = await session.get(AppSetting, "ai.openai_key")
    assert row.encrypted is True
    assert row.value != "sk-super-secret"
    assert "secret" not in row.value
    assert row.value.startswith("gAAAAA")  # Fernet token prefix


async def test_env_var_fallback(monkeypatch):
    # No DB row for anthropic key, but the env var is set -> env wins over default.
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-from-env")
    async with SessionLocal() as session:
        existing = await session.get(AppSetting, "ai.anthropic_key")
        if existing is not None:
            await session.delete(existing)
            await session.commit()
    assert await settings_store.get("ai.anthropic_key") == "sk-from-env"


async def test_public_dump_redacts_secrets():
    await settings_store.set("ai.openai_key", "sk-redact-me")
    dump = await settings_store.public_dump()
    # Sensitive keys collapse to a boolean; non-sensitive keep their value.
    assert dump["ai.openai_key"] is True
    assert isinstance(dump["ai.provider"], str)
