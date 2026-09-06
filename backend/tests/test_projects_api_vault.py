"""API-layer tests for the Ansible Vault endpoints of `projects.py`.

Nothing is mocked: `ansible-vault` ships in the image, so encrypt/decrypt/view
run for real and the assertions are about the bytes that end up on disk. That
matters more here than anywhere else in the API — these routes are the ones that
turn a user's file into ciphertext and back, and a test built on a stub would
prove only that the stub was called.

The credential-resolution helper gets its own coverage because its failure modes
are the ones a user actually hits: a credential id that no longer exists, a
credential of the wrong kind, and one whose secret file went missing.

Requires: git binary, GitPython, and ansible-vault (skipped otherwise).
"""
from __future__ import annotations

import shutil

import pytest

pytest.importorskip("aiosqlite")
pytest.importorskip("cryptography")
pytest.importorskip("git")
if shutil.which("git") is None:
    pytest.skip("git binary not available", allow_module_level=True)
if shutil.which("ansible-vault") is None:
    pytest.skip("ansible-vault not available", allow_module_level=True)

from fastapi import HTTPException

from app.api.projects import (
    VaultFileIn,
    VaultStringIn,
    vault_decrypt,
    vault_encrypt,
    vault_encrypt_string,
    vault_status,
    vault_view,
)
from app.core import credentials as cred_store
from app.core import storage, vault
from app.models.db import Credential, Project, SessionLocal, init_db

SECRET_TEXT = "db_password: hunter2\napi_token: abc123\n"


@pytest.fixture()
async def project_id():
    await init_db()
    paths = storage.create_project("VaultApiTest")
    pid = paths.project_id
    async with SessionLocal() as s:
        if await s.get(Project, pid) is None:
            s.add(Project(id=pid, name="VaultApiTest"))
            await s.commit()
    storage.write_file(pid, "group_vars/secrets.yml", SECRET_TEXT, message="test")
    try:
        yield pid
    finally:
        async with SessionLocal() as s:
            proj = await s.get(Project, pid)
            if proj:
                await s.delete(proj)
                await s.commit()
        storage.delete_project(pid)


async def _make_credential(kind: str, secret: str | None) -> int:
    async with SessionLocal() as s:
        c = Credential(kind=kind, name=f"test-{kind}")
        s.add(c)
        await s.commit()
        await s.refresh(c)
        cid = c.id
    if secret is not None:
        cred_store.write_secret(cid, secret)
    return cid


async def _drop_credential(cid: int) -> None:
    cred_store.delete_secret(cid)
    async with SessionLocal() as s:
        c = await s.get(Credential, cid)
        if c:
            await s.delete(c)
            await s.commit()


@pytest.fixture()
async def vault_cred():
    cid = await _make_credential("vault_password", "correct horse battery staple")
    try:
        yield cid
    finally:
        await _drop_credential(cid)


@pytest.fixture()
async def other_vault_cred():
    """A second, different password — for the wrong-password path."""
    cid = await _make_credential("vault_password", "a completely different one")
    try:
        yield cid
    finally:
        await _drop_credential(cid)


SECRETS = "group_vars/secrets.yml"


# ---------- status / file resolution ----------------------------------------

async def test_status_of_a_plaintext_file(project_id):
    assert (await vault_status(project_id, SECRETS))["encrypted"] is False


async def test_status_of_a_missing_file_is_404(project_id):
    with pytest.raises(HTTPException) as exc:
        await vault_status(project_id, "group_vars/nope.yml")
    assert exc.value.status_code == 404


async def test_status_refuses_a_path_outside_the_project(project_id):
    with pytest.raises(HTTPException) as exc:
        await vault_status(project_id, "../../etc/passwd")
    assert exc.value.status_code == 404


async def test_status_on_unknown_project_is_404():
    with pytest.raises(HTTPException) as exc:
        await vault_status("no-such-project-id", SECRETS)
    assert exc.value.status_code == 404


# ---------- credential resolution -------------------------------------------

async def test_a_missing_credential_is_404(project_id):
    with pytest.raises(HTTPException) as exc:
        await vault_encrypt(project_id, VaultFileIn(path=SECRETS, credential_id=999_999))
    assert exc.value.status_code == 404


async def test_a_credential_of_the_wrong_kind_is_400(project_id):
    cid = await _make_credential("ssh_key", "-----BEGIN OPENSSH PRIVATE KEY-----\n")
    try:
        with pytest.raises(HTTPException) as exc:
            await vault_encrypt(project_id, VaultFileIn(path=SECRETS, credential_id=cid))
        assert exc.value.status_code == 400
        assert "not a vault_password" in exc.value.detail
    finally:
        await _drop_credential(cid)


