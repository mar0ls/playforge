"""User accounts and password hashing.

Foundation for multi-user; nothing enforces roles yet (see the roadmap). This
module owns how a password becomes a stored string and how a role maps to what
someone may do.

Hashing is `hashlib.scrypt` — stdlib, so no new dependency and no lockfile churn,
and a memory-hard KDF rather than a bare hash. Two things measured rather than
assumed:

* OpenSSL's default `maxmem` rejects any N above 2^14, so it's passed explicitly.
  Without it `scrypt(n=2**16, ...)` raises "memory limit exceeded".
* Cost on the reference host (Docker 29.4.0, arm64): 2^14 = 34ms/16MB,
  2^16 = 105ms/64MB, 2^17 = 209ms/128MB.

N = 2^16. OWASP's headline recommendation is 2^17, but that allocates 128MB per
login attempt, and this app is routinely run on small self-hosted boxes where a
handful of concurrent logins would be felt. 2^16 keeps the memory-hardness while
halving that.

The parameters live inside the encoded hash, so raising them later doesn't
invalidate existing passwords — `needs_rehash` reports which stored hashes are
below the current cost.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import os
import re

# --- roles -------------------------------------------------------------------

ADMIN = "admin"
OPERATOR = "operator"
VIEWER = "viewer"

ROLES = (ADMIN, OPERATOR, VIEWER)

# What each role may do. Checked by name so a route can ask for a capability
# rather than hardcoding a role list, which is what makes adding a role later a
# local change instead of a sweep.
#
#   read      see projects, files, runs, history
#   run       execute playbooks and ad-hoc commands, use the agent's run tools
#   write     edit project files, templates, schedules, environments
#   secrets   view/'manage credentials and vault material
#   admin     manage users and app settings
CAPABILITIES: dict[str, frozenset[str]] = {
    VIEWER:   frozenset({"read"}),
    OPERATOR: frozenset({"read", "run", "write"}),
    ADMIN:    frozenset({"read", "run", "write", "secrets", "admin"}),
}


def can(role: str, capability: str) -> bool:
    """True if `role` grants `capability`. Unknown roles grant nothing."""
    return capability in CAPABILITIES.get(role, frozenset())


def valid_role(role: str) -> bool:
    return role in ROLES


# --- password hashing --------------------------------------------------------

_SCRYPT_N = 1 << 16
_SCRYPT_R = 8
_SCRYPT_P = 1
_DKLEN = 32
_SALT_BYTES = 16

_ENCODED = re.compile(r"^scrypt\$(\d+)\$(\d+)\$(\d+)\$([A-Za-z0-9+/=]+)\$([A-Za-z0-9+/=]+)$")


def _derive(password: str, salt: bytes, n: int, r: int, p: int) -> bytes:
    # maxmem must be explicit: OpenSSL's default caps out around 32MB and any
    # n >= 2**15 raises "memory limit exceeded" without it.
    return hashlib.scrypt(password.encode("utf-8"), salt=salt, n=n, r=r, p=p,
                          dklen=_DKLEN, maxmem=128 * n * r * 2)


def hash_password(password: str) -> str:
    """Encode as `scrypt$N$r$p$salt$dk`, all base64. Parameters travel with the
    hash so they can be raised later without invalidating stored passwords."""
    if not password:
        raise ValueError("password must not be empty")
    salt = os.urandom(_SALT_BYTES)
    dk = _derive(password, salt, _SCRYPT_N, _SCRYPT_R, _SCRYPT_P)
    return "scrypt${}${}${}${}${}".format(
        _SCRYPT_N, _SCRYPT_R, _SCRYPT_P,
        base64.b64encode(salt).decode(), base64.b64encode(dk).decode())


def verify_password(password: str, encoded: str) -> bool:
    """Constant-time check of `password` against a stored hash.

    Returns False rather than raising for anything malformed — a corrupted row
    must fail the login, not 500 the request.
    """
    if not password or not encoded:
        return False
    m = _ENCODED.match(encoded)
    if not m:
        return False
    try:
        n, r, p = int(m.group(1)), int(m.group(2)), int(m.group(3))
        salt = base64.b64decode(m.group(4))
        expected = base64.b64decode(m.group(5))
    except (ValueError, TypeError):
        return False
    # Guard against a hostile stored value asking for an absurd allocation.
    if not (0 < n <= (1 << 20)) or not (0 < r <= 32) or not (0 < p <= 16):
        return False
    try:
        dk = _derive(password, salt, n, r, p)
    except ValueError:
        return False
    return hmac.compare_digest(dk, expected)


def needs_rehash(encoded: str) -> bool:
    """True if a stored hash is below the current cost and should be upgraded on
    the next successful login."""
    m = _ENCODED.match(encoded or "")
    if not m:
        return True
    n, r, p = int(m.group(1)), int(m.group(2)), int(m.group(3))
    return (n, r, p) != (_SCRYPT_N, _SCRYPT_R, _SCRYPT_P)


# --- username rules ----------------------------------------------------------

_USERNAME = re.compile(r"^[a-z0-9][a-z0-9._-]{1,31}$")


def normalize_username(name: str) -> str:
    """Lowercase and strip. Usernames are case-insensitive so `Admin` and `admin`
    can't be two accounts."""
    return (name or "").strip().lower()


def valid_username(name: str) -> bool:
    return bool(_USERNAME.match(name or ""))


# --- store -------------------------------------------------------------------

