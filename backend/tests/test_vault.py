"""Tests for the ansible-vault wrapper.

`is_vault_encrypted` is pure and always runs. The round-trip tests shell out to
the real `ansible-vault` binary, so they're skipped where it isn't installed
(they run in the Docker image, which has ansible-core).
"""
from __future__ import annotations

import shutil

import pytest

from app.core import vault
from app.core.vault import VaultError, is_vault_encrypted

_HAS_VAULT = shutil.which("ansible-vault") is not None
requires_vault = pytest.mark.skipif(not _HAS_VAULT, reason="ansible-vault not installed")


# --- pure header detection --------------------------------------------------

def test_is_vault_encrypted_true():
    assert is_vault_encrypted("$ANSIBLE_VAULT;1.1;AES256\n3839...") is True


def test_is_vault_encrypted_tolerates_leading_whitespace():
    assert is_vault_encrypted("\n  $ANSIBLE_VAULT;1.1;AES256\n") is True


def test_is_vault_encrypted_false_for_plaintext():
    assert is_vault_encrypted("key: value\n") is False


def test_empty_password_rejected():
    with pytest.raises(VaultError):
        vault.encrypt_file(__import__("pathlib").Path("/tmp/x"), "")


# --- real round-trips -------------------------------------------------------

@requires_vault
def test_encrypt_decrypt_roundtrip(tmp_path):
    f = tmp_path / "secret.yml"
    f.write_text("db_password: hunter2\n")

    vault.encrypt_file(f, "pw123")
    assert is_vault_encrypted(f.read_text())
    assert "hunter2" not in f.read_text()

    vault.decrypt_file(f, "pw123")
    assert f.read_text() == "db_password: hunter2\n"


@requires_vault
def test_view_does_not_modify_file(tmp_path):
    f = tmp_path / "secret.yml"
    f.write_text("token: abc\n")
    vault.encrypt_file(f, "pw")
    ciphertext = f.read_text()

    plaintext = vault.view_file(f, "pw")
    assert plaintext.strip() == "token: abc"
    assert f.read_text() == ciphertext  # unchanged on disk


@requires_vault
def test_wrong_password_raises(tmp_path):
    f = tmp_path / "secret.yml"
    f.write_text("a: b\n")
    vault.encrypt_file(f, "right")
    with pytest.raises(VaultError):
        vault.decrypt_file(f, "wrong")


@requires_vault
def test_encrypt_string_block(tmp_path):
    block = vault.encrypt_string("api_key", "s3cr3t", "pw")
    assert block.startswith("api_key: !vault |")
    assert "$ANSIBLE_VAULT" in block
    assert "s3cr3t" not in block
