"""Tests for YAML inventory detection and structured host insertion.

`is_yaml_inventory` is pure. `add_host_yaml` needs ruamel, so those tests skip
where it isn't installed (it ships in the image).
"""
from __future__ import annotations

import pytest
import yaml as pyyaml

from app.core.inventory_writer import is_yaml_inventory

pytest.importorskip("ruamel.yaml")
from app.core.inventory_writer import add_host_yaml  # noqa: E402


# --- detection --------------------------------------------------------------

@pytest.mark.parametrize("text, expected", [
    ("all:\n  hosts:\n    web1:\n", True),          # no leading --- but clearly YAML
    ("---\nall:\n  children:\n", True),
    ("web:\n  hosts:\n    h1:\n", True),            # compact top-level group
    ("[web]\nweb1\n", False),
    ("web1 ansible_host=10.0.0.1\n", False),        # INI host with vars
    ("web1.example.com\n", False),                  # bare host
    ("# only a comment\n", False),
])
def test_is_yaml_inventory(text, expected):
    assert is_yaml_inventory(text) is expected


# --- add_host_yaml ----------------------------------------------------------

def test_add_host_to_existing_nested_group():
    text = (
        "all:\n"
        "  children:\n"
        "    web:\n"
        "      hosts:\n"
        "        web1:\n"
    )
    out = add_host_yaml(text, "web", "web2", {"ansible_host": "10.0.0.2"})
    doc = pyyaml.safe_load(out)
    hosts = doc["all"]["children"]["web"]["hosts"]
    assert "web1" in hosts and "web2" in hosts
    assert hosts["web2"]["ansible_host"] == "10.0.0.2"


def test_add_host_to_compact_top_level_group():
    text = "web:\n  hosts:\n    web1:\n"
    out = add_host_yaml(text, "web", "web2", {})
    doc = pyyaml.safe_load(out)
    assert set(doc["web"]["hosts"]) == {"web1", "web2"}


def test_add_host_creates_missing_group():
    text = "all:\n  children:\n    web:\n      hosts:\n        web1:\n"
    out = add_host_yaml(text, "db", "db1", {"ansible_port": 5432})
    doc = pyyaml.safe_load(out)
    assert doc["all"]["children"]["db"]["hosts"]["db1"]["ansible_port"] == 5432


def test_add_host_into_empty_document():
    out = add_host_yaml("", "web", "web1", {"ansible_host": "1.2.3.4"})
    doc = pyyaml.safe_load(out)
    assert doc["web"]["hosts"]["web1"]["ansible_host"] == "1.2.3.4"


def test_add_host_is_idempotent_and_merges_vars():
    text = "web:\n  hosts:\n    web1:\n      ansible_host: 10.0.0.1\n"
    out = add_host_yaml(text, "web", "web1", {"ansible_user": "root"})
    doc = pyyaml.safe_load(out)
    # No duplicate host; existing var kept, new var merged.
    assert list(doc["web"]["hosts"]) == ["web1"]
    assert doc["web"]["hosts"]["web1"] == {"ansible_host": "10.0.0.1", "ansible_user": "root"}


def test_add_host_preserves_comments():
    text = (
        "# production inventory\n"
        "web:\n"
        "  hosts:\n"
        "    web1:  # primary\n"
    )
    out = add_host_yaml(text, "web", "web2", {})
    assert "# production inventory" in out
    assert "# primary" in out
