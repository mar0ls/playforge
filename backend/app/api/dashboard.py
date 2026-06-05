"""Dashboard + system info APIs."""
from __future__ import annotations

import shutil
import subprocess

from fastapi import APIRouter
from sqlalchemy import func, select

from app.core.config import settings
from app.models.db import SessionLocal, Project, Run

router = APIRouter(prefix="/api/dashboard", tags=["dashboard"])


@router.get("/stats")
async def stats():
    async with SessionLocal() as session:
        projects = (await session.execute(select(func.count(Project.id)))).scalar() or 0
        runs_total = (await session.execute(select(func.count(Run.id)))).scalar() or 0
        runs_ok = (await session.execute(select(func.count(Run.id)).where(Run.status == "ok"))).scalar() or 0
        runs_failed = (await session.execute(select(func.count(Run.id)).where(Run.status == "failed"))).scalar() or 0
    return {
        "projects": projects,
        "runs_total": runs_total,
        "runs_ok": runs_ok,
        "runs_failed": runs_failed,
    }


@router.get("/recent-runs")
async def recent_runs(limit: int = 10):
    async with SessionLocal() as session:
        rows = (
            await session.execute(
                select(Run, Project.name)
                .join(Project, Run.project_id == Project.id)
                .order_by(Run.started_at.desc())
                .limit(limit)
            )
        ).all()
    return [
        {
            "id": r.id, "project_id": r.project_id, "project_name": project_name,
            "playbook": r.playbook, "tags": r.tags, "status": r.status,
            "started_at": r.started_at.isoformat() if r.started_at else None,
            "ended_at": r.ended_at.isoformat() if r.ended_at else None,
        }
        for r, project_name in rows
    ]


@router.get("/system")
def system():
    from app.core.credentials import master_key_source
    ansible_version = "unknown"
    if shutil.which("ansible"):
        try:
            out = subprocess.check_output(["ansible", "--version"], text=True, timeout=5)
            ansible_version = out.splitlines()[0].strip()
        except Exception:
            pass
    return {
        "data_dir": str(settings.data_dir),
        "ansible": ansible_version,
        "credentials_encrypted": True,
        "master_key_source": master_key_source(),
    }
