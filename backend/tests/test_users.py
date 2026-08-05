"""User accounts: password hashing, roles, and the store's invariants."""
from __future__ import annotations

import pytest

pytest.importorskip("aiosqlite")
pytest.importorskip("cryptography")

from app.core import users
from app.core.users import ADMIN, OPERATOR, VIEWER
from app.models.db import SessionLocal, User, init_db


@pytest.fixture(autouse=True)
async def _db():
    await init_db()
    async with SessionLocal() as s:
        for u in (await s.execute(__import__("sqlalchemy").select(User))).scalars().all():
            await s.delete(u)
        await s.commit()


# --- hashing -----------------------------------------------------------------

def test_hash_is_not_the_password():
    encoded = users.hash_password("hunter2")
    assert "hunter2" not in encoded


def test_hash_verifies():
    encoded = users.hash_password("hunter2")
    assert users.verify_password("hunter2", encoded) is True


def test_hash_rejects_the_wrong_password():
    encoded = users.hash_password("hunter2")
    assert users.verify_password("hunter3", encoded) is False


def test_same_password_hashes_differently():
    """Per-password salt: identical passwords must not produce identical rows."""
    assert users.hash_password("same") != users.hash_password("same")


def test_hash_carries_its_parameters():
    """Cost lives in the hash so it can be raised without invalidating passwords."""
    encoded = users.hash_password("x")
    scheme, n, r, p, _salt, _dk = encoded.split("$")
    assert scheme == "scrypt"
    assert (int(n), int(r), int(p)) == (users._SCRYPT_N, users._SCRYPT_R, users._SCRYPT_P)


def test_an_old_cost_still_verifies():
    """The whole point of storing parameters: a hash made at a lower cost keeps working."""
    import base64
    import os
    salt = os.urandom(16)
    dk = users._derive("legacy", salt, 1 << 14, 8, 1)
    encoded = "scrypt$%d$8$1$%s$%s" % (
        1 << 14, base64.b64encode(salt).decode(), base64.b64encode(dk).decode())

    assert users.verify_password("legacy", encoded) is True
    assert users.needs_rehash(encoded) is True


def test_current_hash_does_not_need_rehash():
    assert users.needs_rehash(users.hash_password("x")) is False


@pytest.mark.parametrize("junk", ["", "not-a-hash", "scrypt$$$$", "scrypt$a$b$c$d$e",
                                  "md5$1$1$1$AAAA$AAAA"])
def test_malformed_hashes_fail_closed(junk):
    """A corrupted row must fail the login, not raise."""
    assert users.verify_password("anything", junk) is False


def test_absurd_parameters_are_refused_not_allocated():
    """A hostile stored value must not get to request a huge allocation."""
    import base64
    salt = base64.b64encode(b"x" * 16).decode()
    dk = base64.b64encode(b"y" * 32).decode()
    assert users.verify_password("x", f"scrypt${1 << 30}$8$1${salt}${dk}") is False


def test_empty_password_is_refused():
    with pytest.raises(ValueError):
        users.hash_password("")


# --- roles -------------------------------------------------------------------

def test_viewer_can_only_read():
    assert users.can(VIEWER, "read") is True
    for denied in ("run", "write", "secrets", "admin"):
        assert users.can(VIEWER, denied) is False


def test_operator_can_run_and_write_but_not_secrets_or_admin():
    for allowed in ("read", "run", "write"):
        assert users.can(OPERATOR, allowed) is True
    assert users.can(OPERATOR, "secrets") is False
    assert users.can(OPERATOR, "admin") is False


def test_admin_can_do_everything():
    for cap in ("read", "run", "write", "secrets", "admin"):
        assert users.can(ADMIN, cap) is True


def test_unknown_role_grants_nothing():
    assert users.can("wizard", "read") is False
    assert users.valid_role("wizard") is False


# --- usernames ---------------------------------------------------------------

@pytest.mark.parametrize("raw,expected", [("  Admin ", "admin"), ("MiXeD", "mixed")])
def test_usernames_are_normalised(raw, expected):
    assert users.normalize_username(raw) == expected


@pytest.mark.parametrize("name", ["ab", "a1", "marcin", "some.user", "a-b_c"])
def test_valid_usernames(name):
    assert users.valid_username(name) is True


@pytest.mark.parametrize("name", ["", "a", "-starts-with-dash", "has space",
                                  "UPPER", "x" * 33, "with@sign"])
def test_invalid_usernames(name):
    assert users.valid_username(name) is False


