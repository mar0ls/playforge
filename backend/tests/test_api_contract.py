"""The `/api` surface is a promise from 1.0 on. This is what enforces it.

README states it plainly: paths and response fields do not change or disappear
within a major version, new optional fields and new endpoints may arrive at any
time. Until now nothing checked that. `test_migrations.py` does this job for the
database schema; this file does it for the HTTP surface.

What is pinned, and what is not:

* **Pinned here** — every operation keeps existing, and no operation grows a new
  *required* parameter or body field. Both are breaking: a caller that worked
  yesterday stops working. Removing a requirement is a relaxation and passes.
  Adding operations passes too, which is the additive half of the promise.

* **Not pinned here** — response field names. Most handlers return plain dicts
  with no `response_model`, so OpenAPI has nothing to compare against; a snapshot
  would record an empty schema and prove nothing. That half of the promise is
  held by the API tests that assert on the keys they read, and by
  `tests/test_projects_api_*.py` in particular.

Deliberately excluded: the HTML page routes and `/login`, `/setup`, `/logout`.
They are the UI, not the API, and they are free to change. `/health` is in,
because the compose healthcheck and the release smoke test both parse it.

**When a change is intended**, regenerate the snapshot and commit it as part of
the same change, so the diff shows the contract moving:

    make api-contract

It lives in a file rather than being computed at runtime so that a change to the
surface shows up in review, where someone can ask whether callers were
considered.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

pytest.importorskip("aiosqlite")
pytest.importorskip("cryptography")

SNAPSHOT = Path(__file__).parent / "api_contract.json"


def _resolve(spec: dict, node: dict) -> dict:
    """Follow $ref chains into components/schemas."""
    while isinstance(node, dict) and "$ref" in node:
        parts = node["$ref"].split("/")[1:]
        node = spec
        for part in parts:
            node = node[part]
    return node


def build(spec: dict) -> dict:
    """Reduce a full OpenAPI document to the part callers depend on."""
    out: dict[str, dict] = {}
    for path, item in spec["paths"].items():
        if not (path.startswith("/api/") or path == "/health"):
            continue
        for method in ("get", "post", "put", "patch", "delete"):
            op = item.get(method)
            if op is None:
                continue
            required_params = sorted(
                p["name"] for p in op.get("parameters", []) if p.get("required")
            )
            required_body: list[str] = []
            for media in ((op.get("requestBody") or {}).get("content") or {}).values():
                schema = _resolve(spec, media.get("schema") or {})
                required_body = sorted(schema.get("required", []))
                break
            out[f"{method.upper()} {path}"] = {
                "required_params": required_params,
                "required_body": required_body,
            }
    return dict(sorted(out.items()))


def current() -> dict:
    from app.main import app

    return build(app.openapi())


@pytest.fixture(scope="module")
def live() -> dict:
    return current()


@pytest.fixture(scope="module")
def pinned() -> dict:
    return json.loads(SNAPSHOT.read_text())


def test_no_operation_disappeared(live, pinned):
    """A path or method that vanishes breaks every caller using it."""
    missing = sorted(set(pinned) - set(live))
    assert not missing, (
        "these operations are in the contract but no longer exist:\n  "
        + "\n  ".join(missing)
        + "\n\nIf the removal is intended, it is a breaking change: announce it in "
          "CHANGELOG and regenerate with `make api-contract`."
    )


def test_no_operation_gained_a_required_parameter(live, pinned):
    """Adding a required parameter breaks callers that omit it."""
    added = {}
    for op, was in pinned.items():
        if op not in live:
            continue
        new = sorted(set(live[op]["required_params"]) - set(was["required_params"]))
        if new:
            added[op] = new
    assert not added, f"newly required parameters: {added}"


def test_no_operation_gained_a_required_body_field(live, pinned):
    """Same reasoning as parameters: a new required field is a breaking change."""
    added = {}
    for op, was in pinned.items():
        if op not in live:
            continue
        new = sorted(set(live[op]["required_body"]) - set(was["required_body"]))
        if new:
            added[op] = new
    assert not added, f"newly required body fields: {added}"


def test_new_operations_are_allowed_but_visible(live, pinned):
    """Additive change is permitted — this test exists to name it, and to keep the
    snapshot honest by failing once it drifts far enough to be worth a look."""
    added = sorted(set(live) - set(pinned))
    assert len(added) < 10, (
        f"{len(added)} operations are not in the contract snapshot:\n  "
        + "\n  ".join(added)
        + "\n\nRegenerate it with `make api-contract` so the surface stays recorded."
    )


def test_the_snapshot_matches_what_the_app_serves(live, pinned):
    """Not a rule about compatibility — a check that the file is not stale.

    Skipped rather than failed for purely additive drift, which the test above
    already reports.
    """
    if set(live) - set(pinned):
        pytest.skip("new operations present; covered by the additive test")
    assert live == pinned, "the snapshot no longer matches; run `make api-contract`"


def test_the_page_routes_are_not_part_of_the_contract(pinned):
    """The UI is free to change. Only /api and /health are promised."""
    for op in pinned:
        path = op.split(" ", 1)[1]
        assert path.startswith("/api/") or path == "/health", path
