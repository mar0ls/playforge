"""Tests for environment CRUD (create/list/update/delete, project-scoped)."""
from __future__ import annotations

import pytest

pytest.importorskip("aiosqlite")
pytest.importorskip("cryptography")

from fastapi import HTTPException

from app.api.environments import (
    EnvironmentIn,
    EnvironmentUpdate,
    create_environment,
    delete_environment,
    list_environments,
    update_environment,
)
from app.models.db import Project, SessionLocal, init_db


@pytest.fixture(autouse=True)
async def _project():
    await init_db()
    async with SessionLocal() as s:
        if await s.get(Project, "envproj") is None:
            s.add(Project(id="envproj", name="EnvProj"))
            await s.commit()
    yield


async def test_create_and_list():
    out = await create_environment("envproj", EnvironmentIn(
        name="production", inventory_path="inventories/production", default_credential_id=5))
    assert out["name"] == "production"
    assert out["inventory_path"] == "inventories/production"
    assert out["default_credential_id"] == 5

    rows = await list_environments("envproj")
    assert any(e["id"] == out["id"] for e in rows)


async def test_create_requires_name():
    with pytest.raises(HTTPException) as exc:
        await create_environment("envproj", EnvironmentIn(name="   "))
    assert exc.value.status_code == 400


async def test_create_unknown_project_404():
    with pytest.raises(HTTPException) as exc:
        await create_environment("ghost", EnvironmentIn(name="x"))
    assert exc.value.status_code == 404


async def test_update():
    created = await create_environment("envproj", EnvironmentIn(name="staging"))
    updated = await update_environment("envproj", created["id"],
                                       EnvironmentUpdate(inventory_path="inventories/staging"))
    assert updated["inventory_path"] == "inventories/staging"
    assert updated["name"] == "staging"  # unchanged


async def test_update_wrong_project_404():
    created = await create_environment("envproj", EnvironmentIn(name="temp"))
    with pytest.raises(HTTPException) as exc:
        await update_environment("other", created["id"], EnvironmentUpdate(name="z"))
    assert exc.value.status_code == 404


async def test_delete():
    created = await create_environment("envproj", EnvironmentIn(name="doomed"))
    res = await delete_environment("envproj", created["id"])
    assert res["deleted"] == created["id"]
    rows = await list_environments("envproj")
    assert all(e["id"] != created["id"] for e in rows)
