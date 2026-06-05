"""Unit tests for the project-structure auto-detector."""
from __future__ import annotations

from app.core.detect import detect


def _make_project(root):
    (root / "ansible.cfg").write_text("[defaults]\n")

    pb = root / "playbooks"
    pb.mkdir()
    (pb / "site.yml").write_text("---\n- hosts: all\n  tasks: []\n")
    # A YAML file that is NOT a playbook (no hosts / not a list of plays).
    (pb / "vars.yml").write_text("---\nfoo: bar\n")

    inv = root / "inventories" / "production"
    inv.mkdir(parents=True)
    (inv / "hosts").write_text("[web]\nweb1\n")

    role = root / "roles" / "common" / "tasks"
    role.mkdir(parents=True)
    (role / "main.yml").write_text("---\n- name: noop\n  ansible.builtin.debug: {}\n")


def test_detect_finds_playbook_inventory_role(tmp_path):
    _make_project(tmp_path)
    result = detect(tmp_path)

    assert "playbooks/site.yml" in result["playbooks"]
    assert "playbooks/vars.yml" not in result["playbooks"]  # not a play
    assert "roles/common/tasks/main.yml" not in result["playbooks"]  # role file, not a play
    assert result["ansible_cfg"] is True
    assert "roles/common" in result["roles"]
    # The production dir (has a hosts file) is a valid inventory.
    assert "inventories/production" in result["inventories"]


def test_detect_empty_project(tmp_path):
    result = detect(tmp_path)
    assert result == {"playbooks": [], "inventories": [], "roles": [], "ansible_cfg": False}


def test_detect_skips_vcs_and_cache_dirs(tmp_path):
    junk = tmp_path / ".git"
    junk.mkdir()
    (junk / "config.yml").write_text("---\n- hosts: all\n")
    result = detect(tmp_path)
    assert result["playbooks"] == []


def test_detect_flat_ini_inventory(tmp_path):
    """hosts.ini / hosts_vm6.ini at project root must be detected (real-world ssh_playbook layout)."""
    (tmp_path / "ssh_playbook.yml").write_text("---\n- hosts: all\n  tasks: []\n")
    (tmp_path / "hosts.ini").write_text("[vm_servers]\nvps1 ansible_host=1.2.3.4\n")
    (tmp_path / "hosts_vm6.ini").write_text("[vm_servers]\nvps6 ansible_host=1.2.3.6\n")
    (tmp_path / "hosts_old.ini").write_text("[vm_servers]\nvps_old ansible_host=9.9.9.9\n")

    result = detect(tmp_path)

    assert "ssh_playbook.yml" in result["playbooks"]
    assert "hosts.ini" in result["inventories"]
    assert "hosts_vm6.ini" in result["inventories"]
    assert "hosts_old.ini" in result["inventories"]


def test_detect_ini_inventory_in_subdir(tmp_path):
    """hosts_docker.ini inside a test/ subdirectory should also be found."""
    test_dir = tmp_path / "test"
    test_dir.mkdir()
    (test_dir / "hosts_docker.ini").write_text("[docker]\ncontainer1 ansible_host=172.17.0.2\n")

    result = detect(tmp_path)
    assert "test/hosts_docker.ini" in result["inventories"]
