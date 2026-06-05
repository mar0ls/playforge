"""Retrieval over a project's own file *contents* (full RAG, per project).

`doc_index` grounds the assistant in real Ansible *modules*. This grounds it in the
user's real *files* — so it can answer "what does this playbook do?" / "why did
yesterday's run fail?" with the project's actual playbooks, vars and templates,
not guesses.

We reuse the same from-scratch BM25 ranker. Each indexed unit is one text file
(playbooks, group_vars/host_vars, roles, templates, inventory, ansible.cfg),
truncated so a single huge file can't dominate. The index is cached per project
and keyed by a cheap signature (file set + mtimes), so edits invalidate it.
"""
from __future__ import annotations

from pathlib import Path

from app.core import storage
from app.core.doc_index import BM25, tokenize

# Extensions whose contents are worth indexing. Binary/secret-ish files are skipped.
_TEXT_EXT = {".yml", ".yaml", ".j2", ".cfg", ".ini", ".sh", ".py", ".md", ".txt"}
_MAX_FILE_CHARS = 4000          # truncate any single file for the index
_SKIP_DIRS = {".git", "collections", "roles/.galaxy_install_info"}

# project_id -> (signature, file_list, BM25)
_CACHE: dict[str, tuple] = {}


def _is_text(rel: Path) -> bool:
    if any(part in (".git",) for part in rel.parts):
        return False
    if "collections" in rel.parts:          # don't index installed Galaxy content
        return False
    return rel.suffix.lower() in _TEXT_EXT or rel.name in ("ansible.cfg", "hosts")


def _signature(root: Path, files: list[Path]) -> tuple:
    sig = []
    for rel in files:
        try:
            sig.append((str(rel), (root / rel).stat().st_mtime_ns))
        except OSError:
            sig.append((str(rel), 0))
    return tuple(sig)


def _build(project_id: str):
    pp = storage.paths_for(project_id)
    files = sorted(rel for rel in storage.walk_files(project_id) if _is_text(rel))
    sig = _signature(pp.root, files)

    cached = _CACHE.get(project_id)
    if cached and cached[0] == sig:
        return cached

    docs_tokens = []
    contents: list[str] = []
    for rel in files:
        try:
            text = (pp.root / rel).read_text(errors="replace")[:_MAX_FILE_CHARS]
        except OSError:
            text = ""
        contents.append(text)
        # Tokenise the path too so a query mentioning the filename ranks it.
        docs_tokens.append(tokenize(str(rel) + " " + text))
    bm = BM25(docs_tokens)
    entry = (sig, files, bm, contents)
    _CACHE[project_id] = entry
    return entry


def search(project_id: str, query: str, k: int = 4, *, snippet_chars: int = 700) -> list[dict]:
    """Top-k project files relevant to `query`, with a content snippet each.
    Empty list if the project has no indexable files."""
    try:
        _sig, files, bm, contents = _build(project_id)
    except storage.StorageError:
        return []
    if not files:
        return []
    out = []
    for i, score in bm.top_k(query, k):
        out.append({
            "path": str(files[i]),
            "snippet": contents[i][:snippet_chars],
            "score": round(score, 3),
        })
    return out


def invalidate(project_id: str) -> None:
    _CACHE.pop(project_id, None)
