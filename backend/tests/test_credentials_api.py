"""Credentials CRUD: the encrypted secret must follow the row through its life.

The API layer here was largely untested, and it's the one that decides whether a
secret is written, rotated or left behind on disk after a delete.
"""
from __future__ import annotations

import pytest

pytest.importorskip("aiosqlite")
pytest.importorskip("cryptography")

from fastapi import HTTPException

from app.api.credentials import (
    CredIn, CredUpdate, create_credential, delete_credential,
    list_credentials, update_credential,
)
from app.core import credentials as cred_store
from app.models.db import Credential, SessionLocal, init_db


@pytest.fixture(autouse=True)
async def _db():
    await init_db()


async def _create(kind="ssh_key", name="k", secret="the-secret", **kw):
    return await create_credential(CredIn(kind=kind, name=name, secret=secret, **kw))


# --- create ------------------------------------------------------------------

async def test_create_stores_an_encrypted_secret():
    out = await _create(secret="my-private-key")

    assert out.has_secret is True
    assert cred_store.read_secret(out.id) == "my-private-key\n"

    # On disk it must not be readable as plaintext.
    raw = cred_store._path(out.id).read_bytes()
    assert b"my-private-key" not in raw


async def test_create_rejects_unknown_kind():
    with pytest.raises(HTTPException) as e:
        await _create(kind="not-a-kind")
    assert e.value.status_code == 400


@pytest.mark.parametrize("secret", ["", "   ", "\n"])
async def test_create_rejects_blank_secret(secret):
    with pytest.raises(HTTPException) as e:
        await _create(secret=secret)
    assert e.value.status_code == 400


async def test_create_does_not_leave_a_row_behind_for_a_bad_kind():
    before = len(await list_credentials())
    with pytest.raises(HTTPException):
        await _create(kind="bogus")
    assert len(await list_credentials()) == before


# --- read --------------------------------------------------------------------

async def test_list_never_returns_the_secret():
    await _create(name="listed", secret="top-secret")

    rows = await list_credentials()

    assert any(r.name == "listed" for r in rows)
    dumped = " ".join(r.model_dump_json() for r in rows)
    assert "top-secret" not in dumped, "the plaintext secret must never reach the API surface"


async def test_list_is_ordered_by_name():
    await _create(name="zeta")
    await _create(name="alpha")

    names = [r.name for r in await list_credentials()]

    assert names == sorted(names)


# --- update ------------------------------------------------------------------

async def test_update_rotates_the_secret():
    out = await _create(secret="old-key")

    await update_credential(out.id, CredUpdate(secret="new-key"))

    assert cred_store.read_secret(out.id) == "new-key\n"


async def test_update_without_a_secret_keeps_the_existing_one():
    """Renaming a credential must not silently wipe its key."""
    out = await _create(secret="keep-me")

    updated = await update_credential(out.id, CredUpdate(name="renamed"))

    assert updated.name == "renamed"
    assert updated.has_secret is True
    assert cred_store.read_secret(out.id) == "keep-me\n"


async def test_update_with_a_blank_secret_keeps_the_existing_one():
    """A form that submits an empty secret field means 'unchanged', not 'erase'."""
    out = await _create(secret="keep-me")

    await update_credential(out.id, CredUpdate(secret="   "))

    assert cred_store.read_secret(out.id) == "keep-me\n"


async def test_update_bumps_updated_at():
    out = await _create()
    before = out.updated_at

    after = await update_credential(out.id, CredUpdate(description="now documented"))

    assert after.updated_at >= before


async def test_update_missing_credential_is_404():
    with pytest.raises(HTTPException) as e:
        await update_credential(999999, CredUpdate(name="x"))
    assert e.value.status_code == 404


# --- delete ------------------------------------------------------------------

async def test_delete_removes_row_and_secret_file():
    out = await _create(secret="doomed")
    path = cred_store._path(out.id)
    assert path.exists()

    await delete_credential(out.id)

    assert not path.exists(), "the encrypted secret must not outlive the row"
    async with SessionLocal() as s:
        assert await s.get(Credential, out.id) is None


async def test_delete_missing_credential_is_404():
    with pytest.raises(HTTPException) as e:
        await delete_credential(999999)
    assert e.value.status_code == 404


async def test_has_secret_reflects_reality():
    out = await _create()
    cred_store.delete_secret(out.id)

    rows = {r.id: r for r in await list_credentials()}

    assert rows[out.id].has_secret is False
