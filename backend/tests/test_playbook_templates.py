"""Every starter template must be a valid, buildable spec.

This is the safety net for the library: add a template with a typo in its
`args_yaml` or a missing field and this suite fails before it ever reaches a user.
"""
from __future__ import annotations

import pytest
import yaml

from app.core import playbook_templates
from app.core.playbook_builder import build_playbook

CATALOG = playbook_templates.catalog()


def test_catalog_is_non_empty():
    assert len(CATALOG) >= 1


def test_ids_are_unique():
    ids = [t["id"] for t in CATALOG]
    assert len(ids) == len(set(ids))


@pytest.mark.parametrize("tpl", CATALOG, ids=[t["id"] for t in CATALOG])
def test_template_has_required_metadata(tpl):
    for field in ("id", "name", "description", "category", "spec"):
        assert tpl.get(field), f"{tpl.get('id')}: missing {field}"


@pytest.mark.parametrize("tpl", CATALOG, ids=[t["id"] for t in CATALOG])
def test_template_spec_builds_to_valid_yaml(tpl):
    out = build_playbook(tpl["spec"])
    docs = yaml.safe_load(out)
    assert isinstance(docs, list) and len(docs) == 1
    play = docs[0]
    assert play["name"]
    assert play["hosts"]
    # Every task must carry a name and at least one module key besides metadata.
    for task in play.get("tasks", []):
        assert task["name"]
        module_keys = [k for k in task if k not in ("name", "tags", "when")]
        assert module_keys, f"task {task['name']} has no module"


def test_get_by_id_roundtrip():
    first = CATALOG[0]["id"]
    assert playbook_templates.get(first)["id"] == first
    assert playbook_templates.get("nope-not-real") is None
