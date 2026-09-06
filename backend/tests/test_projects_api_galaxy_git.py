"""API-layer tests for the Galaxy and git-remote endpoints of `projects.py`.

Everything that would reach the network is mocked at the boundary
(`galaxy.install` / `add_dependency` / `remove_dependency`, and `storage.git_push`
for the success path). Everything else runs for real: `list_installed` reads
actual directories, and the no-remote failures come from a real repository with
no `origin`, which is how they happen to a user.

The cache-invalidation test is the point of this file. `galaxy add/remove` must
flush every snapshot of ansible-doc state at once — the RAG index, the
known-modules set used by the anti-hallucination layer, and the chat reply cache
whose key hashes a RAG-built prompt. Leaving any single one stale is what makes a
freshly installed collection still read as "doesn't exist".

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

from app.api import projects as projects_api
from app.api.projects import (
    GalaxyDepIn,
    GalaxyInstallIn,
    GitAuthIn,
    GitRemoteIn,
    galaxy_add,
    galaxy_install,
    galaxy_remove,
    galaxy_status,
    git_pull,
    git_push,
    git_set_remote,
    git_status,
)
from app.core import galaxy, storage
from app.models.db import Project, SessionLocal, init_db


@pytest.fixture()
async def project_id():
    await init_db()
    paths = storage.create_project("GalaxyGitApiTest")
    pid = paths.project_id
    async with SessionLocal() as s:
        if await s.get(Project, pid) is None:
            s.add(Project(id=pid, name="GalaxyGitApiTest"))
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


MISSING = "no-such-project-id"


# ---------- galaxy status ----------------------------------------------------

async def test_status_reports_no_requirements_file(project_id):
    result = await galaxy_status(project_id)
    assert result["requirements"] is None
    assert result["requirements_path"] == "requirements.yml"


async def test_status_returns_the_requirements_content(project_id):
    storage.write_file(project_id, "requirements.yml", "collections:\n  - community.general\n",
                       message="test")
    assert "community.general" in (await galaxy_status(project_id))["requirements"]


async def test_status_lists_what_is_installed_on_disk(project_id):
    """No mock: list_installed just walks roles/ and collections/."""
    root = storage.paths_for(project_id).root
    (root / "roles" / "myrole").mkdir(parents=True, exist_ok=True)
    (root / "collections" / "ansible_collections" / "community" / "general").mkdir(parents=True)

    installed = (await galaxy_status(project_id))["installed"]
    assert "myrole" in installed["roles"]
    assert "community.general" in installed["collections"]


async def test_status_on_unknown_project_is_404():
    with pytest.raises(HTTPException) as exc:
        await galaxy_status(MISSING)
    assert exc.value.status_code == 404


# ---------- galaxy install ---------------------------------------------------

async def test_install_adds_the_installed_listing_to_the_result(project_id, monkeypatch):
    monkeypatch.setattr(galaxy, "install", lambda root, rel: {"ok": True, "output": "done"})
    result = await galaxy_install(project_id, GalaxyInstallIn())
    assert result["ok"] is True
    # The route enriches whatever galaxy.install returned.
    assert result["installed"] == {"roles": [], "collections": []}


async def test_install_turns_a_galaxy_error_into_400(project_id, monkeypatch):
    def boom(root, rel):
        raise galaxy.GalaxyError("requirements file not found: requirements.yml")

    monkeypatch.setattr(galaxy, "install", boom)
    with pytest.raises(HTTPException) as exc:
        await galaxy_install(project_id, GalaxyInstallIn())
    assert exc.value.status_code == 400
    assert "requirements file not found" in exc.value.detail


async def test_install_on_unknown_project_is_404():
    with pytest.raises(HTTPException) as exc:
        await galaxy_install(MISSING, GalaxyInstallIn())
    assert exc.value.status_code == 404


# ---------- cache invalidation ----------------------------------------------

CACHES = ["_index", "_collection_corpus", "_module_corpus", "module_params"]


@pytest.fixture()
def cache_spy(monkeypatch):
    """Replace every cache the invalidation is supposed to flush with a recorder.

    `_invalidate_module_caches` imports these modules inside the function body, so
    patching the module attributes is enough — the lookup happens at call time.
    """
    from app.core import ai, ai_validate, doc_index

    cleared: list[str] = []

    class _Cache:
        def __init__(self, label: str):
            self._label = label

        def cache_clear(self) -> None:
            cleared.append(self._label)

    for name in CACHES:
        monkeypatch.setattr(doc_index, name, _Cache(f"doc_index.{name}"))
    monkeypatch.setattr(ai_validate, "known_modules", _Cache("ai_validate.known_modules"))
    monkeypatch.setattr(ai, "clear_chat_cache", lambda: cleared.append("ai.clear_chat_cache"))
    return cleared


EXPECTED_CLEARS = {
    "doc_index._index",
    "doc_index._collection_corpus",
    "doc_index._module_corpus",
    "doc_index.module_params",
    "ai_validate.known_modules",
    "ai.clear_chat_cache",
}


async def test_add_flushes_every_module_cache(project_id, monkeypatch, cache_spy):
    monkeypatch.setattr(galaxy, "add_dependency",
                        lambda root, kind, name: {"added": name, "kind": kind})
    await galaxy_add(project_id, GalaxyDepIn(kind="collection", name="community.general"))
    assert set(cache_spy) == EXPECTED_CLEARS


async def test_remove_flushes_every_module_cache(project_id, monkeypatch, cache_spy):
    monkeypatch.setattr(galaxy, "remove_dependency",
                        lambda root, kind, name: {"removed": name, "kind": kind})
    await galaxy_remove(project_id, GalaxyDepIn(kind="collection", name="community.general"))
    assert set(cache_spy) == EXPECTED_CLEARS


async def test_a_failed_add_leaves_the_caches_alone(project_id, monkeypatch, cache_spy):
    """Nothing changed on disk, so flushing would only cost a rebuild."""
    def boom(root, kind, name):
        raise galaxy.GalaxyError("invalid name")

    monkeypatch.setattr(galaxy, "add_dependency", boom)
    with pytest.raises(HTTPException) as exc:
        await galaxy_add(project_id, GalaxyDepIn(kind="collection", name="bad name"))
    assert exc.value.status_code == 400
    assert cache_spy == []


async def test_add_survives_a_failing_commit(project_id, monkeypatch, cache_spy):
    """The install already happened; a git failure must not turn it into a 500."""
    monkeypatch.setattr(galaxy, "add_dependency", lambda root, kind, name: {"added": name})

    def bad_commit(pid, message):
        raise RuntimeError("git index locked")

    monkeypatch.setattr(storage, "commit_all", bad_commit)
    result = await galaxy_add(project_id, GalaxyDepIn(kind="role", name="geerlingguy.nginx"))
    assert result["added"] == "geerlingguy.nginx"


async def test_add_commits_the_requirements_change(project_id, monkeypatch, cache_spy):
    monkeypatch.setattr(galaxy, "add_dependency", lambda root, kind, name: {"added": name})
    seen: list[str] = []
    monkeypatch.setattr(storage, "commit_all", lambda pid, message: seen.append(message))

    await galaxy_add(project_id, GalaxyDepIn(kind="role", name="geerlingguy.nginx"))
    assert seen == ["Galaxy: add role geerlingguy.nginx"]


# ---------- git --------------------------------------------------------------

async def test_git_status_of_a_fresh_project(project_id):
    info = await git_status(project_id)
    assert info["remote"] is None
    assert info["branch"]
    assert info["last_commit"]["message"]


async def test_git_status_on_unknown_project_is_404():
    with pytest.raises(HTTPException) as exc:
        await git_status(MISSING)
    assert exc.value.status_code == 404


async def test_setting_a_remote_shows_up_in_status(project_id):
    """Setting a remote is local git config — nothing is contacted."""
    url = "https://example.invalid/repo.git"
    result = await git_set_remote(project_id, GitRemoteIn(url=url))
    assert result["remote"] == url
    assert (await git_status(project_id))["remote"] == url


async def test_setting_a_remote_twice_replaces_it(project_id):
    await git_set_remote(project_id, GitRemoteIn(url="https://example.invalid/one.git"))
    result = await git_set_remote(project_id, GitRemoteIn(url="https://example.invalid/two.git"))
    assert result["remote"] == "https://example.invalid/two.git"


async def test_an_empty_remote_url_is_400(project_id):
    with pytest.raises(HTTPException) as exc:
        await git_set_remote(project_id, GitRemoteIn(url="   "))
    assert exc.value.status_code == 400


async def test_push_without_a_remote_is_400(project_id):
    """Real storage.git_push against a real repo with no origin."""
    with pytest.raises(HTTPException) as exc:
        await git_push(project_id, GitAuthIn())
    assert exc.value.status_code == 400
    assert "no remote configured" in exc.value.detail


async def test_pull_without_a_remote_is_400(project_id):
    with pytest.raises(HTTPException) as exc:
        await git_pull(project_id, GitAuthIn())
    assert exc.value.status_code == 400
    assert "no remote configured" in exc.value.detail


async def test_push_returns_the_output_and_fresh_info(project_id, monkeypatch):
    await git_set_remote(project_id, GitRemoteIn(url="https://example.invalid/repo.git"))
    monkeypatch.setattr(storage, "git_push", lambda pid, user, token: "pushed main to origin")

    result = await git_push(project_id, GitAuthIn(username="u", token="t"))
    assert result["output"] == "pushed main to origin"
    assert result["info"]["remote"] == "https://example.invalid/repo.git"


async def test_pull_returns_the_output_and_fresh_info(project_id, monkeypatch):
    await git_set_remote(project_id, GitRemoteIn(url="https://example.invalid/repo.git"))
    monkeypatch.setattr(storage, "git_pull", lambda pid, user, token: "Already up to date.")

    result = await git_pull(project_id, GitAuthIn())
    assert result["output"] == "Already up to date."
    assert result["info"]["branch"]


async def test_the_route_module_and_storage_share_one_object(project_id):
    """Guards the monkeypatching above: the route calls `storage.git_push`
    through the module, so patching `app.core.storage` reaches it."""
    assert projects_api.storage is storage
