"""Integration tests for git remote sync (set-remote / push / pull).

Network-free: a local bare repo stands in for Gitea/GitHub. Needs GitPython +
the git binary, so the module skips where either is missing (runs in the image).
"""
from __future__ import annotations

import shutil

import pytest

pytest.importorskip("git")
if shutil.which("git") is None:
    pytest.skip("git binary not available", allow_module_level=True)

from git import Repo

from app.core import storage
from app.core.storage import StorageError


def _bare_remote(tmp_path):
    bare = tmp_path / "remote.git"
    Repo.init(str(bare), bare=True)
    return bare


# --- _auth_url --------------------------------------------------------------

def test_auth_url_splices_https_credentials():
    assert storage._auth_url("https://host/r.git", "u", "t") == "https://u:t@host/r.git"


def test_auth_url_leaves_ssh_untouched():
    assert storage._auth_url("git@host:o/r.git", "u", "t") == "git@host:o/r.git"


def test_auth_url_without_creds_unchanged():
    assert storage._auth_url("https://host/r.git", None, None) == "https://host/r.git"


# --- set_remote + info ------------------------------------------------------

def test_set_remote_and_info():
    p = storage.create_project("git-info")
    try:
        storage.set_remote(p.project_id, "https://example.com/r.git")
        info = storage.git_info(p.project_id)
        assert info["remote"] == "https://example.com/r.git"
        assert info["last_commit"]["message"].startswith("Initial project layout")
        assert info["branch"]
    finally:
        storage.delete_project(p.project_id)


# --- push -------------------------------------------------------------------

def test_push_lands_commit_on_remote(tmp_path):
    bare = _bare_remote(tmp_path)
    p = storage.create_project("git-push")
    try:
        storage.set_remote(p.project_id, str(bare))
        storage.git_push(p.project_id)
        # Inspect the bare repo's refs directly (no checkout, so default-branch
        # naming can't interfere).
        branch = storage.git_info(p.project_id)["branch"]
        bare_repo = Repo(str(bare))
        assert branch in [h.name for h in bare_repo.heads]
        assert "Initial project layout" in bare_repo.heads[branch].commit.message
    finally:
        storage.delete_project(p.project_id)


def test_push_without_remote_raises():
    p = storage.create_project("git-noremote")
    try:
        with pytest.raises(StorageError, match="no remote"):
            storage.git_push(p.project_id)
    finally:
        storage.delete_project(p.project_id)


# --- pull -------------------------------------------------------------------

def test_pull_brings_remote_commits(tmp_path):
    bare = _bare_remote(tmp_path)
    p = storage.create_project("git-pull")
    try:
        storage.set_remote(p.project_id, str(bare))
        storage.git_push(p.project_id)

        # Push a further commit so the remote is one ahead of where we'll rewind to.
        storage.write_file(p.project_id, "REMOTE_ADDED.yml", "added: true\n", "remote change")
        storage.git_push(p.project_id)

        # Rewind the local branch: now the project is behind the remote.
        Repo(str(p.root)).git.reset("--hard", "HEAD~1")
        assert not (p.root / "REMOTE_ADDED.yml").exists()

        # Pull fast-forwards and restores the remote-only commit.
        storage.git_pull(p.project_id)
        assert (p.root / "REMOTE_ADDED.yml").is_file()
    finally:
        storage.delete_project(p.project_id)
