"""Which capability each API route requires.

One table instead of a decorator on eighty routes: it can be read top to bottom
during a review, and it can be tested exhaustively. `tests/test_authz.py`
enumerates every route the app declares and fails if any of them falls through,
so adding an endpoint forces a decision about who may call it.

Matching is first-hit against the request path, so order matters — the specific
entries sit above the general ones.

Capabilities come from core.users:
    read     see projects, files, runs, history
    run      execute playbooks and ad-hoc commands
    write    change project content, schedules, templates, environments
    secrets  credential material and vault operations
    admin    users, provider config, destroying audit history
"""
from __future__ import annotations

import re

from app.core.users import can

# (methods, path pattern, capability). Methods empty = any.
_RULES: list[tuple[frozenset[str], re.Pattern[str], str]] = []


def _rule(methods: str, pattern: str, capability: str) -> None:
    ms = frozenset(m.strip().upper() for m in methods.split(",") if m.strip())
    _RULES.append((ms, re.compile(pattern), capability))


_ANY = ""
_ID = r"[^/]+"

# --- credentials -------------------------------------------------------------
# Listing is `read`: the Run form needs the names, and the API never returns the
# secret itself. Everything that creates, changes or *uses* a secret is `secrets`.
_rule("GET", rf"^/api/credentials$", "read")
_rule(_ANY, rf"^/api/credentials", "secrets")

# --- vault -------------------------------------------------------------------
_rule("GET", rf"^/api/projects/{_ID}/vault/status$", "read")
_rule(_ANY, rf"^/api/projects/{_ID}/vault/", "secrets")

# --- users and app configuration ---------------------------------------------
_rule(_ANY, r"^/api/users", "admin")
_rule(_ANY, r"^/api/ai/config", "admin")
_rule(_ANY, r"^/api/ai/probe", "admin")
# Clearing run history destroys the audit trail, so it is not a write.
_rule("DELETE", r"^/api/runs$", "admin")

# --- runs --------------------------------------------------------------------
# preflight probes the controller and optionally the targets, so it is `run`
# rather than `read` despite writing no Run row.
_rule("GET", r"^/api/runs", "read")
_rule(_ANY, r"^/api/runs", "run")
_rule("GET", rf"^/api/projects/{_ID}/runs", "read")

# --- the agent ---------------------------------------------------------------
# The agent writes files and can run playbooks; its own per-call flags narrow
# that further, but a viewer must not reach it at all.
_rule(_ANY, r"^/api/ai/agent", "run")

# --- assistant (read-only generation) ----------------------------------------
# These return text and change nothing on disk.
_rule(_ANY, r"^/api/ai/", "read")

# --- project content ---------------------------------------------------------
_rule("GET", rf"^/api/projects/{_ID}/(file|tree|detected|tags|inventory|galaxy|git|settings|templates|environments)", "read")
_rule("POST", rf"^/api/projects/{_ID}/(lint|playbook/preview)$", "read")
_rule(_ANY, rf"^/api/projects/{_ID}/", "write")
_rule("GET", r"^/api/projects$", "read")
_rule(_ANY, r"^/api/projects", "write")

# --- schedules ---------------------------------------------------------------
_rule("GET", r"^/api/schedules", "read")
_rule(_ANY, r"^/api/schedules", "write")

# --- read-only surfaces ------------------------------------------------------
_rule("GET", r"^/api/dashboard/", "read")
_rule("GET", r"^/api/library/", "read")


def required_capability(method: str, path: str) -> str | None:
    """Capability needed for `method path`, or None when nothing matches.

    None means deny: an unmapped route is a route nobody decided about, and
    guessing a default is how a write ends up reachable by a viewer.
    """
    method = (method or "").upper()
    for methods, pattern, capability in _RULES:
        if methods and method not in methods:
            continue
        if pattern.search(path):
            return capability
    return None


def allowed(role: str, method: str, path: str) -> bool:
    capability = required_capability(method, path)
    if capability is None:
        return False
    return can(role, capability)