class UserError(Exception):
    """Bad input or a conflicting account. Callers map this to a 400/409."""


async def count() -> int:
    """How many accounts exist. Zero means the app is still in single-password
    (or no-auth) mode — the switch that keeps existing installs working."""
    from sqlalchemy import func, select

    from app.models.db import SessionLocal, User

    async with SessionLocal() as s:
        return int((await s.execute(select(func.count()).select_from(User))).scalar() or 0)


async def multi_user_enabled() -> bool:
    return await count() > 0


async def get(user_id: int):
    """Look up an account by id, or None. Read on every authenticated request so
    a disabled or deleted account loses its sessions immediately."""
    from app.models.db import SessionLocal, User

    async with SessionLocal() as s:
        return await s.get(User, user_id)


async def create(username: str, password: str, role: str = VIEWER):
    """Create an account. Raises UserError on a bad name/role or a duplicate."""
    from sqlalchemy import select

    from app.models.db import SessionLocal, User

    username = normalize_username(username)
    if not valid_username(username):
        raise UserError("username must be 2-32 chars: lowercase letters, digits, dot, dash, underscore")
    if not password:
        raise UserError("password is required")
    if not valid_role(role):
        raise UserError(f"role must be one of: {', '.join(ROLES)}")

    async with SessionLocal() as s:
        existing = (await s.execute(select(User).where(User.username == username))).scalar_one_or_none()
        if existing is not None:
            raise UserError(f"user {username!r} already exists")
        user = User(username=username, password_hash=hash_password(password), role=role)
        s.add(user)
        await s.commit()
        await s.refresh(user)
        return user


async def authenticate(username: str, password: str):
    """Return the user on a correct password, else None.

    Runs the KDF even when the account is missing, so a wrong username and a wrong
    password take the same time — otherwise the response time enumerates accounts.
    """
    from sqlalchemy import select

    from app.models.db import SessionLocal, User

    username = normalize_username(username)
    async with SessionLocal() as s:
        user = (await s.execute(select(User).where(User.username == username))).scalar_one_or_none()

        if user is None:
            # Same work as a real verification against a throwaway hash.
            verify_password(password or "x", _DUMMY_HASH)
            return None
        if user.disabled:
            verify_password(password or "x", user.password_hash)
            return None
        if not verify_password(password, user.password_hash):
            return None

        from datetime import datetime
        user.last_login_at = datetime.utcnow()
        # Transparently upgrade a hash left behind by older cost parameters.
        if needs_rehash(user.password_hash):
            user.password_hash = hash_password(password)
        await s.commit()
        await s.refresh(user)
        return user


async def set_password(user_id: int, password: str) -> None:
    from datetime import datetime

    from app.models.db import SessionLocal, User

    if not password:
        raise UserError("password is required")
    async with SessionLocal() as s:
        user = await s.get(User, user_id)
        if user is None:
            raise UserError("user not found")
        user.password_hash = hash_password(password)
        user.updated_at = datetime.utcnow()
        await s.commit()


async def set_role(user_id: int, role: str) -> None:
    """Change a role, refusing to remove the last admin — an install with no
    admin can't be administered back into a working state."""
    from datetime import datetime

    from sqlalchemy import func, select

    from app.models.db import SessionLocal, User

    if not valid_role(role):
        raise UserError(f"role must be one of: {', '.join(ROLES)}")
    async with SessionLocal() as s:
        user = await s.get(User, user_id)
        if user is None:
            raise UserError("user not found")
        if user.role == ADMIN and role != ADMIN:
            admins = int((await s.execute(
                select(func.count()).select_from(User)
                .where(User.role == ADMIN, User.disabled.is_(False)))).scalar() or 0)
            if admins <= 1:
                raise UserError("cannot demote the last admin")
        user.role = role
        user.updated_at = datetime.utcnow()
        await s.commit()


async def set_disabled(user_id: int, disabled: bool) -> None:
    from datetime import datetime

    from sqlalchemy import func, select

    from app.models.db import SessionLocal, User

    async with SessionLocal() as s:
        user = await s.get(User, user_id)
        if user is None:
            raise UserError("user not found")
        if disabled and user.role == ADMIN:
            admins = int((await s.execute(
                select(func.count()).select_from(User)
                .where(User.role == ADMIN, User.disabled.is_(False)))).scalar() or 0)
            if admins <= 1:
                raise UserError("cannot disable the last admin")
        user.disabled = disabled
        user.updated_at = datetime.utcnow()
        await s.commit()


async def delete(user_id: int) -> None:
    from sqlalchemy import func, select

    from app.models.db import SessionLocal, User

    async with SessionLocal() as s:
        user = await s.get(User, user_id)
        if user is None:
            raise UserError("user not found")
        if user.role == ADMIN:
            admins = int((await s.execute(
                select(func.count()).select_from(User)
                .where(User.role == ADMIN, User.disabled.is_(False)))).scalar() or 0)
            if admins <= 1:
                raise UserError("cannot delete the last admin")
        await s.delete(user)
        await s.commit()


async def list_users() -> list:
    from sqlalchemy import select

    from app.models.db import SessionLocal, User

    async with SessionLocal() as s:
        return list((await s.execute(select(User).order_by(User.username))).scalars().all())


# A real hash of a value nobody logs in with, so `authenticate` can spend the same
# time on a missing account as on a present one. Built at import to keep the
# comparison path free of a fresh derivation.
_DUMMY_HASH = hash_password("not-a-real-password-timing-equalizer")
