"""Getting the first admin account onto a fresh install.

Two routes, because the deployment model needs both:

**Environment variables** — `ANSIBLE_GUI_ADMIN_USER` / `ANSIBLE_GUI_ADMIN_PASSWORD`,
applied at startup when no accounts exist. This is the path for `docker compose`,
k8s and Ansible, where nobody wants a manual click after every deploy. Each also
accepts a `_FILE` variant pointing at a mounted file, so the password can come
from a docker/k8s secret instead of sitting in `.env`.

**Setup page** — used when those aren't set. Guarded by a one-time token written
to the container log at startup, which closes the window this would otherwise
open: `docker-compose.yml` publishes the port on 0.0.0.0, so between
`docker compose up -d` and the operator reaching the browser, anyone who can
reach the port could otherwise claim the admin account and own the instance. An
attacker who can't read `docker compose logs` can't finish setup.

The token lives in memory and is regenerated on every start, so it can't be
replayed from a stale file, and it is discarded the moment an account exists.
"""
from __future__ import annotations

import logging
import os
import secrets
from pathlib import Path

log = logging.getLogger("ansible_gui.bootstrap")

_setup_token: str | None = None


def _env(name: str) -> str:
    """Read `NAME`, or the contents of the file named by `NAME_FILE`.

    The `_FILE` indirection is the convention used by the official postgres/mysql
    images; it's what lets a compose or k8s secret supply the value without it
    appearing in the environment or in `docker compose config`.
    """
    path = os.getenv(f"{name}_FILE")
    if path:
        try:
            return Path(path).read_text(encoding="utf-8").strip()
        except OSError as e:
            log.error("cannot read %s_FILE (%s): %s", name, path, e)
            return ""
    return (os.getenv(name) or "").strip()


async def apply_env_bootstrap() -> bool:
    """Create the admin named by the environment. Returns True if it created one.

    A no-op once any account exists, so restarting the container doesn't reset a
    password someone has since changed.
    """
    from app.core import users

    username = _env("ANSIBLE_GUI_ADMIN_USER")
    password = _env("ANSIBLE_GUI_ADMIN_PASSWORD")
    if not username or not password:
        return False
    if await users.count() > 0:
        return False

    try:
        user = await users.create(username, password, users.ADMIN)
    except users.UserError as e:
        log.error("ANSIBLE_GUI_ADMIN_USER/PASSWORD did not create an account: %s", e)
        return False

    log.warning(
        "created admin account %r from the environment. The password is in this "
        "container's configuration — change it after signing in.", user.username)
    return True


def setup_token() -> str | None:
    return _setup_token


def issue_setup_token() -> str:
    """Mint the token for this process and log it where the operator can find it."""
    global _setup_token
    _setup_token = secrets.token_urlsafe(32)
    log.warning(
        "\n"
        "─────────────────────────────────────────────────────────────────\n"
        " No accounts exist yet. Create the first administrator at:\n"
        "     /setup\n"
        " Setup token (required):\n"
        "     %s\n"
        " This token is only in this log and changes on every restart.\n"
        "─────────────────────────────────────────────────────────────────",
        _setup_token)
    return _setup_token


def clear_setup_token() -> None:
    global _setup_token
    _setup_token = None


def check_setup_token(candidate: str) -> bool:
    """Constant-time comparison; false when setup isn't open."""
    if not _setup_token or not candidate:
        return False
    return secrets.compare_digest(candidate, _setup_token)


async def prepare() -> None:
    """Run at startup: apply the env bootstrap, else open token-gated setup.

    Setup is only opened when there is no other way in. An install running on the
    legacy shared password is already reachable by its operator, so it is left
    alone rather than being nudged toward accounts it didn't ask for.
    """
    from app.core import auth, users

    if await apply_env_bootstrap():
        clear_setup_token()
        return
    if await users.count() > 0:
        clear_setup_token()
        return
    if auth.auth_enabled():
        # Single-password mode: reachable, and switching it to accounts is the
        # operator's decision, not something a first boot should force.
        clear_setup_token()
        return
    issue_setup_token()
