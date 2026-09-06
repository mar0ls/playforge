import asyncio
import json
import shutil
import tempfile
import zipfile
from datetime import datetime
from pathlib import Path
from urllib.parse import urlparse, urlunparse

from fastapi import APIRouter, HTTPException, UploadFile, File, Form
from pydantic import BaseModel
from sqlalchemy import select
from git import Repo, GitCommandError

from app.core import storage
from app.core import credentials as cred_store
from app.core import vault
from app.core import galaxy
from app.core import ai
from app.core.detect import detect as detect_project
from app.core.inventory import parse as parse_inventory
from app.core.inventory_writer import (
    add_host as add_host_to_ini,
    add_host_yaml,
    build_host_line,
    is_yaml_inventory,
    resolve_hosts_file,
)
from app.core.lint import lint_file
from app.core.playbook_builder import BuilderError, build_playbook, preview as preview_playbook
from app.core.playbook_tags import collect as collect_tags
from app.models.db import SessionLocal, Project, Run, Credential


router = APIRouter(prefix="/api/projects", tags=["projects"])


class ProjectIn(BaseModel):
    name: str
    description: str = ""


class ProjectOut(BaseModel):
    id: str
    name: str
    description: str
    created_at: datetime


class FileTreeOut(BaseModel):
    project_id: str
    tree: dict


@router.get("", response_model=list[ProjectOut])
async def list_projects():
    async with SessionLocal() as session:
        rows = (await session.execute(select(Project).order_by(Project.created_at.desc()))).scalars().all()
        return [ProjectOut(id=r.id, name=r.name, description=r.description, created_at=r.created_at) for r in rows]


@router.post("", response_model=ProjectOut)
async def create_project(payload: ProjectIn):
    paths = storage.create_project(payload.name)
    async with SessionLocal() as session:
        project = Project(id=paths.project_id, name=payload.name, description=payload.description)
        session.add(project)
        await session.commit()
        await session.refresh(project)
        return ProjectOut(id=project.id, name=project.name, description=project.description,
                          created_at=project.created_at)


class ImportPathIn(BaseModel):
    path: str
    name: str | None = None
    description: str = ""


class ImportGitIn(BaseModel):
    url: str
    name: str | None = None
    description: str = ""
    branch: str | None = None
    username: str | None = None
    token: str | None = None
    shallow: bool = True


@router.post("/import-git", response_model=ProjectOut)
async def import_git(payload: ImportGitIn):
    """Clone a remote git repo (Gitea / GitHub / self-hosted) into a new project.

    For private HTTPS repos pass `username` + `token`; we splice them into the URL
    only for the clone, never persist them. For SSH URLs we rely on the host's SSH
    agent (bind-mount the socket in docker-compose.yml).
    """
    url = (payload.url or "").strip()
    if not url:
        raise HTTPException(400, "url is required")

    auth_url = url
    if payload.username and payload.token:
        parsed = urlparse(url)
        if parsed.scheme not in ("http", "https"):
            raise HTTPException(400, "username/token only work with http(s) URLs; for SSH use the agent socket")
        netloc = f"{payload.username}:{payload.token}@{parsed.hostname}"
        if parsed.port:
            netloc += f":{parsed.port}"
        auth_url = urlunparse(parsed._replace(netloc=netloc))

    fallback_name = Path(urlparse(url).path).stem.removesuffix(".git") or "project"
    name = payload.name or fallback_name

    def _clone_and_import() -> str:
        tmp = Path(tempfile.mkdtemp(prefix="git-clone-"))
        try:
            kwargs: dict = {}
            if payload.shallow:
                kwargs["depth"] = 1
            if payload.branch:
                kwargs["branch"] = payload.branch
            Repo.clone_from(auth_url, str(tmp), **kwargs)
            paths = storage.import_directory(name, tmp)
            return paths.project_id
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    try:
        project_id = await asyncio.to_thread(_clone_and_import)
    except GitCommandError as e:
        # Strip the auth URL from any leaked stderr so we don't echo back the token.
        msg = (e.stderr or str(e)).replace(auth_url, url)
        raise HTTPException(400, f"git clone failed: {msg}")
    except storage.StorageError as e:
        raise HTTPException(400, str(e))

    description = (payload.description or f"Cloned from {url}")
    async with SessionLocal() as session:
        project = Project(id=project_id, name=name, description=description)
        session.add(project)
        await session.commit()
        await session.refresh(project)
        return ProjectOut(id=project.id, name=project.name, description=project.description,
                          created_at=project.created_at)


