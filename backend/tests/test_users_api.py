"""Account management endpoints.

Self-modification is the interesting part: the store's "last admin" guard doesn't
help when another admin exists, so an admin could otherwise lock themselves out
of their own session with one click.
"""
from __future__ import annotations

import pytest

pytest.importorskip("aiosqlite")
pytest.importorskip("cryptography")

from fastapi import HTTPException

from app.api.users import UserIn, UserUpdate, create_user, delete_user, list_users, update_user
from app.core import users as users_core
from app.models.db import SessionLocal, User, init_db


class _Req:
    """Stands in for the Request the middleware would have populated."""

    def __init__(self, user):
        self.state = type("S", (), {"user": user})()


@pytest.fixture(autouse=True)
async def _clean(clean_users):
    """See conftest: accounts are global state for the whole suite."""


async def _admin(name="boss"):
    return await users_core.create(name, "pw", users_core.ADMIN)


# --- create / list -----------------------------------------------------------

async def test_create_returns_the_account_without_a_password_field():
    out = await create_user(UserIn(username="viewer1", password="pw", role="viewer"))

    assert out.username == "viewer1"
    assert out.role == "viewer"
    assert "password" not in out.model_dump()


async def test_created_account_can_sign_in():
    await create_user(UserIn(username="opsguy", password="pw", role="operator"))
    assert await users_core.authenticate("opsguy", "pw") is not None


async def test_create_rejects_a_bad_role():
    with pytest.raises(HTTPException) as e:
        await create_user(UserIn(username="x1", password="pw", role="wizard"))
    assert e.value.status_code == 400


async def test_create_rejects_a_duplicate():
    await create_user(UserIn(username="dup", password="pw"))
    with pytest.raises(HTTPException) as e:
        await create_user(UserIn(username="dup", password="pw"))
    assert e.value.status_code == 400


async def test_list_never_exposes_a_hash():
    await _admin()
    rows = await list_users()
    dumped = " ".join(r.model_dump_json() for r in rows)
    assert "scrypt$" not in dumped


# --- update ------------------------------------------------------------------

async def test_promote_a_viewer_to_operator():
    admin = await _admin()
    target = await users_core.create("junior", "pw", users_core.VIEWER)

    out = await update_user(target.id, UserUpdate(role="operator"), _Req(admin))

    assert out.role == "operator"


async def test_disable_an_account_and_it_can_no_longer_sign_in():
    admin = await _admin()
    target = await users_core.create("leaver", "pw", users_core.OPERATOR)

    await update_user(target.id, UserUpdate(disabled=True), _Req(admin))

    assert await users_core.authenticate("leaver", "pw") is None


async def test_reset_a_password():
    admin = await _admin()
    target = await users_core.create("forgetful", "old", users_core.VIEWER)

    await update_user(target.id, UserUpdate(password="new"), _Req(admin))

    assert await users_core.authenticate("forgetful", "old") is None
    assert await users_core.authenticate("forgetful", "new") is not None


async def test_update_missing_user_is_404():
    admin = await _admin()
    with pytest.raises(HTTPException) as e:
        await update_user(999999, UserUpdate(role="viewer"), _Req(admin))
    assert e.value.status_code == 404


# --- self-modification guards ------------------------------------------------

async def test_admin_cannot_disable_themselves():
    admin = await _admin()
    await _admin("deputy")  # another admin exists, so the store's guard won't fire

    with pytest.raises(HTTPException) as e:
        await update_user(admin.id, UserUpdate(disabled=True), _Req(admin))

    assert e.value.status_code == 400
    assert "your own account" in e.value.detail


async def test_admin_cannot_demote_themselves():
    admin = await _admin()
    await _admin("deputy")

    with pytest.raises(HTTPException) as e:
        await update_user(admin.id, UserUpdate(role="viewer"), _Req(admin))

    assert e.value.status_code == 400


async def test_admin_cannot_delete_themselves():
    admin = await _admin()
    await _admin("deputy")

    with pytest.raises(HTTPException) as e:
        await delete_user(admin.id, _Req(admin))

    assert e.value.status_code == 400


async def test_admin_can_still_change_their_own_password():
    admin = await _admin()

    await update_user(admin.id, UserUpdate(password="rotated"), _Req(admin))

    assert await users_core.authenticate("boss", "rotated") is not None


# --- delete ------------------------------------------------------------------

async def test_delete_removes_the_account():
    admin = await _admin()
    target = await users_core.create("temp", "pw", users_core.VIEWER)

    await delete_user(target.id, _Req(admin))

    assert await users_core.get(target.id) is None


async def test_cannot_delete_the_last_admin():
    admin = await _admin()
    # Deleted by someone else, so the self-delete guard isn't what stops it.
    other = await users_core.create("bystander", "pw", users_core.VIEWER)

    with pytest.raises(HTTPException) as e:
        await delete_user(admin.id, _Req(other))

    assert e.value.status_code == 400
    assert "last admin" in e.value.detail
