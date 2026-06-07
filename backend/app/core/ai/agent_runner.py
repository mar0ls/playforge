"""Bridge: drive `core.agent.run_agent` with the current AI provider."""
from __future__ import annotations

import asyncio

from .providers import _provider_chat


async def run_project_agent(goal: str, tools, *, allowed_levels: set, max_steps: int = 8) -> dict:
    from app.core import ai

    provider, cfg = await ai.resolve_provider()
    if provider is None:
        raise RuntimeError("no AI provider configured")

    def chat_fn(system: str, messages: list[dict]) -> str:
        return _provider_chat(provider, system, messages, cfg, max_tokens=1500)

    result = await asyncio.to_thread(
        lambda: _run_agent_sync(goal, tools, chat_fn, allowed_levels, max_steps))
    return {
        "provider": provider, "model": cfg.get("model", ""),
        "summary": result.summary, "finished": result.finished,
        "stopped_reason": result.stopped_reason, "steps": result.steps,
        "needed_permissions": result.needed_levels,
        "warnings": result.warnings,
    }


def _run_agent_sync(goal, tools, chat_fn, allowed_levels, max_steps):
    # We're already on a worker thread; drive run_agent on a fresh loop here
    # so tool callbacks can do sync IO without "loop already running" errors.
    from app.core.agent import run_agent
    return asyncio.run(run_agent(goal, tools, chat_fn=chat_fn,
                                  allowed_levels=allowed_levels, max_steps=max_steps))
