"""HTTP endpoints for the AI helper.

`/explain-failure` runs in a threadpool (sync `def`) because the underlying SDK
calls are blocking network IO.

`/config` lets the Settings page read and update provider config. Sensitive
fields (API keys) are write-only over the API: GET returns `has_key: true/false`
instead of the value.

`/probe` is the "Refresh models" button — given (provider, key/url), it returns
the model list the credentials can reach. We do NOT persist credentials here;
the user has to save them separately via `/config`.
"""
from __future__ import annotations

import asyncio
import json

from fastapi import APIRouter, HTTPException
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

from app.core import ai
from app.core import settings_store
from app.core import storage
from app.core.detect import detect as detect_project


router = APIRouter(prefix="/api/ai", tags=["ai"])


# ---- Status ----------------------------------------------------------------

@router.get("/status")
async def status():
    provider, cfg = await ai.resolve_provider()
    return {
        "enabled": provider is not None,
        "provider": provider,
        "model": cfg.get("model", ""),
        "timeout": cfg.get("timeout", 0),
    }


# ---- Config: read + write --------------------------------------------------

@router.get("/config")
async def get_config():
    return await settings_store.public_dump()


class AIConfigIn(BaseModel):
    # Each is optional — only provided keys are updated. Pass empty string to clear.
    provider: str | None = None
    anthropic_key: str | None = None
    anthropic_model: str | None = None
    openai_key: str | None = None
    openai_model: str | None = None
    openai_base_url: str | None = None
    ollama_url: str | None = None
    ollama_model: str | None = None
    timeout_seconds: str | None = None
    validate_responses: str | None = None
    web_docs: str | None = None


_FIELD_TO_KEY = {
    "provider": "ai.provider",
    "anthropic_key": "ai.anthropic_key",
    "anthropic_model": "ai.anthropic_model",
    "openai_key": "ai.openai_key",
    "openai_model": "ai.openai_model",
    "openai_base_url": "ai.openai_base_url",
    "ollama_url": "ai.ollama_url",
    "ollama_model": "ai.ollama_model",
    "timeout_seconds": "ai.timeout_seconds",
    "validate_responses": "ai.validate_responses",
    "web_docs": "ai.web_docs",
}


@router.put("/config")
async def update_config(payload: AIConfigIn):
    if payload.provider is not None and payload.provider not in ("", "auto", "anthropic", "openai", "ollama"):
        raise HTTPException(400, "provider must be one of: auto, anthropic, openai, ollama, or empty")
    if payload.timeout_seconds is not None:
        try:
            int(payload.timeout_seconds)
        except ValueError:
            raise HTTPException(400, "timeout_seconds must be an integer")
    for field, key in _FIELD_TO_KEY.items():
        value = getattr(payload, field, None)
        if value is None:
            continue
        await settings_store.set(key, value)
    return await settings_store.public_dump()


# ---- Probe: list models for given creds ------------------------------------

class ProbeIn(BaseModel):
    provider: str
    api_key: str | None = None
    base_url: str | None = None
    url: str | None = None
    timeout: float = 30.0


@router.post("/probe")
async def probe(payload: ProbeIn):
    """Return the model list this (provider, credentials) tuple can see.
    If the client doesn't supply the secret/URL, fall back to the one already saved
    in settings — so "Refresh models" works without re-pasting the key."""
    # Resolve credentials: prefer what the client sent, else the saved (decrypted) value.
    api_key = payload.api_key
    base_url = payload.base_url
    url = payload.url
    if payload.provider == "anthropic" and not api_key:
        api_key = await settings_store.get("ai.anthropic_key")
    if payload.provider == "openai":
        if not api_key:
            api_key = await settings_store.get("ai.openai_key")
        if not base_url:
            base_url = await settings_store.get("ai.openai_base_url")
    if payload.provider == "ollama" and not url:
        url = await settings_store.get("ai.ollama_url")

    def _run() -> list[dict]:
        if payload.provider == "anthropic":
            if not api_key:
                raise ValueError("no Anthropic key (enter one or save it first)")
            return ai.probe_anthropic(api_key, timeout=payload.timeout)
        if payload.provider == "openai":
            if not api_key:
                raise ValueError("no OpenAI key (enter one or save it first)")
            return ai.probe_openai(api_key, base_url or "https://api.openai.com/v1",
                                    timeout=payload.timeout)
        if payload.provider == "ollama":
            if not url:
                raise ValueError("no Ollama URL (enter one or save it first)")
            return ai.probe_ollama(url, timeout=payload.timeout)
        raise ValueError(f"unknown provider: {payload.provider}")

    try:
        models = await asyncio.to_thread(_run)
    except ValueError as e:
        raise HTTPException(400, str(e))
    except Exception as e:
        raise HTTPException(502, f"probe failed: {e}")
    return {"models": models}


