"""Parse an Ansible inventory using ansible's own InventoryManager.

This lets us show real groups + hosts in the UI (so `--limit` is a dropdown of
known names, not a free-text field where typos go silent).
"""
from __future__ import annotations

from pathlib import Path

from ansible.inventory.manager import InventoryManager
from ansible.parsing.dataloader import DataLoader


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
