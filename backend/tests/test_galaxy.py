"""Tests for the ansible-galaxy wrapper.

`parse_requirements` and `list_installed` are pure/filesystem and always run.
`install` is tested by monkeypatching `subprocess.run` so we assert the right
commands are built without hitting the network or needing ansible-galaxy.
"""
from __future__ import annotations

import subprocess
import types

import pytest

from app.core import galaxy
from app.core.galaxy import GalaxyError, parse_requirements


# --- parse_requirements -----------------------------------------------------

def test_parse_bare_list_is_roles():
    spec = parse_requirements("- geerlingguy.nginx\n- geerlingguy.docker\n")
    assert len(spec["roles"]) == 2
    assert spec["collections"] == []


def test_parse_mapping_with_both():
    text = (
        "roles:\n"
        "  - name: geerlingguy.nginx\n"
        "collections:\n"
        "  - community.general\n"
        "  - ansible.posix\n"
    )
    spec = parse_requirements(text)
    assert len(spec["roles"]) == 1
    assert len(spec["collections"]) == 2


def test_parse_empty():
    assert parse_requirements("") == {"roles": [], "collections": []}


def test_parse_invalid_yaml_raises():
    with pytest.raises(GalaxyError):
        parse_requirements("roles: [unclosed\n")


# --- list_installed ---------------------------------------------------------

def test_list_installed_roles_and_collections(tmp_path):
    (tmp_path / "roles" / "nginx").mkdir(parents=True)
    (tmp_path / "roles" / "docker").mkdir(parents=True)
    cdir = tmp_path / "collections" / "ansible_collections" / "community" / "general"
    cdir.mkdir(parents=True)
    out = galaxy.list_installed(tmp_path)
    assert out["roles"] == ["docker", "nginx"]
    assert out["collections"] == ["community.general"]


def test_list_installed_empty_project(tmp_path):
    assert galaxy.list_installed(tmp_path) == {"roles": [], "collections": []}


# --- install (subprocess mocked) --------------------------------------------

def _fake_run_factory(calls):
    def _fake_run(args, **kwargs):
        calls.append(args)
        return types.SimpleNamespace(returncode=0, stdout="ok", stderr="")
    return _fake_run


def test_install_runs_only_role_command_for_role_file(tmp_path, monkeypatch):
    (tmp_path / "requirements.yml").write_text("- geerlingguy.nginx\n")
    calls: list = []
    monkeypatch.setattr(subprocess, "run", _fake_run_factory(calls))
    res = galaxy.install(tmp_path)
    assert res["roles_requested"] == 1
    assert res["collections_requested"] == 0
    assert len(calls) == 1
    assert calls[0][:3] == ["ansible-galaxy", "role", "install"]
    assert "--roles-path" in calls[0]


def test_install_runs_both_commands(tmp_path, monkeypatch):
    (tmp_path / "requirements.yml").write_text(
        "roles:\n  - geerlingguy.nginx\ncollections:\n  - community.general\n")
    calls: list = []
    monkeypatch.setattr(subprocess, "run", _fake_run_factory(calls))
    galaxy.install(tmp_path)
    assert len(calls) == 2
    assert calls[0][1] == "role"
    assert calls[1][1] == "collection"
    assert "-p" in calls[1]


def test_install_missing_file_raises(tmp_path):
    with pytest.raises(GalaxyError, match="not found"):
        galaxy.install(tmp_path)


def test_install_empty_requirements_raises(tmp_path, monkeypatch):
    (tmp_path / "requirements.yml").write_text("roles: []\ncollections: []\n")
    with pytest.raises(GalaxyError, match="no roles or collections"):
        galaxy.install(tmp_path)


def test_install_propagates_galaxy_failure(tmp_path, monkeypatch):
    (tmp_path / "requirements.yml").write_text("- some.role\n")

    def _boom(args, **kwargs):
        return types.SimpleNamespace(returncode=1, stdout="", stderr="role not found")
    monkeypatch.setattr(subprocess, "run", _boom)
    with pytest.raises(GalaxyError, match="role not found"):
        galaxy.install(tmp_path)


# --- add/remove single dependency by name -----------------------------------

def test_safe_name_blocks_traversal():
    import pytest as _pt
    from app.core.galaxy import _safe_name, GalaxyError
    assert _safe_name("community.general") == "community.general"
    assert _safe_name("geerlingguy.nginx") == "geerlingguy.nginx"
    for bad in ["../etc", "a/b", "..", ".hidden", "name;rm", ""]:
        with _pt.raises(GalaxyError):
            _safe_name(bad)


def test_add_dependency_installs_and_records(tmp_path, monkeypatch):
    import subprocess, types
    from app.core import galaxy
    calls = []
    monkeypatch.setattr(subprocess, "run",
                        lambda args, **k: calls.append(args) or types.SimpleNamespace(returncode=0, stdout="ok", stderr=""))
    res = galaxy.add_dependency(tmp_path, "collection", "community.general")
    assert calls[0][:3] == ["ansible-galaxy", "collection", "install"]
    assert "community.general" in calls[0]
    # recorded in requirements.yml
    req = (tmp_path / "requirements.yml").read_text()
    assert "community.general" in req
    # idempotent: adding again doesn't duplicate
    galaxy.add_dependency(tmp_path, "collection", "community.general")
    import yaml
    data = yaml.safe_load((tmp_path / "requirements.yml").read_text())
    assert data["collections"].count("community.general") == 1


def test_add_role_uses_role_subcommand(tmp_path, monkeypatch):
    import subprocess, types
    from app.core import galaxy
    calls = []
    monkeypatch.setattr(subprocess, "run",
                        lambda args, **k: calls.append(args) or types.SimpleNamespace(returncode=0, stdout="ok", stderr=""))
    galaxy.add_dependency(tmp_path, "role", "geerlingguy.nginx")
    assert calls[0][1] == "role"
    data = __import__("yaml").safe_load((tmp_path / "requirements.yml").read_text())
    assert "geerlingguy.nginx" in data["roles"]


def test_remove_dependency_deletes_dir_and_entry(tmp_path):
    from app.core import galaxy
    # set up an "installed" collection + requirements
    cdir = tmp_path / "collections" / "ansible_collections" / "community" / "general"
    cdir.mkdir(parents=True)
    (tmp_path / "requirements.yml").write_text("collections:\n  - community.general\n")
    res = galaxy.remove_dependency(tmp_path, "collection", "community.general")
    assert res["removed"] == "community.general"
    assert not cdir.exists()
    assert "community.general" not in (tmp_path / "requirements.yml").read_text()


def test_remove_collection_requires_namespace(tmp_path):
    import pytest as _pt
    from app.core import galaxy
    with _pt.raises(galaxy.GalaxyError):
        galaxy.remove_dependency(tmp_path, "collection", "noNamespace")
