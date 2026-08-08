"""Schedule CRUD.

The scheduler core is well covered; this is the layer that writes the rows it
loads, and it also owns keeping APScheduler in sync with the database. A schedule
that exists in one and not the other either never fires or fires after it was
deleted.
"""
from __future__ import annotations

import pytest

pytest.importorskip("aiosqlite")
pytest.importorskip("apscheduler")

from fastapi import HTTPException

from app.api.schedules import (
    ScheduleIn, create_schedule, delete_schedule, list_schedules, update_schedule,
)
from app.core import scheduler as sched_core
from app.models.db import Project, RunTemplate, Schedule, SessionLocal, init_db

PROJECT = "sched-api-proj"


@pytest.fixture(autouse=True)
async def _seeded():
    await init_db()
    from app.core.config import settings
    (settings.projects_dir / PROJECT).mkdir(parents=True, exist_ok=True)
    async with SessionLocal() as s:
        if await s.get(Project, PROJECT) is None:
            s.add(Project(id=PROJECT, name="Sched API"))
            await s.commit()
        tpl = RunTemplate(project_id=PROJECT, name="nightly-tpl", playbook="site.yml",
                          inventory="hosts.ini", tags="", skip_tags="", limit="")
        s.add(tpl)
        await s.commit()
        await s.refresh(tpl)
        template_id = tpl.id
    yield template_id
    # Leave no APScheduler jobs behind for the next test.
    async with SessionLocal() as s:
        from sqlalchemy import select
        for row in (await s.execute(select(Schedule))).scalars().all():
            sched_core.unsync_schedule(row.id)
            await s.delete(row)
        await s.commit()


def _payload(tpl, **over):
    """Default payload; `over` wins, including over `template_id`."""
    base = dict(name="nightly", project_id=PROJECT, template_id=tpl,
                cron_expr="0 2 * * *", enabled=True, timezone="")
    base.update(over)
    return ScheduleIn(**base)


def _job(schedule_id):
    return sched_core.get_scheduler().get_job(sched_core._job_id(schedule_id))


# --- create ------------------------------------------------------------------

async def test_create_returns_the_schedule(_seeded):
    out = await create_schedule(_payload(_seeded))

    assert out.name == "nightly"
    assert out.cron_expr == "0 2 * * *"
    assert out.enabled is True


async def test_create_registers_the_job(_seeded):
    """A row without a job never fires."""
    out = await create_schedule(_payload(_seeded))
    assert _job(out.id) is not None


async def test_creating_disabled_registers_no_job(_seeded):
    out = await create_schedule(_payload(_seeded, enabled=False))
    assert _job(out.id) is None


async def test_create_normalises_the_cron(_seeded):
    out = await create_schedule(_payload(_seeded, cron_expr="  0 2 * * *  "))
    assert out.cron_expr == "0 2 * * *"


@pytest.mark.parametrize("bad", ["", "not a cron", "99 * * * *", "* * * *"])
async def test_create_rejects_a_bad_cron(_seeded, bad):
    with pytest.raises(HTTPException) as e:
        await create_schedule(_payload(_seeded, cron_expr=bad))
    assert e.value.status_code == 400


async def test_create_rejects_an_unknown_timezone(_seeded):
    with pytest.raises(HTTPException) as e:
        await create_schedule(_payload(_seeded, timezone="Mars/Olympus_Mons"))
    assert e.value.status_code == 400


async def test_create_accepts_an_iana_timezone(_seeded):
    out = await create_schedule(_payload(_seeded, timezone="Europe/Warsaw"))
    assert out.timezone == "Europe/Warsaw"


async def test_create_rejects_an_unknown_project(_seeded):
    with pytest.raises(HTTPException) as e:
        await create_schedule(_payload(_seeded, project_id="no-such-project"))
    assert e.value.status_code == 404


