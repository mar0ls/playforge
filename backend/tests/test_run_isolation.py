"""Run isolation: what actually reaches ansible-runner.

Without isolation a playbook runs as the app user with the whole filesystem in
reach — /data holds master.key, app.db and every other project's repo. These
tests pin the kwargs handed to ansible-runner, so the sandbox can't be silently
dropped by a refactor.

They deliberately stop at the kwargs boundary: whether bubblewrap actually works
on a given kernel is a runtime property of the host, not something a unit test
can assert. That's also why the feature ships off by default.
"""
from __future__ import annotations

from pathlib import Path

import pytest

pytest.importorskip("aiosqlite")
pytest.importorskip("cryptography")

from app.core import runner as runner_mod
from app.core import settings_store
from app.core.runner import RunRequest, _runner_kwargs, isolation_kwargs
from app.models.db import init_db


@pytest.fixture(autouse=True)
async def _db():
    await init_db()
    yield
    for key in ("run.isolation", "run.isolation_executable", "run.isolation_image"):
        await settings_store.set(key, "")


# --- the setting gate --------------------------------------------------------

async def test_isolation_is_off_by_default(tmp_path):
    assert await isolation_kwargs(tmp_path) == {}


@pytest.mark.parametrize("off", ["0", "", "no", "false"])
async def test_falsey_settings_keep_it_off(tmp_path, off):
    await settings_store.set("run.isolation", off)
    assert await isolation_kwargs(tmp_path) == {}


@pytest.mark.parametrize("on", ["1", "true", "yes", "on", "ON"])
async def test_truthy_settings_turn_it_on(tmp_path, on):
    await settings_store.set("run.isolation", on)

    kwargs = await isolation_kwargs(tmp_path)

    assert kwargs["process_isolation"] is True


# --- bwrap path --------------------------------------------------------------

async def test_bwrap_is_the_default_mechanism(tmp_path):
    await settings_store.set("run.isolation", "1")

    kwargs = await isolation_kwargs(tmp_path)

    assert kwargs["process_isolation_executable"] == "bwrap"


async def test_bwrap_shows_the_project_root(tmp_path):
    """bwrap binds only /bin /etc /usr /opt plus what we list, so without this
    the run can't read its own playbook."""
    await settings_store.set("run.isolation", "1")

    kwargs = await isolation_kwargs(tmp_path)

    assert str(tmp_path) in kwargs["process_isolation_show_paths"]


async def test_bwrap_does_not_ask_for_a_container_image(tmp_path):
    await settings_store.set("run.isolation", "1")
    await settings_store.set("run.isolation_image", "some/image:tag")

    kwargs = await isolation_kwargs(tmp_path)

    assert "container_image" not in kwargs
    assert "container_volume_mounts" not in kwargs


# --- container path ----------------------------------------------------------

@pytest.mark.parametrize("engine", ["docker", "podman"])
async def test_container_engine_mounts_the_project(tmp_path, engine):
    await settings_store.set("run.isolation", "1")
    await settings_store.set("run.isolation_executable", engine)

    kwargs = await isolation_kwargs(tmp_path)

    assert kwargs["process_isolation_executable"] == engine
    assert kwargs["container_volume_mounts"] == [f"{tmp_path}:{tmp_path}:rw"]
    # show_paths is a bwrap concept; passing it here would be noise.
    assert "process_isolation_show_paths" not in kwargs


async def test_container_image_is_passed_when_set(tmp_path):
    await settings_store.set("run.isolation", "1")
    await settings_store.set("run.isolation_executable", "docker")
    await settings_store.set("run.isolation_image", "quay.io/ansible/ee:latest")

    kwargs = await isolation_kwargs(tmp_path)

    assert kwargs["container_image"] == "quay.io/ansible/ee:latest"


async def test_blank_image_is_omitted_rather_than_sent_empty(tmp_path):
    await settings_store.set("run.isolation", "1")
    await settings_store.set("run.isolation_executable", "podman")

    kwargs = await isolation_kwargs(tmp_path)

    assert "container_image" not in kwargs


# --- reaching ansible-runner -------------------------------------------------

def test_runner_kwargs_without_isolation_are_unchanged(tmp_path, monkeypatch):
    monkeypatch.setattr(runner_mod.storage, "paths_for",
                        lambda pid: type("P", (), {"root": tmp_path})())
    req = RunRequest(project_id="p1", playbook="site.yml")

    kwargs = _runner_kwargs(req, tmp_path)

    assert "process_isolation" not in kwargs


def test_runner_kwargs_carry_isolation_through(tmp_path, monkeypatch):
    monkeypatch.setattr(runner_mod.storage, "paths_for",
                        lambda pid: type("P", (), {"root": tmp_path})())
    req = RunRequest(project_id="p1", playbook="site.yml")
    iso = {"process_isolation": True, "process_isolation_executable": "bwrap",
           "process_isolation_show_paths": [str(tmp_path)]}

    kwargs = _runner_kwargs(req, tmp_path, isolation=iso)

    assert kwargs["process_isolation"] is True
    assert kwargs["process_isolation_executable"] == "bwrap"
    assert kwargs["process_isolation_show_paths"] == [str(tmp_path)]
    # The isolation merge must not clobber what the run needs.
    assert kwargs["playbook"].endswith("site.yml")
    assert kwargs["json_mode"] is True


def test_isolation_does_not_override_ssh_key(tmp_path, monkeypatch):
    monkeypatch.setattr(runner_mod.storage, "paths_for",
                        lambda pid: type("P", (), {"root": tmp_path})())
    req = RunRequest(project_id="p1", playbook="site.yml", ssh_key_content="KEY")

    kwargs = _runner_kwargs(req, tmp_path, isolation={"process_isolation": True})

    assert kwargs["ssh_key"] == "KEY"
    assert kwargs["process_isolation"] is True
