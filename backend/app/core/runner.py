"""Wrap ansible-runner to execute playbooks and ad-hoc commands.

`ansible-runner` runs ansible in a subprocess but emits structured JSON events
to a known directory. We stream those events to subscribers as they appear so
the UI gets per-task feedback in real time, plus a final summary derived from
the runner stats.
"""
from __future__ import annotations

import asyncio
import json
import os
import re
import shutil
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

import ansible_runner

from app.core import storage


@dataclass
class RunRequest:
    project_id: str
    playbook: str             # relative path to playbook inside project
    inventory: str = ""       # relative path to inventory file or dir
    tags: list[str] = field(default_factory=list)
    skip_tags: list[str] = field(default_factory=list)
    limit: str = ""           # ansible --limit
    extra_vars: dict | None = None
    check: bool = False       # ansible --check
    syntax_check: bool = False  # ansible --syntax-check
    verbosity: int = 0        # 0..4 → maps to -v / -vv / etc.
    ssh_key_content: str | None = None  # injected from a Credential at run time
    ssh_password_content: str | None = None  # SSH login password (sshpass) from a Credential
    vault_password_content: str | None = None  # injected from a vault_password Credential
    become_password_content: str | None = None  # injected from a become_password Credential
    wireguard_keys: dict | None = None  # name -> secret; written to 0600 files at run time


@dataclass
class RunResult:
    status: str               # successful | failed | timeout | canceled
    rc: int
    stats: dict               # per-host counts: ok, changed, failures, unreachable, skipped
    failures: list[dict]      # parsed failed-event payloads
    artifacts_dir: str        # where the runner stored raw events (for debugging)
    changes: list[dict] = field(default_factory=list)  # tasks reporting changed (drift in --check)


_CFG_KEY_RE = re.compile(r"^\s*([A-Za-z_]+)\s*=", re.MULTILINE)


def _cfg_declares(cfg_text: str, key: str) -> bool:
    """True if `ansible.cfg` text sets `key = …` (ignoring section headers/comments)."""
    for line in cfg_text.splitlines():
        s = line.strip()
        if not s or s.startswith("#") or s.startswith(";") or s.startswith("["):
            continue
        m = _CFG_KEY_RE.match(line)
        if m and m.group(1).lower() == key.lower():
            return True
    return False


def _project_envvars(project_root: Path) -> dict[str, str]:
    """Point Ansible at project's ansible.cfg + fill path env vars the cfg omits.

    ansible-runner runs with cwd=private_data_dir so config discovery misses
    the project. Only set path env vars the cfg doesn't already declare —
    env beats cfg, so blindly setting ANSIBLE_ROLES_PATH would silently
    override the user's `roles_path =`.
    """
    env: dict[str, str] = {}
    cfg = project_root / "ansible.cfg"
    cfg_text = ""
    if cfg.is_file():
        env["ANSIBLE_CONFIG"] = str(cfg)
        try:
            cfg_text = cfg.read_text()
        except OSError:
            cfg_text = ""

    roles_dir = project_root / "roles"
    if roles_dir.is_dir() and not _cfg_declares(cfg_text, "roles_path"):
        env["ANSIBLE_ROLES_PATH"] = str(roles_dir)
    # Project-local collections take precedence, but keep the image's baked-in
    # collections (yum/ufw/…) on the path too — otherwise a project with its own
    # collections/ dir would lose access to everything we pre-installed.
    collections_dir = project_root / "collections"
    if collections_dir.is_dir() and not _cfg_declares(cfg_text, "collections_path"):
        baked = os.environ.get("ANSIBLE_COLLECTIONS_PATH", "")
        parts = [str(collections_dir)] + ([baked] if baked else [])
        env["ANSIBLE_COLLECTIONS_PATH"] = ":".join(parts)
    return env


# Common inventory locations, tried in order when the caller didn't specify one and
# `ansible.cfg` has no `inventory =`. Covers both the scaffolded layout and flat
# real-world repos (e.g. ssh_playbook keeps `hosts.ini` at the root).
_INVENTORY_FALLBACKS = (
    "inventories/production", "inventory", "inventories",
    "hosts", "hosts.ini", "inventory.ini", "inventory.yml", "inventory.yaml",
)


