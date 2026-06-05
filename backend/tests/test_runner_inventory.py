"""Tests for default-inventory resolution (flat-layout support).

`_default_inventory` is pure filesystem logic — no ansible-runner needed — so it
runs everywhere. Covers the real cases the scaffolded layout missed: a flat repo
with `hosts.ini` at the root (ssh_playbook), and a project whose `ansible.cfg`
already declares the inventory.
"""
from __future__ import annotations

from app.core.runner import _default_inventory, _INVENTORY_FALLBACKS


def test_scaffolded_layout_picks_inventories_production(tmp_path):
    (tmp_path / "inventories" / "production").mkdir(parents=True)
    (tmp_path / "inventories" / "production" / "hosts").write_text("[web]\nweb1\n")
    assert _default_inventory(tmp_path) == tmp_path / "inventories" / "production"


def test_flat_layout_picks_hosts_ini(tmp_path):
    # ssh_playbook keeps hosts.ini in the repo root, no inventories/ dir.
    (tmp_path / "hosts.ini").write_text("[vm]\nvps1 ansible_host=10.0.0.1\n")
    assert _default_inventory(tmp_path) == tmp_path / "hosts.ini"


def test_ansible_cfg_inventory_defers_to_ansible(tmp_path):
    # When ansible.cfg declares an inventory, return None so Ansible uses it.
    (tmp_path / "ansible.cfg").write_text("[defaults]\ninventory = inventories/production\n")
    (tmp_path / "hosts.ini").write_text("[vm]\nvps1\n")  # present but should be ignored
    assert _default_inventory(tmp_path) is None


def test_ansible_cfg_without_inventory_still_falls_back(tmp_path):
    (tmp_path / "ansible.cfg").write_text("[defaults]\nhost_key_checking = False\n")
    (tmp_path / "inventory").mkdir()
    assert _default_inventory(tmp_path) == tmp_path / "inventory"


def test_nothing_found_returns_none(tmp_path):
    assert _default_inventory(tmp_path) is None


def test_precedence_inventories_production_over_root_hosts(tmp_path):
    (tmp_path / "inventories" / "production").mkdir(parents=True)
    (tmp_path / "inventories" / "production" / "hosts").write_text("x")
    (tmp_path / "hosts").write_text("y")
    # inventories/production comes first in the fallback order.
    assert _default_inventory(tmp_path) == tmp_path / "inventories" / "production"
    assert "inventories/production" in _INVENTORY_FALLBACKS


def test_cleanup_private_dir_removes_tempdir(tmp_path):
    from app.core.runner import _cleanup_private_dir
    d = tmp_path / "ansible-run-xyz"
    (d / "artifacts").mkdir(parents=True)
    (d / "artifacts" / "stdout").write_text("log")
    assert d.exists()
    _cleanup_private_dir(d)
    assert not d.exists()


def test_cleanup_private_dir_is_safe_on_missing(tmp_path):
    from app.core.runner import _cleanup_private_dir
    # Must not raise on a non-existent path (best-effort cleanup).
    _cleanup_private_dir(tmp_path / "does-not-exist")


def test_project_collections_keep_baked_collections_on_path(tmp_path, monkeypatch):
    from app.core.runner import _project_envvars
    monkeypatch.setenv("ANSIBLE_COLLECTIONS_PATH", "/usr/share/ansible/collections")
    (tmp_path / "collections").mkdir()
    env = _project_envvars(tmp_path)
    path = env["ANSIBLE_COLLECTIONS_PATH"]
    # Project path first, baked path still present.
    assert path.split(":")[0] == str(tmp_path / "collections")
    assert "/usr/share/ansible/collections" in path.split(":")


def test_no_project_collections_leaves_env_untouched(tmp_path, monkeypatch):
    from app.core.runner import _project_envvars
    monkeypatch.setenv("ANSIBLE_COLLECTIONS_PATH", "/usr/share/ansible/collections")
    # No collections/ dir → we don't override; the image's env var stays in effect.
    env = _project_envvars(tmp_path)
    assert "ANSIBLE_COLLECTIONS_PATH" not in env
