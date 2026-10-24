"""Read-only access to the audit log. Admin-only — see core/authz."""
from __future__ import annotations

from fastapi import APIRouter

from app.core import audit

router = APIRouter(prefix="/api/audit", tags=["audit"])


@router.get("")
async def list_audit(limit: int = 100, action: str | None = None, user_id: int | None = None):
    """Most recent audit events first. Filter by `action` or `user_id` if given."""
    return await audit.recent(limit=limit, action=action, user_id=user_id)
