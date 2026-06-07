"""Wrap `ansible-galaxy` to install a project's role/collection dependencies.

Everything installs *into the project* (`roles/` and `collections/`) so the runner
finds them — `runner._project_envvars` already points ANSIBLE_ROLES_PATH /
ANSIBLE_COLLECTIONS_PATH at those dirs. We parse `requirements.yml` ourselves to
decide which of the two `ansible-galaxy` subcommands to run (a combined file may
hold `roles:` and/or `collections:`), so we never invoke a subcommand with nothing
to do (which errors).

All calls are blocking subprocess IO — wrap them in a threadpool from async routes.
"""
from __future__ import annotations

import subprocess
from pathlib import Path

import yaml

DEFAULT_REQUIREMENTS = "requirements.yml"


class GalaxyError(RuntimeError):
    """ansible-galaxy failed, isn't installed, or the requirements file is unusable."""


def parse_requirements(text: str) -> dict:
    """Split a requirements file into role and collection entries.

    A requirements file is either a bare list (roles, legacy form) or a mapping
    with `roles:` and/or `collections:` keys.
    """
    try:
        data = yaml.safe_load(text)
    except yaml.YAMLError as e:
        raise GalaxyError(f"requirements.yml is not valid YAML: {e}") from e
    if data is None:
        return {"roles": [], "collections": []}
    if isinstance(data, list):
        return {"roles": data, "collections": []}
    if isinstance(data, dict):
        return {"roles": data.get("roles") or [], "collections": data.get("collections") or []}
    raise GalaxyError("requirements.yml must be a list or a mapping")


def _run(args: list[str], *, timeout: int = 600) -> str:
    try:
        proc = subprocess.run(args, capture_output=True, text=True, timeout=timeout)
    except FileNotFoundError as e:
        raise GalaxyError("ansible-galaxy not found on PATH") from e
    except subprocess.TimeoutExpired as e:
        raise GalaxyError("ansible-galaxy timed out") from e
    combined = (proc.stdout or "") + (proc.stderr or "")
    if proc.returncode != 0:
        raise GalaxyError(combined.strip() or "ansible-galaxy failed")
    return combined


def install(project_root: Path, requirements_rel: str = DEFAULT_REQUIREMENTS) -> dict:
    """Install roles and/or collections from a project's requirements file."""
    req = project_root / requirements_rel
    if not req.is_file():
        raise GalaxyError(f"requirements file not found: {requirements_rel}")
    spec = parse_requirements(req.read_text())
    if not spec["roles"] and not spec["collections"]:
        raise GalaxyError("requirements file lists no roles or collections")

    outputs: list[str] = []
    if spec["roles"]:
        outputs.append(_run([
            "ansible-galaxy", "role", "install", "-r", str(req),
            "--roles-path", str(project_root / "roles"), "--force",
        ]))
    if spec["collections"]:
        outputs.append(_run([
            "ansible-galaxy", "collection", "install", "-r", str(req),
            "-p", str(project_root / "collections"), "--force",
        ]))
    return {
        "output": "\n".join(o.strip() for o in outputs).strip(),
        "roles_requested": len(spec["roles"]),
        "collections_requested": len(spec["collections"]),
    }


def _safe_name(name: str) -> str:
    """Validate a role/collection name: letters, digits, dot, dash, underscore only.
    Blocks path traversal (`..`, `/`) so a name can't escape the project dirs."""
    name = (name or "").strip()
    if not name or "/" in name or ".." in name or name.startswith("."):
        raise GalaxyError(f"invalid name: {name!r}")
    import re
    if not re.fullmatch(r"[A-Za-z0-9_.\-]+", name):
        raise GalaxyError(f"invalid name: {name!r}")
    return name


def _upsert_requirement(project_root: Path, kind: str, name: str) -> None:
    """Add `name` to requirements.yml under roles:/collections: (idempotent)."""
    req = project_root / DEFAULT_REQUIREMENTS
    data: dict = {"roles": [], "collections": []}
    if req.is_file():
        data = parse_requirements(req.read_text())
    key = "roles" if kind == "role" else "collections"
    items = list(data.get(key) or [])
    # entries may be strings or {name: ...} dicts
    present = any((i == name) or (isinstance(i, dict) and i.get("name") == name) for i in items)
    if not present:
        items.append(name)
    out = {"roles": data.get("roles") or [], "collections": data.get("collections") or []}
    out[key] = items
    # keep file tidy: only emit non-empty sections
    rendered = {k: v for k, v in out.items() if v}
    req.write_text(yaml.safe_dump(rendered or {"collections": []}, default_flow_style=False, sort_keys=False))


def add_dependency(project_root: Path, kind: str, name: str) -> dict:
    """Install one role or collection by name into the project, and record it in
    requirements.yml. `kind` is 'role' or 'collection'."""
    if kind not in ("role", "collection"):
        raise GalaxyError("kind must be 'role' or 'collection'")
    name = _safe_name(name)
    if kind == "role":
        out = _run(["ansible-galaxy", "role", "install", name,
                    "--roles-path", str(project_root / "roles"), "--force"])
    else:
        out = _run(["ansible-galaxy", "collection", "install", name,
                    "-p", str(project_root / "collections"), "--force"])
    _upsert_requirement(project_root, kind, name)
    return {"output": out.strip(), "installed": list_installed(project_root)}


def remove_dependency(project_root: Path, kind: str, name: str) -> dict:
    """Delete an installed role/collection directory and drop it from requirements.yml."""
    import shutil
    if kind not in ("role", "collection"):
        raise GalaxyError("kind must be 'role' or 'collection'")
    name = _safe_name(name)

    if kind == "role":
        target = project_root / "roles" / name
    else:
        ns, _, coll = name.partition(".")
        if not ns or not coll:
            raise GalaxyError(f"collection name must be namespace.name, got {name!r}")
        target = project_root / "collections" / "ansible_collections" / ns / coll
    if target.is_dir():
        shutil.rmtree(target, ignore_errors=True)

    # drop from requirements.yml
    req = project_root / DEFAULT_REQUIREMENTS
    if req.is_file():
        data = parse_requirements(req.read_text())
        key = "roles" if kind == "role" else "collections"
        items = [i for i in (data.get(key) or [])
                 if not ((i == name) or (isinstance(i, dict) and i.get("name") == name))]
        out = {"roles": data.get("roles") or [], "collections": data.get("collections") or []}
        out[key] = items
        rendered = {k: v for k, v in out.items() if v}
        req.write_text(yaml.safe_dump(rendered, default_flow_style=False, sort_keys=False) if rendered else "")

    return {"removed": name, "installed": list_installed(project_root)}


def list_installed(project_root: Path) -> dict:
    """List roles and collections currently present under the project."""
    roles: list[str] = []
    roles_dir = project_root / "roles"
    if roles_dir.is_dir():
        roles = sorted(p.name for p in roles_dir.iterdir() if p.is_dir())

    collections: list[str] = []
    coll_dir = project_root / "collections" / "ansible_collections"
    if coll_dir.is_dir():
        for ns in sorted(coll_dir.iterdir()):
            if ns.is_dir():
                for name in sorted(ns.iterdir()):
                    if name.is_dir():
                        collections.append(f"{ns.name}.{name.name}")
    return {"roles": roles, "collections": collections}