# --- store -------------------------------------------------------------------

async def test_no_users_means_single_password_mode():
    assert await users.count() == 0
    assert await users.multi_user_enabled() is False


async def test_create_then_authenticate():
    await users.create("marcin", "s3cret", ADMIN)

    assert await users.multi_user_enabled() is True
    user = await users.authenticate("marcin", "s3cret")
    assert user is not None and user.role == ADMIN


async def test_authenticate_is_case_insensitive_on_username():
    await users.create("marcin", "s3cret", ADMIN)
    assert await users.authenticate("MARCIN", "s3cret") is not None


async def test_authenticate_rejects_a_wrong_password():
    await users.create("marcin", "s3cret", ADMIN)
    assert await users.authenticate("marcin", "wrong") is None


async def test_authenticate_on_a_missing_user_is_none():
    assert await users.authenticate("ghost", "whatever") is None


async def test_disabled_user_cannot_authenticate():
    u = await users.create("temp", "pw", OPERATOR)
    await users.create("boss", "pw", ADMIN)
    await users.set_disabled(u.id, True)

    assert await users.authenticate("temp", "pw") is None


async def test_authenticate_records_last_login():
    u = await users.create("marcin", "s3cret", ADMIN)
    assert u.last_login_at is None

    after = await users.authenticate("marcin", "s3cret")

    assert after.last_login_at is not None


async def test_duplicate_username_is_refused():
    await users.create("marcin", "pw", ADMIN)
    with pytest.raises(users.UserError):
        await users.create("Marcin", "other", VIEWER)


@pytest.mark.parametrize("bad", ["", "a", "has space", "with@sign", "-leading-dash"])
async def test_create_validates_the_username(bad):
    with pytest.raises(users.UserError):
        await users.create(bad, "pw", VIEWER)


async def test_create_normalises_before_validating():
    """`create` lowercases first, so a capitalised name is accepted and stored
    lowercase rather than rejected."""
    u = await users.create("MarCin", "pw", VIEWER)

    assert u.username == "marcin"
    assert await users.authenticate("marcin", "pw") is not None


async def test_create_validates_the_role():
    with pytest.raises(users.UserError):
        await users.create("someone", "pw", "wizard")


async def test_create_requires_a_password():
    with pytest.raises(users.UserError):
        await users.create("someone", "", VIEWER)


async def test_set_password_changes_the_login():
    u = await users.create("marcin", "old", ADMIN)

    await users.set_password(u.id, "new")

    assert await users.authenticate("marcin", "old") is None
    assert await users.authenticate("marcin", "new") is not None


async def test_password_is_never_stored_in_clear():
    await users.create("marcin", "plaintext-check", ADMIN)
    async with SessionLocal() as s:
        row = (await s.execute(
            __import__("sqlalchemy").select(User).where(User.username == "marcin")
        )).scalar_one()
    assert "plaintext-check" not in row.password_hash


# --- last-admin protection ---------------------------------------------------

async def test_cannot_demote_the_last_admin():
    u = await users.create("boss", "pw", ADMIN)
    with pytest.raises(users.UserError, match="last admin"):
        await users.set_role(u.id, VIEWER)


async def test_cannot_delete_the_last_admin():
    u = await users.create("boss", "pw", ADMIN)
    with pytest.raises(users.UserError, match="last admin"):
        await users.delete(u.id)


async def test_cannot_disable_the_last_admin():
    u = await users.create("boss", "pw", ADMIN)
    with pytest.raises(users.UserError, match="last admin"):
        await users.set_disabled(u.id, True)


async def test_can_demote_an_admin_when_another_remains():
    a = await users.create("boss", "pw", ADMIN)
    await users.create("deputy", "pw", ADMIN)

    await users.set_role(a.id, VIEWER)

    assert (await users.authenticate("boss", "pw")).role == VIEWER


async def test_a_disabled_admin_does_not_count_as_cover():
    """Disabling one admin must not then allow disabling the only active one."""
    a = await users.create("boss", "pw", ADMIN)
    b = await users.create("deputy", "pw", ADMIN)
    await users.set_disabled(b.id, True)

    with pytest.raises(users.UserError, match="last admin"):
        await users.set_disabled(a.id, True)


async def test_list_is_ordered_by_username():
    await users.create("zoe", "pw", VIEWER)
    await users.create("adam", "pw", ADMIN)

    names = [u.username for u in await users.list_users()]

    assert names == sorted(names)
