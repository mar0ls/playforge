"""API-layer tests for project import and deletion.

`import-zip` takes an archive straight from the browser and unpacks it on the
server, which makes it the widest input in the API. Its guard against path
traversal in zip entries is asserted here directly, with archives built in the
test rather than fixtures on disk, so what is being rejected is visible in the
test itself.

`import-path` is the other half: a server-side directory chosen by the operator,
where the checks are about what the path *is* (absolute, existing, a directory)
before storage ever copies anything.

Requires: git binary + GitPython (skipped otherwise, matching test_storage.py).
"""
from __future__ import annotations

import io
import shutil
import zipfile
from pathlib import Path

import pytest

pytest.importorskip("aiosqlite")
pytest.importorskip("cryptography")
pytest.importorskip("git")
if shutil.which("git") is None:
    pytest.skip("git binary not available", allow_module_level=True)

from fastapi import HTTPException
from sqlalchemy import select
from starlette.datastructures import UploadFile

from app.api.projects import ImportPathIn, delete_project, import_path, import_zip
from app.core import storage
from app.models.db import Project, SessionLocal, init_db

PLAYBOOK = "---\n- name: Imported play\n  hosts: all\n  tasks: []\n"


@pytest.fixture()
async def cleanup_projects():
    """Track imported project ids and remove them (DB row + directory) afterwards."""
    await init_db()
    created: list[str] = []
    yield created
    for pid in created:
        async with SessionLocal() as s:
            proj = await s.get(Project, pid)
            if proj:
                await s.delete(proj)
                await s.commit()
        try:
            storage.delete_project(pid)
        except Exception:
            pass


def _zip_bytes(entries: dict[str, str]) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        for name, content in entries.items():
            zf.writestr(name, content)
    return buf.getvalue()


def _upload(data: bytes, filename: str = "project.zip") -> UploadFile:
    return UploadFile(file=io.BytesIO(data), filename=filename)


# `import_zip` declares `description: str = Form("")`. FastAPI resolves that
# default per request; calling the function directly does not, so an omitted
# argument arrives as a `Form` object and lands in the DB as one. Every call
# below passes `description` explicitly for that reason.


# ---------- import-path ------------------------------------------------------

async def test_a_relative_path_is_rejected():
    with pytest.raises(HTTPException) as exc:
        await import_path(ImportPathIn(path="some/relative/dir"))
    assert exc.value.status_code == 400
    assert "absolute" in exc.value.detail


async def test_a_missing_path_is_404(tmp_path: Path):
    with pytest.raises(HTTPException) as exc:
        await import_path(ImportPathIn(path=str(tmp_path / "nope")))
    assert exc.value.status_code == 404


async def test_a_file_instead_of_a_directory_is_400(tmp_path: Path):
    target = tmp_path / "site.yml"
    target.write_text(PLAYBOOK)
    with pytest.raises(HTTPException) as exc:
        await import_path(ImportPathIn(path=str(target)))
    assert exc.value.status_code == 400
    assert "not a directory" in exc.value.detail


async def test_importing_a_directory_creates_the_project(tmp_path: Path, cleanup_projects):
    source = tmp_path / "my-ansible"
    (source / "playbooks").mkdir(parents=True)
    (source / "playbooks" / "site.yml").write_text(PLAYBOOK)

    project = await import_path(ImportPathIn(path=str(source), description="imported"))
    cleanup_projects.append(project.id)

    assert project.description == "imported"
    # No explicit name: the directory's own name is used.
    assert project.name == "my-ansible"
    assert (storage.paths_for(project.id).root / "playbooks" / "site.yml").read_text() == PLAYBOOK


async def test_an_explicit_name_wins_over_the_directory_name(tmp_path: Path, cleanup_projects):
    source = tmp_path / "on-disk-name"
    source.mkdir()
    (source / "site.yml").write_text(PLAYBOOK)

    project = await import_path(ImportPathIn(path=str(source), name="Chosen Name"))
    cleanup_projects.append(project.id)
    assert project.name == "Chosen Name"


