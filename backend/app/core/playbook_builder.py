"""Build a fresh Ansible playbook YAML from a structured spec.

We accept either a typed form payload (TaskSpec / PlaybookSpec) or a raw JSON
object; both end up here. Each task's `args_yaml` is parsed as YAML so users can
write multi-line, nested module arguments naturally. We do NOT try to validate
module names against any registry — Ansible itself will report unknown modules
at run time, and we don't want to be the source of false negatives.
"""
from __future__ import annotations

import yaml


class BuilderError(ValueError):
    """Raised when the spec is malformed (bad YAML in task args, etc.)."""


def build_playbook(spec: dict) -> str:
    """Convert a play spec dict into a YAML playbook file (single play).

    Shape of `spec`:
        {
          "name": "Deploy webapp",
          "hosts": "web",
          "become": true,
          "gather_facts": false,        # optional; only emitted if explicitly false
          "tasks": [
            {
              "name": "Install nginx",
              "module": "ansible.builtin.apt",
              "args_yaml": "name: nginx\\nstate: present",
              "tags": ["install"],      # optional
              "when": "ansible_os_family == 'Debian'"  # optional
            }
          ]
        }
    """
    name = (spec.get("name") or "").strip() or "Playbook"
    hosts = (spec.get("hosts") or "").strip() or "all"

    play: dict = {"name": name, "hosts": hosts}
    if spec.get("become"):
        play["become"] = True
    bu = (spec.get("become_user") or "").strip()
    if bu:
        play["become_user"] = bu
    if spec.get("gather_facts") is False:
        play["gather_facts"] = False
    serial = (str(spec.get("serial") or "")).strip()
    if serial:
        play["serial"] = _coerce_scalar(serial)
    strategy = (spec.get("strategy") or "").strip()
    if strategy:
        play["strategy"] = strategy
    play_vars = _parse_vars(spec.get("vars_yaml"), "play")
    if play_vars:
        play["vars"] = play_vars

    play["tasks"] = [_build_task(t, idx) for idx, t in enumerate(spec.get("tasks") or [])]

    handlers_out = [_build_task(h, idx, kind="handler") for idx, h in enumerate(spec.get("handlers") or [])]
    if handlers_out:
        play["handlers"] = handlers_out

    return yaml.dump([play], default_flow_style=False, sort_keys=False,
                     explicit_start=True, allow_unicode=True, width=120)


def _coerce_scalar(s: str):
    """Turn a string into int/bool where it clearly is one (for serial: '2' -> 2)."""
    if s.isdigit():
        return int(s)
    if s.endswith("%"):
        return s
    return s


def _parse_vars(vars_yaml, where: str) -> dict:
    text = (vars_yaml or "").strip()
    if not text:
        return {}
    try:
        parsed = yaml.safe_load(text)
    except yaml.YAMLError as e:
        raise BuilderError(f"{where} vars: YAML parse error: {e}") from e
    if parsed is None:
        return {}
    if not isinstance(parsed, dict):
        raise BuilderError(f"{where} vars must be a YAML mapping (got {type(parsed).__name__})")
    return parsed


def _build_task(t: dict, idx: int, *, kind: str = "task") -> dict:
    if not isinstance(t, dict):
        raise BuilderError(f"{kind} {idx}: expected object, got {type(t).__name__}")
    task_name = (t.get("name") or "").strip()
    module = (t.get("module") or "").strip()
    if not task_name:
        raise BuilderError(f"{kind} {idx + 1}: name is required")
    if not module:
        raise BuilderError(f"{kind} {idx + 1} ({task_name}): module is required")

    args_yaml = t.get("args_yaml") or ""
    args: dict = {}
    if args_yaml.strip():
        try:
            parsed = yaml.safe_load(args_yaml)
        except yaml.YAMLError as e:
            raise BuilderError(f"{kind} '{task_name}': args YAML parse error: {e}") from e
        if parsed is None:
            args = {}
        elif isinstance(parsed, dict):
            args = parsed
        else:
            raise BuilderError(f"{kind} '{task_name}': args must be a YAML mapping (got {type(parsed).__name__})")

    task: dict = {"name": task_name, module: args}

    # become per-task (e.g. only this task needs root)
    if t.get("become"):
        task["become"] = True
    tbu = (t.get("become_user") or "").strip()
    if tbu:
        task["become_user"] = tbu

    when_clause = (t.get("when") or "").strip()
    if when_clause:
        task["when"] = when_clause

    loop_raw = (t.get("loop") or "").strip()
    if loop_raw:
        # A bracketed/${{}} expression is passed through; a comma list becomes a YAML list.
        if loop_raw.startswith(("[", "{")) or "{{" in loop_raw:
            task["loop"] = loop_raw
        elif "," in loop_raw:
            task["loop"] = [s.strip() for s in loop_raw.split(",") if s.strip()]
        else:
            task["loop"] = loop_raw

    reg = (t.get("register") or "").strip()
    if reg:
        task["register"] = reg

    notify = t.get("notify")
    if notify:
        if isinstance(notify, str):
            notify = [s.strip() for s in notify.split(",") if s.strip()]
        task["notify"] = list(notify)

    tags = t.get("tags")
    if tags:
        if isinstance(tags, str):
            tags = [s.strip() for s in tags.split(",") if s.strip()]
        if tags:
            task["tags"] = list(tags)

    return task


def preview(spec: dict) -> str:
    """Same as build_playbook, but errors raise instead of aborting — caller picks
    error UX."""
    return build_playbook(spec)
