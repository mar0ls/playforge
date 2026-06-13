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
from app.api.runs import RunIn, PreflightIn, start_run, list_runs, run_detail, preflight, _build_request
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


async def test_run_detail_by_id(_seeded):
    out = await start_run(RunIn(project_id="pA", playbook="site.yml"))
    d = await run_detail(out["run_id"])
    assert d["id"] == out["run_id"]
    assert d["project_id"] == "pA"
    assert d["playbook"] == "site.yml"
    assert d["status"] in ("ok", "successful")


async def test_run_detail_missing_404(_seeded):
    with pytest.raises(HTTPException) as exc:
        await run_detail(999999)
    assert exc.value.status_code == 404


async def test_preview_returns_diagnostics_on_known_error(monkeypatch, _seeded):
    async def _fake_failed(req, on_event=None, cancel_event=None):
        return RunResult(
            status="failed",
            rc=2,
            stats={"failures": {"localhost": 1}},
            failures=[{
                "host": "localhost",
                "task": "Gathering Facts",
                "result": {"msg": "python3-apt must be installed to use check mode"},
                "stderr": "",
            }],
            artifacts_dir="/tmp/none",
        )

    monkeypatch.setattr(runs_api, "run_playbook", _fake_failed)
    out = await runs_api.preview_run(RunIn(project_id="pA", playbook="site.yml", check=True))
    codes = {d["code"] for d in out.get("diagnostics") or []}
    assert "missing_python3_apt" in codes


async def test_preflight_controller_missing_required(monkeypatch, _seeded):
    # Pretend every binary is missing and no target probing is requested.
    monkeypatch.setattr(runs_api.shutil, "which", lambda _name: None)

    result = await preflight(PreflightIn(project_id="pA", include_targets=False, check=True))
    assert result["ok"] is False
    assert result["controller"]["ok"] is False
    missing = {m["name"] for m in result["controller"]["missing_required"]}
    # Only true controller-must-haves are flagged. `sudo` runs on the target,
    # `apt` likewise; they are informational, not blockers.
    assert "ansible" in missing
    assert "ansible-playbook" in missing
    assert "sudo" not in missing
    assert "apt" not in missing


async def test_preflight_passes_when_optional_tools_missing(monkeypatch, _seeded):
    """A slim controller image without sudo/python3-apt should still preflight clean
    when ansible + ansible-playbook exist (typical for remote-only playbooks)."""
    def _which(name):
        return f"/usr/bin/{name}" if name in {"ansible", "ansible-playbook"} else None
    monkeypatch.setattr(runs_api.shutil, "which", _which)
    monkeypatch.setattr(runs_api.importlib.util, "find_spec", lambda _name: None)

    result = await preflight(PreflightIn(project_id="pA", include_targets=False, check=True))
    assert result["controller"]["ok"] is True
    assert result["ok"] is True


@pytest.mark.parametrize("msg, expected_code", [
    ("sudo: not found", "missing_sudo"),
    ("iptables v1.8.7: Permission denied (you must be root)", "firewall_permission_denied"),
    ("Failed to connect to the host via ssh: ssh: connect to host 10.0.0.5 port 22: Connection refused",
     "host_unreachable"),
    ("Permission denied (publickey,password).", "ssh_auth_failed"),
    ("ssh: Could not resolve hostname db1: Name or service not known", "dns_resolution_failed"),
    ("write error: No space left on device", "disk_full"),
    ("E: Unable to locate package supadupa", "package_not_found"),
    ("couldn't resolve module/action 'community.general.thing'. This often indicates a misspelling, missing collection, or incorrect module path.",
     "missing_collection"),
    ("Missing sudo password", "become_password_required"),
    ("Attempting to decrypt but no vault secrets found", "vault_password_required"),
])
async def test_diagnose_failure_rules(msg, expected_code):
    """Each common Ansible failure signature lights its diagnostic code."""
    out = runs_api._diagnose_failures(
        [{"host": "h1", "task": "Some task", "result": {"msg": msg}, "stderr": ""}],
        check_mode=False,
    )
    codes = {d["code"] for d in out}
    assert expected_code in codes, f"got {codes}"


async def test_diagnose_failures_deduplicates_same_code_per_host_task():
    f = {"host": "h1", "task": "T", "result": {"msg": "Missing sudo password. Missing sudo password"}}
    out = runs_api._diagnose_failures([f], check_mode=False)
    assert len([d for d in out if d["code"] == "become_password_required"]) == 1


async def test_run_ws_summary_includes_diagnostics(_seeded):
    """The WebSocket summary contract carries the same `diagnostics` field as HTTP."""
    from starlette.testclient import TestClient
    from app.main import app

    with TestClient(app) as client:
        with client.websocket_connect("/api/runs/ws") as ws:
            ws.send_json({"project_id": "pA", "playbook": "site.yml"})
            summary = None
            for _ in range(200):
                ev = ws.receive_json()
                if ev.get("event") == "summary":
                    summary = ev
                    break
                if ev.get("event") == "error":
                    raise AssertionError(f"ws errored: {ev}")
            else:
                raise AssertionError("timed out waiting for ws summary event")
    assert summary is not None
    assert "diagnostics" in summary
    assert isinstance(summary["diagnostics"], list)
    assert "run_id" in summary
