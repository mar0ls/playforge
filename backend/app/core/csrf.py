"""Per-request CSRF tokens, signed rather than merely mirrored.

Plain double-submit puts one random value in a cookie and the same value in the
request, and trusts that only our own page could have read the cookie. Cookies
are not origin-scoped: a subdomain an attacker controls can set them for the
parent domain, so it can plant both halves and the check passes. That is the very
gap this project's threat model already named.

So the cookie holds a nonce and the token is an HMAC of that nonce under the
server key. Planting a nonce buys nothing without the key, and the key never
leaves the process. The pairing is verified by recomputing, so there is no token
store to keep, expire or replicate — the same reasoning that made session cookies
signed rather than stateful.

The key is derived from the session-signing key with a distinct label, so a CSRF
token can never be replayed as a session signature or the other way round.
"""
from __future__ import annotations

import hashlib
import hmac
import secrets

COOKIE = "agui_csrf"
HEADER = "x-csrf-token"
FORM_FIELD = "csrf_token"

_NONCE_BYTES = 32


def _key() -> bytes:
    """Domain-separated from the session key it is derived from."""
    from app.core.auth import _signing_key

    return hashlib.sha256(b"csrf|" + _signing_key()).digest()


def new_nonce() -> str:
    return secrets.token_urlsafe(_NONCE_BYTES)


def token_for(nonce: str) -> str:
    """The value a page embeds and a request must send back."""
    return hmac.new(_key(), nonce.encode(), hashlib.sha256).hexdigest()


def verify(nonce: str | None, token: str | None) -> bool:
    """True when `token` is the signature of `nonce`. Constant time."""
    if not nonce or not token:
        return False
    return hmac.compare_digest(token, token_for(nonce))
