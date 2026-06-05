"""HTTP + WebSocket endpoints for playbook runs."""
from __future__ import annotations

import asyncio
import json
from datetime import datetime

from fastapi import APIRouter, HTTPException, WebSocket, WebSocketDisconnect
from pydantic import BaseModel
from sqlalchemy import select

from app.core import credentials as cred_store
from app.core import storage
from app.core.runner import RunRequest, run_playbook, run_adhoc, summarize
from app.models.db import SessionLocal, Run, Credential, RunTemplate, Project, Environment


async def _capture_artifacts(project_id: str, run_id: int) -> list[str]:
    """After a run, commit any files it wrote into the project repo (generated
    keys, rendered configs, fetched files) so they're versioned and visible in the
    Files tab. Returns the changed paths. Best-effort — never fails the run."""
    try:
        return await asyncio.to_thread(
            storage.commit_all, project_id, f"Run #{run_id} artifacts")
    except Exception:
        return []


router = APIRouter(prefix="/api/runs", tags=["runs"])


class RunIn(BaseModel):
    project_id: str
    playbook: str = ""
    inventory: str = ""
    tags: list[str] = []
    skip_tags: list[str] = []
    limit: str = ""
    extra_vars: dict | None = None
    check: bool = False
    syntax_check: bool = False
    verbosity: int = 0
    template_id: int | None = None
    environment_id: int | None = None
    # None = fall back to the template's credentials. An explicit list (even []) overrides.
    credential_ids: list[int] | None = None


async def _build_request(payload: RunIn) -> tuple[RunRequest, int | None]:
    """Translate a RunIn (which may reference a template + credentials) into a
    concrete RunRequest the runner understands, plus the resolved environment_id
    (carried separately for run attribution — the runner doesn't need it).

    Templates supply defaults; any explicit field in `payload` overrides them.
    Credentials are loaded by ID and their secret content is read off disk and
    threaded through. An explicit `environment_id` is validated against the
    project; if omitted it falls back to the template's environment_id."""
    base = payload.model_dump()
    template_id = base.pop("template_id")
    environment_id = base.pop("environment_id")
    credential_ids = base.pop("credential_ids")  # None means "fall back to template", [] means "explicitly none"

    if template_id is not None:
        async with SessionLocal() as session:
            tpl = await session.get(RunTemplate, template_id)
            if tpl is None:
                raise HTTPException(404, f"template {template_id} not found")
            # Only fill fields the caller left as defaults/empty.
            base["playbook"] = base.get("playbook") or tpl.playbook
            base["inventory"] = base.get("inventory") or tpl.inventory
            base["tags"] = base.get("tags") or [s for s in tpl.tags.split(",") if s]
            base["skip_tags"] = base.get("skip_tags") or [s for s in tpl.skip_tags.split(",") if s]
            base["limit"] = base.get("limit") or tpl.limit
            base["check"] = base.get("check") or tpl.check
            base["syntax_check"] = base.get("syntax_check") or tpl.syntax_check
            base["extra_vars"] = base.get("extra_vars") or json.loads(tpl.extra_vars_json or "{}")
            if credential_ids is None:
                credential_ids = json.loads(tpl.credential_ids_json or "[]")
            if environment_id is None:
                environment_id = tpl.environment_id

    if environment_id is not None:
        async with SessionLocal() as session:
            env = await session.get(Environment, environment_id)
        if env is None:
            raise HTTPException(404, f"environment {environment_id} not found")
        if env.project_id != base.get("project_id"):
            raise HTTPException(400, "environment does not belong to this project")

    if credential_ids is None:
        credential_ids = []

    if not base.get("playbook"):
        raise HTTPException(400, "playbook is required (directly or via a template)")

    # Resolve credentials: pick the first SSH-key credential and inject its content.
    if credential_ids:
        async with SessionLocal() as session:
            creds = (await session.execute(
                select(Credential).where(Credential.id.in_(credential_ids))
            )).scalars().all()
        for c in creds:
            if c.kind == "ssh_key":
                content = cred_store.read_secret(c.id)
                if content:
                    base["ssh_key_content"] = content
                break  # ansible-runner supports one ssh_key per run; first wins
        for c in creds:
            if c.kind == "vault_password":
                vpass = cred_store.read_secret(c.id)
                if vpass:
                    base["vault_password_content"] = vpass.rstrip("\n")
                break  # one vault password per run; first wins

    return RunRequest(**base), environment_id


@router.post("")
async def start_run(payload: RunIn):
    """Run a playbook synchronously (waits for completion). For live output use the WebSocket."""
    req, environment_id = await _build_request(payload)
    async with SessionLocal() as session:
        run = Run(project_id=req.project_id, playbook=req.playbook, inventory=req.inventory,
                  tags=",".join(req.tags), status="running", environment_id=environment_id)
        session.add(run)
        await session.commit()
        await session.refresh(run)
        run_id = run.id

    result = await run_playbook(req)
    summary = summarize(result)
    artifacts = await _capture_artifacts(req.project_id, run_id)

    async with SessionLocal() as session:
        run = await session.get(Run, run_id)
        if run is not None:
            run.status = "canceled" if summary.get("status") == "canceled" else summary["overall"]
            run.ended_at = datetime.utcnow()
            run.stats_json = json.dumps(summary["hosts"])
            run.failures_json = json.dumps(summary["failures"])
            run.artifacts_json = json.dumps(artifacts)
            await session.commit()

    return {"run_id": run_id, "artifacts": artifacts, **summary}


