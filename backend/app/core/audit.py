"""Append-only audit log for sensitive actions.

The repudiation half of the threat model: `SECURITY.md` records that `Run.user_id`
attributes a run, but the AI agent's file edits / playbook runs and reads of
decrypted credential material left no trail of their own. This module writes one
`AuditEvent` row per such action, attributed to the acting user when there is one.

`record()` is best-effort by design: an audit-backend failure (disk full, DB
locked) must never turn a successful action into an error for the user. But it is
not silently swallowed either — a failure is logged, so a gap in the trail is
visible in the container log rather than invisible. (bladedr learned this the hard
way: a discarded `AppendAudit` left no trace an outage had happened.)
"""
from __future__ import annotations

import logging

from sqlalchemy import delete, select

from app.models.db import AuditEvent, SessionLocal, User

log = logging.getLogger(__name__)

# Actions worth recording, as stable string constants so call sites and queries
# agree. `agent.*` is filled in dynamically from the tool name.
SECRET_VAULT_VIEW = "secret.vault_view"
SECRET_VAULT_PASSWORD = "secret.vault_password"
SECRET_RUN_CREDENTIALS = "secret.run_credentials"
ADMIN_USER_CREATE = "admin.user_create"
ADMIN_USER_UPDATE = "admin.user_update"
ADMIN_USER_DELETE = "admin.user_delete"
ADMIN_AI_CONFIG = "admin.ai_config"

_MAX_DETAIL = 480

# The log is append-only and the agent can generate rows quickly, so cap it: the
# app runs on one SQLite file with no external log store, and an unbounded audit
# table is its own availability problem. We keep the newest _MAX_ROWS and prune
# opportunistically (not on every insert — once per _PRUNE_EVERY rows, keyed off
# the autoincrement id so it costs one extra DELETE very rarely).
_MAX_ROWS = 50_000
_PRUNE_EVERY = 1_000


async def record(action: str, target: str = "", *, user_id: int | None = None,
                 detail: str = "", ok: bool = True) -> None:
    """Write one audit row. Never raises: a logging failure is logged, not propagated."""
    try:
        async with SessionLocal() as session:
            event = AuditEvent(
                action=action, target=(target or "")[:512], user_id=user_id,
                detail=(detail or "")[:_MAX_DETAIL], ok=ok,
            )
            session.add(event)
            await session.commit()
            # Opportunistic retention: roughly once per _PRUNE_EVERY rows, drop
            # everything older than the newest _MAX_ROWS by id.
            if event.id and event.id % _PRUNE_EVERY == 0:
                cutoff = event.id - _MAX_ROWS
                if cutoff > 0:
                    await session.execute(delete(AuditEvent).where(AuditEvent.id <= cutoff))
                    await session.commit()
    except Exception as e:  # pragma: no cover - exercised only on a DB outage
        log.error("audit: failed to record %r on %r: %s", action, target, e)


async def recent(limit: int = 100, *, action: str | None = None,
                 user_id: int | None = None) -> list[dict]:
    """Most recent audit events first, optionally filtered by action/user.

    Each row carries the resolved `username` (or None) so the UI doesn't have to
    cross-reference the users API — which it couldn't for an account since deleted.
    """
    stmt = select(AuditEvent).order_by(AuditEvent.at.desc()).limit(max(1, min(limit, 1000)))
    if action:
        stmt = stmt.where(AuditEvent.action == action)
    if user_id is not None:
        stmt = stmt.where(AuditEvent.user_id == user_id)
    async with SessionLocal() as session:
        rows = (await session.execute(stmt)).scalars().all()
        ids = {r.user_id for r in rows if r.user_id is not None}
        names: dict[int, str] = {}
        if ids:
            for u in (await session.execute(select(User).where(User.id.in_(ids)))).scalars().all():
                names[u.id] = u.username
    return [
        {"id": r.id, "at": r.at.isoformat() if r.at else None, "user_id": r.user_id,
         "username": names.get(r.user_id) if r.user_id is not None else None,
         "action": r.action, "target": r.target, "detail": r.detail, "ok": r.ok}
        for r in rows
    ]
