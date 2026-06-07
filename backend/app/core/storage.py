"""Project storage: per-project directory on disk, each one a git repo.

A project lives at `<data_dir>/projects/<project_id>/`. The first commit is made
automatically when the project is created or imported. Every subsequent write
through `write_file` makes a new commit so the user has full history and diff
without us building our own versioning.
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Iterable
from urllib.parse import urlparse, urlunparse
import re
import shutil
import uuid

from git import Repo, Actor, GitCommandError

from app.core.config import settings


GIT_AUTHOR = Actor("Playforge", "playforge@localhost")


class StorageError(Exception):
    """Raised when a storage operation violates project sandboxing or git state."""


@dataclass(frozen=True)
class ProjectPaths:
    project_id: str
    root: Path

    @property
    def inventories_dir(self) -> Path:
        return self.root / "inventories"

    @property
    def roles_dir(self) -> Path:
        return self.root / "roles"

    @property
    def group_vars_dir(self) -> Path:
        return self.root / "group_vars"

    @property
    def host_vars_dir(self) -> Path:
        return self.root / "host_vars"


def _new_id() -> str:
    return uuid.uuid4().hex[:12]


def _project_root(project_id: str) -> Path:
    safe = "".join(c for c in project_id if c.isalnum() or c in "-_")
    if not safe or safe != project_id:
        raise StorageError(f"invalid project id: {project_id!r}")
    return settings.projects_dir / safe


def paths_for(project_id: str) -> ProjectPaths:
    root = _project_root(project_id)
    if not root.exists():
        raise StorageError(f"project not found: {project_id}")
    return ProjectPaths(project_id=project_id, root=root)


def list_projects() -> list[str]:
    return sorted(p.name for p in settings.projects_dir.iterdir() if p.is_dir())


def create_project(name: str) -> ProjectPaths:
    """Create an empty project with the recommended Ansible directory layout.

    Reference: https://docs.ansible.com/ansible/latest/tips_tricks/sample_setup.html
    """
    project_id = _new_id()
    root = _project_root(project_id)
    if root.exists():
        raise StorageError(f"project {project_id} already exists")

    root.mkdir(parents=True)
    for sub in ("inventories/production", "inventories/staging",
                "group_vars", "host_vars", "roles", "playbooks"):
        (root / sub).mkdir(parents=True, exist_ok=True)

    # Leave host_key_checking commented — scaffolding `= False` would silently
    # disable MITM protection in every new project.
    (root / "ansible.cfg").write_text(
        "[defaults]\n"
        "# host_key_checking = False  # uncomment on disposable lab networks only\n"
        "inventory = inventories/production\n"
        "roles_path = roles\n"
        "stdout_callback = yaml\n"
    )
    (root / "playbooks" / "site.yml").write_text(
        "---\n"
        f"# {name} — main playbook\n"
        "- name: Example play\n"
        "  hosts: all\n"
        "  gather_facts: false\n"
        "  tasks:\n"
        "    - name: Ping all hosts\n"
        "      ansible.builtin.ping:\n"
    )
    (root / "inventories" / "production" / "hosts").write_text(
        "# Inventory file — define your hosts here.\n"
        "# Example:\n"
        "# [web]\n"
        "# web1.example.com\n"
    )
    (root / ".gitignore").write_text(
        "*.retry\n"
        ".vault_pass\n"
        "__pycache__/\n"
    )

    repo = Repo.init(root)
    # Exclude `.git/` internals: rglob runs after init, so without this filter the
    # repo's own metadata gets staged — which later makes `git merge`/`pull` abort
    # ("BUG: .git/ in index").
    repo.index.add([str(p.relative_to(root)) for p in root.rglob("*")
                    if p.is_file() and ".git" not in p.parts])
    repo.index.commit(f"Initial project layout for {name}", author=GIT_AUTHOR, committer=GIT_AUTHOR)

    return ProjectPaths(project_id=project_id, root=root)


# Directories/files that are never part of an Ansible project and only bloat the
# import — virtualenvs, caches, VCS internals, editor/OS cruft, ad-hoc logs.
# Real projects on disk routinely carry these (e.g. opnet shipped a 5000-file .venv),
# so copying them verbatim corrupts detection and the per-project git history.
_IGNORE_DIRS = {
    ".git", ".venv", "venv", "env", "__pycache__", ".pytest_cache", ".mypy_cache",
    ".ruff_cache", ".tox", "node_modules", ".idea", ".vscode", ".cache",
}
_IGNORE_FILES = {".DS_Store", ".retry"}
_IGNORE_SUFFIXES = {".pyc", ".pyo", ".retry", ".log"}


def _import_ignore(_dir: str, names: list[str]) -> set[str]:
    """`shutil.copytree` ignore callback: skip junk dirs/files anywhere in the tree."""
    skip = set()
    for n in names:
        if n in _IGNORE_DIRS or n in _IGNORE_FILES:
            skip.add(n)
        elif any(n.endswith(suf) for suf in _IGNORE_SUFFIXES):
            skip.add(n)
    return skip


def import_directory(name: str, source: Path) -> ProjectPaths:
    """Import an existing Ansible project directory, preserving its layout but
    dropping virtualenvs, caches, VCS internals and other non-project junk."""
    source = source.resolve()
    if not source.is_dir():
        raise StorageError(f"source is not a directory: {source}")

    project_id = _new_id()
    root = _project_root(project_id)
    shutil.copytree(source, root, dirs_exist_ok=False, symlinks=False, ignore=_import_ignore)

    repo = Repo.init(root)
    repo.index.add([str(p.relative_to(root)) for p in root.rglob("*")
                    if p.is_file() and ".git" not in p.parts])
    repo.index.commit(f"Import {name} from {source.name}", author=GIT_AUTHOR, committer=GIT_AUTHOR)
    return ProjectPaths(project_id=project_id, root=root)


def delete_project(project_id: str) -> None:
    root = _project_root(project_id)
    if root.exists():
        shutil.rmtree(root)


def _resolve_safe(project_root: Path, relative: str) -> Path:
    """Resolve a path inside a project, refusing escapes via .. or absolute paths,
    and refusing any path inside the project's own `.git/` — writing there (a hook,
    `core.hooksPath`/`fsmonitor` in config, corrupt refs) is a code-execution /
    repo-integrity risk, since the app auto-commits on every change."""
    candidate = (project_root / relative).resolve()
    project_root_resolved = project_root.resolve()
    try:
        rel = candidate.relative_to(project_root_resolved)
    except ValueError as exc:
        raise StorageError(f"path escapes project: {relative}") from exc
    if candidate == project_root_resolved:
        # empty / "." / "a/.." → the project root itself; never a valid target
        raise StorageError(f"invalid path: {relative!r}")
    if ".git" in rel.parts:
        raise StorageError(f"path is inside .git (not allowed): {relative}")
    return candidate


def read_file(project_id: str, relative: str) -> str:
    pp = paths_for(project_id)
    target = _resolve_safe(pp.root, relative)
    if not target.is_file():
        raise StorageError(f"file not found: {relative}")
    return target.read_text()


def write_file(project_id: str, relative: str, content: str, message: str | None = None) -> None:
    pp = paths_for(project_id)
    target = _resolve_safe(pp.root, relative)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(content)

    # _resolve_safe() returns a symlink-resolved path; compute the in-repo
    # relative path against the equally-resolved root, otherwise relative_to()
    # raises when the project root lives behind a symlink (macOS /var, bind mounts).
    root_resolved = pp.root.resolve()
    repo = Repo(pp.root)
    repo.index.add([str(target.relative_to(root_resolved))])
    if repo.is_dirty(index=True, working_tree=False, untracked_files=False):
        repo.index.commit(
            message or f"Update {target.relative_to(root_resolved)}",
            author=GIT_AUTHOR, committer=GIT_AUTHOR,
        )


# Filename patterns that usually hold generated secrets (private keys, passwords).
# Used by `commit_all(protect_secrets=True)` to keep them OUT of git history.
_SECRET_PATTERNS = (
    re.compile(r"(^|/)id_[a-z0-9]+$"),          # id_rsa, id_ed25519, id_ssh_rsa_*
    re.compile(r"\.(pem|key)$"),                # *.pem, *.key
    re.compile(r"(^|/)[^/]*private[^/]*$", re.I),
    re.compile(r"(^|/)wg[-_].*\.conf$"),        # wireguard configs
    re.compile(r"(^|/)[^/]*\.ovpn$"),           # openvpn
    re.compile(r"(^|/)(generated_passwords|secrets|vault_pass)\.ya?ml$", re.I),
)


def _looks_secret(path: str) -> bool:
    return any(p.search(path) for p in _SECRET_PATTERNS)


def commit_all(project_id: str, message: str, *, protect_secrets: bool = False) -> list[str]:
    """Stage and commit every working-tree change in the project, returning the
    list of changed paths. Used after a run so artifacts the playbook wrote into
    the repo (generated SSH keys, rendered configs, fetched files) are captured,
    versioned and visible in the Files tab instead of being invisible/ephemeral.

    If `protect_secrets` is set, files that look like generated secrets (keys,
    passwords, *.ovpn, wg*.conf, ...) are NOT committed: they're added to the
    project's `.gitignore` instead, so they stay on disk but out of git history.
    Returns the list of committed paths (excludes any protected secrets).
    """
    pp = paths_for(project_id)
    repo = Repo(pp.root)
    raw: list[str | None] = [item.a_path or item.b_path for item in repo.index.diff(None)]
    raw += repo.untracked_files
    changed = sorted({c for c in raw if c and ".git" not in c.split("/")})
    if not changed:
        return []

    if protect_secrets:
        secrets = [c for c in changed if _looks_secret(c)]
        if secrets:
            gi = pp.root / ".gitignore"
            existing = gi.read_text().splitlines() if gi.is_file() else []
            additions = [s for s in secrets if s not in existing]
            if additions:
                with gi.open("a") as fh:
                    fh.write("\n# run-generated secrets (auto-protected)\n" + "\n".join(additions) + "\n")
            # Make sure none of them get staged, even if already tracked.
            for s in secrets:
                try:
                    repo.git.rm("--cached", "-f", "--ignore-unmatch", s)
                except GitCommandError:
                    pass
            changed = [c for c in changed if c not in secrets]

    repo.git.add(A=True)
    if repo.is_dirty(index=True, working_tree=False, untracked_files=True):
        repo.index.commit(message, author=GIT_AUTHOR, committer=GIT_AUTHOR)
    return changed


def delete_file(project_id: str, relative: str) -> None:
    pp = paths_for(project_id)
    target = _resolve_safe(pp.root, relative)
    if not target.exists():
        return
    rel = str(target.relative_to(pp.root.resolve()))
    if target.is_file():
        target.unlink()
    else:
        shutil.rmtree(target)
    repo = Repo(pp.root)
    repo.index.remove([rel], r=True, working_tree=False)
    repo.index.commit(f"Delete {rel}", author=GIT_AUTHOR, committer=GIT_AUTHOR)


def move_path(project_id: str, src_rel: str, dst_rel: str, message: str | None = None) -> str:
    """Rename or move a file/directory inside the project (like `git mv`).
    Both paths are sandboxed; refuses to overwrite an existing destination or to
    move a directory into itself. Returns the new in-repo relative path."""
    pp = paths_for(project_id)
    root = pp.root.resolve()
    src = _resolve_safe(pp.root, src_rel)
    dst = _resolve_safe(pp.root, dst_rel)
    if not src.exists():
        raise StorageError(f"source not found: {src_rel}")
    if dst.exists():
        raise StorageError(f"destination already exists: {dst_rel}")
    # Block moving a directory inside itself (e.g. roles -> roles/sub).
    if src.is_dir() and (dst == src or str(dst).startswith(str(src) + "/")):
        raise StorageError("cannot move a directory into itself")

    src_in = str(src.relative_to(root))
    dst_in = str(dst.relative_to(root))
    dst.parent.mkdir(parents=True, exist_ok=True)

    repo = Repo(pp.root)
    # Use git mv so history follows the rename; fall back to a filesystem move for
    # untracked files (git mv refuses those).
    try:
        repo.git.mv(src_in, dst_in)
    except GitCommandError:
        shutil.move(str(src), str(dst))
        repo.git.add("-A")
    if repo.is_dirty(index=True, working_tree=False, untracked_files=True):
        repo.index.commit(message or f"Move {src_in} -> {dst_in}",
                          author=GIT_AUTHOR, committer=GIT_AUTHOR)
    return dst_in


def create_dir(project_id: str, relative: str) -> str:
    """Create a new directory inside the project. Git doesn't track empty dirs, so
    we drop a `.gitkeep` and commit it. Returns the in-repo relative path."""
    pp = paths_for(project_id)
    root = pp.root.resolve()
    target = _resolve_safe(pp.root, relative)
    if target.exists():
        raise StorageError(f"already exists: {relative}")
    target.mkdir(parents=True)
    keep = target / ".gitkeep"
    keep.write_text("")
    rel = str(keep.relative_to(root))
    repo = Repo(pp.root)
    repo.index.add([rel])
    repo.index.commit(f"Create directory {target.relative_to(root)}",
                      author=GIT_AUTHOR, committer=GIT_AUTHOR)
    return str(target.relative_to(root))


# ---- Git remote sync (push / pull) -----------------------------------------

def _auth_url(url: str, username: str | None, token: str | None) -> str:
    """Splice username/token into an http(s) URL for a single operation.

    Returns the URL unchanged for SSH (auth comes from the agent) or when no
    credentials are supplied. The caller must never log or persist the result.
    """
    if username and token:
        p = urlparse(url)
        if p.scheme in ("http", "https"):
            netloc = f"{username}:{token}@{p.hostname}"
            if p.port:
                netloc += f":{p.port}"
            return urlunparse(p._replace(netloc=netloc))
    return url


def git_info(project_id: str) -> dict:
    """Remote URL, current branch, dirty flag, and the HEAD commit summary."""
    pp = paths_for(project_id)
    repo = Repo(pp.root)
    try:
        remote = repo.remote("origin").url
    except ValueError:
        remote = None
    try:
        branch = repo.active_branch.name
    except TypeError:
        branch = "(detached)"
    last = None
    try:
        c = repo.head.commit
        msg = (c.message or "").strip().splitlines()
        last = {"message": msg[0] if msg else "", "when": c.committed_datetime.isoformat()}
    except Exception:
        pass
    return {"remote": remote, "branch": branch,
            "dirty": repo.is_dirty(untracked_files=True), "last_commit": last}


def set_remote(project_id: str, url: str) -> None:
    """Set (or replace) the project's `origin` remote."""
    url = (url or "").strip()
    if not url:
        raise StorageError("remote url is required")
    repo = Repo(paths_for(project_id).root)
    try:
        repo.delete_remote("origin")  # type: ignore[arg-type]  # GitPython accepts a name
    except Exception:
        pass
    repo.create_remote("origin", url)