@router.get("")
async def list_runs(project_id: str | None = None, status: str | None = None,
                    playbook: str | None = None, limit: int = 100, offset: int = 0):
    """Global, filterable run history. Powers the /runs page."""
    async with SessionLocal() as session:
        stmt = (select(Run, Project.name, Environment.name)
                .join(Project, Run.project_id == Project.id)
                .outerjoin(Environment, Run.environment_id == Environment.id))
        if project_id:
            stmt = stmt.where(Run.project_id == project_id)
        if status:
            stmt = stmt.where(Run.status == status)
        if playbook:
            stmt = stmt.where(Run.playbook.contains(playbook))
        stmt = stmt.order_by(Run.started_at.desc()).limit(min(limit, 500)).offset(max(offset, 0))
        rows = (await session.execute(stmt)).all()
    return [
        {
            "id": r.id, "project_id": r.project_id, "project_name": pname,
            "playbook": r.playbook, "inventory": r.inventory, "tags": r.tags,
            "status": r.status,
            "environment_id": r.environment_id, "environment_name": ename,
            "started_at": r.started_at.isoformat() if r.started_at else None,
            "ended_at": r.ended_at.isoformat() if r.ended_at else None,
        }
        for r, pname, ename in rows
    ]


@router.post("/preview")
async def preview_run(payload: RunIn):
    """Dry-run in check mode: report which tasks would change on which hosts,
    without applying anything or writing a row to run history."""
    req, _ = await _build_request(payload)
    req.check = True
    req.syntax_check = False
    result = await run_playbook(req)
    summary = summarize(result)
    return {"overall": summary["overall"], "status": summary["status"],
            "hosts": summary["hosts"], "failures": summary["failures"],
            "changes": result.changes}


class AdhocIn(BaseModel):
    project_id: str
    host_pattern: str = "all"
    module: str = "ping"
    args: str = ""
    inventory: str = ""


@router.post("/adhoc")
async def adhoc(payload: AdhocIn):
    result = await run_adhoc(payload.project_id, payload.host_pattern,
                             payload.module, payload.args, payload.inventory)
    return summarize(result)


@router.websocket("/ws")
async def run_ws(ws: WebSocket):
    """Client connects, sends a RunIn JSON, then receives events until done.

    While the run is in flight, the client may also send `{"action": "cancel"}`
    on the same WebSocket; that flips an `asyncio.Event` the runner polls via
    ansible-runner's cancel_callback.
    """
    await ws.accept()
    try:
        payload_dict = await ws.receive_json()
        run_in = RunIn(**payload_dict)
        req, environment_id = await _build_request(run_in)
    except HTTPException as e:
        await ws.send_json({"event": "error", "message": e.detail})
        await ws.close()
        return
    except (KeyError, TypeError, ValueError) as e:
        await ws.send_json({"event": "error", "message": f"bad request: {e}"})
        await ws.close()
        return

    queue: asyncio.Queue = asyncio.Queue()
    cancel_event = asyncio.Event()

    def push(event: dict) -> None:
        queue.put_nowait(event)

    async def pump() -> None:
        while True:
            event = await queue.get()
            if event is None:
                return
            try:
                await ws.send_json(event)
            except (WebSocketDisconnect, RuntimeError):
                return

    async def listen_for_control() -> None:
        """Read client messages while the run is in flight; flip cancel on request
        or on early disconnect."""
        while True:
            try:
                msg = await ws.receive_json()
            except (WebSocketDisconnect, RuntimeError):
                cancel_event.set()
                return
            if isinstance(msg, dict) and msg.get("action") == "cancel":
                cancel_event.set()
                return

    # Persist the run upfront so it shows up in history immediately and we have an
    # id to associate with the WS session. We finalize the row after the run ends.
    async with SessionLocal() as session:
        run_row = Run(
            project_id=req.project_id, playbook=req.playbook, inventory=req.inventory,
            tags=",".join(req.tags), status="running",
            template_id=run_in.template_id, environment_id=environment_id,
        )
        session.add(run_row)
        await session.commit()
        await session.refresh(run_row)
        run_id = run_row.id

    pumper = asyncio.create_task(pump())
    listener = asyncio.create_task(listen_for_control())
    try:
        result = await run_playbook(req, on_event=push, cancel_event=cancel_event)
        summary = summarize(result)
        if cancel_event.is_set():
            summary["canceled"] = True
        artifacts = await _capture_artifacts(req.project_id, run_id)
        async with SessionLocal() as session:
            run = await session.get(Run, run_id)
            if run is not None:
                run.status = "canceled" if summary.get("canceled") else summary["overall"]
                run.ended_at = datetime.utcnow()
                run.stats_json = json.dumps(summary["hosts"])
                run.failures_json = json.dumps(summary["failures"])
                run.artifacts_json = json.dumps(artifacts)
                await session.commit()
        await ws.send_json({"event": "summary", "run_id": run_id, "artifacts": artifacts, **summary})
    except Exception as e:
        async with SessionLocal() as session:
            run = await session.get(Run, run_id)
            if run is not None:
                run.status = "failed"
                run.ended_at = datetime.utcnow()
                await session.commit()
        await ws.send_json({"event": "error", "message": str(e)})
    finally:
        listener.cancel()
        queue.put_nowait(None)
        await pumper
        try:
            await ws.close()
        except RuntimeError:
            pass
