"""Lightweight writer for INI-style Ansible inventory files.

Goal: insert a new host line into an existing inventory without destroying the
user's comments or formatting. We don't try to parse the whole file structurally
(Ansible's INI format is permissive — groups, group-of-groups, `:children`,
`:vars`, etc.). We just find the target `[group]` section by name and append at
the end of it, or create the section at EOF if missing.

For YAML-style inventories we return an explicit error — round-tripping YAML
with comments would need ruamel and a structural model; defer.
"""
from __future__ import annotations

import re

_SECTION_RE = re.compile(r"^\s*\[([^\]:]+)(:[^\]]+)?\]\s*$")


_YAML_KEY_RE = re.compile(r"^[^\s=]+:(\s|$)")  # `all:` / `web1:` / `foo: bar` — a mapping key


def is_yaml_inventory(text: str) -> bool:
    """Decide INI vs YAML from the first meaningful line.

    YAML if it's a doc marker (`---`) or a mapping key (`all:`, `web1:`, `foo: bar`).
    INI if it's a `[group]` header or a bare host / `key=value` entry.
    """
    for line in text.splitlines():
        s = line.strip()
        if not s or s.startswith("#"):
            continue
        if s == "---":
            return True
        if _SECTION_RE.match(s):
            return False
        if _YAML_KEY_RE.match(s):
            return True
        # Bare host line or `host k=v` — INI.
        return False
    return False


def build_host_line(name: str, vars_: dict) -> str:
    """Build an INI-style host line: `name k=v k2="v with spaces"`."""
    parts = [name]
    for k, v in vars_.items():
        if v is None or v == "":
            continue
        sval = str(v)
        if any(ws in sval for ws in (" ", "\t")):
            sval = '"' + sval.replace('"', '\\"') + '"'
        parts.append(f"{k}={sval}")
    return " ".join(parts)


def add_host(text: str, group: str, host_line: str) -> str:
    """Insert `host_line` into the `[group]` section. Append section at EOF if absent.

    Idempotent on the exact `host_line`: if a line equal to it already exists
    in the target section, returns the file unchanged.
    """
    lines = text.splitlines()
    section_start = None       # index of the `[group]` header line
    section_end = len(lines)   # exclusive — index of the next section header or EOF
    in_target = False

    for i, line in enumerate(lines):
        m = _SECTION_RE.match(line.strip())
        if m:
            if in_target:
                section_end = i
                break
            # Only a bare `[group]` header holds hosts; `[group:children]` and
            # `[group:vars]` must never receive a host line (it would be parsed as
            # a child-group name / var and corrupt the inventory).
            if m.group(1) == group and not m.group(2):
                section_start = i
                in_target = True

    if section_start is None:
        # Group not present — append a fresh section at EOF, preceded by a blank line.
        out = list(lines)
        if out and out[-1].strip():
            out.append("")
        out.append(f"[{group}]")
        out.append(host_line)
        return "\n".join(out) + ("\n" if not text.endswith("\n") else "")

    # Group present — check for idempotence, then insert before the trailing blanks.
    body = lines[section_start + 1:section_end]
    if any(l.strip() == host_line.strip() for l in body if l.strip()):
        return text  # already there

    insert_at = section_end
    while insert_at > section_start + 1 and not lines[insert_at - 1].strip():
        insert_at -= 1

    out = lines[:insert_at] + [host_line] + lines[insert_at:]
    return "\n".join(out) + ("\n" if text.endswith("\n") else "")


_DIR_INVENTORY_NAMES = ("hosts", "hosts.yml", "hosts.yaml", "inventory.yml", "inventory.yaml", "inventory.ini")


def resolve_hosts_file(project_root, inventory_rel: str):
    """Given an inventory pointer (file or directory), return the actual hosts FILE path.

    - If it's a file, return as-is.
    - If it's a directory, return the first well-known inventory file inside it
      (`hosts`, `hosts.yml`, `inventory.yml`, ...).
    - Otherwise raise FileNotFoundError.
    """
    target = (project_root / inventory_rel).resolve()
    if target.is_file():
        return target
    if target.is_dir():
        for fname in _DIR_INVENTORY_NAMES:
            cand = target / fname
            if cand.is_file():
                return cand
    raise FileNotFoundError(f"no hosts file at {inventory_rel}")


def add_host_yaml(text: str, group: str, name: str, vars_: dict) -> str:
    """Insert a host into a YAML inventory, preserving comments and formatting.

    Uses ruamel's round-trip loader. Supports both the canonical nested form
    (`all: -> children: -> <group>: -> hosts:`) and the compact top-level form
    (`<group>: -> hosts:`); the existing document's shape is followed. Idempotent
    on the host name (re-adding merges vars rather than duplicating).
    """
    from ruamel.yaml import YAML
    import io

    yaml = YAML()
    yaml.preserve_quotes = True
    data = yaml.load(text)
    if data is None:
        data = {}
    if not isinstance(data, dict):
        raise ValueError("inventory root is not a YAML mapping")

    # Pick where groups live: under `all.children` if that shape is in use, else top level.
    if isinstance(data.get("all"), dict):
        container = data["all"].setdefault("children", {})
    else:
        container = data

    grp = container.get(group)
    if grp is None:
        grp = {}
        container[group] = grp
    if not isinstance(grp, dict):
        raise ValueError(f"group {group!r} is not a mapping")

    hosts = grp.get("hosts")
    if hosts is None:
        hosts = {}
        grp["hosts"] = hosts
    if not isinstance(hosts, dict):
        raise ValueError(f"'hosts' of group {group!r} is not a mapping")

    clean_vars = {k: v for k, v in vars_.items() if v not in (None, "")}
    existing = hosts.get(name)
    if name not in hosts:
        hosts[name] = clean_vars or None
    elif clean_vars:
        merged = existing if isinstance(existing, dict) else {}
        merged.update(clean_vars)
        hosts[name] = merged

    buf = io.StringIO()
    yaml.dump(data, buf)
    return buf.getvalue()
