"""AI helper: failure-explain, generate, suggest, narrate, chat, runbook, agent.

Re-exports everything callers and tests touch (`ai.setting`, `ai.validate_text`,
the `_provider_*` callables, the `_*_BACKENDS` dicts). Submodules look these up
via `ai.<name>` at call time so `monkeypatch.setattr(ai, ...)` actually
propagates downstream — direct submodule imports would freeze a pre-patch ref.
"""
from app.core.playbook_builder import BuilderError
from app.core.settings_store import get as setting
from app.core.ai_validate import validate_text

from .providers import (
    resolve_provider, ai_enabled,
    probe_anthropic, probe_openai, probe_ollama,
    _openai_chat, _ollama_chat,
    _provider_chat, _provider_text, _provider_json,
    _provider_chat_stream,
)
from .explain import (
    explain_failure, EXPLAIN_SYSTEM, CRITIQUE_SYSTEM,
    _format_user_prompt, _format_critique_prompt,
    _parse_critique, _should_self_critique,
    _EXPLAIN_BACKENDS, _CRITIQUE_BACKENDS,
)
from .generate import (
    generate_playbook, GENERATE_SYSTEM,
    _parse_spec, _to_builder_spec, _GENERATE_BACKENDS,
)
from .suggest import suggest_fix, SUGGEST_SYSTEM
from .narrate import narrate_plan, NARRATE_SYSTEM, _narrate_user_prompt
from .chat import (
    chat, chat_stream, extract_files, CHAT_SYSTEM,
    _CHAT_CACHE, clear_chat_cache, _chat_cache_key,
)
from .runbook import generate_runbook, RUNBOOK_SYSTEM
from .agent_runner import run_project_agent

__all__ = [
    "BuilderError", "setting",
    "resolve_provider", "ai_enabled",
    "probe_anthropic", "probe_openai", "probe_ollama",
    "explain_failure", "generate_playbook", "suggest_fix", "narrate_plan",
    "chat", "chat_stream", "extract_files", "generate_runbook", "run_project_agent",
    "clear_chat_cache",
]
