"""Tests for api/dashboard.py — stats aggregation, recent-runs join, system info."""
from __future__ import annotations

from datetime import datetime

import pytest

pytest.importorskip("aiosqlite")
pytest.importorskip("cryptography")

from app.api import dashboard
from app.models.db import Project, Run, SessionLocal, init_db


async def _seed():
    await init_db()
    async with SessionLocal() as s:
        p = Project(id="dash" + datetime.utcnow().strftime("%H%M%S%f")[:12], name="dash")
        s.add(p)
        await s.commit()
        for status in ("ok", "ok", "failed"):
            s.add(Run(project_id=p.id, playbook="site.yml", status=status,
                      started_at=datetime.utcnow()))
        await s.commit()
        return p.id


async def test_stats_counts_runs_by_status():
    pid = await _seed()
    out = await dashboard.stats()
    assert out["projects"] >= 1
    assert out["runs_total"] >= 3
    assert out["runs_ok"] >= 2 and out["runs_failed"] >= 1
    # internal consistency
    assert out["runs_total"] >= out["runs_ok"] + out["runs_failed"]
    assert pid  # seeded


async def test_recent_runs_joins_the_project_name_and_honours_limit():
    await _seed()
    rows = await dashboard.recent_runs(limit=2)
    assert len(rows) <= 2
    assert rows, "expected at least one recent run"
    top = rows[0]
    assert top["project_name"] == "dash"
    assert top["status"] in ("ok", "failed")
    assert set(top) >= {"id", "project_id", "playbook", "status", "started_at"}


def test_system_reports_data_dir_and_key_source():
    out = dashboard.system()
    assert out["credentials_encrypted"] is True
    assert out["master_key_source"].startswith(("env:", "file:", "unset"))
    assert out["data_dir"]
    assert "ansible" in out
