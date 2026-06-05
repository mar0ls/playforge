"""Run ansible-lint on a single file inside a project and parse its JSON output.

ansible-lint emits a JSON array followed by a human-readable rule summary, so we
scan stdout for the first line that parses as a JSON array. We also pass the
project envvars (ANSIBLE_CONFIG, ANSIBLE_ROLES_PATH) so lint sees the same role
resolution that the runner does.
"""
from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

from app.core.runner import _project_envvars


def lint_file(project_root: Path, file_rel: str) -> dict:
    target = (project_root / file_rel).resolve()
    try:
        target.relative_to(project_root.resolve())
    except ValueError as exc:
        raise ValueError(f"path escapes project: {file_rel}") from exc
    if not target.is_file():
        return {"file": file_rel, "rc": 0, "issues": [], "error": "not a file"}

    env = {**os.environ, **_project_envvars(project_root)}
    try:
        proc = subprocess.run(
            ["ansible-lint", "--format=json", "--nocolor", str(target)],
            cwd=str(project_root), env=env, timeout=60, text=True,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        )
    except subprocess.TimeoutExpired:
        return {"file": file_rel, "rc": -1, "issues": [], "error": "lint timed out after 60s"}
    except FileNotFoundError:
        return {"file": file_rel, "rc": -1, "issues": [], "error": "ansible-lint is not installed"}

    raw_issues: list[dict] = []
    for line in (proc.stdout or "").splitlines():
        stripped = line.strip()
        if stripped.startswith("["):
            try:
                raw_issues = json.loads(stripped)
                break
            except json.JSONDecodeError:
                continue

    return {
        "file": file_rel,
        "rc": proc.returncode,
        "issues": [_normalize(i) for i in raw_issues if isinstance(i, dict)],
        "stderr": (proc.stderr or "")[-2000:],
    }


_SEVERITY_TO_MONACO = {
    "blocker": "error",
    "critical": "error",
    "major": "error",
    "minor": "warning",
    "info": "info",
    "warning": "warning",
}


def _normalize(raw: dict) -> dict:
    loc = raw.get("location", {}) or {}
    pos = loc.get("positions", {}) or {}
    begin = pos.get("begin") or {}
    end = pos.get("end") or {}
    begin_line = int(begin.get("line", 1) or 1)
    begin_col = int(begin.get("column", 1) or 1)
    severity = (raw.get("severity") or "info").lower()
    return {
        "rule": raw.get("check_name") or "unknown",
        "severity": severity,
        "monaco_severity": _SEVERITY_TO_MONACO.get(severity, "info"),
        "description": (raw.get("description") or "").strip(),
        "line": begin_line,
        "column": begin_col,
        "end_line": int(end.get("line", begin_line) or begin_line),
        "end_column": int(end.get("column", begin_col + 20) or begin_col + 20),
        "url": raw.get("url") or "",
    }
