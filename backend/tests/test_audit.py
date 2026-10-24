"""Audit log: the repudiation trail for agent actions and secret reads.

Covers the core record/query roundtrip, the admin-only authz rule, and the two
hook points (agent steps, credential resolution) that feed it.
"""
from __future__ import annotations

import pytest

pytest.importorskip("aiosqlite")
pytest.importorskip("cryptography")

from app.core import audit
from app.core.authz import required_capability
from app.models.db import init_db


async def _fresh_db():
    await init_db()


async def test_record_and_recent_roundtrip():
    await _fresh_db()
    await audit.record("secret.vault_view", "proj:group_vars/all.yml", user_id=7, detail="x")
    rows = await audit.recent(limit=5, action="secret.vault_view")
    assert rows, "recorded event not returned"
    top = rows[0]
    assert top["action"] == "secret.vault_view"
    assert top["target"] == "proj:group_vars/all.yml"
    assert top["user_id"] == 7
    assert top["ok"] is True


async def test_recent_filters_by_action():
    await _fresh_db()
    await audit.record("agent.write_file", "proj/site.yml", user_id=1)
    await audit.record("secret.vault_view", "proj:secrets.yml", user_id=1)
    only = await audit.recent(limit=50, action="agent.write_file")
    assert all(r["action"] == "agent.write_file" for r in only)
    assert any(r["target"] == "proj/site.yml" for r in only)


async def test_record_never_raises_on_bad_backend(monkeypatch):
    """A DB failure must not propagate out of the audited action."""
    import app.core.audit as audit_mod

    class _Boom:
        def __call__(self, *a, **k):
            raise RuntimeError("db down")
    monkeypatch.setattr(audit_mod, "SessionLocal", _Boom())
    # Must not raise.
    await audit.record("agent.delete", "proj/x", user_id=1)


def test_audit_endpoint_is_admin_only():
    assert required_capability("GET", "/api/audit") == "admin"


async def test_audit_agent_steps_records_only_state_changing_tools():
    await _fresh_db()
    from app.api.ai import _audit_agent_steps

    result = {"steps": [
        {"tool": "read_file", "args": {"path": "a.yml"}, "observation": {"content": "x"}},
        {"tool": "write_file", "args": {"path": "playbooks/site.yml"}, "observation": {"saved": "ok"}},
        {"tool": "run_playbook", "args": {"playbook": "site.yml"}, "observation": {"error": "boom"}},
    ]}
    await _audit_agent_steps("projA", result, request=None)

    rows = await audit.recent(limit=50)
    actions = {(r["action"], r["ok"]) for r in rows}
    assert ("agent.write_file", True) in actions
    assert ("agent.run_playbook", False) in actions          # failed step recorded as ok=False
    assert not any(r["action"] == "agent.read_file" for r in rows)  # read-only not audited


async def test_resolve_credentials_audits_secret_read():
    await _fresh_db()
    from app.api.runs import _resolve_credentials
    from app.core import credentials as cred_store
    from app.models.db import Credential, SessionLocal

    async with SessionLocal() as s:
        c = Credential(kind="ssh_password", name="prod-ssh")
        s.add(c)
        await s.commit()
        await s.refresh(c)
        cid = c.id
    cred_store.write_secret(cid, "hunter2")

    await _resolve_credentials([cid], user_id=42, project_id="projX")

    rows = await audit.recent(limit=50, action=audit.SECRET_RUN_CREDENTIALS)
    assert rows and rows[0]["user_id"] == 42
    assert rows[0]["target"] == "projX"
    assert "prod-ssh" in rows[0]["detail"]


async def test_recent_resolves_username():
    await _fresh_db()
    from app.models.db import SessionLocal, User

    async with SessionLocal() as s:
        u = User(username="auditor", password_hash="x", role="admin")
        s.add(u)
        await s.commit()
        await s.refresh(u)
        uid = u.id
    await audit.record("admin.user_create", "someone", user_id=uid, detail="role=viewer")
    rows = await audit.recent(limit=5, action="admin.user_create")
    assert rows[0]["username"] == "auditor"
    assert rows[0]["user_id"] == uid

    async with SessionLocal() as s:
        await s.delete(await s.get(User, uid))
        await s.commit()


async def test_retention_prunes_to_the_cap(monkeypatch):
    await _fresh_db()
    import app.core.audit as audit_mod
    from sqlalchemy import func, select
    from app.models.db import AuditEvent, SessionLocal

    monkeypatch.setattr(audit_mod, "_MAX_ROWS", 3)
    monkeypatch.setattr(audit_mod, "_PRUNE_EVERY", 1)  # prune on every insert
    for i in range(8):
        await audit.record("agent.write_file", f"f{i}.yml")

    async with SessionLocal() as s:
        total = (await s.execute(select(func.count()).select_from(AuditEvent))).scalar_one()
    assert total <= 3, f"retention cap not enforced: {total} rows"


async def test_update_config_audits_the_change():
    await _fresh_db()
    from app.api.ai import update_config, AIConfigIn

    await update_config(AIConfigIn(provider="ollama"), request=None)
    rows = await audit.recent(limit=5, action=audit.ADMIN_AI_CONFIG)
    assert rows and rows[0]["target"] == "ai.config"
    assert "provider" in rows[0]["detail"]


async def test_user_create_and_delete_are_audited():
    await _fresh_db()
    from app.api.users import create_user, delete_user, UserIn
    from tests.conftest import delete_all_users

    await delete_all_users()
    try:
        created = await create_user(UserIn(username="tempauditee", password="pw123456", role="viewer"),
                                    request=None)
        rows = await audit.recent(limit=5, action=audit.ADMIN_USER_CREATE)
        assert rows[0]["target"] == "tempauditee" and "viewer" in rows[0]["detail"]

        await delete_user(created.id, request=None)
        drows = await audit.recent(limit=5, action=audit.ADMIN_USER_DELETE)
        assert drows[0]["target"] == "tempauditee"
    finally:
        await delete_all_users()