def git_push(project_id: str, username: str | None = None, token: str | None = None) -> str:
    repo = Repo(paths_for(project_id).root)
    try:
        remote_url = repo.remote("origin").url
    except ValueError:
        raise StorageError("no remote configured — set one first")
    try:
        branch = repo.active_branch.name
    except TypeError:
        raise StorageError("cannot push a detached HEAD")
    auth = _auth_url(remote_url, username, token)
    try:
        out = repo.git.push(auth, f"HEAD:refs/heads/{branch}")
    except GitCommandError as e:
        msg = (e.stderr or str(e)).replace(auth, remote_url)
        raise StorageError(f"push failed: {msg.strip()}")
    return out.strip() or f"pushed {branch} to origin"


def git_pull(project_id: str, username: str | None = None, token: str | None = None) -> str:
    repo = Repo(paths_for(project_id).root)
    try:
        remote_url = repo.remote("origin").url
    except ValueError:
        raise StorageError("no remote configured — set one first")
    try:
        branch = repo.active_branch.name
    except TypeError:
        raise StorageError("cannot pull onto a detached HEAD")
    auth = _auth_url(remote_url, username, token)
    # `--ff-only` keeps us from silently merging divergent histories — a non-FF
    # surfaces as a clear error for the user to resolve. (`git merge` needs a
    # committer identity even for a fast-forward; the image sets one system-wide.)
    try:
        out = repo.git.pull("--ff-only", auth, branch)
    except GitCommandError as e:
        msg = (e.stderr or str(e)).replace(auth, remote_url)
        raise StorageError(f"pull failed: {msg.strip()}")
    return out.strip() or f"pulled {branch} from origin"