# ---- Explain failure -------------------------------------------------------

class ExplainIn(BaseModel):
    failure: dict
    playbook: str = ""
    validate_response: bool = True


class GeneratePlaybookIn(BaseModel):
    description: str
    hosts: str = ""


@router.post("/generate-playbook")
async def generate_playbook(payload: GeneratePlaybookIn):
    """Natural-language → playbook spec + YAML, validated for hallucinated modules."""
    if not await ai.ai_enabled():
        raise HTTPException(503, "AI helper not configured. Go to Settings → AI helper to set it up.")
    try:
        return await ai.generate_playbook(payload.description, hosts=payload.hosts)
    except ai.BuilderError as e:
        raise HTTPException(422, f"AI produced an invalid playbook: {e}")
    except Exception as e:
        raise HTTPException(500, f"generation failed: {e}")


class ChatIn(BaseModel):
    messages: list[dict]            # [{role: "user"|"assistant", content: str}, ...]
    project_id: str | None = None   # reserved for future project-aware answers


def _project_context(project_id: str | None) -> str:
    """A compact summary of a project's structure, so the assistant can reference
    the user's real playbooks/inventories/roles (first retrieval step)."""
    if not project_id:
        return ""
    try:
        paths = storage.paths_for(project_id)
    except storage.StorageError:
        return ""
    d = detect_project(paths.root)
    parts = []
    if d.get("playbooks"):
        parts.append("playbooks: " + ", ".join(d["playbooks"][:25]))
    if d.get("inventories"):
        parts.append("inventories: " + ", ".join(d["inventories"][:25]))
    if d.get("roles"):
        parts.append("roles: " + ", ".join(d["roles"][:25]))
    return "\n".join(parts)


@router.post("/chat")
async def chat(payload: ChatIn):
    """Conversational AI assistant (multi-turn). The client sends the running history."""
    if not await ai.ai_enabled():
        raise HTTPException(503, "AI helper not configured. Go to Settings → AI helper to set it up.")
    project_root = None
    if payload.project_id:
        try:
            project_root = storage.paths_for(payload.project_id).root
        except storage.StorageError:
            project_root = None
    try:
        return await ai.chat(payload.messages,
                             project_context=_project_context(payload.project_id),
                             project_root=project_root,
                             project_id=payload.project_id)
    except Exception as e:
        raise HTTPException(500, f"chat failed: {e}")


@router.post("/chat/stream")
async def chat_stream(payload: ChatIn):
    """Streaming variant of /chat: newline-delimited JSON events. Each line is
    `{"type":"token","text":...}` as the reply arrives, then a final
    `{"type":"done", reply, validation, files, ...}`. On error, a
    `{"type":"error","message":...}` line. The client should fall back to /chat
    if this returns non-200."""
    if not await ai.ai_enabled():
        raise HTTPException(503, "AI helper not configured. Go to Settings → AI helper to set it up.")
    project_root = None
    if payload.project_id:
        try:
            project_root = storage.paths_for(payload.project_id).root
        except storage.StorageError:
            project_root = None

    async def _ndjson():
        try:
            async for event in ai.chat_stream(payload.messages,
                                               project_context=_project_context(payload.project_id),
                                               project_root=project_root,
                                               project_id=payload.project_id):
                yield json.dumps(event, default=str) + "\n"
        except Exception as e:
            yield json.dumps({"type": "error", "message": str(e)}) + "\n"

    return StreamingResponse(_ndjson(), media_type="application/x-ndjson")