@router.post("/import-path", response_model=ProjectOut)
async def import_path(payload: ImportPathIn):
    """Import an existing Ansible project from a local directory path on the server."""
    source = Path(payload.path).expanduser()
    if not source.is_absolute():
        raise HTTPException(400, "path must be absolute (e.g. /data/import/my-project)")
    if not source.exists():
        raise HTTPException(404, f"path does not exist: {source}")
    if not source.is_dir():
        raise HTTPException(400, f"not a directory: {source}")

    name = payload.name or source.name
    try:
        paths = storage.import_directory(name, source)
    except storage.StorageError as e:
        raise HTTPException(400, str(e))

    async with SessionLocal() as session:
        project = Project(id=paths.project_id, name=name, description=payload.description)
        session.add(project)
        await session.commit()
        await session.refresh(project)
        return ProjectOut(id=project.id, name=project.name, description=project.description,
                          created_at=project.created_at)


@router.post("/import-zip", response_model=ProjectOut)
async def import_zip(name: str = Form(...), description: str = Form(""), upload: UploadFile = File(...)):
    if not upload.filename or not upload.filename.lower().endswith(".zip"):
        raise HTTPException(400, "expected a .zip upload")

    with tempfile.TemporaryDirectory() as tmp:
        zip_path = Path(tmp) / "incoming.zip"
        with zip_path.open("wb") as out:
            shutil.copyfileobj(upload.file, out)
        extract_dir = Path(tmp) / "extracted"
        extract_dir.mkdir()
        with zipfile.ZipFile(zip_path) as zf:
            for member in zf.namelist():
                # Block path traversal in zip entries. `is_relative_to`, not a
                # string prefix: `extracted_evil` starts with `extracted`, so a
                # prefix test waves through a sibling directory. Nothing escapes
                # today only because zipfile strips `..` from member names
                # itself — a guard that leans on the thing it is guarding is not
                # one, and the day this loop stops calling extractall it would
                # stop holding.
                target = (extract_dir / member).resolve()
                if not target.is_relative_to(extract_dir.resolve()):
                    raise HTTPException(400, f"unsafe path in zip: {member}")
            zf.extractall(extract_dir)

        # If the zip contained a single top-level directory, treat that as the project root.
        entries = [p for p in extract_dir.iterdir() if not p.name.startswith(".")]
        source = entries[0] if len(entries) == 1 and entries[0].is_dir() else extract_dir
        paths = storage.import_directory(name, source)

    async with SessionLocal() as session:
        project = Project(id=paths.project_id, name=name, description=description)
        session.add(project)
        await session.commit()
        await session.refresh(project)
        return ProjectOut(id=project.id, name=project.name, description=project.description,
                          created_at=project.created_at)


@router.delete("/{project_id}")
async def delete_project(project_id: str):
    async with SessionLocal() as session:
        project = await session.get(Project, project_id)
        if project is None:
            raise HTTPException(404, "project not found")
        await session.delete(project)
        await session.commit()
    storage.delete_project(project_id)
    return {"deleted": project_id}


@router.get("/{project_id}/tree", response_model=FileTreeOut)
async def get_tree(project_id: str):
    try:
        tree = storage.file_tree(project_id)
    except storage.StorageError as e:
        raise HTTPException(404, str(e))
    return FileTreeOut(project_id=project_id, tree=tree)


