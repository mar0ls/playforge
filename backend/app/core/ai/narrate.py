"""Plain-language narration of a `--check` preview."""
from __future__ import annotations

import asyncio


NARRATE_SYSTEM = """You explain, in plain language for a DevOps engineer, what an Ansible
run WILL DO before it runs. You are given the tasks that a --check (dry-run) reported as
"would change", grouped by host. Write 2-5 short sentences:
- summarise what changes and on which hosts,
- call out anything risky (service restarts, package removals, file overwrites, reboots),
- if the list is empty, say the hosts already match desired state (no drift).
Be concrete and brief. Plain text, no markdown headers."""


def _narrate_user_prompt(changes: list[dict], playbook: str) -> str:
    if not changes:
        return f"Playbook: {playbook or '(unknown)'}\nCheck-mode reported NO changes."
    lines = [f"- {c.get('host', '?')}: {c.get('task', '?')}" for c in changes[:80]]
    return (f"Playbook: {playbook or '(unknown)'}\n"
            f"Would-change tasks ({len(changes)}):\n" + "\n".join(lines))


async def narrate_plan(changes: list[dict], *, playbook: str = "") -> dict:
    from app.core import ai

    provider, cfg = await ai.resolve_provider()
    if provider is None:
        raise RuntimeError("no AI provider configured")
    text = await asyncio.to_thread(ai._provider_text, provider, NARRATE_SYSTEM,
                                   _narrate_user_prompt(changes, playbook), cfg)
    return {"provider": provider, "model": cfg.get("model", ""), "narration": (text or "").strip()}
