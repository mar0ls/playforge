"""Tests for full RAG over a project's file contents."""
from __future__ import annotations

import shutil

import pytest

pytest.importorskip("git")
if shutil.which("git") is None:
    pytest.skip("git binary not available", allow_module_level=True)

from app.core import project_index, storage


def _seed(project_id, files: dict):
    for path, content in files.items():
        storage.write_file(project_id, path, content)


def test_search_ranks_relevant_file_first():
    p = storage.create_project("rag")
    try:
        _seed(p.project_id, {
            "playbooks/nginx.yml": "- hosts: web\n  tasks:\n    - name: install nginx\n      ansible.builtin.apt: {name: nginx}\n",
            "playbooks/db.yml": "- hosts: db\n  tasks:\n    - name: install postgres\n      ansible.builtin.apt: {name: postgresql}\n",
            "group_vars/all.yml": "http_port: 8080\n",
        })
        project_index.invalidate(p.project_id)
        hits = project_index.search(p.project_id, "where do I configure nginx?", 3)
        assert hits
        assert hits[0]["path"] == "playbooks/nginx.yml"
        assert "nginx" in hits[0]["snippet"]
    finally:
        storage.delete_project(p.project_id)


def test_search_empty_project():
    p = storage.create_project("rag-empty-ish")
    try:
        # Scaffolded project has some files; a nonsense query should still not crash.
        hits = project_index.search(p.project_id, "zzz_no_match_term_xyz", 3)
        assert isinstance(hits, list)
    finally:
        storage.delete_project(p.project_id)


def test_index_invalidates_on_edit():
    p = storage.create_project("rag-edit")
    try:
        _seed(p.project_id, {"playbooks/site.yml": "- hosts: all\n  tasks: []\n"})
        project_index.invalidate(p.project_id)
        first = project_index.search(p.project_id, "wireguard tunnel", 3)
        assert not any("wireguard" in h["snippet"] for h in first)
        # add a file mentioning wireguard → new content must be searchable
        _seed(p.project_id, {"roles/wg/tasks/main.yml": "- name: set up wireguard tunnel\n  ansible.builtin.command: wg show\n"})
        hits = project_index.search(p.project_id, "wireguard tunnel", 3)
        assert any("wireguard" in h["snippet"] for h in hits)
    finally:
        storage.delete_project(p.project_id)


def test_collections_dir_not_indexed():
    p = storage.create_project("rag-coll")
    try:
        _seed(p.project_id, {
            "playbooks/site.yml": "- hosts: all\n",
            "collections/ansible_collections/community/general/plugins/modules/ufw.py": "# huge installed module\nufw stuff\n",
        })
        project_index.invalidate(p.project_id)
        hits = project_index.search(p.project_id, "ufw", 5)
        assert all("collections/" not in h["path"] for h in hits)
    finally:
        storage.delete_project(p.project_id)
