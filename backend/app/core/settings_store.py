"""Runtime-editable key/value settings store.

`get(key)` returns the DB value if present, otherwise the configured env-var
fallback, otherwise the default. `set(key, value, encrypted=True)` upserts; if
`encrypted=True` the value is Fernet-sealed in the DB.

Used today for AI provider config (`ai.provider`, `ai.anthropic_key`, ...) so
the Settings page can change behavior without container restarts. Env vars stay
as the "factory default" for fresh installs.
"""
from __future__ import annotations

import os
from dataclasses import dataclass

from cryptography.fernet import InvalidToken
from sqlalchemy import select

from app.core.credentials import get_fernet
from app.models.db import AppSetting, SessionLocal


@dataclass
class SettingSpec:
    """Describes one setting: its env-var fallback (if any), default, sensitivity."""
    env_var: str | None = None
    default: str = ""
    encrypted: bool = False


# Single source of truth for known settings.
SPECS: dict[str, SettingSpec] = {
    "ai.provider":         SettingSpec(default="auto"),
    "ai.anthropic_key":    SettingSpec(env_var="ANTHROPIC_API_KEY", encrypted=True),
    "ai.anthropic_model":  SettingSpec(env_var="ANSIBLE_GUI_AI_MODEL", default="claude-opus-4-7"),
    "ai.openai_key":       SettingSpec(env_var="OPENAI_API_KEY", encrypted=True),
    "ai.openai_model":     SettingSpec(default="gpt-4o-mini"),
    "ai.openai_base_url":  SettingSpec(default="https://api.openai.com/v1"),
    "ai.ollama_url":       SettingSpec(env_var="OLLAMA_URL", default=""),
    "ai.ollama_model":     SettingSpec(env_var="OLLAMA_MODEL", default="llama3.1"),
    # Keep the model resident in Ollama's RAM between requests. Without this Ollama
    # unloads after each call and the next one pays a full cold load (~15-20s for a
    # big model), which feels like "the assistant is thinking forever".
    "ai.ollama_keep_alive": SettingSpec(env_var="OLLAMA_KEEP_ALIVE", default="30m"),
    "ai.timeout_seconds":  SettingSpec(default="300"),  # local models can be slow; give them room
    # Auto by default: self-critique (a 2nd model call) runs only for strong cloud
    # providers (Anthropic/OpenAI). It doubles latency and is unreliable on small
    # local models, so those stay off unless the user forces "1".
    "ai.validate_responses": SettingSpec(default="auto"),  # "1"=on / "0"=off / "auto"=cloud only
    # Off by default (air-gap friendly): when a module isn't installed locally,
    # look up its parameters on docs.ansible.com. Only enable if the box has internet.
    "ai.web_docs": SettingSpec(env_var="ANSIBLE_GUI_WEB_DOCS", default="0"),  # "1"/"0"
}


def _spec(key: str) -> SettingSpec:
    return SPECS.get(key) or SettingSpec()


async def get(key: str) -> str:
    """Return the effective setting value: DB → env → default."""
    spec = _spec(key)
    async with SessionLocal() as session:
        row = await session.get(AppSetting, key)
    if row is not None and row.value:
        if row.encrypted:
            try:
                return get_fernet().decrypt(row.value.encode()).decode()
            except InvalidToken:
                return ""
        return row.value
    if spec.env_var:
        env_val = os.getenv(spec.env_var) or ""
        if env_val:
            return env_val
    return spec.default


async def set(key: str, value: str) -> None:
    """Upsert a setting. Encryption is decided by the spec, not the caller."""
    spec = _spec(key)
    stored = value or ""
    if spec.encrypted and stored:
        stored = get_fernet().encrypt(stored.encode()).decode()
    async with SessionLocal() as session:
        row = await session.get(AppSetting, key)
        if row is None:
            row = AppSetting(key=key, value=stored, encrypted=spec.encrypted)
            session.add(row)
        else:
            row.value = stored
            row.encrypted = spec.encrypted
        await session.commit()


async def get_all() -> dict[str, str]:
    return {k: await get(k) for k in SPECS}


def is_sensitive(key: str) -> bool:
    return _spec(key).encrypted


async def public_dump() -> dict:
    """Same as `get_all` but redacts sensitive values to a boolean flag.
    For surfacing config to the UI without echoing keys back."""
    out: dict = {}
    for k in SPECS:
        v = await get(k)
        if is_sensitive(k):
            out[k] = bool(v)
        else:
            out[k] = v
    return out
