"""Wrap the `ansible-vault` CLI to encrypt/decrypt files and strings in a project.

The vault password comes from the caller (resolved from a `vault_password`
Credential). We never persist it: each operation writes the password to a 0600
temp file, passes it via `--vault-password-file`, and unlinks it in a finally.
The secret value for `encrypt_string` is fed on stdin, so it never appears in the
process argument list.

Callers pass absolute `Path`s; the API layer validates them against the project
sandbox (`storage._resolve_safe`) before we get here. All calls are blocking
subprocess IO — wrap them in a threadpool from async routes.
"""
from __future__ import annotations

import os
import subprocess
import tempfile
from contextlib import contextmanager
from pathlib import Path

VAULT_HEADER = "$ANSIBLE_VAULT"


class VaultError(RuntimeError):
    """ansible-vault failed: wrong password, file not encrypted, binary missing, ..."""


def is_vault_encrypted(text: str) -> bool:
    """True if `text` is an ansible-vault payload (starts with the magic header)."""
    return text.lstrip().startswith(VAULT_HEADER)


@contextmanager
def _password_file(password: str):
    fd, path = tempfile.mkstemp(prefix="vault-pass-")
    try:
        os.write(fd, (password or "").encode("utf-8"))
        os.close(fd)
        os.chmod(path, 0o600)
        yield path
    finally:
        try:
            os.unlink(path)
        except OSError:
            pass


def _run(args: list[str], password: str, *, stdin: str | None = None) -> str:
    if not password:
        raise VaultError("vault password is empty")
    with _password_file(password) as pf:
        cmd = ["ansible-vault", *args, "--vault-password-file", pf]
        try:
            proc = subprocess.run(cmd, input=stdin, capture_output=True, text=True, timeout=30)
        except FileNotFoundError as e:
            raise VaultError("ansible-vault not found on PATH") from e
        except subprocess.TimeoutExpired as e:
            raise VaultError("ansible-vault timed out") from e
    if proc.returncode != 0:
        raise VaultError((proc.stderr or proc.stdout or "ansible-vault failed").strip())
    return proc.stdout


def encrypt_file(path: Path, password: str) -> None:
    """Encrypt a file in place. No-op-safe to call only on plaintext files."""
    _run(["encrypt", str(path)], password)


def decrypt_file(path: Path, password: str) -> None:
    """Decrypt a file in place, leaving plaintext on disk."""
    _run(["decrypt", str(path)], password)


def view_file(path: Path, password: str) -> str:
    """Return decrypted plaintext WITHOUT modifying the file on disk."""
    return _run(["view", str(path)], password)


def encrypt_string(name: str, value: str, password: str) -> str:
    """Return a `name: !vault |` YAML block for an inline secret in group_vars.

    The secret is passed on stdin (`--stdin-name`) so it never lands in argv.
    """
    out = _run(["encrypt_string", "--stdin-name", name], password, stdin=value)
    return out.rstrip("\n") + "\n"
