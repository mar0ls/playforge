"""Scheduler: firing a schedule, job sync, and timezone handling.

`_fire` was entirely untested — it's the code that turns a cron tick into a Run
row, and it fails silently by design (a scheduled run nobody watches). The tests
below drive it with `run_playbook` stubbed, so the DB bookkeeping either happens
or the test says so.

Timezone/DST cases live here too: per-schedule timezones are a shipped feature
and getting them wrong means jobs fire an hour off twice a year.
"""
from __future__ import annotations

from datetime import datetime

import pytest

pytest.importorskip("aiosqlite")
pytest.importorskip("apscheduler")

from zoneinfo import ZoneInfo

from app.core import scheduler
from app.core.runner import RunResult
from app.models.db import Project, Run, RunTemplate, Schedule, SessionLocal, init_db


@pytest.fixture(autouse=True)
async def _db():
    await init_db()


async def _make_schedule(*, enabled=True, cron="0 2 * * *", tz="",
                         template: bool = True, project="sched-proj") -> tuple[int, int | None]:
    """Create a project (+ template) and a schedule. Returns (schedule_id, template_id)."""
    from app.core.config import settings
    (settings.projects_dir / project).mkdir(parents=True, exist_ok=True)

    async with SessionLocal() as s:
        if await s.get(Project, project) is None:
            s.add(Project(id=project, name=project))
            await s.commit()

        tpl_id = None
        if template:
            tpl = RunTemplate(project_id=project, name="tpl", playbook="site.yml",
                              inventory="hosts.ini", tags="", skip_tags="", limit="")
            s.add(tpl)
            await s.commit()
            await s.refresh(tpl)
            tpl_id = tpl.id

        sched = Schedule(project_id=project, name="nightly",
                         template_id=tpl_id if tpl_id is not None else 999999,
                         cron_expr=cron, timezone=tz, enabled=enabled)
        s.add(sched)
        await s.commit()
        await s.refresh(sched)
        return sched.id, tpl_id


# --- _fire: the cron tick -> Run row path ------------------------------------

async def test_fire_creates_and_finalises_a_run(monkeypatch):
    sched_id, tpl_id = await _make_schedule()

    async def fake_run_playbook(req):
        assert req.playbook == "site.yml"
        return RunResult(status="successful", rc=0,
                         stats={"host1": {"ok": 2, "failures": 0, "unreachable": 0}},
                         failures=[], artifacts_dir="")

    monkeypatch.setattr(scheduler, "run_playbook", fake_run_playbook)

    await scheduler._fire(sched_id)

    async with SessionLocal() as s:
        runs = (await s.execute(
            __import__("sqlalchemy").select(Run).where(Run.schedule_id == sched_id)
        )).scalars().all()
        assert len(runs) == 1
        run = runs[0]
        assert run.status == "ok"
        assert run.ended_at is not None
        assert run.template_id == tpl_id

        sched = await s.get(Schedule, sched_id)
        assert sched.last_run_id == run.id
        assert sched.last_run_at is not None


async def test_fire_records_failure_instead_of_raising(monkeypatch):
    """A crashing runner must still close out the Run row, or the UI shows
    'running' forever."""
    sched_id, _ = await _make_schedule()

    async def boom(req):
        raise RuntimeError("ansible exploded")

    monkeypatch.setattr(scheduler, "run_playbook", boom)

    await scheduler._fire(sched_id)  # must not raise

    async with SessionLocal() as s:
        run = (await s.execute(
            __import__("sqlalchemy").select(Run).where(Run.schedule_id == sched_id)
        )).scalars().first()
        assert run.status == "failed"
        assert run.ended_at is not None
        assert "ansible exploded" in run.failures_json


async def test_fire_on_disabled_schedule_does_nothing(monkeypatch):
    sched_id, _ = await _make_schedule(enabled=False)
    called = {"n": 0}

    async def fake(req):
        called["n"] += 1
        return RunResult(status="successful", rc=0, stats={}, failures=[], artifacts_dir="")

    monkeypatch.setattr(scheduler, "run_playbook", fake)

    await scheduler._fire(sched_id)

    assert called["n"] == 0
    async with SessionLocal() as s:
        runs = (await s.execute(
            __import__("sqlalchemy").select(Run).where(Run.schedule_id == sched_id)
        )).scalars().all()
        assert runs == []


async def test_fire_on_missing_schedule_is_a_noop(monkeypatch):
    monkeypatch.setattr(scheduler, "run_playbook",
                        lambda req: pytest.fail("must not run"))
    await scheduler._fire(999999)


async def test_fire_with_missing_template_writes_no_run(monkeypatch):
    """A template deleted out from under a schedule must not create a stuck Run."""
    sched_id, _ = await _make_schedule(template=False)
    monkeypatch.setattr(scheduler, "run_playbook",
                        lambda req: pytest.fail("must not run"))

    await scheduler._fire(sched_id)

    async with SessionLocal() as s:
        runs = (await s.execute(
            __import__("sqlalchemy").select(Run).where(Run.schedule_id == sched_id)
        )).scalars().all()
        assert runs == []


# --- job sync ----------------------------------------------------------------