def file_history(project_id: str, relative: str, limit: int = 30) -> list[dict]:
    """Commits touching `relative`, newest first; [] when path is untracked."""
    pp = paths_for(project_id)
    _resolve_safe(pp.root, relative)
    repo = Repo(pp.root)
    try:
        commits = list(repo.iter_commits(paths=relative, max_count=max(1, min(limit, 100))))
    except GitCommandError:
        return []
    out: list[dict] = []
    for c in commits:
        msg = (c.message or "").strip().splitlines()
        out.append({
            "sha": c.hexsha,
            "short_sha": c.hexsha[:8],
            "message": msg[0] if msg else "",
            "author": c.author.name or "",
            "when": c.committed_datetime.isoformat(),
        })
    return out


def file_at(project_id: str, relative: str, sha: str) -> str:
    """Content of `relative` at `sha`. Raises StorageError if absent there."""
    pp = paths_for(project_id)
    _resolve_safe(pp.root, relative)
    # Catch typos here — GitPython's error for a bad hex is opaque.
    if not re.fullmatch(r"[0-9a-fA-F]{4,40}", sha or ""):
        raise StorageError(f"invalid sha: {sha!r}")
    repo = Repo(pp.root)
    try:
        return repo.git.show(f"{sha}:{relative}")
    except GitCommandError as e:
        raise StorageError(f"file not in {sha[:8]}: {e.stderr or e}".strip())


def walk_files(project_id: str) -> Iterable[Path]:
    """Yield every file in a project as a path relative to the project root."""
    pp = paths_for(project_id)
    for p in pp.root.rglob("*"):
        if p.is_file() and ".git" not in p.parts:
            yield p.relative_to(pp.root)


def file_tree(project_id: str) -> dict:
    """Return a nested dict representing the project's file tree."""
    pp = paths_for(project_id)
    tree: dict = {}
    for rel in walk_files(project_id):
        node = tree
        parts = rel.parts
        for part in parts[:-1]:
            node = node.setdefault(part, {})
        node[parts[-1]] = None  # leaf = file
    return tree
