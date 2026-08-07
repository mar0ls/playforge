"""Cron-style scheduler attached to the FastAPI process.

We use APScheduler's AsyncIOScheduler so jobs run on the same event loop the app
serves requests from — no separate worker process, no Redis. Schedules are
persisted in SQLite (`schedules` table); on startup we read them all and add a
job per enabled row. CRUD endpoints keep APScheduler in sync via `sync_schedule`.

Each fired job inserts a Run row, executes via the existing `run_playbook`
pipeline (so all the runner improvements — credentials, verbosity, cancel logic
— work for scheduled runs too), then updates `schedules.last_run_at` /
`last_run_id`. The runner call itself blocks on subprocess so we wrap it in
`run_in_executor`-style fashion the way the live runner already does.
"""
from __future__ import annotations

import asyncio
import json
import logging
from datetime import datetime

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from croniter import croniter
from sqlalchemy import select

from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from app.core.runner import RunRequest, run_playbook, summarize
from app.models.db import RunTemplate, Schedule, SessionLocal, Run


log = logging.getLogger("ansible_gui.scheduler")

_scheduler: AsyncIOScheduler | None = None


def get_scheduler() -> AsyncIOScheduler:
    global _scheduler
    if _scheduler is None:
        _scheduler = AsyncIOScheduler(timezone="UTC")
    return _scheduler


def validate_cron(expr: str) -> str:
    """Validate a 5-field crontab. Returns the expression normalized (stripped)."""
    expr = (expr or "").strip()
    if not croniter.is_valid(expr):
        raise ValueError(f"invalid cron expression: {expr!r}")
    return expr


def validate_timezone(tz_name: str) -> str:
    """IANA name, or '' for UTC."""
    tz_name = (tz_name or "").strip()
    if not tz_name:
        return ""
    try:
        ZoneInfo(tz_name)
    except ZoneInfoNotFoundError:
        raise ValueError(f"unknown timezone: {tz_name!r}")
    return tz_name


def _tz_for(name: str) -> ZoneInfo | None:
    """tzinfo for `name`, or None (= scheduler default UTC) when blank/unknown."""
    if not name:
        return None
    try:
        return ZoneInfo(name)
    except ZoneInfoNotFoundError:
        return None


async def _fire(schedule_id: int) -> None:
    """Run a schedule once. Lives on the APScheduler event loop."""
    async with SessionLocal() as session:
        sched = await session.get(Schedule, schedule_id)
        if sched is None or not sched.enabled:
            return
        tpl = await session.get(RunTemplate, sched.template_id)
        if tpl is None:
            log.warning("schedule %s references missing template %s; skipping",
                        schedule_id, sched.template_id)
            return
        run = Run(
            project_id=sched.project_id, playbook=tpl.playbook, inventory=tpl.inventory,
            tags=tpl.tags, status="running",
            template_id=tpl.id, schedule_id=sched.id, environment_id=tpl.environment_id,
        )
        session.add(run)
        await session.commit()
        await session.refresh(run)
        run_id = run.id
        project_id = sched.project_id

    req = RunRequest(
        project_id=project_id,
        playbook=tpl.playbook,
        inventory=tpl.inventory,
        tags=[s for s in tpl.tags.split(",") if s],
        skip_tags=[s for s in tpl.skip_tags.split(",") if s],
        limit=tpl.limit, check=tpl.check, syntax_check=tpl.syntax_check,
    )
    artifacts: list[str] = []
    try:
        result = await run_playbook(req)
        summary = summarize(result)
        try:
            from app.core import storage
            artifacts = await asyncio.to_thread(storage.commit_all, project_id, f"Run #{run_id} artifacts")
        except Exception:
            artifacts = []
    except Exception as e:
        log.exception("schedule %s fire failed", schedule_id)
        summary = {"overall": "failed", "hosts": {}, "failures": [{"error": str(e)}], "status": "failed"}

    async with SessionLocal() as session:
        run_row = await session.get(Run, run_id)
        if run_row is not None:
            run_row.status = summary["overall"]
            run_row.ended_at = datetime.utcnow()
            run_row.stats_json = json.dumps(summary.get("hosts") or {})
            run_row.failures_json = json.dumps(summary.get("failures") or [])
            run_row.artifacts_json = json.dumps(artifacts)
            await session.commit()
        sched = await session.get(Schedule, schedule_id)
        if sched is not None:
            sched.last_run_at = datetime.utcnow()
            sched.last_run_id = run_id
            await session.commit()


def _job_id(schedule_id: int) -> str:
    return f"schedule-{schedule_id}"


def _build_trigger(schedule: Schedule) -> CronTrigger:
    """Per-schedule cron trigger that honours the row's `timezone` (empty = UTC).

    UTC is passed explicitly for blank/unknown timezones. `CronTrigger.from_crontab`
    with no timezone falls back to the *process* local zone, not the scheduler's,
    so setting TZ on the container used to silently shift every schedule that
    didn't name one — and `next_fire_iso` computes blank-timezone times in real
    UTC, so the "next fire" shown in the UI disagreed with when the job ran.
    """
    tz = _tz_for(getattr(schedule, "timezone", "") or "") or ZoneInfo("UTC")
    return CronTrigger.from_crontab(schedule.cron_expr, timezone=tz)


def sync_schedule(schedule: Schedule) -> None:
    """Add / update / remove the APScheduler job for a Schedule row.

    Called after every CRUD operation so the in-memory scheduler stays in sync
    with the DB. Removing a row → caller invokes `unsync_schedule(id)` instead.
    """
    s = get_scheduler()
    job_id = _job_id(schedule.id)
    if schedule.enabled:
        s.add_job(
            _fire, _build_trigger(schedule),
            args=[schedule.id], id=job_id, replace_existing=True,
            misfire_grace_time=300, coalesce=True, max_instances=1,
        )
    else:
        try:
            s.remove_job(job_id)
        except Exception:
            pass


def unsync_schedule(schedule_id: int) -> None:
    s = get_scheduler()
    try:
        s.remove_job(_job_id(schedule_id))
    except Exception:
        pass


async def load_all() -> None:
    """Re-hydrate the in-memory scheduler from the DB on startup."""
    async with SessionLocal() as session:
        rows = (await session.execute(select(Schedule).where(Schedule.enabled.is_(True)))).scalars().all()
    s = get_scheduler()
    for sched in rows:
        try:
            s.add_job(
                _fire, _build_trigger(sched),
                args=[sched.id], id=_job_id(sched.id), replace_existing=True,
                misfire_grace_time=300, coalesce=True, max_instances=1,
            )
        except Exception:
            log.exception("could not load schedule %s (cron %r tz %r)",
                          sched.id, sched.cron_expr, getattr(sched, "timezone", ""))


def next_fire_iso(cron_expr: str, base: datetime | None = None, tz: str = "") -> str | None:
    """Next fire time (ISO). `tz` = IANA name, '' = UTC. None on invalid cron."""
    try:
        zone = _tz_for(tz) if tz else None
        if zone is not None:
            from datetime import datetime as _dt
            now = _dt.now(zone)
            it = croniter(cron_expr, base or now)
        else:
            it = croniter(cron_expr, base or datetime.utcnow())
        return it.get_next(datetime).isoformat()
    except Exception:
        return None
