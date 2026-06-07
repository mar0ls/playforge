"""Failure explanation + optional self-critique pass."""
from __future__ import annotations

import asyncio
import json

import anthropic

from app.core.ai_validate import downgrade_confidence
from .providers import _openai_chat, _ollama_chat
# `validate_text` reached via `ai.validate_text` (late binding) so tests
# monkeypatching it at the package level propagate here.


EXPLAIN_SYSTEM = """You are an Ansible expert helping a DevOps engineer diagnose a failed task.
Given a failure record, write 2-4 short sentences explaining:
1. The most likely root cause
2. A concrete next step or fix

Be specific: reference the host, task name, module, and the key error fragment.
If the failure result reveals a misconfiguration (missing host key, wrong port, denied auth,
missing var, etc.), name it directly. If it's a network/SSH/DNS issue, say so.
Do not invent details that aren't in the record. Plain text — no markdown headers."""


CRITIQUE_SYSTEM = """You audit Ansible failure explanations for accuracy.
Given an ORIGINAL FAILURE RECORD and a PROPOSED EXPLANATION, list any claims in
the explanation that are NOT supported by the failure record. A claim is
"unsupported" if the failure record contains no evidence for it.

Be strict but specific. Examples of unsupported claims:
- saying "firewall is blocking" when the record only shows a DNS error
- naming a module/parameter that doesn't appear in the record
- assuming the host runs Debian when the OS is unknown

Reply ONLY as JSON: {"unsupported_claims": ["specific quoted claim 1", ...]}.
Empty list if every claim is grounded."""


def _format_user_prompt(failure: dict, playbook: str) -> str:
    result_json = json.dumps(failure.get("result") or {}, indent=2, default=str)[:3000]
    stderr_excerpt = (failure.get("stderr") or "")[:1500]
    return (
        f"Playbook: {playbook or '(unknown)'}\n"
        f"Host: {failure.get('host') or '(unknown)'}\n"
        f"Task: {failure.get('task') or '(unknown)'}\n\n"
        f"Result JSON:\n{result_json}\n\n"
        f"Stderr excerpt:\n{stderr_excerpt}"
    )


def _format_critique_prompt(failure: dict, explanation: str) -> str:
    return (
        "ORIGINAL FAILURE RECORD:\n"
        + json.dumps(failure, indent=2, default=str)[:3000]
        + "\n\nPROPOSED EXPLANATION:\n"
        + explanation
    )


# ---- Backends --------------------------------------------------------------

def _anthropic_explain(failure: dict, playbook: str, cfg: dict) -> dict:
    client = anthropic.Anthropic(api_key=cfg["api_key"], timeout=cfg["timeout"])
    resp = client.messages.create(
        model=cfg["model"], max_tokens=512,
        system=[{"type": "text", "text": EXPLAIN_SYSTEM, "cache_control": {"type": "ephemeral"}}],
        messages=[{"role": "user", "content": _format_user_prompt(failure, playbook)}],
    )
    text = next((b.text for b in resp.content if b.type == "text"), "")
    return {
        "provider": "anthropic", "model": resp.model, "explanation": text,
        "input_tokens": resp.usage.input_tokens, "output_tokens": resp.usage.output_tokens,
        "cache_read_input_tokens": getattr(resp.usage, "cache_read_input_tokens", 0),
    }


def _anthropic_critique(failure: dict, explanation: str, cfg: dict) -> dict:
    client = anthropic.Anthropic(api_key=cfg["api_key"], timeout=cfg["timeout"])
    resp = client.messages.create(
        model=cfg["model"], max_tokens=400, system=CRITIQUE_SYSTEM,
        messages=[{"role": "user", "content": _format_critique_prompt(failure, explanation)}],
        output_config={
            "format": {
                "type": "json_schema",
                "schema": {
                    "type": "object",
                    "properties": {"unsupported_claims": {"type": "array", "items": {"type": "string"}}},
                    "required": ["unsupported_claims"],
                    "additionalProperties": False,
                },
            }
        },
    )
    text = next((b.text for b in resp.content if b.type == "text"), "")
    return _parse_critique(text)


