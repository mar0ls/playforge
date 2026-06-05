"""Integration tests for project storage (path sandboxing + git auto-commit).

Needs GitPython and the `git` binary, so the whole module is skipped where either
is missing (e.g. a bare CI venv). It runs in the Docker image, which has both.
"""
from __future__ import annotations

import shutil

import pytest

pytest.importorskip("git")
if shutil.which("git") is None:
    pytest.skip("git binary not available", allow_module_level=True)

from app.core import storage
from app.core.storage import StorageError


def test_create_project_lays_out_skeleton():
    paths = storage.create_project("demo")
    try:
        assert paths.root.is_dir()
        assert (paths.root / "ansible.cfg").is_file()
        assert (paths.root / "playbooks" / "site.yml").is_file()
        assert (paths.root / "inventories" / "production" / "hosts").is_file()
        assert (paths.root / ".git").is_dir()  # committed
    finally:
        storage.delete_project(paths.project_id)


def test_resolve_safe_rejects_escape():
    paths = storage.create_project("safe")
    try:
        with pytest.raises(StorageError):
            storage._resolve_safe(paths.root, "../../../etc/passwd")
    finally:
        storage.delete_project(paths.project_id)


def test_write_then_read_roundtrip_and_commit():
    paths = storage.create_project("rw")
    try:
        storage.write_file(paths.project_id, "group_vars/all.yml", "key: value\n", "add vars")
        assert storage.read_file(paths.project_id, "group_vars/all.yml") == "key: value\n"
        # The write should have produced a commit beyond the initial one.
        from git import Repo
        assert len(list(Repo(paths.root).iter_commits())) >= 2
    finally:
        storage.delete_project(paths.project_id)


def test_read_file_traversal_blocked():
    paths = storage.create_project("trav")
    try:
        with pytest.raises(StorageError):
            storage.read_file(paths.project_id, "../../secrets")
    finally:
        storage.delete_project(paths.project_id)


def test_import_directory_strips_inner_git(tmp_path):
    src = tmp_path / "existing"
    (src / "playbooks").mkdir(parents=True)
    (src / "playbooks" / "site.yml").write_text("---\n- hosts: all\n")
    (src / ".git").mkdir()  # simulate an existing repo
    (src / ".git" / "OLD_MARKER").write_text("from the source repo\n")

    paths = storage.import_directory("imported", src)
    try:
        assert (paths.root / "playbooks" / "site.yml").is_file()
        assert not (paths.root / ".git" / "OLD_MARKER").exists()  # source .git not copied
        # Fresh history: the imported .git must be ours, with exactly one commit.
        from git import Repo
        commits = list(Repo(paths.root).iter_commits())
        assert len(commits) == 1
        assert "Import imported" in commits[0].message
    finally:
        storage.delete_project(paths.project_id)


def test_import_directory_skips_venv_caches_and_cruft(tmp_path):
    """Regression for a real import (opnet shipped a 5000-file .venv + caches).
    Junk must not land in the project or its git history."""
    src = tmp_path / "realproj"
    (src / "playbooks").mkdir(parents=True)
    (src / "playbooks" / "site.yml").write_text("---\n- hosts: all\n  roles: [common]\n")
    (src / "roles" / "common" / "tasks").mkdir(parents=True)
    (src / "roles" / "common" / "tasks" / "main.yml").write_text("---\n- ansible.builtin.ping:\n")
    # Junk that real-world repos carry around:
    (src / ".venv" / "bin").mkdir(parents=True)
    (src / ".venv" / "bin" / "ansible").write_text("#!/bin/sh\n")
    (src / "filter_plugins" / "__pycache__").mkdir(parents=True)
    (src / "filter_plugins" / "__pycache__" / "topology.cpython-313.pyc").write_text("x")
    (src / ".pytest_cache").mkdir()
    (src / ".pytest_cache" / "README.md").write_text("cache")
    (src / "run_output.log").write_text("noise")
    (src / ".DS_Store").write_text("macos")

    paths = storage.import_directory("realproj", src)
    try:
        # Real content preserved.
        assert (paths.root / "playbooks" / "site.yml").is_file()
        assert (paths.root / "roles" / "common" / "tasks" / "main.yml").is_file()
        # Junk dropped.
        assert not (paths.root / ".venv").exists()
        assert not any(p.name == "__pycache__" for p in paths.root.rglob("*"))
        assert not (paths.root / ".pytest_cache").exists()
        assert not (paths.root / "run_output.log").exists()
        assert not (paths.root / ".DS_Store").exists()
        # And none of it is in the git index either.
        from git import Repo
        tracked = {e[0] for e in Repo(paths.root).index.entries}
        assert not any(".venv" in t or "__pycache__" in t or t.endswith(".log") for t in tracked)
    finally:
        storage.delete_project(paths.project_id)


def test_commit_all_captures_run_artifacts():
    """After a run, files the playbook wrote into the repo (e.g. generated keys)
    must be committed and reported — the run-artifact persistence path."""
    paths = storage.create_project("artifacts")
    try:
        # Simulate a run writing artifacts directly into the project tree.
        (paths.root / "keys").mkdir()
        (paths.root / "keys" / "id_ed25519").write_text("PRIVATE KEY\n")
        (paths.root / "rendered.conf").write_text("generated = true\n")

        changed = storage.commit_all(paths.project_id, "Run #7 artifacts")
        assert "keys/id_ed25519" in changed
        assert "rendered.conf" in changed
        # They are now committed (clean working tree) and on disk.
        from git import Repo
        assert not Repo(paths.root).is_dirty(untracked_files=True)
        assert (paths.root / "keys" / "id_ed25519").is_file()
        # A second call with no changes reports nothing.
        assert storage.commit_all(paths.project_id, "noop") == []
    finally:
        storage.delete_project(paths.project_id)


def test_commit_all_ignores_git_internals():
    paths = storage.create_project("artifacts2")
    try:
        (paths.root / "out.txt").write_text("x\n")
        changed = storage.commit_all(paths.project_id, "Run artifacts")
        assert changed == ["out.txt"]
        assert all(".git" not in c for c in changed)
    finally:
        storage.delete_project(paths.project_id)


def test_import_flat_layout_project(tmp_path):
    """ssh_playbook-style layout: playbook + inventory at the repo root (no playbooks/ dir)."""
    src = tmp_path / "flat"
    src.mkdir()
    (src / "ssh_playbook.yml").write_text("---\n- hosts: all\n  tasks: []\n")
    (src / "hosts.ini").write_text("[vm]\nvps1 ansible_host=10.0.0.1\n")

    paths = storage.import_directory("flat", src)
    try:
        assert (paths.root / "ssh_playbook.yml").is_file()
        assert (paths.root / "hosts.ini").is_file()
    finally:
        storage.delete_project(paths.project_id)
