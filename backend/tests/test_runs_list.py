"""Tests for the global run-history list endpoint (filtering + project join)."""
from __future__ import annotations

import pytest

pytest.importorskip("aiosqlite")
pytest.importorskip("cryptography")

from app.api.runs import list_runs
from app.models.db import Project, Run, SessionLocal, init_db


@pytest.fixture(autouse=True)
async def _seeded_db():
    await init_db()
    async with SessionLocal() as s:
        if await s.get(Project, "pX") is None:
            s.add(Project(id="pX", name="ProjX"))
            s.add(Project(id="pY", name="ProjY"))
            s.add(Run(project_id="pX", playbook="site.yml", status="ok", tags="deploy"))
            s.add(Run(project_id="pX", playbook="deploy.yml", status="failed"))
            s.add(Run(project_id="pY", playbook="site.yml", status="running"))
            await s.commit()
    yield


async def test_filter_by_project():
    rows = await list_runs(project_id="pX")
    assert rows and all(r["project_id"] == "pX" for r in rows)
    assert {r["playbook"] for r in rows} == {"site.yml", "deploy.yml"}


async def test_filter_by_status():
    rows = await list_runs(status="failed")
    assert rows and all(r["status"] == "failed" for r in rows)


async def test_filter_by_playbook_contains():
    rows = await list_runs(playbook="site")
    assert rows and all("site" in r["playbook"] for r in rows)


async def test_includes_project_name():
    rows = await list_runs(project_id="pY")
    assert rows[0]["project_name"] == "ProjY"


async def test_combined_filters():
    rows = await list_runs(project_id="pX", status="ok")
    assert len(rows) == 1
    assert rows[0]["playbook"] == "site.yml"
