"""CRUD for project environments (prod / staging / ...).

An Environment bundles an inventory path with an optional default credential, so a
user picks "production" once instead of restating inventory + key on every run.
Scoped under a project; deleting the project cascades (FK ondelete=CASCADE).
"""
from __future__ import annotations

from datetime import datetime

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel
from sqlalchemy import select

from app.models.db import SessionLocal, Environment, Project

router = APIRouter(prefix="/api/projects/{project_id}/environments", tags=["environments"])


class EnvironmentIn(BaseModel):
    name: str
    description: str = ""
    inventory_path: str = ""
    default_credential_id: int | None = None


class EnvironmentUpdate(BaseModel):
    name: str | None = None
    description: str | None = None
    inventory_path: str | None = None
    default_credential_id: int | None = None


def _to_out(e: Environment) -> dict:
    return {
        "id": e.id, "project_id": e.project_id, "name": e.name,
        "description": e.description, "inventory_path": e.inventory_path,
        "default_credential_id": e.default_credential_id,
        "created_at": e.created_at.isoformat() if e.created_at else None,
    }


@router.get("")
async def list_environments(project_id: str):
    async with SessionLocal() as session:
        rows = (await session.execute(
            select(Environment).where(Environment.project_id == project_id).order_by(Environment.name)
        )).scalars().all()
    return [_to_out(e) for e in rows]


@router.post("")
async def create_environment(project_id: str, payload: EnvironmentIn):
    if not payload.name.strip():
        raise HTTPException(400, "name is required")
    async with SessionLocal() as session:
        if await session.get(Project, project_id) is None:
            raise HTTPException(404, "project not found")
        env = Environment(
            project_id=project_id, name=payload.name.strip(),
            description=payload.description, inventory_path=payload.inventory_path,
            default_credential_id=payload.default_credential_id,
        )
        session.add(env)
        await session.commit()
        await session.refresh(env)
        return _to_out(env)


@router.put("/{env_id}")
async def update_environment(project_id: str, env_id: int, payload: EnvironmentUpdate):
    async with SessionLocal() as session:
        env = await session.get(Environment, env_id)
        if env is None or env.project_id != project_id:
            raise HTTPException(404, "environment not found")
        if payload.name is not None:
            env.name = payload.name.strip()
        if payload.description is not None:
            env.description = payload.description
        if payload.inventory_path is not None:
            env.inventory_path = payload.inventory_path
        if payload.default_credential_id is not None:
            env.default_credential_id = payload.default_credential_id or None
        await session.commit()
        await session.refresh(env)
        return _to_out(env)


@router.delete("/{env_id}")
async def delete_environment(project_id: str, env_id: int):
    async with SessionLocal() as session:
        env = await session.get(Environment, env_id)
        if env is None or env.project_id != project_id:
            raise HTTPException(404, "environment not found")
        await session.delete(env)
        await session.commit()
    return {"deleted": env_id}