def _openai_explain(failure: dict, playbook: str, cfg: dict) -> dict:
    data = _openai_chat(
        [{"role": "system", "content": EXPLAIN_SYSTEM},
         {"role": "user", "content": _format_user_prompt(failure, playbook)}],
        cfg,
    )
    msg = data["choices"][0]["message"]["content"]
    usage = data.get("usage", {})
    return {
        "provider": "openai", "model": data.get("model", cfg["model"]),
        "explanation": msg.strip(),
        "input_tokens": usage.get("prompt_tokens", 0),
        "output_tokens": usage.get("completion_tokens", 0),
    }


def _openai_critique(failure: dict, explanation: str, cfg: dict) -> dict:
    data = _openai_chat(
        [{"role": "system", "content": CRITIQUE_SYSTEM},
         {"role": "user", "content": _format_critique_prompt(failure, explanation)}],
        cfg, json_mode=True, max_tokens=400,
    )
    return _parse_critique(data["choices"][0]["message"]["content"])


def _ollama_explain(failure: dict, playbook: str, cfg: dict) -> dict:
    data = _ollama_chat(cfg, [
        {"role": "system", "content": EXPLAIN_SYSTEM},
        {"role": "user", "content": _format_user_prompt(failure, playbook)},
    ], temperature=0.3, num_predict=512)
    return {
        "provider": "ollama", "model": data.get("model", cfg["model"]),
        "explanation": (data.get("message") or {}).get("content", "").strip(),
        "input_tokens": data.get("prompt_eval_count") or 0,
        "output_tokens": data.get("eval_count") or 0,
    }


def _ollama_critique(failure: dict, explanation: str, cfg: dict) -> dict:
    data = _ollama_chat(cfg, [
        {"role": "system", "content": CRITIQUE_SYSTEM},
        {"role": "user", "content": _format_critique_prompt(failure, explanation)},
    ], fmt="json", temperature=0.1, num_predict=400)
    return _parse_critique((data.get("message") or {}).get("content", ""))


def _parse_critique(raw: str) -> dict:
    s = (raw or "").strip()
    if s.startswith("```"):
        s = s.strip("`")
        if s.startswith("json"):
            s = s[4:].lstrip()
    try:
        parsed = json.loads(s)
    except json.JSONDecodeError:
        return {"unsupported_claims": [], "_parse_error": True, "_raw_excerpt": raw[:200]}
    if not isinstance(parsed, dict):
        return {"unsupported_claims": [], "_parse_error": True}
    claims = parsed.get("unsupported_claims") or []
    return {"unsupported_claims": [str(c) for c in claims if c]}


# Module-level so tests can monkeypatch.setitem these dicts.
_EXPLAIN_BACKENDS = {
    "anthropic": _anthropic_explain,
    "openai":    _openai_explain,
    "ollama":    _ollama_explain,
}
_CRITIQUE_BACKENDS = {
    "anthropic": _anthropic_critique,
    "openai":    _openai_critique,
    "ollama":    _ollama_critique,
}
# Self-critique doubles latency; on for cloud, off for Ollama by default.
_CRITIQUE_AUTO_PROVIDERS = {"anthropic", "openai"}


def _should_self_critique(provider: str, setting_value: str) -> bool:
    # '1' force on, '0' force off, anything else = auto (cloud only)
    if setting_value == "1":
        return True
    if setting_value == "0":
        return False
    return provider in _CRITIQUE_AUTO_PROVIDERS


async def explain_failure(failure: dict, *, playbook: str = "", validate: bool = True) -> dict:
    # Backends are sync (SDK / httpx); hop to threadpool to keep the event loop free.
    from app.core import ai

    provider, cfg = await ai.resolve_provider()
    if provider is None:
        raise RuntimeError("no AI provider configured")
    backend = _EXPLAIN_BACKENDS[provider]
    result = await asyncio.to_thread(backend, failure, playbook, cfg)

    if not (result.get("explanation") or "").strip():
        result["explanation"] = ("The model returned an empty explanation. Try again, or set a "
                                  "stronger provider (Anthropic / OpenAI) in Settings → AI helper.")

    validation = ai.validate_text(result.get("explanation", ""))

    if validate and _should_self_critique(provider, await ai.setting("ai.validate_responses")):
        try:
            critique = await asyncio.to_thread(
                _CRITIQUE_BACKENDS[provider], failure, result["explanation"], cfg
            )
            validation["self_critique"] = critique
            downgrade_confidence(validation, unsupported_claims=len(critique.get("unsupported_claims", [])))
        except Exception as e:
            validation["self_critique_error"] = str(e)

    result["validation"] = validation
    return result
