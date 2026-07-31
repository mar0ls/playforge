"""Deterministic, rule-based checker for AI-generated playbooks.

The "symbolic" half of a neuro-symbolic loop: the LLM proposes a playbook, this
module verifies it against a small knowledge base of Ansible rules. Inspired by
CS50AI's *Knowledge* and *CSP* lectures — each rule is a constraint the playbook
must satisfy, and a violation is reported with a plain reason + severity. This
catches the logic mistakes the module-name (anti-hallucination) check can't see:
malformed `vars`, tasks with no module, handler misuse, destructive ops,
contradictions, and ordering risks like disabling passwords before a key exists.

Pure (yaml + re only) so it's trivially testable and never touches the network.
"""
from __future__ import annotations

import re
from pathlib import Path

import yaml

# Play/task keywords that are NOT modules — used to decide "this task has no module".
_TASK_DIRECTIVES = {
    "name", "when", "tags", "become", "become_user", "become_method", "loop", "with_items",
    "with_dict", "with_fileglob", "register", "notify", "listen", "vars", "ignore_errors",
    "changed_when", "failed_when", "block", "rescue", "always", "delegate_to", "run_once",
    "environment", "args", "until", "retries", "delay", "no_log", "check_mode", "diff",
    "loop_control", "any_errors_fatal", "throttle",
}


# Stable identifiers for every rule. Findings used to carry only a severity and a
# prose message, so anything asserting on them (tests, the golden set in
# tests/golden/) broke whenever the wording changed. `rule` is what you match on;
# `message` stays free to be reworded.
RULE_PLAY_NOT_MAPPING = "play-not-mapping"
RULE_PLAY_MISSING_HOSTS = "play-missing-hosts"
RULE_PLAY_VARS_NOT_MAPPING = "play-vars-not-mapping"
RULE_TASK_NO_MODULE = "task-no-module"
RULE_LISTEN_OUTSIDE_HANDLERS = "listen-outside-handlers"
RULE_SSH_LOCKOUT = "ssh-lockout-password-auth"
RULE_USER_REMOVED = "user-removed"
RULE_USER_CONTRADICTORY = "user-contradictory-state"
RULE_UFW_LOCKOUT = "ufw-lockout-no-ssh"
RULE_INVALID_YAML = "invalid-yaml"

ALL_RULES = {
    RULE_PLAY_NOT_MAPPING, RULE_PLAY_MISSING_HOSTS, RULE_PLAY_VARS_NOT_MAPPING,
    RULE_TASK_NO_MODULE, RULE_LISTEN_OUTSIDE_HANDLERS, RULE_SSH_LOCKOUT,
    RULE_USER_REMOVED, RULE_USER_CONTRADICTORY, RULE_UFW_LOCKOUT, RULE_INVALID_YAML,
}


def _finding(severity: str, message: str, rule: str) -> dict:
    return {"severity": severity, "message": message, "rule": rule}


def _as_list(value) -> list:
    return value if isinstance(value, list) else []


def _module_keys(task: dict) -> list[str]:
    return [k for k in task if k not in _TASK_DIRECTIVES]


def _short_module(key: str) -> str:
    return key.split(".")[-1]


def _disables_password_auth(task: dict) -> bool:
    """True if the task sets `PasswordAuthentication no` (via lineinfile/replace/copy)."""
    for k in _module_keys(task):
        v = task[k]
        text = " ".join(str(x) for x in v.values()) if isinstance(v, dict) else str(v)
        if re.search(r"passwordauthentication\s+no", text, re.I):
            return True
    return False


def _is_authorized_key(task: dict) -> bool:
    return any(_short_module(k) == "authorized_key" for k in _module_keys(task))


def _user_task(task: dict):
    """If the task is a `user` module call, return (name, state); else (None, None)."""
    for k in _module_keys(task):
        if _short_module(k) == "user" and isinstance(task[k], dict):
            return task[k].get("name"), (task[k].get("state") or "present")
    return None, None


def _ufw_args(task: dict) -> dict | None:
    """Return the args dict if this task calls the `ufw` module, else None."""
    for k in _module_keys(task):
        if _short_module(k) == "ufw":
            return task[k] if isinstance(task[k], dict) else {}
    return None


def _ufw_sets_default_deny_incoming(args: dict) -> bool:
    policy = str(args.get("default") or args.get("policy") or "").lower()
    direction = str(args.get("direction") or "incoming").lower()
    return policy == "deny" and direction in ("in", "incoming")


def _ufw_allows_ssh(args: dict) -> bool:
    """True if this ufw rule opens SSH (port 22 / 'ssh' / 'OpenSSH')."""
    if str(args.get("rule") or "").lower() != "allow":
        return False
    port = str(args.get("port") or "")
    name = str(args.get("name") or "")
    app = str(args.get("app") or "")
    blob = f"{port} {name} {app}".lower()
    return port == "22" or "ssh" in blob or "openssh" in blob


