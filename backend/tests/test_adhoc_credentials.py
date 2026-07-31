"""Ad-hoc commands must carry the same credentials a normal run does.

Before this, `POST /api/runs/adhoc` took no credential ids at all, so ad-hoc
against a host needing key or sudo auth failed with "Permission denied" while a
playbook run in the same project succeeded.
"""
from __future__ import annotations

import pytest

pytest.importorskip("aiosqlite")
pytest.importorskip("cryptography")

from fastapi import HTTPException

from app.api import runs as runs_api
from app.api.runs import AdhocIn, adhoc, _resolve_credentials
from app.core import credentials as cred_store
from app.core.config import settings
from app.core.runner import RunResult
from app.models.db import Credential, Project, SessionLocal, init_db


@pytest.fixture(autouse=True)
async def _seeded():
    await init_db()
    (settings.projects_dir / "adhoc-proj").mkdir(parents=True, exist_ok=True)
    async with SessionLocal() as s:
        if await s.get(Project, "adhoc-proj") is None:
            s.add(Project(id="adhoc-proj", name="Adhoc"))
            await s.commit()


async def _make_credential(kind: str, secret: str) -> int:
    async with SessionLocal() as s:
        c = Credential(name=f"{kind}-cred", kind=kind)
        s.add(c)
        await s.commit()
        await s.refresh(c)
        cred_store.write_secret(c.id, secret)
        return c.id


@pytest.fixture
def captured(monkeypatch):
    """Replace run_adhoc, recording the kwargs the endpoint hands it."""
    calls: list[dict] = []

    async def fake_run_adhoc(project_id, host_pattern, module, args="", inventory="", **kw):
        calls.append({"project_id": project_id, "host_pattern": host_pattern,
                      "module": module, "args": args, "inventory": inventory, **kw})
        return RunResult(status="successful", rc=0, stats={}, failures=[], artifacts_dir="")

    monkeypatch.setattr(runs_api, "run_adhoc", fake_run_adhoc)
    return calls


async def test_ssh_key_reaches_the_runner(captured):
    cid = await _make_credential("ssh_key", "-----BEGIN PRIVATE KEY-----\nabc\n")

    await adhoc(AdhocIn(project_id="adhoc-proj", credential_ids=[cid]))

    assert captured[0]["ssh_key_content"] == "-----BEGIN PRIVATE KEY-----\nabc\n"


async def test_become_password_reaches_the_runner(captured):
    cid = await _make_credential("become_password", "sudo-secret\n")

    await adhoc(AdhocIn(project_id="adhoc-proj", module="shell", args="whoami",
                        credential_ids=[cid]))

    # Trailing newline is stripped — it would otherwise end up in the password file.
    assert captured[0]["become_password_content"] == "sudo-secret"


async def test_ssh_password_reaches_the_runner(captured):
    cid = await _make_credential("ssh_password", "hunter2\n")

    await adhoc(AdhocIn(project_id="adhoc-proj", credential_ids=[cid]))

    assert captured[0]["ssh_password_content"] == "hunter2"


async def test_no_credentials_passes_empty_strings(captured):
    await adhoc(AdhocIn(project_id="adhoc-proj"))

    assert captured[0]["ssh_key_content"] == ""
    assert captured[0]["become_password_content"] == ""
    assert captured[0]["ssh_password_content"] == ""


async def test_unknown_project_is_404(captured):
    with pytest.raises(HTTPException) as e:
        await adhoc(AdhocIn(project_id="does-not-exist"))

    assert e.value.status_code == 404
    assert captured == [], "must not invoke ansible for a project that isn't there"


async def test_resolve_credentials_picks_one_per_kind():
    first = await _make_credential("ssh_key", "key-one")
    await _make_credential("ssh_key", "key-two")

    out = await _resolve_credentials([first])
    # Keys keep the trailing newline write_secret adds — ssh rejects a key without one.
    assert out["ssh_key_content"] == "key-one\n"

    assert await _resolve_credentials([]) == {}


async def test_resolve_credentials_handles_multiple_kinds_at_once():
    key = await _make_credential("ssh_key", "the-key")
    become = await _make_credential("become_password", "the-sudo-pass\n")

    out = await _resolve_credentials([key, become])

    assert out["ssh_key_content"] == "the-key\n"   # key: newline kept
    assert out["become_password_content"] == "the-sudo-pass"  # password: stripped
