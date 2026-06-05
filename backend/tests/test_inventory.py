"""Tests for the inventory parser (core/inventory.py).

Exercises the real ansible InventoryManager against tiny on-disk INI inventories,
plus the path-sandboxing and missing-file guards.
"""
from __future__ import annotations

import pytest

pytest.importorskip("ansible")

from app.core.inventory import parse


def test_parse_groups_and_hosts(tmp_path):
    inv = tmp_path / "hosts"
    inv.write_text(
        "[web]\n"
        "web1.example.com\n"
        "web2.example.com\n"
        "\n"
        "[db]\n"
        "db1.example.com\n"
    )
    out = parse(tmp_path, "hosts")
    assert set(out["hosts"]) == {"web1.example.com", "web2.example.com", "db1.example.com"}
    assert out["groups"]["web"]["hosts"] == ["web1.example.com", "web2.example.com"]
    assert out["groups"]["db"]["hosts"] == ["db1.example.com"]


def test_parse_group_of_groups_children(tmp_path):
    inv = tmp_path / "hosts"
    inv.write_text(
        "[frontend]\n"
        "fe1\n"
        "[backend]\n"
        "be1\n"
        "[web:children]\n"
        "frontend\n"
        "backend\n"
    )
    out = parse(tmp_path, "hosts")
    assert set(out["groups"]["web"]["children"]) == {"frontend", "backend"}
    assert set(out["hosts"]) == {"fe1", "be1"}


def test_parse_directory_inventory(tmp_path):
    d = tmp_path / "inventories" / "production"
    d.mkdir(parents=True)
    (d / "hosts").write_text("[web]\nweb1\n")
    out = parse(tmp_path, "inventories/production")
    assert "web1" in out["hosts"]


def test_parse_path_escape_rejected(tmp_path):
    with pytest.raises(ValueError):
        parse(tmp_path, "../../etc/hosts")


def test_parse_missing_file_raises(tmp_path):
    with pytest.raises(FileNotFoundError):
        parse(tmp_path, "nope")
