"""Content-Security-Policy: per-request nonce, no 'unsafe-inline' in script-src.

The policy keeps 'unsafe-eval' (Alpine/htmx evaluate with `new Function`) but drops
'unsafe-inline': every inline <script> carries a per-request nonce, so an injected
script tag without the nonce can't run. These assertions pin exactly that.
"""
from __future__ import annotations

import re

import pytest

pytest.importorskip("aiosqlite")
pytest.importorskip("cryptography")

from starlette.testclient import TestClient


@pytest.fixture
def client(clean_users, monkeypatch):
    monkeypatch.delenv("ANSIBLE_GUI_PASSWORD", raising=False)
    from app.main import app
    with TestClient(app) as c:
        yield c


def _script_src(csp: str) -> str:
    for directive in csp.split(";"):
        directive = directive.strip()
        if directive.startswith("script-src"):
            return directive
    return ""


def test_script_src_has_a_nonce_and_no_unsafe_inline(client):
    r = client.get("/login")
    csp = r.headers["Content-Security-Policy"]
    script_src = _script_src(csp)
    assert "'unsafe-inline'" not in script_src, script_src
    assert re.search(r"'nonce-[A-Za-z0-9_-]+'", script_src), script_src
    # Alpine/htmx still need eval; that's the documented, unavoidable part.
    assert "'unsafe-eval'" in script_src


def test_the_nonce_is_fresh_per_request(client):
    def nonce_of(resp):
        m = re.search(r"'nonce-([A-Za-z0-9_-]+)'", resp.headers["Content-Security-Policy"])
        return m.group(1) if m else None
    first = nonce_of(client.get("/login"))
    second = nonce_of(client.get("/login"))
    assert first and second and first != second


def test_inline_scripts_in_the_page_carry_the_header_nonce(client):
    """The nonce in the CSP header must match the one stamped on the page's inline
    <script>, or the browser would block the app's own scripts."""
    r = client.get("/login")
    header_nonce = re.search(r"'nonce-([A-Za-z0-9_-]+)'",
                             r.headers["Content-Security-Policy"]).group(1)
    # Every inline <script> on the page should use that exact nonce.
    inline = re.findall(r"<script(?![^>]*\bsrc=)[^>]*>", r.text)
    for tag in inline:
        assert f'nonce="{header_nonce}"' in tag, tag
