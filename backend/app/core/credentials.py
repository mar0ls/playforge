"""On-disk storage for credential secrets, encrypted at rest with Fernet.

Each credential's secret ends up at `<data_dir>/credentials/<id>.priv` containing
a Fernet token (URL-safe base64 of HMAC + AES-128-CBC). 0600 file perms still
matter — they keep unauthorized local readers from even attempting decryption.

**Master key resolution**, first match wins:
1. `ANSIBLE_GUI_MASTER_KEY` env var (a Fernet key — 32-byte url-safe base64).
   Use this in multi-host deploys or anywhere you don't want the key on disk.
2. `<data_dir>/master.key` — auto-generated on first start with 0600 perms.
   Logged loudly so the operator knows to back it up. Lose it = lose every
   credential, with no recovery.

**Legacy migration:** a `.priv` file written before encryption was enabled is
plaintext. `read_secret` detects that (Fernet token won't validate) and
re-writes it encrypted on the fly. Zero-touch upgrade.
"""
from __future__ import annotations

import logging
import os
from pathlib import Path

from cryptography.fernet import Fernet, InvalidToken

from app.core.config import settings


log = logging.getLogger("ansible_gui.credentials")
_KEY_FILENAME = "master.key"
_fernet: Fernet | None = None


def _master_key_path() -> Path:
    return settings.data_dir / _KEY_FILENAME


def _load_or_create_key() -> bytes:
    env_key = os.getenv("ANSIBLE_GUI_MASTER_KEY") or ""
    if env_key.strip():
        return env_key.strip().encode()

    keyfile = _master_key_path()
    if keyfile.is_file():
        return keyfile.read_bytes().strip()

    key = Fernet.generate_key()
    keyfile.write_bytes(key)
    keyfile.chmod(0o600)
    # nosemgrep: python-logger-credential-disclosure -- logs the key file PATH, never the key.
    log.warning(
        "Generated a new credential master key at %s. BACK THIS UP — "
        "deleting or losing it means every stored credential becomes unreadable. "
        "Set ANSIBLE_GUI_MASTER_KEY in docker-compose.yml to keep the key out of the data volume.",
        keyfile,
    )
    return key


def get_fernet() -> Fernet:
    global _fernet
    if _fernet is None:
        _fernet = Fernet(_load_or_create_key())
    return _fernet


def master_key_source() -> str:
    """Where the active key came from — for the Settings page."""
    if os.getenv("ANSIBLE_GUI_MASTER_KEY"):
        return "env:ANSIBLE_GUI_MASTER_KEY"
    if _master_key_path().is_file():
        return f"file:{_master_key_path()}"
    return "unset"


def _path(credential_id: int) -> Path:
    return settings.credentials_dir / f"{credential_id}.priv"


def write_secret(credential_id: int, secret: str) -> Path:
    """Encrypt and persist a secret. Trailing newline is added if missing so tools
    that expect text files (ssh, vault) don't choke."""
    if not secret.endswith("\n"):
        secret = secret + "\n"
    token = get_fernet().encrypt(secret.encode("utf-8"))
    p = _path(credential_id)
    p.write_bytes(token)
    p.chmod(0o600)
    return p


def read_secret(credential_id: int) -> str | None:
    """Return the plaintext secret, or None if missing. Auto-migrates legacy
    plaintext files to encrypted form on first read."""
    p = _path(credential_id)
    if not p.is_file():
        return None
    data = p.read_bytes()
    try:
        return get_fernet().decrypt(data).decode("utf-8")
    except InvalidToken:
        # Legacy plaintext — pre-encryption format. Migrate.
        try:
            text = data.decode("utf-8")
        except UnicodeDecodeError:
            # nosemgrep: python-logger-credential-disclosure -- logs the numeric credential id, not the secret.
            log.error("credential %s appears corrupt (neither Fernet token nor valid UTF-8)", credential_id)
            return None
        # nosemgrep: python-logger-credential-disclosure -- logs the numeric credential id, not the secret.
        log.info("Migrating credential %s to encrypted-at-rest format", credential_id)
        write_secret(credential_id, text)
        return text


def secret_path(credential_id: int) -> Path:
    """Return the on-disk path. NOTE: it now points at an *encrypted* file —
    callers that pass this to subprocesses (ssh, ansible-playbook --private-key)
    must decrypt first via `read_secret` and write to a temp file themselves.
    Today's only caller (runner) reads the content and hands it to ansible-runner
    which writes its own ephemeral copy, so this is fine."""
    return _path(credential_id)


def delete_secret(credential_id: int) -> None:
    p = _path(credential_id)
    if p.exists():
        p.unlink()