# ---------- import-zip -------------------------------------------------------

async def test_a_non_zip_filename_is_rejected():
    with pytest.raises(HTTPException) as exc:
        await import_zip(name="p", description="", upload=_upload(b"not a zip", filename="project.tar.gz"))
    assert exc.value.status_code == 400
    assert "expected a .zip" in exc.value.detail


async def test_a_zip_entry_climbing_out_is_rejected():
    data = _zip_bytes({"../evil.txt": "pwned"})
    with pytest.raises(HTTPException) as exc:
        await import_zip(name="p", description="", upload=_upload(data))
    assert exc.value.status_code == 400
    assert "unsafe path in zip" in exc.value.detail


async def test_a_zip_entry_with_an_absolute_path_is_rejected():
    data = _zip_bytes({"/etc/passwd": "pwned"})
    with pytest.raises(HTTPException) as exc:
        await import_zip(name="p", description="", upload=_upload(data))
    assert exc.value.status_code == 400
    assert "unsafe path in zip" in exc.value.detail


async def test_nothing_is_imported_when_an_entry_is_unsafe(cleanup_projects):
    """The check runs over every member before extractall, so a poisoned archive
    creates no project at all — not a partial one."""
    async with SessionLocal() as s:
        before = len((await s.execute(select(Project))).scalars().all())

    data = _zip_bytes({"playbooks/site.yml": PLAYBOOK, "../evil.txt": "pwned"})
    with pytest.raises(HTTPException):
        await import_zip(name="p", description="", upload=_upload(data))

    async with SessionLocal() as s:
        after = len((await s.execute(select(Project))).scalars().all())
    assert after == before


async def test_a_single_top_level_directory_becomes_the_project_root(cleanup_projects):
    data = _zip_bytes({
        "my-project/playbooks/site.yml": PLAYBOOK,
        "my-project/ansible.cfg": "[defaults]\n",
    })
    project = await import_zip(name="Zipped", description="", upload=_upload(data))
    cleanup_projects.append(project.id)

    root = storage.paths_for(project.id).root
    # The wrapper directory is stripped, not preserved.
    assert (root / "playbooks" / "site.yml").is_file()
    assert not (root / "my-project").exists()


async def test_a_flat_zip_is_imported_as_is(cleanup_projects):
    data = _zip_bytes({"site.yml": PLAYBOOK, "hosts.ini": "[web]\nweb1\n"})
    project = await import_zip(name="Flat", description="", upload=_upload(data))
    cleanup_projects.append(project.id)

    root = storage.paths_for(project.id).root
    assert (root / "site.yml").is_file()
    assert (root / "hosts.ini").is_file()


async def test_the_imported_project_gets_a_db_row(cleanup_projects):
    data = _zip_bytes({"site.yml": PLAYBOOK})
    project = await import_zip(name="Rowed", description="from a zip", upload=_upload(data))
    cleanup_projects.append(project.id)

    async with SessionLocal() as s:
        row = await s.get(Project, project.id)
    assert row is not None
    assert row.name == "Rowed"
    assert row.description == "from a zip"


# ---------- delete -----------------------------------------------------------

async def test_deleting_an_unknown_project_is_404():
    with pytest.raises(HTTPException) as exc:
        await delete_project("no-such-project-id")
    assert exc.value.status_code == 404


async def test_delete_removes_the_row_and_the_directory():
    await init_db()
    paths = storage.create_project("ToDelete")
    pid = paths.project_id
    async with SessionLocal() as s:
        s.add(Project(id=pid, name="ToDelete"))
        await s.commit()

    result = await delete_project(pid)
    assert result == {"deleted": pid}
    assert not paths.root.exists()
    async with SessionLocal() as s:
        assert await s.get(Project, pid) is None
