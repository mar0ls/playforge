"""Unit tests for tag extraction from playbook YAML."""
from __future__ import annotations

from app.core.playbook_tags import collect


def test_collect_string_and_list_tags(tmp_path):
    pb = tmp_path / "site.yml"
    pb.write_text(
        "---\n"
        "- hosts: all\n"
        "  tasks:\n"
        "    - name: one\n"
        "      ansible.builtin.debug: {}\n"
        "      tags: setup\n"
        "    - name: two\n"
        "      ansible.builtin.debug: {}\n"
        "      tags: [deploy, restart]\n"
    )
    assert collect(pb) == ["deploy", "restart", "setup"]


def test_pseudo_tags_filtered(tmp_path):
    pb = tmp_path / "site.yml"
    pb.write_text(
        "---\n"
        "- hosts: all\n"
        "  tasks:\n"
        "    - name: t\n"
        "      ansible.builtin.debug: {}\n"
        "      tags: [always, never, real]\n"
    )
    assert collect(pb) == ["real"]


def test_malformed_yaml_returns_empty(tmp_path):
    pb = tmp_path / "broken.yml"
    pb.write_text("---\n- hosts: [unclosed\n")
    assert collect(pb) == []


def test_missing_file_returns_empty(tmp_path):
    assert collect(tmp_path / "does-not-exist.yml") == []
