"""Heuristics for detecting Ansible structure inside a project directory.

We scan the project tree once and surface what looks like a runnable artefact:
- Playbooks: YAML files containing at least one play with `hosts:`
- Inventories: file named `hosts`/`inventory*`, or directory named `inventories/`,
  `inventory/`, with a `hosts` file or `group_vars/` inside.
- Roles: subdirectories under `roles/` that contain `tasks/main.yml`.
- ansible.cfg: present at root.

Scanning is best-effort and tolerant of malformed YAML.
"""
from __future__ import annotations

from pathlib import Path
from typing import Iterable

import yaml

PLAYBOOK_HINT_EXTS = {".yml", ".yaml"}
SKIP_DIRS = {".git", ".github", ".venv", "__pycache__", "node_modules", ".cache"}

# *.ini files whose stem starts with one of these are treated as inventories.
_INVENTORY_INI_STEMS = ("hosts", "inventory")


def _walk_relative(root: Path) -> Iterable[Path]:
    for p in root.rglob("*"):
        if any(part in SKIP_DIRS for part in p.relative_to(root).parts):
            continue
        yield p


def _looks_like_playbook(path: Path) -> bool:
    if path.suffix.lower() not in PLAYBOOK_HINT_EXTS:
        return False
    if path.name in {"main.yml", "main.yaml"} and "tasks" in path.parts:
        return False  # role task file, not a playbook
    try:
        with path.open("r", encoding="utf-8") as fh:
            doc = yaml.safe_load(fh)
    except (yaml.YAMLError, UnicodeDecodeError, OSError):
        return False
    if not isinstance(doc, list) or not doc:
        return False
    for entry in doc:
        if isinstance(entry, dict) and ("hosts" in entry or "import_playbook" in entry):
            return True
    return False


_INVENTORY_FILE_NAMES = {"hosts", "inventory", "inventory.ini", "inventory.yml", "inventory.yaml"}


def _looks_like_inventory(path: Path) -> bool:
    """A path is an inventory if `ansible -i <path>` would accept it.

    Files: well-known inventory filenames, plus any *.ini whose stem begins with
    "hosts" or "inventory" (e.g. hosts.ini, hosts_prod.ini, hosts_vm6.ini).
    Directories: contain a `hosts` file directly, or contain `group_vars/` / `host_vars/`
    at their root. The umbrella `inventories/` folder does not qualify (it just groups
    per-environment subdirs).
    """
    if path.is_file():
        name_lower = path.name.lower()
        if name_lower in _INVENTORY_FILE_NAMES:
            return True
        if path.suffix.lower() == ".ini":
            stem_lower = path.stem.lower()
            if any(stem_lower == s or stem_lower.startswith(s + "_") or stem_lower.startswith(s + "-")
                   for s in _INVENTORY_INI_STEMS):
                return True
        return False
    if path.is_dir():
        if (path / "hosts").is_file():
            return True
        if (path / "group_vars").is_dir() or (path / "host_vars").is_dir():
            return True
    return False


def detect(project_root: Path) -> dict:
    project_root = project_root.resolve()
    playbooks: list[str] = []
    inventories: list[str] = []
    roles: list[str] = []

    has_ansible_cfg = (project_root / "ansible.cfg").is_file()

    for p in _walk_relative(project_root):
        rel = p.relative_to(project_root)
        if p.is_file() and _looks_like_playbook(p):
            playbooks.append(str(rel))
        if _looks_like_inventory(p):
            inventories.append(str(rel))

    roles_dir = project_root / "roles"
    if roles_dir.is_dir():
        for role in roles_dir.iterdir():
            if role.is_dir() and (role / "tasks" / "main.yml").is_file():
                roles.append(str(role.relative_to(project_root)))

    # If a directory inventory was detected, drop any `hosts` files whose parent is that dir
    # (ansible accepts the directory, so listing the inner file is redundant).
    inv_dirs = {i for i in inventories if (project_root / i).is_dir()}
    inventories = [
        i for i in inventories
        if (project_root / i).is_dir() or str(Path(i).parent) not in inv_dirs
    ]
    inventories = sorted(set(inventories))
    playbooks = sorted(set(playbooks))
    roles = sorted(set(roles))

    return {
        "playbooks": playbooks,
        "inventories": inventories,
        "roles": roles,
        "ansible_cfg": has_ansible_cfg,
    }