@router.get("/{project_id}/file")
async def get_file(project_id: str, path: str):
    try:
        return {"path": path, "content": storage.read_file(project_id, path)}
    except storage.StorageError as e:
        raise HTTPException(404, str(e))


@router.get("/{project_id}/file/history")
async def file_history(project_id: str, path: str, limit: int = 30):
    try:
        return {"path": path, "commits": storage.file_history(project_id, path, limit=limit)}
    except storage.StorageError as e:
        raise HTTPException(404, str(e))


@router.get("/{project_id}/file/at")
async def file_at_sha(project_id: str, path: str, sha: str):
    try:
        return {"path": path, "sha": sha, "content": storage.file_at(project_id, path, sha)}
    except storage.StorageError as e:
        raise HTTPException(404, str(e))


class FileWriteIn(BaseModel):
    path: str
    content: str
    message: str | None = None


@router.put("/{project_id}/file")
async def put_file(project_id: str, payload: FileWriteIn):
    try:
        storage.write_file(project_id, payload.path, payload.content, payload.message)
    except storage.StorageError as e:
        raise HTTPException(400, str(e))
    return {"saved": payload.path}


@router.delete("/{project_id}/file")
async def delete_file(project_id: str, path: str):
    """Delete a file (or directory tree) inside the project and commit the removal."""
    try:
        paths = storage.paths_for(project_id)
    except storage.StorageError as e:
        raise HTTPException(404, str(e))
    try:
        target = storage._resolve_safe(paths.root, path)
    except storage.StorageError as e:
        raise HTTPException(400, str(e))
    if not target.exists():
        raise HTTPException(404, f"not found: {path}")
    try:
        storage.delete_file(project_id, path)
    except storage.StorageError as e:
        raise HTTPException(400, str(e))
    return {"deleted": path}


class MovePathIn(BaseModel):
    src: str
    dst: str


@router.post("/{project_id}/move")
async def move_path(project_id: str, payload: MovePathIn):
    """Rename or move a file/directory inside the project."""
    try:
        new_path = await asyncio.to_thread(storage.move_path, project_id, payload.src, payload.dst)
    except storage.StorageError as e:
        raise HTTPException(400, str(e))
    return {"moved": payload.src, "to": new_path}


class ProjectSettingsIn(BaseModel):
    protect_secrets: bool | None = None


@router.get("/{project_id}/settings")
async def get_project_settings(project_id: str):
    from app.core import settings_store
    return {
        "protect_secrets": (await settings_store.get(f"project.{project_id}.protect_secrets")) == "1",
    }


@router.put("/{project_id}/settings")
async def put_project_settings(project_id: str, payload: ProjectSettingsIn):
    from app.core import settings_store
    if payload.protect_secrets is not None:
        await settings_store.set(f"project.{project_id}.protect_secrets",
                                 "1" if payload.protect_secrets else "0")
    return await get_project_settings(project_id)


class MkdirIn(BaseModel):
    path: str


@router.post("/{project_id}/dir")
async def make_dir(project_id: str, payload: MkdirIn):
    """Create a new (empty) directory inside the project."""
    try:
        created = await asyncio.to_thread(storage.create_dir, project_id, payload.path)
    except storage.StorageError as e:
        raise HTTPException(400, str(e))
    return {"created": created}


class GalaxyInstallIn(BaseModel):
    requirements_path: str = "requirements.yml"


@router.get("/{project_id}/galaxy")
async def galaxy_status(project_id: str, requirements_path: str = "requirements.yml"):
    """Requirements file content (if any) + roles/collections currently installed."""
    try:
        paths = storage.paths_for(project_id)
    except storage.StorageError as e:
        raise HTTPException(404, str(e))
    req = paths.root / requirements_path
    return {
        "requirements_path": requirements_path,
        "requirements": req.read_text() if req.is_file() else None,
        "installed": galaxy.list_installed(paths.root),
    }