def _default_inventory(project_root: Path) -> Path | None:
    """Pick an inventory when none was given. Returns None if `ansible.cfg` already
    declares one (let Ansible use it) or nothing plausible exists."""
    cfg = project_root / "ansible.cfg"
    if cfg.is_file():
        try:
            for line in cfg.read_text().splitlines():
                s = line.strip()
                if s.startswith("inventory") and "=" in s:
                    return None  # ansible.cfg drives it
        except OSError:
            pass
    for cand in _INVENTORY_FALLBACKS:
        p = project_root / cand
        if p.exists():
            return p
    return None


def _cleanup_private_dir(private_data_dir: Path) -> None:
    """Remove the runner's tempdir. Best-effort: a failure to clean up must never
    turn a successful run into an error."""
    try:
        shutil.rmtree(private_data_dir, ignore_errors=True)
    except Exception:
        pass


def _runner_kwargs(req: RunRequest, private_data_dir: Path) -> dict:
    pp = storage.paths_for(req.project_id)
    inventory_path = (pp.root / req.inventory) if req.inventory else _default_inventory(pp.root)
    playbook_path = pp.root / req.playbook

    cmdline_parts: list[str] = []
    if req.tags:
        cmdline_parts += ["--tags", ",".join(req.tags)]
    if req.skip_tags:
        cmdline_parts += ["--skip-tags", ",".join(req.skip_tags)]
    if req.limit:
        cmdline_parts += ["--limit", req.limit]
    if req.check:
        cmdline_parts.append("--check")
    if req.syntax_check:
        cmdline_parts.append("--syntax-check")
    if req.verbosity and req.verbosity > 0:
        cmdline_parts.append("-" + "v" * min(int(req.verbosity), 4))

    # Vault: write the password to a 0600 file inside the run's private dir and point
    # ansible-playbook at it, so vault-encrypted vars/files decrypt during the run.
    if req.vault_password_content:
        vault_pass_file = private_data_dir / "vault_pass"
        vault_pass_file.write_text(req.vault_password_content)
        vault_pass_file.chmod(0o600)
        cmdline_parts += ["--vault-password-file", str(vault_pass_file)]

    # become (sudo) password: same pattern — a 0600 file passed via
    # --become-password-file, so privileged tasks don't need it inline.
    if req.become_password_content:
        become_file = private_data_dir / "become_pass"
        become_file.write_text(req.become_password_content)
        become_file.chmod(0o600)
        cmdline_parts += ["--become-password-file", str(become_file)]

    # SSH login password (no key): hand it to ansible as a connection var. The image
    # ships sshpass, which ansible uses for password auth. extravars land in a 0600
    # file in the private dir that we delete after the run.
    extravars = dict(req.extra_vars or {})
    if req.ssh_password_content:
        extravars["ansible_password"] = req.ssh_password_content

    # WireGuard (and similar) key material: write each secret to a 0600 file in the
    # run's private dir and hand the playbook a `wireguard_keys` map of name->path.
    if req.wireguard_keys:
        wg_dir = private_data_dir / "wg_keys"
        wg_dir.mkdir(exist_ok=True)
        wg_paths = {}
        for name, secret in req.wireguard_keys.items():
            safe = "".join(c for c in name if c.isalnum() or c in "-_.") or "key"
            kf = wg_dir / safe
            kf.write_text(secret if secret.endswith("\n") else secret + "\n")
            kf.chmod(0o600)
            wg_paths[name] = str(kf)
        extravars["wireguard_keys"] = wg_paths

    kwargs = {
        "private_data_dir": str(private_data_dir),
        "project_dir": str(pp.root),
        "playbook": str(playbook_path),
        "extravars": extravars,
        "envvars": _project_envvars(pp.root),
        "cmdline": " ".join(cmdline_parts) if cmdline_parts else None,
        "quiet": True,                 # we read events from disk, not stdout
        "json_mode": True,
    }
    # Only pass an explicit inventory if we resolved one; otherwise let Ansible fall
    # back to the project's ansible.cfg / its own default (avoids pointing at a
    # non-existent `inventories/production` for flat-layout repos).
    if inventory_path is not None:
        kwargs["inventory"] = str(inventory_path)
    # ansible-runner writes ssh_key into private_data_dir/env/ssh_key with 0600
    # and uses it via ssh-agent automatically. Empty / None means "no key injected".
    if req.ssh_key_content:
        kwargs["ssh_key"] = req.ssh_key_content
    return kwargs


