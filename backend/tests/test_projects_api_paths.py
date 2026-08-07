"""projects API: path containment, file lifecycle, and import validation.

`api/projects.py` is the biggest module in the app and was the least covered. The
tests here go after the parts where being wrong is expensive: escaping the
project directory, deleting the wrong thing, and importing from somewhere the
caller shouldn't reach.
"""
from __future__ import annotations

import pytest

pytest.importorskip("aiosqlite")
pytest.importorskip("git")

from fastapi import HTTPException

from app.api.projects import (
    ImportPathIn, MkdirIn, MovePathIn, FileWriteIn, ProjectIn,
    create_project, delete_file, file_at_sha, file_history, get_file, get_tree,
    import_path, make_dir, move_path, put_file,
)
from app.core import storage
from app.models.db import init_db


@pytest.fixture(autouse=True)
async def _db():
    await init_db()


@pytest.fixture
async def project():
    out = await create_project(ProjectIn(name="paths-test", description=""))
    await put_file(out.id, FileWriteIn(path="playbooks/site.yml",
                                       content="---\n- hosts: all\n  tasks: []\n"))
    return out.id


# --- containment -------------------------------------------------------------

# Paths that must never reach outside the project directory. Note `~` is NOT a
# traversal here: pathlib doesn't expand it when joining, and `_resolve_safe`
# deliberately never calls expanduser(), so "~/x" becomes a literal directory
# named "~" inside the project. It's included to pin that behaviour.
TRAVERSALS = [
    "../../../etc/passwd",
    "/etc/passwd",
    "playbooks/../../../../etc/passwd",
    "~/.ssh/id_rsa",
]


def _assert_contained(project_id: str, path: str) -> None:
    """The operation may be refused, but it must never resolve outside the root."""
    root = storage.paths_for(project_id).root.resolve()
    resolved = storage._resolve_safe(root, path)
    assert resolved.is_relative_to(root), f"{path!r} escaped to {resolved}"


@pytest.mark.parametrize("bad", TRAVERSALS)
async def test_resolution_never_escapes_the_project(project, bad):
    """The single property everything below depends on."""
    try:
        _assert_contained(project, bad)
    except storage.StorageError:
        pass  # refused outright, which is also fine


@pytest.mark.parametrize("bad", TRAVERSALS)
async def test_read_cannot_escape_the_project(project, bad):
    with pytest.raises(HTTPException) as e:
        await get_file(project, bad)
    assert e.value.status_code in (400, 404)


@pytest.mark.parametrize("bad", TRAVERSALS)
async def test_write_is_refused_or_stays_inside(project, bad):
    try:
        await put_file(project, FileWriteIn(path=bad, content="pwned"))
    except HTTPException as e:
        assert e.status_code in (400, 404)
    else:
        _assert_contained(project, bad)


@pytest.mark.parametrize("bad", TRAVERSALS)
async def test_delete_cannot_escape_the_project(project, bad):
    with pytest.raises(HTTPException) as e:
        await delete_file(project, bad)
    assert e.value.status_code in (400, 404)


@pytest.mark.parametrize("bad", TRAVERSALS)
async def test_move_is_refused_or_stays_inside(project, bad):
    try:
        await move_path(project, MovePathIn(src="playbooks/site.yml", dst=bad))
    except HTTPException as e:
        assert e.status_code in (400, 404)
    else:
        _assert_contained(project, bad)


@pytest.mark.parametrize("bad", TRAVERSALS)
async def test_mkdir_is_refused_or_stays_inside(project, bad):
    try:
        await make_dir(project, MkdirIn(path=bad))
    except HTTPException as e:
        assert e.status_code in (400, 404)
    else:
        _assert_contained(project, bad)


async def test_writing_outside_leaves_no_file_behind(project, tmp_path):
    """Belt and braces: the escape is refused *and* nothing lands on disk."""
    victim = tmp_path / "victim.txt"
    with pytest.raises(HTTPException):
        await put_file(project, FileWriteIn(path=str(victim), content="pwned"))
    assert not victim.exists()


# --- unknown project ---------------------------------------------------------

async def test_tree_of_unknown_project_is_404():
    with pytest.raises(HTTPException) as e:
        await get_tree("no-such-project")
    assert e.value.status_code == 404


async def test_read_from_unknown_project_is_404():
    with pytest.raises(HTTPException) as e:
        await get_file("no-such-project", "site.yml")
    assert e.value.status_code == 404


async def test_delete_in_unknown_project_is_404():
    with pytest.raises(HTTPException) as e:
        await delete_file("no-such-project", "site.yml")
    assert e.value.status_code == 404


# --- file lifecycle ----------------------------------------------------------

