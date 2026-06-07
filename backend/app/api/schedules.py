"""CRUD for cron-style schedules."""
from __future__ import annotations

from datetime import datetime

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel
from sqlalchemy import select

from app.core.scheduler import (
    next_fire_iso, sync_schedule, unsync_schedule, validate_cron, validate_timezone,
)
from app.models.db import Project, RunTemplate, Schedule, SessionLocal


router = APIRouter(prefix="/api/schedules", tags=["schedules"])


class ScheduleIn(BaseModel):
    name: str
    project_id: str
    template_id: int
    cron_expr: str
    enabled: bool = True
    timezone: str = ""   # IANA name, e.g. "Europe/Warsaw"; empty = UTC


class ScheduleOut(BaseModel):
    id: int
    name: str
    project_id: str
    template_id: int
    cron_expr: str
    timezone: str
    enabled: bool
    last_run_at: datetime | None
    last_run_id: int | None
    next_fire_at: str | None
    created_at: datetime
    updated_at: datetime


def _to_out(s: Schedule) -> ScheduleOut:
    tz = getattr(s, "timezone", "") or ""
    return ScheduleOut(
        id=s.id, name=s.name, project_id=s.project_id, template_id=s.template_id,
        cron_expr=s.cron_expr, timezone=tz, enabled=s.enabled,
        last_run_at=s.last_run_at, last_run_id=s.last_run_id,
        next_fire_at=next_fire_iso(s.cron_expr, tz=tz) if s.enabled else None,
        created_at=s.created_at, updated_at=s.updated_at,
    )


@router.get("", response_model=list[ScheduleOut])
async def list_schedules():
    async with SessionLocal() as session:
        rows = (await session.execute(select(Schedule).order_by(Schedule.name))).scalars().all()
    return [_to_out(s) for s in rows]


@router.post("", response_model=ScheduleOut)
async def create_schedule(payload: ScheduleIn):
    try:
        cron = validate_cron(payload.cron_expr)
        tz = validate_timezone(payload.timezone)
    except ValueError as e:
        raise HTTPException(400, str(e))
    async with SessionLocal() as session:
        if not await session.get(Project, payload.project_id):
            raise HTTPException(404, "project not found")
        if not await session.get(RunTemplate, payload.template_id):
            raise HTTPException(404, "template not found")
        s = Schedule(
            name=payload.name, project_id=payload.project_id,
            template_id=payload.template_id, cron_expr=cron, timezone=tz,
            enabled=payload.enabled,
        )
        session.add(s)
        await session.commit()
        await session.refresh(s)
        sync_schedule(s)
        return _to_out(s)


@router.put("/{schedule_id}", response_model=ScheduleOut)
async def update_schedule(schedule_id: int, payload: ScheduleIn):
    try:
        cron = validate_cron(payload.cron_expr)
        tz = validate_timezone(payload.timezone)
    except ValueError as e:
        raise HTTPException(400, str(e))
    async with SessionLocal() as session:
        s = await session.get(Schedule, schedule_id)
        if s is None:
            raise HTTPException(404, "schedule not found")
        s.name = payload.name
        s.project_id = payload.project_id
        s.template_id = payload.template_id
        s.cron_expr = cron
        s.timezone = tz
        s.enabled = payload.enabled
        s.updated_at = datetime.utcnow()
        await session.commit()
        await session.refresh(s)
        sync_schedule(s)
        return _to_out(s)


@router.delete("/{schedule_id}")
async def delete_schedule(schedule_id: int):
    async with SessionLocal() as session:
        s = await session.get(Schedule, schedule_id)
        if s is None:
            raise HTTPException(404, "schedule not found")
        await session.delete(s)
        await session.commit()
    unsync_schedule(schedule_id)
    return {"deleted": schedule_id}
