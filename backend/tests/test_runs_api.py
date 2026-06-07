"""API-layer tests for the runs endpoints.

Follows the repo convention of calling the async route functions directly (rather
than spinning up a TestClient/lifespan). `run_playbook` is monkeypatched so no
real Ansible subprocess runs — we only verify the HTTP-layer wiring: environment
attribution, template fallback, validation, and that history surfaces the env.
"""
from __future__ import annotations

import pytest

pytest.importorskip("aiosqlite")
pytest.importorskip("cryptography")

from fastapi import HTTPException

from app.api import runs as runs_api
from app.api.runs import RunIn, start_run, list_runs, _build_request
from app.core.runner import RunResult
from app.models.db import (
    Environment, Project, RunTemplate, Run, SessionLocal, init_db,
)


@pytest.fixture(autouse=True)
async def _seeded(monkeypatch):
    await init_db()
    # _build_request now asserts the project exists on disk (so we 404 a typo'd
    # project_id instead of silently writing a 'failed' Run row). Tests historically
    # only seeded the DB, so we also drop empty dirs for pA/pB here.
    from app.core.config import settings as _cfg
    for pid in ("pA", "pB"):
        (_cfg.projects_dir / pid).mkdir(parents=True, exist_ok=True)
    async with SessionLocal() as s:
        if await s.get(Project, "pA") is None:
            s.add(Project(id="pA", name="ProjA"))
            s.add(Project(id="pB", name="ProjB"))
            await s.commit()
        # Environment in project pA.
        env = Environment(project_id="pA", name="production",
                          inventory_path="inventories/production")
        s.add(env)
        await s.commit()
        await s.refresh(env)
        env_id = env.id

    # Stub the runner so no real playbook executes.
    async def _fake_run_playbook(req, on_event=None, cancel_event=None):
        return RunResult(status="successful", rc=0,
                         stats={"ok": {"localhost": 1}}, failures=[],
                         artifacts_dir="/tmp/none")

    monkeypatch.setattr(runs_api, "run_playbook", _fake_run_playbook)
    return {"env_id": env_id}


async def _latest_run_id() -> int:
    async with SessionLocal() as s:
        from sqlalchemy import select
        return (await s.execute(select(Run.id).order_by(Run.id.desc()))).scalars().first()


async def test_start_run_persists_environment_id(_seeded):
    env_id = _seeded["env_id"]
    out = await start_run(RunIn(project_id="pA", playbook="site.yml", environment_id=env_id))
    assert out["run_id"]
    async with SessionLocal() as s:
        run = await s.get(Run, out["run_id"])
        assert run.environment_id == env_id


async def test_start_run_without_environment_is_null(_seeded):
    out = await start_run(RunIn(project_id="pA", playbook="site.yml"))
    async with SessionLocal() as s:
        run = await s.get(Run, out["run_id"])
        assert run.environment_id is None


async def test_environment_from_other_project_rejected(_seeded):
    env_id = _seeded["env_id"]  # belongs to pA
    with pytest.raises(HTTPException) as exc:
        await start_run(RunIn(project_id="pB", playbook="site.yml", environment_id=env_id))
    assert exc.value.status_code == 400


async def test_unknown_environment_rejected(_seeded):
    with pytest.raises(HTTPException) as exc:
        await start_run(RunIn(project_id="pA", playbook="site.yml", environment_id=999999))
    assert exc.value.status_code == 404


async def test_environment_id_falls_back_to_template(_seeded):
    env_id = _seeded["env_id"]
    async with SessionLocal() as s:
        tpl = RunTemplate(project_id="pA", name="deploy", playbook="site.yml",
                          environment_id=env_id)
        s.add(tpl)
        await s.commit()
        await s.refresh(tpl)
        tpl_id = tpl.id

    # No explicit environment_id → inherits the template's.
    req, resolved = await _build_request(RunIn(project_id="pA", template_id=tpl_id))
    assert resolved == env_id


async def test_explicit_environment_overrides_template(_seeded):
    env_id = _seeded["env_id"]
    async with SessionLocal() as s:
        env2 = Environment(project_id="pA", name="staging")
        s.add(env2)
        tpl = RunTemplate(project_id="pA", name="deploy2", playbook="site.yml",
                          environment_id=env_id)
        s.add(tpl)
        await s.commit()
        await s.refresh(env2)
        await s.refresh(tpl)
        env2_id, tpl_id = env2.id, tpl.id

    req, resolved = await _build_request(
        RunIn(project_id="pA", template_id=tpl_id, environment_id=env2_id))
    assert resolved == env2_id


async def test_playbook_required(_seeded):
    with pytest.raises(HTTPException) as exc:
        await start_run(RunIn(project_id="pA"))
    assert exc.value.status_code == 400


async def test_list_runs_surfaces_environment(_seeded):
    env_id = _seeded["env_id"]
    out = await start_run(RunIn(project_id="pA", playbook="site.yml", environment_id=env_id))
    rows = await list_runs(project_id="pA")
    row = next(r for r in rows if r["id"] == out["run_id"])
    assert row["environment_id"] == env_id
    assert row["environment_name"] == "production"


async def test_list_runs_null_environment(_seeded):
    out = await start_run(RunIn(project_id="pA", playbook="site.yml"))
    rows = await list_runs(project_id="pA")
    row = next(r for r in rows if r["id"] == out["run_id"])
    assert row["environment_id"] is None
    assert row["environment_name"] is None
