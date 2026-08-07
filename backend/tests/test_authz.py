"""Capability policy: exhaustive over the real route table.

The point of these tests is that a new endpoint cannot be added without someone
deciding who may call it — an unmapped route denies everyone, and the coverage
test below turns that into a failing build rather than a surprise in production.
"""
from __future__ import annotations

import re

import pytest

pytest.importorskip("aiosqlite")

from app.core.authz import allowed, required_capability
from app.core.users import ADMIN, OPERATOR, VIEWER


def _walk(routes) -> list:
    """Flatten the route tree.

    FastAPI ≥ 0.141 keeps `include_router` results as nested router objects rather
    than flattening them into `app.routes`; 0.136 flattened them. The image runs
    the locked (newer) version and the dev venv may not, so this has to handle
    both — a non-recursive walk finds zero API routes on the newer one, which is
    what `test_the_route_table_is_not_empty` exists to catch.
    """
    out = []
    for route in routes:
        # 0.136 nests under `.routes`; 0.141's `_IncludedRouter` exposes the
        # original router instead and has no `.routes` of its own.
        nested = (getattr(route, "routes", None)
                  or getattr(getattr(route, "original_router", None), "routes", None))
        if nested and not hasattr(route, "methods"):
            out.extend(_walk(nested))
        else:
            out.append(route)
    return out


def _routes() -> list[tuple[str, str]]:
    """(method, concrete path) for every declared API route."""
    from app.main import app

    out: list[tuple[str, str]] = []
    for route in _walk(app.routes):
        path = getattr(route, "path", "")
        if not path.startswith("/api"):
            continue
        # Path params only need to be *something*; the policy matches on shape.
        concrete = re.sub(r"\{[^}]+\}", "x", path)
        methods = getattr(route, "methods", None) or ["GET"]  # websocket has none
        for method in methods:
            if method in ("HEAD", "OPTIONS"):
                continue
            out.append((method, concrete))
    return out


def test_the_route_table_is_not_empty():
    assert len(_routes()) > 50, "route discovery broke; the coverage test below would pass vacuously"


def test_every_api_route_has_a_capability():
    """Fail-closed: an unmapped route is one nobody decided about."""
    unmapped = [(m, p) for m, p in _routes() if required_capability(m, p) is None]
    assert unmapped == [], f"routes with no capability rule: {unmapped}"


# --- the shape of each role --------------------------------------------------

# POSTs that carry a payload but change nothing on disk, so a viewer may call
# them. Verified against the handlers: `lint` only reads, `playbook/preview`
# renders YAML without saving, and none of the assistant endpoints write — the
# agent is the only AI route that touches the repo, and it sits behind `run`.
# Anything added here is a deliberate decision that the route is side-effect-free.
READ_ONLY_POSTS = {
    "/api/projects/x/lint",
    "/api/projects/x/playbook/preview",
    "/api/ai/chat",
    "/api/ai/chat/stream",
    "/api/ai/generate-playbook",
    "/api/ai/narrate-plan",
    "/api/ai/suggest-fix",
    "/api/ai/explain-failure",
}


def test_viewer_cannot_reach_any_state_changing_route():
    leaked = [(m, p) for m, p in _routes()
              if m in ("POST", "PUT", "PATCH", "DELETE")
              and p not in READ_ONLY_POSTS
              and allowed(VIEWER, m, p)]
    assert leaked == [], f"viewer can change state: {leaked}"


def test_the_read_only_post_list_has_not_gone_stale():
    """If one of these disappears or is renamed, the exemption must be revisited
    rather than silently covering nothing."""
    declared = {p for _, p in _routes()}
    missing = READ_ONLY_POSTS - declared
    assert missing == set(), f"exempted routes that no longer exist: {missing}"


def test_viewer_can_read_the_dashboard_and_projects():
    assert allowed(VIEWER, "GET", "/api/projects") is True
    assert allowed(VIEWER, "GET", "/api/dashboard/stats") is True
    assert allowed(VIEWER, "GET", "/api/projects/x/tree") is True


def test_operator_can_run_and_write_but_not_touch_secrets():
    assert allowed(OPERATOR, "POST", "/api/runs") is True
    assert allowed(OPERATOR, "PUT", "/api/projects/x/file") is True
    assert allowed(OPERATOR, "POST", "/api/credentials") is False
    assert allowed(OPERATOR, "DELETE", "/api/credentials/1") is False


def test_operator_cannot_administer():
    assert allowed(OPERATOR, "PUT", "/api/ai/config") is False
    assert allowed(OPERATOR, "POST", "/api/users") is False


def test_admin_can_reach_everything_declared():
    denied = [(m, p) for m, p in _routes() if not allowed(ADMIN, m, p)]
    assert denied == [], f"admin denied: {denied}"


# --- specific decisions worth pinning ----------------------------------------

def test_listing_credentials_is_read_but_changing_them_is_not():
    """The Run form needs credential names; the API never returns the secret."""
    assert required_capability("GET", "/api/credentials") == "read"
    assert required_capability("POST", "/api/credentials") == "secrets"
    assert required_capability("PATCH", "/api/credentials/1") == "secrets"


def test_testing_a_credential_needs_secrets_because_it_uses_one():
    assert required_capability("POST", "/api/credentials/1/test") == "secrets"


def test_clearing_run_history_is_an_admin_action():
    """It destroys the audit trail, so it is deliberately not a plain write."""
    assert required_capability("DELETE", "/api/runs") == "admin"
    assert allowed(OPERATOR, "DELETE", "/api/runs") is False


def test_vault_operations_need_secrets_but_status_does_not():
    assert required_capability("GET", "/api/projects/x/vault/status") == "read"
    assert required_capability("POST", "/api/projects/x/vault/view") == "secrets"
    assert allowed(OPERATOR, "POST", "/api/projects/x/vault/decrypt") is False


def test_the_agent_is_closed_to_viewers():
    assert required_capability("POST", "/api/ai/agent") == "run"
    assert allowed(VIEWER, "POST", "/api/ai/agent") is False
    assert allowed(OPERATOR, "POST", "/api/ai/agent") is True


def test_assistant_chat_is_readable_by_viewers():
    """Chat changes nothing on disk."""
    assert allowed(VIEWER, "POST", "/api/ai/chat") is True
    assert allowed(VIEWER, "POST", "/api/ai/generate-playbook") is True


def test_provider_config_is_admin_only_because_it_holds_api_keys():
    assert required_capability("GET", "/api/ai/config") == "admin"
    assert required_capability("PUT", "/api/ai/config") == "admin"


def test_preflight_counts_as_running_not_reading():
    """It probes the controller and optionally the targets."""
    assert required_capability("POST", "/api/runs/preflight") == "run"
    assert allowed(VIEWER, "POST", "/api/runs/preflight") is False


def test_reading_run_history_is_open_to_viewers():
    assert allowed(VIEWER, "GET", "/api/runs") is True
    assert allowed(VIEWER, "GET", "/api/runs/1") is True
    assert allowed(VIEWER, "GET", "/api/projects/x/runs") is True


def test_unknown_route_denies_everyone():
    assert required_capability("GET", "/api/not-a-real-endpoint") is None
    for role in (ADMIN, OPERATOR, VIEWER):
        assert allowed(role, "GET", "/api/not-a-real-endpoint") is False