def _role_names(play: dict) -> list[str]:
    """Role names referenced by a play's `roles:` list (str or {role: name} forms)."""
    names = []
    for r in _as_list(play.get("roles")):
        if isinstance(r, str):
            names.append(r)
        elif isinstance(r, dict) and r.get("role"):
            names.append(str(r["role"]))
    return names


def _load_role_tasks(project_root: Path, role: str) -> list[dict]:
    """Load a role's `tasks/main.yml` from the project. Looks in the conventional
    `roles/<name>/` and in collection paths under `collections/`. Best-effort:
    returns [] if the file is missing or unparseable (don't fail the whole check)."""
    safe = role.split(".")[-1].replace("/", "")  # tolerate fqcn / stray separators
    candidates = [
        project_root / "roles" / safe / "tasks" / "main.yml",
        project_root / "roles" / safe / "tasks" / "main.yaml",
    ]
    for c in candidates:
        try:
            if c.is_file():
                doc = yaml.safe_load(c.read_text())
                return [t for t in _as_list(doc) if isinstance(t, dict)]
        except (yaml.YAMLError, OSError):
            return []
    return []


def _check_play(play, idx: int, project_root: Path | None = None, *, require_hosts: bool = True) -> list[dict]:
    if not isinstance(play, dict):
        return [_finding("error", f"play {idx}: is not a mapping", RULE_PLAY_NOT_MAPPING)]
    findings: list[dict] = []

    # --- structural constraints ---
    if require_hosts and "hosts" not in play and "import_playbook" not in play:
        findings.append(_finding("error", f"play {idx}: missing 'hosts'", RULE_PLAY_MISSING_HOSTS))
    pvars = play.get("vars")
    if pvars is not None and not isinstance(pvars, dict):
        findings.append(_finding(
            "error", f"play {idx}: 'vars' must be a mapping, not a {type(pvars).__name__} "
                     "— remove the '-' before each variable", RULE_PLAY_VARS_NOT_MAPPING))

    tasks = _as_list(play.get("tasks"))
    handlers = _as_list(play.get("handlers"))

    # If we have the project on disk, expand `roles:` into their actual tasks so the
    # checks below see what really runs (lockout, destructive ops, etc. often live in
    # roles, not the playbook). Roles run between pre_tasks and tasks.
    role_tasks: list[dict] = []
    if project_root is not None:
        for role in _role_names(play):
            role_tasks.extend(_load_role_tasks(project_root, role))

    # pre_tasks run first, then roles, then tasks, then post_tasks. The lockout
    # ordering check depends on this, and the structural checks (no module, listen,
    # destructive ops) apply to every task block — not just `tasks:`. Scanning only
    # `tasks:` silently let pre_tasks/post_tasks issues through.
    ordered = _as_list(play.get("pre_tasks")) + role_tasks + tasks + _as_list(play.get("post_tasks"))
    for t in ordered + handlers:
        if not isinstance(t, dict) or "block" in t:
            continue
        if not _module_keys(t):
            findings.append(_finding("error", f"task '{t.get('name', '?')}': no module specified",
                                     RULE_TASK_NO_MODULE))

    # --- handler misuse: `listen` only valid inside handlers: ---
    for t in ordered:
        if isinstance(t, dict) and "listen" in t:
            findings.append(_finding(
                "warning", f"task '{t.get('name', '?')}': 'listen' only works inside 'handlers:', "
                           "not 'tasks:' — move it to a handlers section", RULE_LISTEN_OUTSIDE_HANDLERS))

    # --- ordering constraint: don't disable passwords before a key is installed ---
    seen_key = False
    for t in ordered:
        if not isinstance(t, dict):
            continue
        if _is_authorized_key(t):
            seen_key = True
        if _disables_password_auth(t) and not seen_key:
            findings.append(_finding(
                "warning", f"task '{t.get('name', '?')}': disables password auth, but no SSH key "
                           "was added earlier in this play — risk of locking yourself out", RULE_SSH_LOCKOUT))

    # --- destructive ops + contradictions on users ---
    user_states: dict[str, set] = {}
    for t in ordered:
        if not isinstance(t, dict):
            continue
        name, state = _user_task(t)
        if name is None:
            continue
        if state == "absent":
            findings.append(_finding(
                "warning", f"task '{t.get('name', '?')}': removes user '{name}' (state: absent) — destructive",
                RULE_USER_REMOVED))
        user_states.setdefault(str(name), set()).add(state)
    for uname, states in user_states.items():
        if "absent" in states and len(states) > 1:
            findings.append(_finding(
                "warning", f"user '{uname}' is both modified/created and removed in the same play — contradictory",
                RULE_USER_CONTRADICTORY))

    # --- firewall lockout: UFW default-deny incoming without an SSH allow rule ---
    ufw_default_deny = any(
        ((ua := _ufw_args(t)) is not None and _ufw_sets_default_deny_incoming(ua))
        for t in ordered if isinstance(t, dict))
    ufw_allows_ssh = any(
        ((ua := _ufw_args(t)) is not None and _ufw_allows_ssh(ua))
        for t in ordered if isinstance(t, dict))
    if ufw_default_deny and not ufw_allows_ssh:
        findings.append(_finding(
            "warning", "UFW sets default deny for incoming traffic but no rule allows SSH — "
                       "you may lock yourself out; add an `allow` rule for port 22 before enabling",
            RULE_UFW_LOCKOUT))

    return findings