@router.post("/{project_id}/galaxy/install")
async def galaxy_install(project_id: str, payload: GalaxyInstallIn):
    """Run `ansible-galaxy install` for the project's requirements into roles/ + collections/."""
    try:
        paths = storage.paths_for(project_id)
    except storage.StorageError as e:
        raise HTTPException(404, str(e))
    try:
        result = await asyncio.to_thread(galaxy.install, paths.root, payload.requirements_path)
    except galaxy.GalaxyError as e:
        raise HTTPException(400, str(e))
    result["installed"] = galaxy.list_installed(paths.root)
    return result


class GalaxyDepIn(BaseModel):
    kind: str   # "role" | "collection"
    name: str


@router.post("/{project_id}/galaxy/add")
async def galaxy_add(project_id: str, payload: GalaxyDepIn):
    """Install one role/collection by name and record it in requirements.yml."""
    try:
        paths = storage.paths_for(project_id)
    except storage.StorageError as e:
        raise HTTPException(404, str(e))
    try:
        result = await asyncio.to_thread(galaxy.add_dependency, paths.root, payload.kind, payload.name)
    except galaxy.GalaxyError as e:
        raise HTTPException(400, str(e))
    _invalidate_module_caches()
    # Commit the requirements.yml change + any new files.
    try:
        storage.commit_all(project_id, f"Galaxy: add {payload.kind} {payload.name}")
    except Exception:
        pass
    return result


@router.post("/{project_id}/galaxy/remove")
async def galaxy_remove(project_id: str, payload: GalaxyDepIn):
    """Delete an installed role/collection and drop it from requirements.yml."""
    try:
        paths = storage.paths_for(project_id)
    except storage.StorageError as e:
        raise HTTPException(404, str(e))
    try:
        result = await asyncio.to_thread(galaxy.remove_dependency, paths.root, payload.kind, payload.name)
    except galaxy.GalaxyError as e:
        raise HTTPException(400, str(e))
    _invalidate_module_caches()
    try:
        storage.commit_all(project_id, f"Galaxy: remove {payload.kind} {payload.name}")
    except Exception:
        pass
    return result


def _invalidate_module_caches() -> None:
    """Flush every snapshot of `ansible-doc`/galaxy state after add/remove.

    The RAG index, the anti-hallucination known-modules set, and the chat
    reply cache (its key hashes a RAG-built system prompt) all need to go
    together — leaving any one stale produces ghost or "doesn't exist"
    modules from the user's POV.
    """
    from app.core import ai, ai_validate, doc_index
    doc_index._index.cache_clear()
    doc_index._collection_corpus.cache_clear()
    doc_index._module_corpus.cache_clear()
    doc_index.module_params.cache_clear()
    ai_validate.known_modules.cache_clear()
    ai.clear_chat_cache()


# ---- Git remote sync -------------------------------------------------------

class GitRemoteIn(BaseModel):
    url: str


class GitAuthIn(BaseModel):
    username: str | None = None  # HTTPS only; for SSH rely on the agent socket
    token: str | None = None     # never persisted — used for a single push/pull


@router.get("/{project_id}/git")
async def git_status(project_id: str):
    try:
        return storage.git_info(project_id)
    except storage.StorageError as e:
        raise HTTPException(404, str(e))


@router.post("/{project_id}/git/remote")
async def git_set_remote(project_id: str, payload: GitRemoteIn):
    try:
        storage.set_remote(project_id, payload.url)
        return storage.git_info(project_id)
    except storage.StorageError as e:
        raise HTTPException(400, str(e))


@router.post("/{project_id}/git/push")
async def git_push(project_id: str, payload: GitAuthIn):
    try:
        out = await asyncio.to_thread(storage.git_push, project_id, payload.username, payload.token)
    except storage.StorageError as e:
        raise HTTPException(400, str(e))
    return {"output": out, "info": storage.git_info(project_id)}


@router.post("/{project_id}/git/pull")
async def git_pull(project_id: str, payload: GitAuthIn):
    try:
        out = await asyncio.to_thread(storage.git_pull, project_id, payload.username, payload.token)
    except storage.StorageError as e:
        raise HTTPException(400, str(e))
    return {"output": out, "info": storage.git_info(project_id)}