class _EventCollector:
    """Event buckets shared by the async + sync runner paths.

    Drops rescued failures on overall success (matches Ansible's recap);
    synthesizes a pre-task failure entry when the run died before any host fired.
    """
    def __init__(self) -> None:
        self.failures: list[dict] = []
        self.changes: list[dict] = []
        self._error_tail: list[str] = []

    def capture(self, event: dict) -> None:
        ev_type = event.get("event")
        if ev_type in ("runner_on_failed", "runner_on_unreachable"):
            ed = event.get("event_data", {}) or {}
            self.failures.append({
                "host": ed.get("host"),
                "task": ed.get("task"),
                "result": ed.get("res", {}) or {},
                "stderr": event.get("stdout", ""),
            })
        elif ev_type == "runner_on_ok":
            ed = event.get("event_data", {}) or {}
            if (ed.get("res", {}) or {}).get("changed"):
                self.changes.append({"host": ed.get("host"), "task": ed.get("task")})
        stdout = event.get("stdout") or ""
        if stdout and ("[ERROR]" in stdout or "ERROR!" in stdout or "fatal:" in stdout):
            self._error_tail.append(stdout)
            if len(self._error_tail) > 20:
                self._error_tail.pop(0)

    def finalize(self, runner_status: str) -> list[dict]:
        if runner_status == "successful":
            return []
        if not self.failures and self._error_tail:
            return [{
                "host": None,
                "task": "(pre-task error)",
                "result": {"msg": "\n".join(self._error_tail).strip()},
                "stderr": "",
            }]
        return self.failures


async def run_playbook(
    req: RunRequest,
    on_event: Callable[[dict], None] | None = None,
    cancel_event: asyncio.Event | None = None,
) -> RunResult:
    """Run a playbook and call `on_event` for each ansible-runner event.

    If `cancel_event` is provided and gets set while the run is in progress,
    ansible-runner is asked to abort (its `cancel_callback` returns True).
    Events are JSON dicts; the full list is documented at
    https://ansible.readthedocs.io/projects/runner/.
    """
    loop = asyncio.get_running_loop()
    private_data_dir = Path(tempfile.mkdtemp(prefix="ansible-run-"))
    collector = _EventCollector()

    def _event_handler(event: dict) -> bool:
        collector.capture(event)
        if on_event is not None:
            loop.call_soon_threadsafe(on_event, event)
        return True

    def _should_cancel() -> bool:
        # ansible-runner polls this from its own thread. asyncio.Event.is_set()
        # is safe to call from any thread.
        return cancel_event is not None and cancel_event.is_set()

    def _run_blocking():
        kwargs = _runner_kwargs(req, private_data_dir)
        return ansible_runner.run(event_handler=_event_handler, cancel_callback=_should_cancel, **kwargs)

    try:
        runner = await loop.run_in_executor(None, _run_blocking)
    except Exception as exc:
        _cleanup_private_dir(private_data_dir)
        return RunResult(status="failed", rc=-1, stats={},
                         failures=[{"error": str(exc)}], artifacts_dir="")

    result = RunResult(
        status=runner.status,
        rc=runner.rc,
        stats=runner.stats or {},
        failures=collector.finalize(runner.status),
        artifacts_dir="",
        changes=collector.changes,
    )
    # Sweep the tempdir — it held ephemeral vault/ssh keys for this run.
    _cleanup_private_dir(private_data_dir)
    return result