# Keys that mark a list element as a *play* (vs a bare task).
_PLAY_MARKERS = {"hosts", "import_playbook", "roles", "tasks", "pre_tasks", "post_tasks"}


def _looks_like_task_list(doc: list) -> bool:
    """True if `doc` is a list of tasks (a `tasks/*.yml` include file) rather than a
    list of plays. Such files legitimately have no `hosts:` — flagging them as
    'play missing hosts' is a false positive. A task list is one where no element
    carries a play marker and at least one carries a module/`block`."""
    if not doc:
        return False
    saw_task = False
    for item in doc:
        if not isinstance(item, dict):
            return False
        if _PLAY_MARKERS & set(item.keys()):
            return False  # an element looks like a play → treat the whole doc as plays
        if "block" in item or _module_keys(item):
            saw_task = True
    return saw_task


def check_doc(doc, project_root: Path | None = None) -> list[dict]:
    """Check a parsed playbook (list of plays). Non-playbook docs yield nothing.
    If `project_root` is given, `roles:` are expanded and their tasks checked too.
    A bare task list (`tasks/*.yml` include file) is checked as tasks, not plays —
    so it isn't wrongly flagged for a 'missing hosts'."""
    if not isinstance(doc, list):
        return []
    if _looks_like_task_list(doc):
        # Wrap as a hostless virtual play so the task-level rules still run
        # (no module, listen, lockout, destructive ops) without the hosts check.
        return _check_play({"tasks": doc}, 1, project_root, require_hosts=False)
    out: list[dict] = []
    for i, play in enumerate(doc, 1):
        out.extend(_check_play(play, i, project_root))
    return out


def check_text(text: str, project_root: Path | None = None) -> list[dict]:
    try:
        doc = yaml.safe_load(text)
    except yaml.YAMLError as e:
        return [_finding("error", f"invalid YAML: {str(e).splitlines()[0]}", RULE_INVALID_YAML)]
    return check_doc(doc, project_root)


# A `key: {{ jinja }}` value with the `{{` unquoted is invalid YAML (the `{` starts
# a flow mapping). This is the single most common mistake models make in Ansible
# YAML, so we fix it deterministically rather than hoping a retry gets it right.
_BARE_JINJA_RE = re.compile(r'^(\s*[\w.-]+:\s+)(\{\{.*\}\})\s*$')


def autofix_yaml(text: str) -> str:
    """Best-effort repair of common model YAML mistakes. Currently: quote bare
    `key: {{ var }}` values. Only rewrites lines that are still valid after the
    change; returns the text unchanged if it already parses."""
    try:
        yaml.safe_load(text)
        return text  # already valid — don't touch it
    except yaml.YAMLError:
        pass
    out = []
    for line in text.splitlines():
        m = _BARE_JINJA_RE.match(line)
        if m:
            out.append(f'{m.group(1)}"{m.group(2)}"')
        else:
            out.append(line)
    return "\n".join(out)


def autofix_reply(reply: str) -> str:
    """Apply `autofix_yaml` to every ```yaml block in an assistant reply."""
    def _repl(m):
        fence, body = m.group(1), m.group(2)
        return f"{fence}{autofix_yaml(body)}```"
    return re.sub(r"(```(?:ya?ml)?\s*\n)(.*?)```", _repl, reply or "", flags=re.DOTALL)


def check_reply(reply: str, project_root: Path | None = None) -> list[dict]:
    """Run the rules over every ```yaml block in an assistant reply, de-duplicated."""
    out: list[dict] = []
    for block in re.findall(r"```(?:ya?ml)?\s*\n(.*?)```", reply or "", re.DOTALL):
        out.extend(check_text(block, project_root))
    seen, uniq = set(), []
    for f in out:
        if f["message"] not in seen:
            seen.add(f["message"])
            uniq.append(f)
    return uniq
