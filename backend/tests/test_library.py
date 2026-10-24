"""Tests for api/library.py — the read-only starter-template catalog."""
from __future__ import annotations

import pytest

from fastapi import HTTPException

from app.api import library


async def test_catalog_lists_templates_with_stable_ids():
    cat = await library.playbook_templates_list()
    assert isinstance(cat, list) and cat
    ids = {t["id"] for t in cat}
    assert {"blank", "ping"} <= ids   # the always-present starters


async def test_detail_returns_a_known_template():
    tpl = await library.playbook_template_detail("ping")
    assert tpl["id"] == "ping"


async def test_detail_404s_on_unknown_id():
    with pytest.raises(HTTPException) as exc:
        await library.playbook_template_detail("no-such-template")
    assert exc.value.status_code == 404