class AgentIn(BaseModel):
    project_id: str
    goal: str
    allow_mutate: bool = False    # let the agent change files (write/move/mkdir/galaxy)
    allow_confirm: bool = False   # let it delete / fetch web (destructive/external)
    max_steps: int = 8


def _make_get_run(project_id: str):
    """Sync run lookup for the agent's get_run tool. Reads SQLite directly with the
    stdlib driver — the tool runs inside the agent's own event loop, so an async
    session (or nested asyncio.run) would blow up with 'running event loop'."""
    import json as _json
    import sqlite3
    from app.core.config import settings

    def _get(run_id: int) -> dict:
        try:
            conn = sqlite3.connect(str(settings.db_path))
            try:
                row = conn.execute(
                    "SELECT id, project_id, status, playbook, failures_json FROM runs WHERE id = ?",
                    (run_id,)).fetchone()
            finally:
                conn.close()
        except sqlite3.Error as e:
            return {"error": f"db error: {e}"}
        if row is None or row[1] != project_id:
            return {"error": "run not found"}
        return {"id": row[0], "status": row[2], "playbook": row[3],
                "failures": _json.loads(row[4] or "[]")}
    return _get


@router.post("/agent")
async def agent(payload: AgentIn):
    """Run the tool-using agent against a project. Read-only tools always work;
    mutating/destructive tools require the matching opt-in flag."""
    if not await ai.ai_enabled():
        raise HTTPException(503, "AI helper not configured. Go to Settings → AI helper to set it up.")
    try:
        storage.paths_for(payload.project_id)
    except storage.StorageError as e:
        raise HTTPException(404, str(e))

    from app.core import agent_tools
    from app.core.agent import READ, MUTATE, CONFIRM
    from app.core.runner import isolation_kwargs
    # Resolved here: the agent's run/preview tools are sync callbacks on a worker
    # thread and can't read the (async) setting themselves.
    isolation = await isolation_kwargs(storage.paths_for(payload.project_id).root)
    tools = agent_tools.build_tools(payload.project_id,
                                    get_run=_make_get_run(payload.project_id),
                                    isolation=isolation)
    levels = {READ}
    if payload.allow_mutate:
        levels.add(MUTATE)
    if payload.allow_confirm:
        levels.add(CONFIRM)
    try:
        return await ai.run_project_agent(payload.goal, tools, allowed_levels=levels,
                                          max_steps=max(1, min(payload.max_steps, 15)))
    except Exception as e:
        raise HTTPException(500, f"agent failed: {e}")


class NarratePlanIn(BaseModel):
    changes: list[dict] = []
    playbook: str = ""


@router.post("/narrate-plan")
async def narrate_plan(payload: NarratePlanIn):
    """Plain-language narration of a check-mode preview ('what this run will do')."""
    if not await ai.ai_enabled():
        raise HTTPException(503, "AI helper not configured. Go to Settings → AI helper to set it up.")
    try:
        return await ai.narrate_plan(payload.changes, playbook=payload.playbook)
    except Exception as e:
        raise HTTPException(500, f"narration failed: {e}")


class SuggestFixIn(BaseModel):
    project_id: str
    failure: dict
    playbook: str = ""


@router.post("/suggest-fix")
async def suggest_fix(payload: SuggestFixIn):
    """AI remediation: given a failed task, propose a concrete fix (reviewable patch)."""
    if not await ai.ai_enabled():
        raise HTTPException(503, "AI helper not configured. Go to Settings → AI helper to set it up.")
    content = ""
    if payload.playbook:
        try:
            content = storage.read_file(payload.project_id, payload.playbook)
        except storage.StorageError:
            content = ""
    try:
        return await ai.suggest_fix(payload.failure, playbook_path=payload.playbook, playbook_content=content)
    except Exception as e:
        raise HTTPException(500, f"suggest-fix failed: {e}")


@router.post("/explain-failure")
async def explain(payload: ExplainIn):
    if not await ai.ai_enabled():
        raise HTTPException(503, "AI helper not configured. Go to Settings → AI helper to set it up.")
    try:
        return await ai.explain_failure(payload.failure, playbook=payload.playbook,
                                         validate=payload.validate_response)
    except Exception as e:
        raise HTTPException(500, f"AI request failed: {e}")
