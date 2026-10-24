"""Account management. Admin-only — see core/authz, which gates `/api/users`.

The password never travels back out: there is no field for it on the way out and
no endpoint that returns one.
"""
from __future__ import annotations

from datetime import datetime

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel

from app.core import users as users_core
from app.core import audit

router = APIRouter(prefix="/api/users", tags=["users"])


class UserOut(BaseModel):
    id: int
    username: str
    role: str
    disabled: bool
    created_at: datetime
    last_login_at: datetime | None


class UserIn(BaseModel):
    username: str
    password: str
    role: str = users_core.VIEWER


class UserUpdate(BaseModel):
    role: str | None = None
    disabled: bool | None = None
    password: str | None = None


def _out(u) -> UserOut:
    return UserOut(id=u.id, username=u.username, role=u.role, disabled=u.disabled,
                   created_at=u.created_at, last_login_at=u.last_login_at)


def _actor_id(request: Request | None) -> int | None:
    user = getattr(getattr(request, "state", None), "user", None)
    return getattr(user, "id", None)


@router.get("", response_model=list[UserOut])
async def list_users():
    return [_out(u) for u in await users_core.list_users()]


@router.post("", response_model=UserOut, status_code=201)
async def create_user(payload: UserIn, request: Request = None):  # type: ignore[assignment]
    try:
        user = await users_core.create(payload.username, payload.password, payload.role)
    except users_core.UserError as e:
        raise HTTPException(400, str(e))
    await audit.record(audit.ADMIN_USER_CREATE, payload.username, user_id=_actor_id(request),
                       detail=f"role={payload.role}")
    return _out(user)


@router.patch("/{user_id}", response_model=UserOut)
async def update_user(user_id: int, payload: UserUpdate,
                      request: Request = None):  # type: ignore[assignment]
    """Change a role, enable/disable, or set a password.

    An admin can't disable or demote their own account: doing so mid-session
    leaves them unable to undo it, and the "last admin" guard in the store
    wouldn't catch it while another admin exists.
    """
    if await users_core.get(user_id) is None:
        raise HTTPException(404, "user not found")

    self_id = _actor_id(request)
    if self_id == user_id:
        if payload.disabled is True:
            raise HTTPException(400, "you cannot disable your own account")
        if payload.role is not None and payload.role != users_core.ADMIN:
            raise HTTPException(400, "you cannot remove your own admin role")

    try:
        if payload.role is not None:
            await users_core.set_role(user_id, payload.role)
        if payload.disabled is not None:
            await users_core.set_disabled(user_id, payload.disabled)
        if payload.password is not None:
            await users_core.set_password(user_id, payload.password)
    except users_core.UserError as e:
        raise HTTPException(400, str(e))

    updated = await users_core.get(user_id)
    # Record what actually changed — a role change, a disable, a password reset
    # are each a thing an admin may later need to account for.
    changed = [f for f, v in (("role", payload.role), ("disabled", payload.disabled),
                              ("password", "***" if payload.password is not None else None))
               if v is not None]
    await audit.record(audit.ADMIN_USER_UPDATE, getattr(updated, "username", str(user_id)),
                       user_id=self_id, detail=f"changed={changed}")
    return _out(updated)


@router.delete("/{user_id}")
async def delete_user(user_id: int,
                      request: Request = None):  # type: ignore[assignment]
    if _actor_id(request) == user_id:
        raise HTTPException(400, "you cannot delete your own account")
    victim = await users_core.get(user_id)
    try:
        await users_core.delete(user_id)
    except users_core.UserError as e:
        raise HTTPException(400 if "last admin" in str(e) else 404, str(e))
    await audit.record(audit.ADMIN_USER_DELETE, getattr(victim, "username", str(user_id)),
                       user_id=_actor_id(request))
    return {"deleted": user_id}