async def test_create_rejects_an_unknown_template(_seeded):
    with pytest.raises(HTTPException) as e:
        await create_schedule(_payload(_seeded, template_id=999999))
    assert e.value.status_code == 404


async def test_a_rejected_create_leaves_no_row(_seeded):
    before = len(await list_schedules())
    with pytest.raises(HTTPException):
        await create_schedule(_payload(_seeded, template_id=999999))
    assert len(await list_schedules()) == before


# --- read --------------------------------------------------------------------

async def test_list_reports_the_next_fire_time(_seeded):
    await create_schedule(_payload(_seeded))

    rows = await list_schedules()

    assert rows[0].next_fire_at is not None


async def test_a_disabled_schedule_has_no_next_fire_time(_seeded):
    """It isn't going to fire, so showing a time would be a lie."""
    await create_schedule(_payload(_seeded, enabled=False))

    rows = await list_schedules()

    assert rows[0].next_fire_at is None


async def test_next_fire_time_follows_the_timezone(_seeded):
    await create_schedule(_payload(_seeded, name="utc", cron_expr="0 12 * * *"))
    await create_schedule(_payload(_seeded, name="warsaw", cron_expr="0 12 * * *",
                                   timezone="Europe/Warsaw"))

    rows = {r.name: r.next_fire_at for r in await list_schedules()}

    assert rows["utc"] != rows["warsaw"], "noon in Warsaw is not noon UTC"


# --- update ------------------------------------------------------------------

async def test_update_changes_the_cron_and_resyncs(_seeded):
    out = await create_schedule(_payload(_seeded))

    updated = await update_schedule(out.id, _payload(_seeded, cron_expr="30 4 * * *"))

    assert updated.cron_expr == "30 4 * * *"
    assert _job(out.id) is not None


async def test_disabling_via_update_removes_the_job(_seeded):
    """Otherwise a schedule switched off in the UI keeps firing until restart."""
    out = await create_schedule(_payload(_seeded))
    assert _job(out.id) is not None

    await update_schedule(out.id, _payload(_seeded, enabled=False))

    assert _job(out.id) is None


async def test_re_enabling_via_update_restores_the_job(_seeded):
    out = await create_schedule(_payload(_seeded, enabled=False))

    await update_schedule(out.id, _payload(_seeded, enabled=True))

    assert _job(out.id) is not None


async def test_update_rejects_a_bad_cron(_seeded):
    out = await create_schedule(_payload(_seeded))
    with pytest.raises(HTTPException) as e:
        await update_schedule(out.id, _payload(_seeded, cron_expr="nonsense"))
    assert e.value.status_code == 400


async def test_update_of_a_missing_schedule_is_404(_seeded):
    with pytest.raises(HTTPException) as e:
        await update_schedule(999999, _payload(_seeded))
    assert e.value.status_code == 404


async def test_update_rejects_an_unknown_template(_seeded):
    """create refuses a template that doesn't exist; update must too, or a
    working schedule can be pointed at nothing and only fail at fire time."""
    out = await create_schedule(_payload(_seeded))

    with pytest.raises(HTTPException) as e:
        await update_schedule(out.id, _payload(_seeded, template_id=999999))

    assert e.value.status_code == 404


async def test_update_rejects_an_unknown_project(_seeded):
    out = await create_schedule(_payload(_seeded))

    with pytest.raises(HTTPException) as e:
        await update_schedule(out.id, _payload(_seeded, project_id="no-such-project"))

    assert e.value.status_code == 404


# --- delete ------------------------------------------------------------------

async def test_delete_removes_the_row_and_the_job(_seeded):
    out = await create_schedule(_payload(_seeded))

    await delete_schedule(out.id)

    assert _job(out.id) is None, "a deleted schedule that keeps its job still fires"
    assert await list_schedules() == []


async def test_delete_of_a_missing_schedule_is_404(_seeded):
    with pytest.raises(HTTPException) as e:
        await delete_schedule(999999)
    assert e.value.status_code == 404
