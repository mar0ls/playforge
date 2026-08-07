"""Run template CRUD.

Templates are what schedules point at and what runs inherit their defaults from,
so a template that is wrong or missing surfaces as a run that misbehaves rather
than as an error anyone sees.
"""
from __future__ import annotations

import json

import pytest

pytest.importorskip("aiosqlite")

from fastapi import HTTPException

from app.api.templates import (
    TemplateIn, create_template, delete_template, list_templates, update_template,
)
from app.models.db import Project, RunTemplate, Schedule, SessionLocal, init_db

PROJECT = "tpl-proj"
OTHER = "tpl-other"


@pytest.fixture(autouse=True)
async def _projects():
    await init_db()
    from app.core.config import settings
    from sqlalchemy import select
    for pid in (PROJECT, OTHER):
        (settings.projects_dir / pid).mkdir(parents=True, exist_ok=True)
    async with SessionLocal() as s:
        for pid in (PROJECT, OTHER):
            if await s.get(Project, pid) is None:
                s.add(Project(id=pid, name=pid))
        await s.commit()
    yield
    async with SessionLocal() as s:
        for row in (await s.execute(select(Schedule))).scalars().all():
            await s.delete(row)
        for row in (await s.execute(select(RunTemplate))).scalars().all():
            await s.delete(row)
        await s.commit()


def _payload(**over):
    base = dict(name="deploy", playbook="playbooks/site.yml")
    base.update(over)
    return TemplateIn(**base)


# --- create ------------------------------------------------------------------

async def test_create_returns_the_template():
    out = await create_template(PROJECT, _payload())
    assert out.name == "deploy"
    assert out.playbook == "playbooks/site.yml"


async def test_create_rejects_an_unknown_project():
    with pytest.raises(HTTPException) as e:
        await create_template("no-such-project", _payload())
    assert e.value.status_code == 404


async def test_list_and_skip_tags_round_trip():
    """They are stored comma-joined; a bad split shows up as a run with the
    wrong tags, which is silent."""
    out = await create_template(PROJECT, _payload(tags=["web", "db"], skip_tags=["slow"]))

    assert out.tags == ["web", "db"]
    assert out.skip_tags == ["slow"]


async def test_empty_tag_lists_stay_empty():
    """`"".split(",")` yields `[""]`, which would become a bogus `--tags ` argument."""
    out = await create_template(PROJECT, _payload(tags=[], skip_tags=[]))

    assert out.tags == []
    assert out.skip_tags == []


async def test_extra_vars_round_trip():
    out = await create_template(PROJECT, _payload(extra_vars={"env": "prod", "n": 3}))
    assert out.extra_vars == {"env": "prod", "n": 3}


async def test_credential_ids_round_trip():
    out = await create_template(PROJECT, _payload(credential_ids=[2, 5]))
    assert out.credential_ids == [2, 5]


async def test_flags_round_trip():
    out = await create_template(PROJECT, _payload(check=True, syntax_check=True, limit="web01"))
    assert out.check is True
    assert out.syntax_check is True
    assert out.limit == "web01"


# --- read --------------------------------------------------------------------

async def test_list_is_scoped_to_the_project():
    """Templates from another project must not leak into this one's Run form."""
    await create_template(PROJECT, _payload(name="mine"))
    await create_template(OTHER, _payload(name="theirs"))

    names = [t.name for t in await list_templates(PROJECT)]

    assert names == ["mine"]


async def test_list_is_ordered_by_name():
    await create_template(PROJECT, _payload(name="zeta"))
    await create_template(PROJECT, _payload(name="alpha"))

    names = [t.name for t in await list_templates(PROJECT)]

    assert names == sorted(names)


# --- update ------------------------------------------------------------------

async def test_update_replaces_the_fields():
    out = await create_template(PROJECT, _payload(tags=["old"]))

    updated = await update_template(PROJECT, out.id,
                                    _payload(name="renamed", tags=["new"], limit="db01"))

    assert updated.name == "renamed"
    assert updated.tags == ["new"]
    assert updated.limit == "db01"


async def test_update_can_clear_a_list():
    out = await create_template(PROJECT, _payload(tags=["web"]))

    updated = await update_template(PROJECT, out.id, _payload(tags=[]))

    assert updated.tags == []


async def test_update_of_a_missing_template_is_404():
    with pytest.raises(HTTPException) as e:
        await update_template(PROJECT, 999999, _payload())
    assert e.value.status_code == 404


async def test_cannot_update_a_template_through_another_project():
    """The project id in the path is an authorisation boundary, not decoration."""
    out = await create_template(OTHER, _payload(name="theirs"))

    with pytest.raises(HTTPException) as e:
        await update_template(PROJECT, out.id, _payload(name="hijacked"))

    assert e.value.status_code == 404


# --- delete ------------------------------------------------------------------

async def test_delete_removes_the_template():
    out = await create_template(PROJECT, _payload())

    await delete_template(PROJECT, out.id)

    assert await list_templates(PROJECT) == []


async def test_delete_of_a_missing_template_is_404():
    with pytest.raises(HTTPException) as e:
        await delete_template(PROJECT, 999999)
    assert e.value.status_code == 404


async def test_cannot_delete_a_template_through_another_project():
    out = await create_template(OTHER, _payload())

    with pytest.raises(HTTPException) as e:
        await delete_template(PROJECT, out.id)

    assert e.value.status_code == 404
    assert await list_templates(OTHER) != []


async def test_delete_is_refused_while_a_schedule_uses_it():
    """Otherwise the schedule survives pointing at nothing, and the only symptom
    is that it stops running — at 2am, silently."""
    tpl = await create_template(PROJECT, _payload())
    async with SessionLocal() as s:
        s.add(Schedule(project_id=PROJECT, name="nightly", template_id=tpl.id,
                       cron_expr="0 2 * * *", timezone="", enabled=True))
        await s.commit()

    with pytest.raises(HTTPException) as e:
        await delete_template(PROJECT, tpl.id)

    assert e.value.status_code == 409
    assert "nightly" in e.value.detail
    assert await list_templates(PROJECT) != [], "the template must survive a refused delete"


async def test_delete_is_allowed_once_the_schedule_is_gone():
    tpl = await create_template(PROJECT, _payload())
    async with SessionLocal() as s:
        sched = Schedule(project_id=PROJECT, name="nightly", template_id=tpl.id,
                         cron_expr="0 2 * * *", timezone="", enabled=True)
        s.add(sched)
        await s.commit()
        await s.delete(sched)
        await s.commit()

    await delete_template(PROJECT, tpl.id)

    assert await list_templates(PROJECT) == []
