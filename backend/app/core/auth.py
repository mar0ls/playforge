"""Optional single-password auth with signed session cookies.

Design goals: zero new infra, opt-in, air-gap friendly.

- If `ANSIBLE_GUI_PASSWORD` is unset, auth is DISABLED — the app behaves exactly
  as before (so existing single-user local installs don't break).
- If it's set, every request must carry a valid session cookie; otherwise it's
  redirected to /login (HTML) or gets 401 (API). The cookie is an HMAC-signed,
  expiring token — no server-side session store needed.
- The password is compared in constant time; the signing key derives from the
  password + the credential master key, so a leaked cookie can't be forged
  without both.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import os
import time
from dataclasses import dataclass

SESSION_COOKIE = "agui_session"
_SESSION_TTL = 7 * 24 * 3600  # 7 days


def auth_enabled() -> bool:
    return bool(os.getenv("ANSIBLE_GUI_PASSWORD"))


def _password() -> str:
    return os.getenv("ANSIBLE_GUI_PASSWORD") or ""


class AuthKeyUnavailable(RuntimeError):
    """The credential master key can't be read, so no session key can be derived."""


def _signing_key() -> bytes:
    """Derive the cookie-signing key from the credential master key, plus the
    shared password when there is one.

    The master key is required, not optional. This used to fall back to a fixed
    string when it couldn't be read, which was survivable only because the shared
    password was also in the mix — with accounts, there may be no shared password,
    and the key would then be derived entirely from a constant in this file, i.e.
    anyone could forge a session. Failing here is the correct outcome: a request
    gets a 500 rather than a forgeable cookie.
    """
    from app.core.credentials import _load_or_create_key
    try:
        salt = _load_or_create_key().decode("utf-8", "replace")
    except Exception as e:
        raise AuthKeyUnavailable(f"cannot read the credential master key: {e}") from e
    return hashlib.sha256((_password() + "|" + salt).encode()).digest()


def check_password(candidate: str) -> bool:
    return bool(candidate) and hmac.compare_digest(candidate, _password())


def _b64(b: bytes) -> str:
    return base64.urlsafe_b64encode(b).decode().rstrip("=")


def _unb64(s: str) -> bytes:
    return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))


@dataclass(frozen=True)
class Session:
    """A verified session cookie. `user_id` is None in single-password mode."""
    expires_at: int
    user_id: int | None


def issue_token(user_id: int | None = None, now: float | None = None) -> str:
    """Create a signed `<expiry>.<user_id>.<sig>` token.

    `user_id` is empty in single-password mode, where there is no account to name.
    It is inside the signed payload, so a cookie can't be edited to claim another
    account.
    """
    exp = int((now or time.time()) + _SESSION_TTL)
    uid = "" if user_id is None else str(int(user_id))
    payload = f"{exp}.{uid}"
    sig = hmac.new(_signing_key(), payload.encode(), hashlib.sha256).digest()
    return f"{payload}.{_b64(sig)}"


def read_token(token: str | None, now: float | None = None) -> Session | None:
    """Verify a cookie and return its session, or None if it isn't usable.

    Tokens issued before user identity existed had no `user_id` field and no
    longer parse — those sessions end at the upgrade and the holder logs in again.
    """
    if not token:
        return None
    parts = token.split(".")
    if len(parts) != 3:
        return None
    exp_str, uid_str, sig_b64 = parts
    try:
        exp = int(exp_str)
        sig = _unb64(sig_b64)
        user_id = int(uid_str) if uid_str else None
    except (ValueError, TypeError):
        return None
    if exp < (now or time.time()):
        return None
    expected = hmac.new(_signing_key(), f"{exp_str}.{uid_str}".encode(), hashlib.sha256).digest()
    if not hmac.compare_digest(sig, expected):
        return None
    return Session(expires_at=exp, user_id=user_id)


def verify_token(token: str | None, now: float | None = None) -> bool:
    """Back-compat boolean check. Prefer `read_token` when the identity matters."""
    return read_token(token, now) is not None


# --- failed-login throttling -------------------------------------------------
#
# The password is a single shared secret with no user to lock out, so an
# unthrottled /login is an open guessing oracle for anyone who can reach the
# port. Failures are counted per client IP; after _MAX_FAILS within _FAIL_WINDOW
# the address is locked out, and each further lockout doubles up to _MAX_LOCKOUT.
#
# State is in-process on purpose: the app is a single uvicorn process by design
# (see the scheduler's "no separate worker" note). If it ever runs multiple
# workers this becomes per-worker and the effective limit multiplies.
#
# `request.client.host` is the real peer — uvicorn runs without --proxy-headers,
# so X-Forwarded-For is not trusted and can't be spoofed to dodge a lockout.
# Behind a reverse proxy every request appears to come from the proxy, which
# makes the lockout global rather than per-client; that fails closed, not open.

_MAX_FAILS = 5
_FAIL_WINDOW = 300.0        # failures older than this stop counting
_BASE_LOCKOUT = 30.0
_MAX_LOCKOUT = 900.0
_MAX_TRACKED = 4096         # bound memory against spoofed/rotating sources


class _Attempts:
    __slots__ = ("fails", "first_fail", "locked_until", "lockouts")

    def __init__(self) -> None:
        self.fails = 0
        self.first_fail = 0.0
        self.locked_until = 0.0
        self.lockouts = 0


_attempts: dict[str, _Attempts] = {}


def _prune(now: float) -> None:
    """Drop entries that are neither locked nor inside the failure window."""
    for key in [k for k, a in _attempts.items()
                if a.locked_until < now and (now - a.first_fail) > _FAIL_WINDOW]:
        _attempts.pop(key, None)


def lockout_remaining(client: str, now: float | None = None) -> float:
    """Seconds left on this client's lockout, or 0 when it may attempt a login."""
    now = now or time.time()
    a = _attempts.get(client)
    if a is None:
        return 0.0
    return max(0.0, a.locked_until - now)


def record_failure(client: str, now: float | None = None) -> float:
    """Count a failed attempt. Returns the lockout in seconds (0 = not locked)."""
    now = now or time.time()
    if len(_attempts) >= _MAX_TRACKED:
        _prune(now)
    a = _attempts.get(client)
    if a is None:
        a = _attempts[client] = _Attempts()
    # A first failure, or one after the window lapsed, starts a fresh streak.
    if a.fails == 0 or (now - a.first_fail) > _FAIL_WINDOW:
        a.fails = 0
        a.first_fail = now
    a.fails += 1
    if a.fails >= _MAX_FAILS:
        a.lockouts += 1
        penalty = min(_BASE_LOCKOUT * (2 ** (a.lockouts - 1)), _MAX_LOCKOUT)
        a.locked_until = now + penalty
        a.fails = 0            # streak consumed; the lockout is the punishment
        return penalty
    return 0.0


def record_success(client: str) -> None:
    """Clear a client's failure history after a correct password."""
    _attempts.pop(client, None)


def reset_throttle() -> None:
    """Drop all throttling state. For tests."""
    _attempts.clear()
