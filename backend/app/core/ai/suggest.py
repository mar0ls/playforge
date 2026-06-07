"""Failure → proposed fix. Caller applies the patch; this never writes."""
from __future__ import annotations

import asyncio
import json


SUGGEST_SYSTEM = """You are an Ansible expert fixing a failed task. Given the failure
record and the playbook that produced it, propose ONE concrete fix.
Reply with ONLY a JSON object:
{
  "root_cause": "<one sentence>",
  "fix_summary": "<what to change and why, 1-3 sentences>",
  "target_path": "<relative path of the file to edit, usually the playbook>",
  "new_content": "<COMPLETE new content of target_path with the fix applied, or null>",
  "manual_steps": ["<step>"]
}
Rules:
- If a file edit fixes it, return the WHOLE corrected file in new_content (not a diff).
- Only propose editing the playbook you were given (or a vars file it references).
- Use real modules/parameters only; keep the change minimal.
- If the failure is environmental (auth, DNS, unreachable host, missing key), set
  new_content to null and put the human actions in manual_steps."""


def _suggest_user_prompt(failure: dict, playbook_path: str, playbook_content: str) -> str:
    return (
        "FAILURE RECORD:\n"
        + json.dumps(failure, indent=2, default=str)[:3000]
        + f"\n\nPLAYBOOK ({playbook_path or 'unknown'}):\n"
        + (playbook_content or "(not available)")[:6000]
    )


async def suggest_fix(failure: dict, *, playbook_path: str = "", playbook_content: str = "") -> dict:
    from app.core import ai

    provider, cfg = await ai.resolve_provider()
    if provider is None:
        raise RuntimeError("no AI provider configured")
    user = _suggest_user_prompt(failure, playbook_path, playbook_content)
    parsed = await asyncio.to_thread(ai._provider_json, provider, SUGGEST_SYSTEM, user, cfg)

    new_content = parsed.get("new_content")
    if not isinstance(new_content, str) or not new_content.strip():
        new_content = None
    return {
        "provider": provider, "model": cfg.get("model", ""),
        "root_cause": str(parsed.get("root_cause") or ""),
        "fix_summary": str(parsed.get("fix_summary") or ""),
        "target_path": str(parsed.get("target_path") or playbook_path or ""),
        "new_content": new_content,
        "manual_steps": [str(s) for s in (parsed.get("manual_steps") or []) if s],
        "validation": ai.validate_text(new_content or ""),
    }
