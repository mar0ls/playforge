"""CRUD for saved run templates (per project)."""
from __future__ import annotations

import json
from datetime import datetime

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel
from sqlalchemy import select

from app.models.db import SessionLocal, RunTemplate, Project, Schedule


router = APIRouter(prefix="/api/projects/{project_id}/templates", tags=["templates"])


class TemplateIn(BaseModel):
    name: str
    description: str = ""
    playbook: str
    inventory: str = ""
    environment_id: int | None = None
    tags: list[str] = []
    skip_tags: list[str] = []
    limit: str = ""
    check: bool = False
    syntax_check: bool = False
    extra_vars: dict = {}
    credential_ids: list[int] = []


class TemplateOut(BaseModel):
    id: int
    name: str
    description: str
    playbook: str
    inventory: str
    environment_id: int | None
    tags: list[str]
    skip_tags: list[str]
    limit: str
    check: bool
    syntax_check: bool
    extra_vars: dict
    credential_ids: list[int]
    created_at: datetime
    updated_at: datetime


def _to_out(t: RunTemplate) -> TemplateOut:
    return TemplateOut(
        id=t.id, name=t.name, description=t.description,
        playbook=t.playbook, inventory=t.inventory, environment_id=t.environment_id,
        tags=[s for s in t.tags.split(",") if s], skip_tags=[s for s in t.skip_tags.split(",") if s],
        limit=t.limit, check=t.check, syntax_check=t.syntax_check,
        extra_vars=json.loads(t.extra_vars_json or "{}"),
        credential_ids=json.loads(t.credential_ids_json or "[]"),
        created_at=t.created_at, updated_at=t.updated_at,
    )


@router.get("", response_model=list[TemplateOut])
async def list_templates(project_id: str):
    async with SessionLocal() as session:
        rows = (await session.execute(
            select(RunTemplate).where(RunTemplate.project_id == project_id).order_by(RunTemplate.name)
        )).scalars().all()
    return [_to_out(t) for t in rows]


@router.post("", response_model=TemplateOut)
async def create_template(project_id: str, payload: TemplateIn):
    async with SessionLocal() as session:
        if not await session.get(Project, project_id):
            raise HTTPException(404, "project not found")
        t = RunTemplate(
            project_id=project_id, name=payload.name, description=payload.description,
            playbook=payload.playbook, inventory=payload.inventory,
            environment_id=payload.environment_id,
            tags=",".join(payload.tags), skip_tags=",".join(payload.skip_tags),
            limit=payload.limit, check=payload.check, syntax_check=payload.syntax_check,
            extra_vars_json=json.dumps(payload.extra_vars),
            credential_ids_json=json.dumps(payload.credential_ids),
        )
        session.add(t)
        await session.commit()
        await session.refresh(t)
        return _to_out(t)


@router.put("/{template_id}", response_model=TemplateOut)
async def update_template(project_id: str, template_id: int, payload: TemplateIn):
    async with SessionLocal() as session:
        t = await session.get(RunTemplate, template_id)
        if t is None or t.project_id != project_id:
            raise HTTPException(404, "template not found")
        t.name = payload.name
        t.description = payload.description
        t.playbook = payload.playbook
        t.inventory = payload.inventory
        t.environment_id = payload.environment_id
        t.tags = ",".join(payload.tags)
        t.skip_tags = ",".join(payload.skip_tags)
        t.limit = payload.limit
        t.check = payload.check
        t.syntax_check = payload.syntax_check
        t.extra_vars_json = json.dumps(payload.extra_vars)
        t.credential_ids_json = json.dumps(payload.credential_ids)
        t.updated_at = datetime.utcnow()
        await session.commit()
        await session.refresh(t)
        return _to_out(t)


@router.delete("/{template_id}")
async def delete_template(project_id: str, template_id: int):
    async with SessionLocal() as session:
        t = await session.get(RunTemplate, template_id)
        if t is None or t.project_id != project_id:
            raise HTTPException(404, "template not found")
        # A schedule holds a plain template_id, not a foreign key, so deleting the
        # template leaves it pointing at nothing. It then fails at fire time, which
        # nobody is watching — the schedule just stops running. Refuse and say which.
        used_by = (await session.execute(
            select(Schedule).where(Schedule.template_id == template_id)
        )).scalars().all()
        if used_by:
            names = ", ".join(sorted(s.name for s in used_by))
            raise HTTPException(
                409, f"template is used by schedule(s): {names} — delete or repoint them first")
        await session.delete(t)
        await session.commit()
    return {"deleted": template_id}