async def test_write_then_read_roundtrip(project):
    await put_file(project, FileWriteIn(path="vars/main.yml", content="key: value\n"))

    got = await get_file(project, "vars/main.yml")

    assert got["content"] == "key: value\n"


async def test_written_file_appears_in_the_tree(project):
    await put_file(project, FileWriteIn(path="roles/web/tasks/main.yml", content="[]\n"))

    tree = await get_tree(project)

    assert "roles" in str(tree.tree)


async def test_delete_removes_the_file(project):
    await put_file(project, FileWriteIn(path="scratch.yml", content="x\n"))

    await delete_file(project, "scratch.yml")

    with pytest.raises(HTTPException) as e:
        await get_file(project, "scratch.yml")
    assert e.value.status_code == 404


async def test_deleting_a_missing_file_is_404(project):
    with pytest.raises(HTTPException) as e:
        await delete_file(project, "never-existed.yml")
    assert e.value.status_code == 404


async def test_move_relocates_the_file(project):
    await move_path(project, MovePathIn(src="playbooks/site.yml", dst="playbooks/main.yml"))

    assert (await get_file(project, "playbooks/main.yml"))["content"].startswith("---")
    with pytest.raises(HTTPException):
        await get_file(project, "playbooks/site.yml")


async def test_mkdir_creates_a_directory(project):
    await make_dir(project, MkdirIn(path="library/custom_modules"))

    paths = storage.paths_for(project)
    assert (paths.root / "library" / "custom_modules").is_dir()


async def test_mkdir_on_an_existing_directory_is_400(project):
    """create_project scaffolds group_vars/, so this is the realistic collision."""
    with pytest.raises(HTTPException) as e:
        await make_dir(project, MkdirIn(path="group_vars"))
    assert e.value.status_code == 400


# --- history -----------------------------------------------------------------

async def test_history_records_each_save(project):
    await put_file(project, FileWriteIn(path="playbooks/site.yml",
                                        content="---\n- hosts: web\n  tasks: []\n",
                                        message="narrow to web"))

    hist = await file_history(project, "playbooks/site.yml")

    assert len(hist["commits"]) >= 2
    assert any("narrow to web" in (c.get("message") or "") for c in hist["commits"])


async def test_can_read_a_previous_version(project):
    """The 'Restore past version' feature depends on this.

    Uses a file this test creates, not the scaffolded site.yml — that one already
    has a commit from create_project, so its oldest revision isn't ours.
    """
    first = "---\n- hosts: all\n  tasks: []\n"
    await put_file(project, FileWriteIn(path="vars/versioned.yml", content=first))
    await put_file(project, FileWriteIn(path="vars/versioned.yml", content="---\n- hosts: changed\n"))

    hist = await file_history(project, "vars/versioned.yml")
    oldest_sha = hist["commits"][-1]["sha"]

    at = await file_at_sha(project, "vars/versioned.yml", oldest_sha)
    assert at["content"] == first


async def test_history_of_unknown_project_is_404():
    with pytest.raises(HTTPException) as e:
        await file_history("no-such-project", "site.yml")
    assert e.value.status_code == 404


# --- import-path validation --------------------------------------------------

async def test_import_path_requires_an_absolute_path():
    with pytest.raises(HTTPException) as e:
        await import_path(ImportPathIn(path="relative/dir", name="rel"))
    assert e.value.status_code == 400
    assert "absolute" in e.value.detail


async def test_import_path_missing_directory_is_404(tmp_path):
    with pytest.raises(HTTPException) as e:
        await import_path(ImportPathIn(path=str(tmp_path / "nope"), name="missing"))
    assert e.value.status_code == 404


async def test_import_path_rejects_a_file(tmp_path):
    f = tmp_path / "a-file.yml"
    f.write_text("---\n")

    with pytest.raises(HTTPException) as e:
        await import_path(ImportPathIn(path=str(f), name="afile"))

    assert e.value.status_code == 400
    assert "not a directory" in e.value.detail


async def test_import_path_copies_the_tree(tmp_path):
    src = tmp_path / "myproj"
    (src / "playbooks").mkdir(parents=True)
    (src / "playbooks" / "deploy.yml").write_text("---\n- hosts: all\n")
    (src / "hosts.ini").write_text("[web]\nhost1\n")

    out = await import_path(ImportPathIn(path=str(src), name="imported"))

    assert (await get_file(out.id, "playbooks/deploy.yml"))["content"].startswith("---")
    assert (await get_file(out.id, "hosts.ini"))["content"].startswith("[web]")


async def test_import_path_defaults_the_name_to_the_directory(tmp_path):
    src = tmp_path / "auto-named"
    src.mkdir()
    (src / "site.yml").write_text("---\n")

    out = await import_path(ImportPathIn(path=str(src)))

    assert out.name == "auto-named"
