"""API-layer tests for project file CRUD endpoints.

Calls route functions directly (no TestClient/lifespan).  Storage uses the real
data/projects/ directory (same pattern as test_storage.py) — projects are created
before each test and deleted in a finally block.

Requires: git binary + GitPython (skipped otherwise, matching test_storage.py).
"""
from __future__ import annotations

import shutil
import pytest

pytest.importorskip("aiosqlite")
pytest.importorskip("cryptography")
pytest.importorskip("git")
if shutil.which("git") is None:
    pytest.skip("git binary not available", allow_module_level=True)

from fastapi import HTTPException

from app.core import storage
from app.api.projects import (
    put_file, get_file, delete_file as api_delete_file, get_tree,
    FileWriteIn,
)
from app.models.db import SessionLocal, Project, init_db


@pytest.fixture()
async def project_id():
    """Create a real project, seed DB, yield its id, then clean up."""
    await init_db()
    paths = storage.create_project("FileApiTest")
    pid = paths.project_id
    async with SessionLocal() as s:
        if await s.get(Project, pid) is None:
            s.add(Project(id=pid, name="FileApiTest"))
            await s.commit()
    try:
        yield pid
    finally:
        async with SessionLocal() as s:
            proj = await s.get(Project, pid)
            if proj:
                await s.delete(proj)
                await s.commit()
        storage.delete_project(pid)


# ---------- GET /{project_id}/file -------------------------------------------

async def test_get_existing_file(project_id):
    content = await get_file(project_id, "playbooks/site.yml")
    assert "hosts: all" in content["content"]
    assert content["path"] == "playbooks/site.yml"


async def test_get_missing_file_raises_404(project_id):
    with pytest.raises(HTTPException) as exc_info:
        await get_file(project_id, "playbooks/nope.yml")
    assert exc_info.value.status_code == 404


async def test_get_traversal_blocked(project_id):
    with pytest.raises(HTTPException) as exc_info:
        await get_file(project_id, "../../etc/passwd")
    assert exc_info.value.status_code == 404


# ---------- PUT /{project_id}/file -------------------------------------------

async def test_put_creates_new_file(project_id):
    payload = FileWriteIn(path="playbooks/deploy.yml", content="---\n- hosts: web\n  tasks: []\n")
    result = await put_file(project_id, payload)
    assert result == {"saved": "playbooks/deploy.yml"}
    read = await get_file(project_id, "playbooks/deploy.yml")
    assert "hosts: web" in read["content"]


async def test_put_creates_missing_parent_directories(project_id):
    payload = FileWriteIn(path="roles/myrole/tasks/main.yml", content="---\n- name: noop\n  debug:\n")
    result = await put_file(project_id, payload)
    assert result["saved"] == "roles/myrole/tasks/main.yml"


async def test_put_overwrites_existing_file(project_id):
    payload = FileWriteIn(path="playbooks/site.yml", content="---\n- hosts: new\n  tasks: []\n")
    await put_file(project_id, payload)
    read = await get_file(project_id, "playbooks/site.yml")
    assert "hosts: new" in read["content"]


async def test_put_traversal_blocked(project_id):
    payload = FileWriteIn(path="../escape.yml", content="bad")
    with pytest.raises(HTTPException) as exc_info:
        await put_file(project_id, payload)
    assert exc_info.value.status_code == 400


# ---------- DELETE /{project_id}/file ----------------------------------------

async def test_delete_existing_file(project_id):
    await put_file(project_id, FileWriteIn(path="tmp/deleteme.yml", content="---\n"))
    result = await api_delete_file(project_id, path="tmp/deleteme.yml")
    assert result == {"deleted": "tmp/deleteme.yml"}
    with pytest.raises(HTTPException) as exc_info:
        await get_file(project_id, "tmp/deleteme.yml")
    assert exc_info.value.status_code == 404


async def test_delete_missing_file_raises_404(project_id):
    with pytest.raises(HTTPException) as exc_info:
        await api_delete_file(project_id, path="nonexistent.yml")
    assert exc_info.value.status_code == 404


async def test_delete_traversal_blocked(project_id):
    with pytest.raises(HTTPException) as exc_info:
        await api_delete_file(project_id, path="../../etc/passwd")
    assert exc_info.value.status_code == 400


# ---------- GET /{project_id}/tree -------------------------------------------

async def test_tree_shows_all_files(project_id):
    tree_out = await get_tree(project_id)
    assert tree_out.project_id == project_id
    assert "playbooks" in tree_out.tree
    assert "site.yml" in tree_out.tree["playbooks"]


async def test_tree_reflects_new_file(project_id):
    await put_file(project_id, FileWriteIn(path="playbooks/new.yml", content="---\n- hosts: x\n"))
    tree_out = await get_tree(project_id)
    assert "new.yml" in tree_out.tree["playbooks"]


async def test_tree_after_delete(project_id):
    await put_file(project_id, FileWriteIn(path="tmp/gone.yml", content="---\n"))
    await api_delete_file(project_id, path="tmp/gone.yml")
    tree_out = await get_tree(project_id)
    assert "gone.yml" not in (tree_out.tree.get("tmp") or {})