def run_playbook_sync(req: RunRequest) -> RunResult:
    """Sync runner for the agent's tool callbacks (no stream, no cancel)."""
    private_data_dir = Path(tempfile.mkdtemp(prefix="ansible-agentrun-"))
    collector = _EventCollector()

    def _event_handler(event: dict) -> bool:
        collector.capture(event)
        return True

    try:
        kwargs = _runner_kwargs(req, private_data_dir)
        runner = ansible_runner.run(event_handler=_event_handler, **kwargs)
    except Exception as exc:
        _cleanup_private_dir(private_data_dir)
        return RunResult(status="failed", rc=-1, stats={},
                         failures=[{"error": str(exc)}], artifacts_dir="")

    result = RunResult(status=runner.status, rc=runner.rc, stats=runner.stats or {},
                       failures=collector.finalize(runner.status),
                       artifacts_dir="", changes=collector.changes)
    _cleanup_private_dir(private_data_dir)
    return result


async def run_adhoc(project_id: str, host_pattern: str, module: str, args: str = "",
                    inventory: str = "",
                    on_event: Callable[[dict], None] | None = None,
                    *,
                    ssh_key_content: str = "",
                    ssh_password_content: str = "",
                    become_password_content: str = "") -> RunResult:
    """Run an ad-hoc command (default use: `ansible <pattern> -m ping`).

    Credentials are injected the same way as `run_playbook` — key via ansible-runner's
    `ssh_key`, SSH password as the `ansible_password` extravar (sshpass is in the
    image), become password through a 0600 file. Without them, ad-hoc against hosts
    that need key or sudo auth fails with "Permission denied" while a normal run of
    the same project succeeds.
    """
    loop = asyncio.get_running_loop()
    private_data_dir = Path(tempfile.mkdtemp(prefix="ansible-adhoc-"))
    pp = storage.paths_for(project_id)
    inventory_path = (pp.root / inventory) if inventory else _default_inventory(pp.root)
    failures: list[dict] = []

    def _capture(event: dict) -> bool:
        if event.get("event") in ("runner_on_failed", "runner_on_unreachable"):
            failures.append({
                "host": event.get("event_data", {}).get("host"),
                "result": event.get("event_data", {}).get("res", {}),
            })
        if on_event is not None:
            loop.call_soon_threadsafe(on_event, event)
        return True

    def _run_blocking():
        adhoc_kwargs: dict = dict(
            private_data_dir=str(private_data_dir),
            host_pattern=host_pattern,
            module=module,
            module_args=args,
            event_handler=_capture,
            quiet=True,
            json_mode=True,
        )
        if inventory_path is not None:
            adhoc_kwargs["inventory"] = str(inventory_path)

        extravars: dict = {}
        if ssh_password_content:
            extravars["ansible_password"] = ssh_password_content
        if extravars:
            adhoc_kwargs["extravars"] = extravars

        if become_password_content:
            become_file = private_data_dir / "become_pass"
            become_file.write_text(become_password_content)
            become_file.chmod(0o600)
            adhoc_kwargs["cmdline"] = f"--become-password-file {become_file}"

        if ssh_key_content:
            adhoc_kwargs["ssh_key"] = ssh_key_content

        return ansible_runner.run(**adhoc_kwargs)

    try:
        runner = await loop.run_in_executor(None, _run_blocking)
        return RunResult(status=runner.status, rc=runner.rc, stats=runner.stats or {},
                         failures=failures, artifacts_dir="")
    finally:
        _cleanup_private_dir(private_data_dir)


def summarize(result: RunResult) -> dict:
    """Produce a UI-friendly summary from runner stats and captured failures."""
    stats = result.stats or {}
    hosts: dict[str, dict] = {}
    for bucket in ("ok", "changed", "failures", "unreachable", "skipped", "rescued", "ignored"):
        for host, count in (stats.get(bucket) or {}).items():
            hosts.setdefault(host, {})[bucket] = count

    overall = "ok"
    if any(h.get("failures", 0) or h.get("unreachable", 0) for h in hosts.values()):
        overall = "failed"
    elif result.status not in ("successful", "ok"):
        overall = "failed"

    return {
        "overall": overall,
        "rc": result.rc,
        "status": result.status,
        "hosts": hosts,
        "failures": result.failures,
    }
