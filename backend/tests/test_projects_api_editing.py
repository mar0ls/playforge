"""API-layer tests for the project editing endpoints that need nothing external.

`app/api/projects.py` is the least covered module in the codebase: the core
helpers underneath it (storage, playbook_builder, inventory_writer, detect) are
well tested, but the routes wrapping them — the layer that decides which failure
becomes a 400, a 404 or a 409 — were not exercised at all. Those status codes are
a contract the UI depends on, and from 1.0 they are a promise.

Calls route functions directly (no TestClient/lifespan), same pattern as
test_projects_file_api.py. Endpoints needing a binary (`lint`) or the network
(git, galaxy) are deliberately out of scope here.

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

from app.api.projects import (
    InventoryHostIn,
    MkdirIn,
    MovePathIn,
    NewPlaybookIn,
    ProjectSettingsIn,
    add_inventory_host,
    detected,
    get_playbook_tags,
    get_project_settings,
    make_dir,
    move_path,
    new_playbook,
    playbook_preview,
    put_project_settings,
)
from app.core import storage
from app.models.db import Project, SessionLocal, init_db


@pytest.fixture()
async def project_id():
    """A real project on disk plus its DB row, torn down afterwards."""
    await init_db()
    paths = storage.create_project("EditingApiTest")
    pid = paths.project_id
    async with SessionLocal() as s:
        if await s.get(Project, pid) is None:
            s.add(Project(id=pid, name="EditingApiTest"))
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


# ---------- settings ---------------------------------------------------------

async def test_protect_secrets_defaults_to_false(project_id):
    assert await get_project_settings(project_id) == {"protect_secrets": False}


async def test_protect_secrets_round_trips(project_id):
    assert (await put_project_settings(project_id, ProjectSettingsIn(protect_secrets=True)))[
        "protect_secrets"
    ] is True
    assert (await get_project_settings(project_id))["protect_secrets"] is True

    assert (await put_project_settings(project_id, ProjectSettingsIn(protect_secrets=False)))[
        "protect_secrets"
    ] is False
    assert (await get_project_settings(project_id))["protect_secrets"] is False


async def test_omitted_field_leaves_the_setting_alone(project_id):
    """`None` means "not supplied", not "set to false"."""
    await put_project_settings(project_id, ProjectSettingsIn(protect_secrets=True))
    await put_project_settings(project_id, ProjectSettingsIn())
    assert (await get_project_settings(project_id))["protect_secrets"] is True


async def test_the_setting_is_per_project(project_id):
    """The store is one flat key space; the project id has to be in the key."""
    other = storage.create_project("EditingApiTestOther")
    try:
        await put_project_settings(project_id, ProjectSettingsIn(protect_secrets=True))
        assert (await get_project_settings(other.project_id))["protect_secrets"] is False
    finally:
        storage.delete_project(other.project_id)


# ---------- detected ---------------------------------------------------------

async def test_detected_finds_the_scaffold(project_id):
    result = await detected(project_id)
    assert "playbooks/site.yml" in result["playbooks"]
    assert result["ansible_cfg"] is True
    # The scaffold's inventory is a directory; detect prefers it over the inner file.
    assert "inventories/production" in result["inventories"]


async def test_detected_on_unknown_project_is_404():
    with pytest.raises(HTTPException) as exc:
        await detected(MISSING)
    assert exc.value.status_code == 404


# ---------- dir / move -------------------------------------------------------

async def test_make_dir_creates_it(project_id):
    await make_dir(project_id, MkdirIn(path="group_vars/webservers"))
    assert (storage.paths_for(project_id).root / "group_vars" / "webservers").is_dir()


async def test_make_dir_refuses_to_escape_the_project(project_id):
    with pytest.raises(HTTPException) as exc:
        await make_dir(project_id, MkdirIn(path="../escaped"))
    assert exc.value.status_code == 400


async def test_move_path_renames_a_file(project_id):
    result = await move_path(project_id, MovePathIn(src="playbooks/site.yml", dst="playbooks/main.yml"))
    assert result["moved"] == "playbooks/site.yml"
    root = storage.paths_for(project_id).root
    assert (root / "playbooks" / "main.yml").is_file()
    assert not (root / "playbooks" / "site.yml").exists()


async def test_move_path_with_a_missing_source_is_400(project_id):
    with pytest.raises(HTTPException) as exc:
        await move_path(project_id, MovePathIn(src="playbooks/nope.yml", dst="playbooks/x.yml"))
    assert exc.value.status_code == 400


# ---------- tags -------------------------------------------------------------

TAGGED_PLAYBOOK = """---
- name: Tagged play
  hosts: all
  tasks:
    - name: One
      ansible.builtin.ping:
      tags: [install]
    - name: Two
      ansible.builtin.ping:
      tags: [configure]
