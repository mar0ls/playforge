"""Parse an Ansible inventory using ansible's own InventoryManager.

This lets us show real groups + hosts in the UI (so `--limit` is a dropdown of
known names, not a free-text field where typos go silent).

Parsing is a *display* operation reachable with the `read` capability (and fired
automatically when the Inventory tab opens). Ansible treats an **executable**
inventory file as a dynamic-inventory script and runs it — so pointing this at a
crafted, executable file in an imported project would execute arbitrary code as
the app user, outside run isolation and below the `run`/`operator` trust boundary
the rest of the app enforces. We refuse executable sources here; genuine dynamic
inventories still work at *run* time (ansible-runner in its own subprocess), which
is already gated by `run` and the optional sandbox.
"""
from __future__ import annotations

import os
from pathlib import Path

from ansible.inventory.manager import InventoryManager
from ansible.parsing.dataloader import DataLoader


def _reject_executable(source: Path, inventory_rel: str) -> None:
    """Refuse an executable inventory source: a file Ansible would run as a script,
    or a directory holding one. Static INI/YAML inventories are never executable."""
    candidates = [source]
    if source.is_dir():
        candidates += [p for p in source.iterdir() if p.is_file()]
    for p in candidates:
        if p.is_file() and os.access(p, os.X_OK):
            raise ValueError(
                f"refusing to parse an executable inventory source "
                f"(dynamic-inventory scripts are not previewed): {inventory_rel}")


def parse(project_root: Path, inventory_rel: str) -> dict:
    """Return groups (with their host lists) and the flat host list for an inventory.

    `inventory_rel` may be a path to an inventory file or directory inside the project.
    """
    source = (project_root / inventory_rel).resolve()
    try:
        source.relative_to(project_root.resolve())
    except ValueError as exc:
        raise ValueError(f"inventory path escapes project: {inventory_rel}") from exc
    if not source.exists():
        raise FileNotFoundError(f"inventory not found: {inventory_rel}")
    _reject_executable(source, inventory_rel)

    loader = DataLoader()
    manager = InventoryManager(loader=loader, sources=[str(source)])

    groups: dict[str, dict] = {}
    for name, group in manager.groups.items():
        if name == "ungrouped" and not group.hosts:
            continue
        groups[name] = {
            "hosts": sorted(h.name for h in group.hosts),
            "children": sorted(g.name for g in group.child_groups),
        }

    hosts = sorted({h.name for h in manager.get_hosts()})
    return {"hosts": hosts, "groups": groups}
