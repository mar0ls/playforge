"""Keep app.__version__ the single source of truth.

Before 0.1.0 the version lived only in git tags and the CHANGELOG, and the sidebar
showed a hardcoded `v0.1` that had drifted.
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

from app import __version__

REPO_ROOT = Path(__file__).resolve().parents[2]


def test_version_is_semver():
    assert re.fullmatch(r"\d+\.\d+\.\d+(?:-[0-9A-Za-z.-]+)?", __version__), __version__


def test_openapi_schema_reports_the_version():
    pytest.importorskip("aiosqlite")
    from app.main import app

    assert app.version == __version__


async def test_health_reports_version_and_schema_version():
    pytest.importorskip("aiosqlite")
    from app.main import health
    from app.models.db import init_db
    from app.models.migrations import SCHEMA_VERSION

    await init_db()
    payload = await health()

    assert payload["status"] == "ok"
    assert payload["version"] == __version__
    assert payload["schema_version"] == SCHEMA_VERSION


def test_sidebar_renders_the_version_dynamically():
    """Guards the specific regression: a literal version baked into the template."""
    base = Path(__file__).resolve().parents[1] / "app" / "templates" / "base.html"
    footer = [ln for ln in base.read_text().splitlines() if "footer-note" in ln or "app_version" in ln]
    assert any("app_version" in ln for ln in footer), "sidebar footer must render app_version"

    body = base.read_text()
    assert not re.search(r">\s*v\d+\.\d+", body), "hardcoded version string in base.html"


def test_changelog_documents_the_current_version():
    """Refuse to ship a version nobody wrote release notes for."""
    changelog = REPO_ROOT / "CHANGELOG.md"
    if not changelog.exists():
        pytest.skip("CHANGELOG.md not present (running from inside the image)")

    assert f"[{__version__}]" in changelog.read_text(), (
        f"CHANGELOG.md has no `## [{__version__}]` section"
    )