"""


async def test_tags_are_collected_from_a_playbook(project_id):
    storage.write_file(project_id, "playbooks/tagged.yml", TAGGED_PLAYBOOK, message="test")
    result = await get_playbook_tags(project_id, "playbooks/tagged.yml")
    assert set(result["tags"]) >= {"install", "configure"}


async def test_tags_for_a_missing_playbook_is_404(project_id):
    with pytest.raises(HTTPException) as exc:
        await get_playbook_tags(project_id, "playbooks/nope.yml")
    assert exc.value.status_code == 404


async def test_tags_refuses_a_path_outside_the_project(project_id):
    with pytest.raises(HTTPException) as exc:
        await get_playbook_tags(project_id, "../../etc/passwd")
    assert exc.value.status_code == 400


# ---------- playbook builder -------------------------------------------------

GOOD_SPEC = {
    "name": "Deploy webapp",
    "hosts": "web",
    "become": True,
    "tasks": [
        {"name": "Install nginx", "module": "ansible.builtin.apt",
         "args_yaml": "name: nginx\nstate: present"},
    ],
}

# A task with no module: the builder rejects it, and the route must turn that
# into a 400 rather than a 500.
BAD_SPEC = {"name": "Broken", "hosts": "all", "tasks": [{"name": "No module here"}]}


def test_preview_renders_yaml_without_writing(project_id):
    result = playbook_preview(project_id, NewPlaybookIn(path="playbooks/x.yml", spec=GOOD_SPEC))
    assert "hosts: web" in result["yaml"]
    assert "ansible.builtin.apt" in result["yaml"]
    assert not (storage.paths_for(project_id).root / "playbooks" / "x.yml").exists()


def test_preview_of_an_invalid_spec_is_400(project_id):
    with pytest.raises(HTTPException) as exc:
        playbook_preview(project_id, NewPlaybookIn(path="playbooks/x.yml", spec=BAD_SPEC))
    assert exc.value.status_code == 400


def test_new_playbook_saves_the_file(project_id):
    result = new_playbook(project_id, NewPlaybookIn(path="playbooks/deploy.yml", spec=GOOD_SPEC))
    assert result["saved"] == "playbooks/deploy.yml"
    written = (storage.paths_for(project_id).root / "playbooks" / "deploy.yml").read_text()
    assert "hosts: web" in written


def test_new_playbook_requires_a_yaml_extension(project_id):
    with pytest.raises(HTTPException) as exc:
        new_playbook(project_id, NewPlaybookIn(path="playbooks/deploy.txt", spec=GOOD_SPEC))
    assert exc.value.status_code == 400


def test_new_playbook_refuses_to_clobber_without_overwrite(project_id):
    new_playbook(project_id, NewPlaybookIn(path="playbooks/once.yml", spec=GOOD_SPEC))
    with pytest.raises(HTTPException) as exc:
        new_playbook(project_id, NewPlaybookIn(path="playbooks/once.yml", spec=GOOD_SPEC))
    assert exc.value.status_code == 409


def test_new_playbook_overwrites_when_asked(project_id):
    new_playbook(project_id, NewPlaybookIn(path="playbooks/twice.yml", spec=GOOD_SPEC))
    changed = {**GOOD_SPEC, "hosts": "db"}
    result = new_playbook(
        project_id, NewPlaybookIn(path="playbooks/twice.yml", spec=changed, overwrite=True)
    )
    assert "hosts: db" in result["yaml"]


def test_new_playbook_on_unknown_project_is_404():
    with pytest.raises(HTTPException) as exc:
        new_playbook(MISSING, NewPlaybookIn(path="playbooks/x.yml", spec=GOOD_SPEC))
    assert exc.value.status_code == 404


# ---------- inventory host ---------------------------------------------------

def test_add_host_appends_to_the_ini_inventory(project_id):
    """The scaffold's inventory pointer is a directory — the route resolves the
    `hosts` file inside it."""
    result = add_inventory_host(
        project_id,
        InventoryHostIn(inventory_path="inventories/production", name="web1.example.com", group="web"),
    )
    assert result["saved"] is True
    assert result["format"] == "ini"
    text = (storage.paths_for(project_id).root / "inventories" / "production" / "hosts").read_text()
    assert "[web]" in text
    assert "web1.example.com" in text


def test_adding_the_same_host_twice_is_a_no_op(project_id):
    payload = InventoryHostIn(
        inventory_path="inventories/production", name="web1.example.com", group="web"
    )
    add_inventory_host(project_id, payload)
    second = add_inventory_host(project_id, payload)
    assert second["saved"] is False


def test_add_host_requires_a_name_and_a_group(project_id):
    with pytest.raises(HTTPException) as exc:
        add_inventory_host(
            project_id,
            InventoryHostIn(inventory_path="inventories/production", name="   ", group="web"),
        )
    assert exc.value.status_code == 400


def test_add_host_to_a_missing_inventory_is_404(project_id):
    with pytest.raises(HTTPException) as exc:
        add_inventory_host(
            project_id,
            InventoryHostIn(inventory_path="inventories/nowhere", name="h", group="g"),
        )
    assert exc.value.status_code == 404