@router.post("/{project_id}/runbook")
async def generate_runbook(project_id: str):
    """AI-generated living documentation: summarise the project's playbooks/inventory/roles
    into a Markdown runbook. Returned for review; the UI saves it as docs/RUNBOOK.md."""
    try:
        paths = storage.paths_for(project_id)
    except storage.StorageError as e:
        raise HTTPException(404, str(e))
    if not await ai.ai_enabled():
        raise HTTPException(503, "AI helper not configured. Go to Settings → AI helper to set it up.")

    d = detect_project(paths.root)
    parts: list[str] = []
    for pb in (d.get("playbooks") or [])[:10]:
        try:
            content = storage.read_file(project_id, pb)
        except storage.StorageError:
            content = ""
        parts.append(f"### Playbook: {pb}\n{content[:1500]}")
    if d.get("inventories"):
        parts.append("Inventories: " + ", ".join(d["inventories"]))
    if d.get("roles"):
        parts.append("Roles: " + ", ".join(d["roles"]))
    context = "\n\n".join(parts)[:9000] or "(empty project)"

    async with SessionLocal() as session:
        proj = await session.get(Project, project_id)
        name = proj.name if proj else project_id
    try:
        return await ai.generate_runbook(context, project_name=name)
    except Exception as e:
        raise HTTPException(500, f"runbook generation failed: {e}")


@router.get("/{project_id}/detected")
async def detected(project_id: str):
    try:
        paths = storage.paths_for(project_id)
    except storage.StorageError as e:
        raise HTTPException(404, str(e))
    return detect_project(paths.root)


@router.post("/{project_id}/lint")
def lint(project_id: str, path: str):
    """Sync `def` on purpose: ansible-lint can take 5-15s and we don't want it
    blocking the event loop. FastAPI runs sync routes in a threadpool, which
    keeps the rest of the app responsive."""
    try:
        paths = storage.paths_for(project_id)
    except storage.StorageError as e:
        raise HTTPException(404, str(e))
    try:
        return lint_file(paths.root, path)
    except ValueError as e:
        raise HTTPException(400, str(e))


@router.get("/{project_id}/tags")
async def get_playbook_tags(project_id: str, playbook: str):
    try:
        paths = storage.paths_for(project_id)
    except storage.StorageError as e:
        raise HTTPException(404, str(e))
    try:
        target = storage._resolve_safe(paths.root, playbook)
    except storage.StorageError as e:
        raise HTTPException(400, str(e))
    if not target.is_file():
        raise HTTPException(404, f"playbook not found: {playbook}")
    return {"playbook": playbook, "tags": collect_tags(target)}


class InventoryHostIn(BaseModel):
    inventory_path: str  # relative to project root — file OR directory containing `hosts`
    name: str
    group: str
    vars: dict = {}


class NewPlaybookIn(BaseModel):
    path: str               # rel path inside the project, e.g. "playbooks/deploy.yml"
    spec: dict              # see core.playbook_builder.build_playbook
    overwrite: bool = False


@router.post("/{project_id}/playbook/preview")
def playbook_preview(project_id: str, payload: NewPlaybookIn):
    """Render the YAML from a spec without writing — used by the UI's live preview."""
    try:
        return {"yaml": preview_playbook(payload.spec)}
    except BuilderError as e:
        raise HTTPException(400, str(e))


@router.post("/{project_id}/playbook")
def new_playbook(project_id: str, payload: NewPlaybookIn):
    """Build a YAML playbook from the spec and save it at `path`."""
    try:
        paths = storage.paths_for(project_id)
    except storage.StorageError as e:
        raise HTTPException(404, str(e))
    if not payload.path.lower().endswith((".yml", ".yaml")):
        raise HTTPException(400, "path must end with .yml or .yaml")
    try:
        target = storage._resolve_safe(paths.root, payload.path)
    except storage.StorageError as e:
        raise HTTPException(400, str(e))
    if target.exists() and not payload.overwrite:
        raise HTTPException(409, f"{payload.path} already exists; set overwrite=true to replace")

    try:
        yaml_text = build_playbook(payload.spec)
    except BuilderError as e:
        raise HTTPException(400, str(e))

    storage.write_file(project_id, payload.path, yaml_text,
                       message=f"Add playbook: {payload.path}")
    return {"saved": payload.path, "yaml": yaml_text}


