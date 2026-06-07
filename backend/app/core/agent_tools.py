"""Concrete tool set for the agent, bound to one project.

Each tool wraps an existing, already-tested core function (storage / galaxy /
doc_index / project_index / rules), so the agent reuses the same sandboxing,
git auto-commit and validation as the rest of the app. Tools return small
JSON-serialisable dicts (the agent's "observation").

Trust levels (see core.agent): read-only tools just inspect; mutating tools change
the project and each makes its own git commit; web_fetch is gated behind a domain
allow-list and the caller's opt-in.
"""
from __future__ import annotations

import re

from app.core import doc_index, galaxy, playbook_rules, project_index, storage
from app.core.agent import Tool, READ, MUTATE, CONFIRM
from app.core.ai_validate import validate_text

# Web-fetch is restricted to documentation hosts — never arbitrary URLs.
_WEB_ALLOW = ("docs.ansible.com", "galaxy.ansible.com")
_MAX_OBS_FILE = 6000


def _truncate(s: str, n: int = _MAX_OBS_FILE) -> str:
    s = s or ""
    return s if len(s) <= n else s[:n] + f"\n… (truncated, {len(s)} chars total)"


def build_tools(project_id: str, *, get_run=None) -> dict[str, Tool]:
    """Build the tool registry for `project_id`. `get_run(run_id)->dict` is injected
    by the API layer (it needs the DB) so this module stays sync + importable."""
    root = storage.paths_for(project_id).root

    # ---- read-only ----
    def t_read_file(a: dict) -> dict:
        return {"path": a.get("path"), "content": _truncate(storage.read_file(project_id, a["path"]))}

    def t_list_tree(a: dict) -> dict:
        return {"tree": storage.file_tree(project_id)}

    def t_search_docs(a: dict) -> dict:
        return {"modules": doc_index.search_modules(str(a.get("query", "")), int(a.get("k", 6)))}

    def t_search_project(a: dict) -> dict:
        return {"hits": project_index.search(project_id, str(a.get("query", "")), int(a.get("k", 4)))}

    def t_lint_playbook(a: dict) -> dict:
        text = a.get("content")
        if text is None and a.get("path"):
            text = storage.read_file(project_id, a["path"])
        fixed = playbook_rules.autofix_yaml(text or "")
        v = validate_text(fixed)
        return {"issues": playbook_rules.check_text(fixed, root),
                "invalid_modules": v.get("invalid_modules", []),
                "uninstalled_modules": v.get("uninstalled_modules", []),
                "autofixed": fixed != (text or ""), "content": _truncate(fixed)}

    def t_get_run(a: dict) -> dict:
        if get_run is None:
            return {"error": "run lookup not available"}
        return get_run(int(a.get("run_id") or 0))

    # ---- mutating (each commits) ----
    def t_write_file(a: dict) -> dict:
        is_yaml = a.get("path", "").endswith((".yml", ".yaml"))
        content = playbook_rules.autofix_yaml(a.get("content", "")) if is_yaml else a.get("content", "")
        storage.write_file(project_id, a["path"], content, a.get("message") or f"AI: write {a['path']}")
        obs: dict = {"saved": a["path"]}
        if is_yaml:
            # The self-checking layer: report rule violations AND hallucinated modules
            # so the agent can fix them before finishing (it's told to in the prompt).
            obs["issues"] = playbook_rules.check_text(content, root)
            v = validate_text(content)
            obs["invalid_modules"] = v.get("invalid_modules", [])
            obs["uninstalled_modules"] = v.get("uninstalled_modules", [])
        return obs

    def t_move(a: dict) -> dict:
        new = storage.move_path(project_id, a["src"], a["dst"], f"AI: move {a['src']} -> {a['dst']}")
        return {"moved": a["src"], "to": new}

    def t_mkdir(a: dict) -> dict:
        return {"created": storage.create_dir(project_id, a["path"])}

    def t_galaxy_add(a: dict) -> dict:
        res = galaxy.add_dependency(root, a.get("kind", "collection"), a["name"])
        # Flush every snapshot of "installed modules" the assistant relies on.
        # Imports are local so this module stays cheap to import.
        from app.api.projects import _invalidate_module_caches
        _invalidate_module_caches()
        storage.commit_all(project_id, f"AI: galaxy add {a.get('kind','collection')} {a['name']}")
        return {"installed": res.get("installed")}

    def _run(playbook: str, inventory: str, check: bool) -> dict:
        from app.core.runner import RunRequest, run_playbook_sync
        req = RunRequest(project_id=project_id, playbook=playbook, inventory=inventory or "", check=check)
        res = run_playbook_sync(req)
        # Trim failures to what the agent needs to reason about a fix.
        fails = [{"host": f.get("host"), "task": f.get("task"),
                  "msg": (f.get("result", {}) or {}).get("msg") or f.get("error") or f.get("stderr", "")}
                 for f in (res.failures or [])][:8]
        return {"status": res.status, "rc": res.rc, "failures": fails,
                "changed": len(res.changes or [])}

    def t_preview(a: dict) -> dict:
        """Dry-run (--check): connects to hosts but makes no changes."""
        return {"check": True, **_run(a["playbook"], a.get("inventory", ""), check=True)}

    # ---- confirm (destructive / external) ----
    def t_run_playbook(a: dict) -> dict:
        """Real run — makes changes on the target hosts."""
        return {"check": False, **_run(a["playbook"], a.get("inventory", ""), check=False)}

    def t_delete(a: dict) -> dict:
        storage.delete_file(project_id, a["path"])
        return {"deleted": a["path"]}

    def t_web_fetch(a: dict) -> dict:
        url = str(a.get("url", ""))
        host = re.sub(r"^https?://", "", url).split("/")[0].lower()
        if not any(host == d or host.endswith("." + d) for d in _WEB_ALLOW):
            return {"error": f"domain not allowed; only {', '.join(_WEB_ALLOW)}"}
        # Reuse the module-doc fetcher when a module name is given; otherwise refuse
        # (we don't do arbitrary scraping).
        mod = a.get("module")
        if mod:
            sig = doc_index.fetch_module_doc_web(str(mod))
            return {"module": mod, "signature": doc_index.format_module_signature(str(mod), allow_web=True)
                    if sig else None}
        return {"error": "provide a 'module' name to fetch its docs"}

    tools = [
        Tool("read_file", READ, "Read a project file. args: {path}", t_read_file),
        Tool("list_tree", READ, "List the project file tree. args: {}", t_list_tree),
        Tool("search_docs", READ, "BM25 search installed Ansible modules. args: {query, k?}", t_search_docs),
        Tool("search_project", READ, "BM25 search the project's own files. args: {query, k?}", t_search_project),
        Tool("lint_playbook", READ, "Validate YAML/playbook (autofix + rules). args: {content} or {path}", t_lint_playbook),
        Tool("get_run", READ, "Fetch a past run's status/failures. args: {run_id}", t_get_run),
        Tool("write_file", MUTATE, "Create/overwrite a file (commits). args: {path, content, message?}", t_write_file),
        Tool("move", MUTATE, "Rename/move a file or dir (commits). args: {src, dst}", t_move),
        Tool("mkdir", MUTATE, "Create a directory (commits). args: {path}", t_mkdir),
        Tool("galaxy_add", MUTATE, "Install a role/collection (commits). args: {kind, name}", t_galaxy_add),
        Tool("preview", MUTATE, "Dry-run a playbook with --check (no changes made). args: {playbook, inventory?}", t_preview),
        Tool("run_playbook", CONFIRM, "REALLY run a playbook against the hosts (makes changes). args: {playbook, inventory?}", t_run_playbook),
        Tool("delete", CONFIRM, "Delete a file/dir (commits). args: {path}", t_delete),
        Tool("web_fetch", CONFIRM, "Fetch module docs from docs.ansible.com. args: {module}", t_web_fetch),
    ]
    return {t.name: t for t in tools}
