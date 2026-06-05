"""Unit tests for the INI inventory writer (comment-preserving, idempotent)."""
from __future__ import annotations

import pytest

from app.core.inventory_writer import (
    add_host,
    build_host_line,
    is_yaml_inventory,
    resolve_hosts_file,
)


# --- build_host_line --------------------------------------------------------

def test_host_line_plain():
    assert build_host_line("web1", {}) == "web1"


def test_host_line_with_vars():
    line = build_host_line("web1", {"ansible_host": "10.0.0.1", "ansible_port": 2222})
    assert line == "web1 ansible_host=10.0.0.1 ansible_port=2222"


def test_host_line_quotes_values_with_spaces():
    line = build_host_line("web1", {"descr": "main box"})
    assert line == 'web1 descr="main box"'


def test_host_line_skips_empty_values():
    line = build_host_line("web1", {"a": "", "b": None, "c": "x"})
    assert line == "web1 c=x"


# --- is_yaml_inventory ------------------------------------------------------

@pytest.mark.parametrize("text, expected", [
    ("---\nall:\n  hosts:\n    web1:\n", True),
    ("[web]\nweb1.example.com\n", False),
    ("# comment\nweb1.example.com\n", False),
    ("", False),
])
def test_is_yaml_inventory(text, expected):
    assert is_yaml_inventory(text) is expected


# --- add_host ---------------------------------------------------------------

def test_add_host_to_existing_group():
    text = "[web]\nweb1\n\n[db]\ndb1\n"
    out = add_host(text, "web", "web2")
    assert "web2" in out
    # inserted inside [web], before the blank line separating it from [db]
    assert out.index("web2") < out.index("[db]")


def test_add_host_creates_missing_group_at_eof():
    text = "[web]\nweb1\n"
    out = add_host(text, "db", "db1")
    assert out.strip().endswith("[db]\ndb1")


def test_add_host_is_idempotent():
    text = "[web]\nweb1\n"
    assert add_host(text, "web", "web1") == text


def test_add_host_preserves_comments():
    text = "# my inventory\n[web]\n# prod boxes\nweb1\n"
    out = add_host(text, "web", "web2")
    assert "# my inventory" in out
    assert "# prod boxes" in out


# --- resolve_hosts_file -----------------------------------------------------

def test_resolve_hosts_file_direct_file(tmp_path):
    f = tmp_path / "hosts"
    f.write_text("[web]\nweb1\n")
    assert resolve_hosts_file(tmp_path, "hosts") == f


def test_resolve_hosts_file_directory_with_hosts(tmp_path):
    d = tmp_path / "inventories" / "production"
    d.mkdir(parents=True)
    (d / "hosts").write_text("[web]\nweb1\n")
    assert resolve_hosts_file(tmp_path, "inventories/production") == d / "hosts"


def test_resolve_hosts_file_missing_raises(tmp_path):
    with pytest.raises(FileNotFoundError):
        resolve_hosts_file(tmp_path, "nope")


def test_add_host_does_not_pollute_children_section():
    """Regression: a host must never be appended into [group:children] / [group:vars];
    a real [group] hosts section is created instead."""
    text = "[web:children]\nfrontend\nbackend\n"
    out = add_host(text, "web", "web1.example.com")
    # children section is untouched...
    assert "[web:children]\nfrontend\nbackend" in out
    # ...and a proper [web] hosts group now holds the host.
    assert "[web]\nweb1.example.com" in out
    # the host is NOT a sibling of the child group names.
    children_block = out.split("[web]")[0]
    assert "web1.example.com" not in children_block


def test_add_host_into_vars_section_creates_hosts_group():
    text = "[db]\ndb1\n\n[db:vars]\nansible_user=postgres\n"
    out = add_host(text, "db", "db2")
    # db2 goes into the bare [db] group, before [db:vars]
    assert out.index("db2") < out.index("[db:vars]")
    assert "ansible_user=postgres" in out