@router.post("/{project_id}/inventory/host")
def add_inventory_host(project_id: str, payload: InventoryHostIn):
    """Append a host to an INI-style inventory file. YAML inventories not supported yet."""
    try:
        paths = storage.paths_for(project_id)
    except storage.StorageError as e:
        raise HTTPException(404, str(e))
    if not payload.name.strip() or not payload.group.strip():
        raise HTTPException(400, "host name and group are required")
    try:
        hosts_file = resolve_hosts_file(paths.root, payload.inventory_path)
    except FileNotFoundError as e:
        raise HTTPException(404, str(e))

    text = hosts_file.read_text()
    rel = str(hosts_file.relative_to(paths.root))
    name, group = payload.name.strip(), payload.group.strip()

    if is_yaml_inventory(text):
        try:
            new_text = add_host_yaml(text, group, name, payload.vars)
        except ValueError as e:
            raise HTTPException(400, str(e))
        if new_text == text:
            return {"saved": False, "file": rel, "format": "yaml", "note": "host already present"}
        storage.write_file(project_id, rel, new_text,
                           message=f"Inventory: add {name} to {group} (YAML)")
        return {"saved": True, "file": rel, "format": "yaml"}

    host_line = build_host_line(name, payload.vars)
    new_text = add_host_to_ini(text, group, host_line)
    if new_text == text:
        return {"saved": False, "host_line": host_line, "note": "host line already present"}

    storage.write_file(project_id, rel, new_text,
                       message=f"Inventory: add {name} to [{group}]")
    return {"saved": True, "host_line": host_line, "file": rel, "format": "ini"}


# ---- Ansible Vault (in-repo secrets) ---------------------------------------

class VaultFileIn(BaseModel):
    path: str           # rel path to the file inside the project
    credential_id: int  # a Credential with kind == "vault_password"


class VaultStringIn(BaseModel):
    name: str           # YAML key name for the inline !vault block
    value: str          # plaintext secret
    credential_id: int


async def _resolve_vault_password(credential_id: int) -> str:
    """Load a `vault_password` credential's secret. Raises HTTPException on misuse."""
    async with SessionLocal() as session:
        c = await session.get(Credential, credential_id)
    if c is None:
        raise HTTPException(404, f"credential {credential_id} not found")
    if c.kind != "vault_password":
        raise HTTPException(400, "credential is not a vault_password")
    secret = cred_store.read_secret(c.id)
    if not secret:
        raise HTTPException(400, "vault password is empty or missing")
    return secret.rstrip("\n")


def _project_file(project_id: str, rel: str):
    """Resolve a sandboxed, existing file path inside a project. Returns (paths, target)."""
    try:
        paths = storage.paths_for(project_id)
        target = storage._resolve_safe(paths.root, rel)
    except storage.StorageError as e:
        raise HTTPException(404, str(e))
    if not target.is_file():
        raise HTTPException(404, f"file not found: {rel}")
    return paths, target


@router.get("/{project_id}/vault/status")
async def vault_status(project_id: str, path: str):
    _paths, target = _project_file(project_id, path)
    return {"path": path, "encrypted": vault.is_vault_encrypted(target.read_text(errors="replace"))}


