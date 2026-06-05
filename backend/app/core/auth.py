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

SESSION_COOKIE = "agui_session"
_SESSION_TTL = 7 * 24 * 3600  # 7 days


def auth_enabled() -> bool:
    return bool(os.getenv("ANSIBLE_GUI_PASSWORD"))


def _password() -> str:
    return os.getenv("ANSIBLE_GUI_PASSWORD") or ""


def _signing_key() -> bytes:
    """Derive the cookie-signing key from the password plus, if available, the
    credential master key — so forging a cookie needs more than the password file."""
    salt = ""
    try:
        from app.core.credentials import _load_or_create_key
        salt = _load_or_create_key().decode("utf-8", "replace")
    except Exception:
        salt = "ansible-gui-static-salt"
    return hashlib.sha256((_password() + "|" + salt).encode()).digest()


def check_password(candidate: str) -> bool:
    return bool(candidate) and hmac.compare_digest(candidate, _password())


def _b64(b: bytes) -> str:
    return base64.urlsafe_b64encode(b).decode().rstrip("=")


def _unb64(s: str) -> bytes:
    return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))


def issue_token(now: float | None = None) -> str:
    """Create a signed `<expiry>.<sig>` session token."""
    exp = int((now or time.time()) + _SESSION_TTL)
    payload = str(exp).encode()
    sig = hmac.new(_signing_key(), payload, hashlib.sha256).digest()
    return f"{exp}.{_b64(sig)}"


def verify_token(token: str | None, now: float | None = None) -> bool:
    if not token or "." not in token:
        return False
    exp_str, _, sig_b64 = token.partition(".")
    try:
        exp = int(exp_str)
        sig = _unb64(sig_b64)
    except (ValueError, Exception):
        return False
    if exp < (now or time.time()):
        return False
    expected = hmac.new(_signing_key(), exp_str.encode(), hashlib.sha256).digest()
    return hmac.compare_digest(sig, expected)