async def test_a_credential_with_no_secret_on_disk_is_400(project_id):
    """The DB row can outlive its secret file — a restore that missed
    credentials/, for instance."""
    cid = await _make_credential("vault_password", None)
    try:
        with pytest.raises(HTTPException) as exc:
            await vault_encrypt(project_id, VaultFileIn(path=SECRETS, credential_id=cid))
        assert exc.value.status_code == 400
    finally:
        await _drop_credential(cid)


# ---------- encrypt / decrypt round trip ------------------------------------

async def test_encrypt_rewrites_the_file_as_ciphertext(project_id, vault_cred):
    result = await vault_encrypt(project_id, VaultFileIn(path=SECRETS, credential_id=vault_cred))
    assert result["encrypted"] is True

    on_disk = (storage.paths_for(project_id).root / SECRETS).read_text()
    assert on_disk.startswith("$ANSIBLE_VAULT")
    assert "hunter2" not in on_disk
    assert (await vault_status(project_id, SECRETS))["encrypted"] is True


async def test_encrypting_twice_is_409(project_id, vault_cred):
    await vault_encrypt(project_id, VaultFileIn(path=SECRETS, credential_id=vault_cred))
    with pytest.raises(HTTPException) as exc:
        await vault_encrypt(project_id, VaultFileIn(path=SECRETS, credential_id=vault_cred))
    assert exc.value.status_code == 409


async def test_decrypt_restores_the_original_bytes(project_id, vault_cred):
    await vault_encrypt(project_id, VaultFileIn(path=SECRETS, credential_id=vault_cred))
    result = await vault_decrypt(project_id, VaultFileIn(path=SECRETS, credential_id=vault_cred))

    assert result["encrypted"] is False
    assert (storage.paths_for(project_id).root / SECRETS).read_text() == SECRET_TEXT


async def test_decrypting_a_plaintext_file_is_409(project_id, vault_cred):
    with pytest.raises(HTTPException) as exc:
        await vault_decrypt(project_id, VaultFileIn(path=SECRETS, credential_id=vault_cred))
    assert exc.value.status_code == 409


async def test_decrypt_with_the_wrong_password_is_400(project_id, vault_cred, other_vault_cred):
    await vault_encrypt(project_id, VaultFileIn(path=SECRETS, credential_id=vault_cred))
    with pytest.raises(HTTPException) as exc:
        await vault_decrypt(project_id, VaultFileIn(path=SECRETS, credential_id=other_vault_cred))
    assert exc.value.status_code == 400

    # The file must survive a failed attempt intact.
    assert vault.is_vault_encrypted((storage.paths_for(project_id).root / SECRETS).read_text())


# ---------- view -------------------------------------------------------------

async def test_view_returns_plaintext_without_touching_the_file(project_id, vault_cred):
    await vault_encrypt(project_id, VaultFileIn(path=SECRETS, credential_id=vault_cred))
    result = await vault_view(project_id, VaultFileIn(path=SECRETS, credential_id=vault_cred))

    assert result["plaintext"] == SECRET_TEXT
    # Viewing is read-only: the file on disk stays encrypted.
    assert (storage.paths_for(project_id).root / SECRETS).read_text().startswith("$ANSIBLE_VAULT")


async def test_view_with_the_wrong_password_is_400(project_id, vault_cred, other_vault_cred):
    await vault_encrypt(project_id, VaultFileIn(path=SECRETS, credential_id=vault_cred))
    with pytest.raises(HTTPException) as exc:
        await vault_view(project_id, VaultFileIn(path=SECRETS, credential_id=other_vault_cred))
    assert exc.value.status_code == 400


# ---------- encrypt-string ---------------------------------------------------

async def test_encrypt_string_produces_an_inline_vault_block(project_id, vault_cred):
    result = await vault_encrypt_string(
        project_id, VaultStringIn(name="db_password", value="hunter2", credential_id=vault_cred)
    )
    assert "!vault" in result["block"]
    assert "db_password" in result["block"]
    assert "hunter2" not in result["block"]


async def test_encrypt_string_requires_a_name(project_id, vault_cred):
    with pytest.raises(HTTPException) as exc:
        await vault_encrypt_string(
            project_id, VaultStringIn(name="   ", value="x", credential_id=vault_cred)
        )
    assert exc.value.status_code == 400