@router.post("/{project_id}/vault/encrypt")
async def vault_encrypt(project_id: str, payload: VaultFileIn):
    _paths, target = _project_file(project_id, payload.path)
    if vault.is_vault_encrypted(target.read_text(errors="replace")):
        raise HTTPException(409, "file is already vault-encrypted")
    password = await _resolve_vault_password(payload.credential_id)
    try:
        await asyncio.to_thread(vault.encrypt_file, target, password)
    except vault.VaultError as e:
        raise HTTPException(400, str(e))
    storage.write_file(project_id, payload.path, target.read_text(),
                       message=f"Vault: encrypt {payload.path}")
    return {"path": payload.path, "encrypted": True}


@router.post("/{project_id}/vault/decrypt")
async def vault_decrypt(project_id: str, payload: VaultFileIn):
    _paths, target = _project_file(project_id, payload.path)
    if not vault.is_vault_encrypted(target.read_text(errors="replace")):
        raise HTTPException(409, "file is not vault-encrypted")
    password = await _resolve_vault_password(payload.credential_id)
    try:
        await asyncio.to_thread(vault.decrypt_file, target, password)
    except vault.VaultError as e:
        raise HTTPException(400, str(e))
    storage.write_file(project_id, payload.path, target.read_text(),
                       message=f"Vault: decrypt {payload.path}")
    return {"path": payload.path, "encrypted": False}


@router.post("/{project_id}/vault/view")
async def vault_view(project_id: str, payload: VaultFileIn):
    _paths, target = _project_file(project_id, payload.path)
    password = await _resolve_vault_password(payload.credential_id)
    try:
        plaintext = await asyncio.to_thread(vault.view_file, target, password)
    except vault.VaultError as e:
        raise HTTPException(400, str(e))
    return {"path": payload.path, "plaintext": plaintext}


@router.post("/{project_id}/vault/encrypt-string")
async def vault_encrypt_string(project_id: str, payload: VaultStringIn):
    if not payload.name.strip():
        raise HTTPException(400, "name is required")
    password = await _resolve_vault_password(payload.credential_id)
    try:
        block = await asyncio.to_thread(vault.encrypt_string, payload.name.strip(),
                                        payload.value, password)
    except vault.VaultError as e:
        raise HTTPException(400, str(e))
    return {"block": block}


@router.get("/{project_id}/inventory")
async def inventory(project_id: str, path: str):
    try:
        paths = storage.paths_for(project_id)
    except storage.StorageError as e:
        raise HTTPException(404, str(e))
    try:
        return parse_inventory(paths.root, path)
    except FileNotFoundError as e:
        raise HTTPException(404, str(e))
    except ValueError as e:
        raise HTTPException(400, str(e))
    except Exception as e:
        raise HTTPException(500, f"inventory parse failed: {e}")


@router.get("/{project_id}/runs")
async def project_runs(project_id: str, limit: int = 50):
    async with SessionLocal() as session:
        rows = (await session.execute(
            select(Run).where(Run.project_id == project_id).order_by(Run.started_at.desc()).limit(limit)
        )).scalars().all()
    return [
        {
            "id": r.id, "playbook": r.playbook, "inventory": r.inventory, "tags": r.tags,
            "status": r.status,
            "started_at": r.started_at.isoformat() if r.started_at else None,
            "ended_at": r.ended_at.isoformat() if r.ended_at else None,
        }
        for r in rows
    ]


@router.get("/{project_id}/runs/{run_id}")
async def run_detail(project_id: str, run_id: int):
    async with SessionLocal() as session:
        run = await session.get(Run, run_id)
        if run is None or run.project_id != project_id:
            raise HTTPException(404, "run not found")
        return {
            "id": run.id, "project_id": run.project_id,
            "playbook": run.playbook, "inventory": run.inventory,
            "tags": run.tags, "status": run.status,
            "started_at": run.started_at.isoformat() if run.started_at else None,
            "ended_at": run.ended_at.isoformat() if run.ended_at else None,
            "stats": json.loads(run.stats_json or "{}"),
            "failures": json.loads(run.failures_json or "[]"),
            "artifacts": json.loads(run.artifacts_json or "[]"),
            "template_id": run.template_id,
            "environment_id": run.environment_id,
        }
