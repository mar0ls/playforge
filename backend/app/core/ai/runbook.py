"""Project files → Markdown runbook."""
from __future__ import annotations

import asyncio


RUNBOOK_SYSTEM = """You write a concise operational runbook (Markdown) for an Ansible project,
aimed at a DevOps engineer who must understand and operate it. Based ONLY on the playbooks,
inventories and roles you are given (never invent), produce:

# <Project> — Runbook
## Overview
One or two sentences: what this project manages.
## Playbooks
For each playbook: what it does, its target hosts, key tasks, and important tags/vars.
## Inventory & environments
## How to run
Short, practical notes / example invocations.
## Cautions
Anything risky: service restarts, destructive tasks (removals), reboots, SSH lockout risks.

Keep it tight and accurate. Output Markdown only — no preamble."""


async def generate_runbook(context: str, *, project_name: str = "") -> dict:
    from app.core import ai

    provider, cfg = await ai.resolve_provider()
    if provider is None:
        raise RuntimeError("no AI provider configured")
    user = f"Project: {project_name or '(unnamed)'}\n\n{context}"
    md = await asyncio.to_thread(ai._provider_text, provider, RUNBOOK_SYSTEM, user, cfg, max_tokens=1800)
    return {"provider": provider, "model": cfg.get("model", ""), "markdown": (md or "").strip()}