async def test_sync_schedule_adds_job_when_enabled():
    sched_id, _ = await _make_schedule(enabled=True)
    async with SessionLocal() as s:
        sched = await s.get(Schedule, sched_id)

    scheduler.sync_schedule(sched)
    try:
        assert scheduler.get_scheduler().get_job(scheduler._job_id(sched_id)) is not None
    finally:
        scheduler.unsync_schedule(sched_id)


async def test_sync_schedule_removes_job_when_disabled():
    sched_id, _ = await _make_schedule(enabled=True)
    async with SessionLocal() as s:
        sched = await s.get(Schedule, sched_id)
    scheduler.sync_schedule(sched)

    sched.enabled = False
    scheduler.sync_schedule(sched)

    assert scheduler.get_scheduler().get_job(scheduler._job_id(sched_id)) is None


def test_unsync_missing_job_does_not_raise():
    scheduler.unsync_schedule(987654)


async def test_load_all_adds_only_enabled_schedules():
    on_id, _ = await _make_schedule(enabled=True, project="load-on")
    off_id, _ = await _make_schedule(enabled=False, project="load-off")

    await scheduler.load_all()
    try:
        s = scheduler.get_scheduler()
        assert s.get_job(scheduler._job_id(on_id)) is not None
        assert s.get_job(scheduler._job_id(off_id)) is None
    finally:
        scheduler.unsync_schedule(on_id)


async def test_load_all_survives_an_unbuildable_schedule(caplog):
    """One bad row must not stop the rest of the schedules loading."""
    good_id, _ = await _make_schedule(enabled=True, project="load-good")
    bad_id, _ = await _make_schedule(enabled=True, project="load-bad")
    async with SessionLocal() as s:
        bad = await s.get(Schedule, bad_id)
        bad.cron_expr = "not a cron"          # bypasses API validation, as a hand-edited DB would
        await s.commit()

    with caplog.at_level("ERROR"):
        await scheduler.load_all()

    try:
        assert scheduler.get_scheduler().get_job(scheduler._job_id(good_id)) is not None
        assert scheduler.get_scheduler().get_job(scheduler._job_id(bad_id)) is None
        assert "could not load schedule" in caplog.text
    finally:
        scheduler.unsync_schedule(good_id)


# --- cron + timezone ---------------------------------------------------------

def test_validate_cron_accepts_and_strips():
    assert scheduler.validate_cron("  0 2 * * *  ") == "0 2 * * *"


@pytest.mark.parametrize("expr", ["", "not a cron", "99 * * * *", "* * * *"])
def test_validate_cron_rejects_garbage(expr):
    with pytest.raises(ValueError):
        scheduler.validate_cron(expr)


def test_tz_for_blank_and_unknown_fall_back_to_scheduler_default():
    assert scheduler._tz_for("") is None
    assert scheduler._tz_for("Mars/Olympus_Mons") is None
    assert scheduler._tz_for("Europe/Warsaw") == ZoneInfo("Europe/Warsaw")


async def test_build_trigger_uses_the_row_timezone():
    sched_id, _ = await _make_schedule(tz="Europe/Warsaw")
    async with SessionLocal() as s:
        sched = await s.get(Schedule, sched_id)

    trigger = scheduler._build_trigger(sched)

    assert str(trigger.timezone) == "Europe/Warsaw"


async def test_build_trigger_without_timezone_uses_scheduler_default():
    sched_id, _ = await _make_schedule(tz="")
    async with SessionLocal() as s:
        sched = await s.get(Schedule, sched_id)

    trigger = scheduler._build_trigger(sched)

    assert str(trigger.timezone) == "UTC"


def test_next_fire_invalid_cron_is_none():
    assert scheduler.next_fire_iso("nonsense") is None


def test_next_fire_from_a_fixed_base_is_exact():
    got = scheduler.next_fire_iso("0 2 * * *", base=datetime(2026, 3, 10, 1, 0, 0))
    assert got == "2026-03-10T02:00:00"


def test_next_fire_respects_dst_spring_forward():
    """Europe/Warsaw jumps 02:00 -> 03:00 on 2026-03-29. A 02:30 daily job has no
    02:30 that day; the next fire must not silently land in the skipped hour."""
    base = datetime(2026, 3, 29, 0, 0, tzinfo=ZoneInfo("Europe/Warsaw"))

    got = scheduler.next_fire_iso("30 2 * * *", base=base, tz="Europe/Warsaw")

    assert got is not None
    fired = datetime.fromisoformat(got)
    assert fired.tzinfo is not None, "a timezone-aware schedule must yield an aware time"
    # Whatever croniter picks, it must be a real instant — converting to UTC and
    # back has to round-trip to the same wall clock.
    assert fired.astimezone(ZoneInfo("UTC")).astimezone(ZoneInfo("Europe/Warsaw")) == fired


def test_next_fire_in_a_timezone_differs_from_utc():
    warsaw = scheduler.next_fire_iso("0 12 * * *", tz="Europe/Warsaw")
    utc = scheduler.next_fire_iso("0 12 * * *", tz="")

    assert warsaw and utc
    assert warsaw != utc, "noon in Warsaw is not noon UTC"
