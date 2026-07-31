"""HTTP + WebSocket endpoints for playbook runs."""
from __future__ import annotations

import asyncio
import importlib.util
import json
import shutil
import subprocess
from datetime import datetime

from fastapi import APIRouter, HTTPException, WebSocket, WebSocketDisconnect
from pydantic import BaseModel
from sqlalchemy import select

from app.core import auth
from app.core import credentials as cred_store
from app.core import storage
from app.core.runner import RunRequest, run_playbook, run_adhoc, summarize
from app.models.db import SessionLocal, Run, Credential, RunTemplate, Project, Environment


async def _capture_artifacts(project_id: str, run_id: int) -> list[str]:
    """After a run, commit any files it wrote into the project repo (generated
    keys, rendered configs, fetched files) so they're versioned and visible in the
    Files tab. Returns the changed paths. Best-effort — never fails the run.

    If the project opted into secret protection, run-generated secrets are kept out
    of git (added to .gitignore) instead of committed."""
    from app.core import settings_store
    protect = (await settings_store.get(f"project.{project_id}.protect_secrets")) == "1"
    try:
        return await asyncio.to_thread(
            storage.commit_all, project_id, f"Run #{run_id} artifacts", protect_secrets=protect)
    except Exception:
        return []


router = APIRouter(prefix="/api/runs", tags=["runs"])


class RunIn(BaseModel):
    project_id: str
    playbook: str = ""
    inventory: str = ""
    tags: list[str] = []
    skip_tags: list[str] = []
    limit: str = ""
    extra_vars: dict | None = None
    check: bool = False
    syntax_check: bool = False
    verbosity: int = 0
    template_id: int | None = None
    environment_id: int | None = None
    # None = fall back to the template's credentials. An explicit list (even []) overrides.
    credential_ids: list[int] | None = None


class PreflightIn(BaseModel):
    project_id: str
    inventory: str = ""
    host_pattern: str = "all"
    # If true, include checks specific to --check mode (notably python-apt).
    check: bool = False
    # Network probes touch target hosts; callers can disable for controller-only checks.
    include_targets: bool = True


async def _build_request(payload: RunIn) -> tuple[RunRequest, int | None]:
    """RunIn → RunRequest + resolved env_id. Template fills defaults; payload overrides.

    Validates project_id exists on disk — without it a typo'd id used to write
    a 'failed' Run row instead of returning 404.
    """
    base = payload.model_dump()
    try:
        storage.paths_for(base["project_id"])
    except storage.StorageError as e:
        raise HTTPException(404, str(e))
    template_id = base.pop("template_id")
    environment_id = base.pop("environment_id")
    credential_ids = base.pop("credential_ids")  # None means "fall back to template", [] means "explicitly none"

    if template_id is not None:
        async with SessionLocal() as session:
            tpl = await session.get(RunTemplate, template_id)
            if tpl is None:
                raise HTTPException(404, f"template {template_id} not found")
            # Only fill fields the caller left as defaults/empty.
            base["playbook"] = base.get("playbook") or tpl.playbook
            base["inventory"] = base.get("inventory") or tpl.inventory
            base["tags"] = base.get("tags") or [s for s in tpl.tags.split(",") if s]
            base["skip_tags"] = base.get("skip_tags") or [s for s in tpl.skip_tags.split(",") if s]
            base["limit"] = base.get("limit") or tpl.limit
            base["check"] = base.get("check") or tpl.check
            base["syntax_check"] = base.get("syntax_check") or tpl.syntax_check
            base["extra_vars"] = base.get("extra_vars") or json.loads(tpl.extra_vars_json or "{}")
            if credential_ids is None:
                credential_ids = json.loads(tpl.credential_ids_json or "[]")
            if environment_id is None:
                environment_id = tpl.environment_id

    if environment_id is not None:
        async with SessionLocal() as session:
            env = await session.get(Environment, environment_id)
        if env is None:
            raise HTTPException(404, f"environment {environment_id} not found")
        if env.project_id != base.get("project_id"):
            raise HTTPException(400, "environment does not belong to this project")

    if credential_ids is None:
        credential_ids = []

    if not base.get("playbook"):
        raise HTTPException(400, "playbook is required (directly or via a template)")

    base.update(await _resolve_credentials(credential_ids))

    return RunRequest(**base), environment_id


async def _resolve_credentials(credential_ids: list[int]) -> dict:
    """Credential ids → decrypted RunRequest fields.

    Shared by the playbook path and the ad-hoc endpoint so the two can't drift —
    ad-hoc used to accept no credentials at all, which made it fail on exactly the
    hosts a normal run could reach.
    """
    if not credential_ids:
        return {}

    async with SessionLocal() as session:
        creds = (await session.execute(
            select(Credential).where(Credential.id.in_(credential_ids))
        )).scalars().all()

    out: dict = {}
    # ansible-runner takes one of each per run; first of a kind wins.
    for kind, field, strip in (
        ("ssh_key", "ssh_key_content", False),
        ("ssh_password", "ssh_password_content", True),
        ("vault_password", "vault_password_content", True),
        ("become_password", "become_password_content", True),
    ):
        for c in creds:
            if c.kind != kind:
                continue
            secret = cred_store.read_secret(c.id)
            if secret:
                out[field] = secret.rstrip("\n") if strip else secret
            break

    # WireGuard keys (and any other key material the playbook needs as a file) are
    # written to a 0600 temp dir at run time; their paths are exposed as the
    # `wireguard_keys` extra-var (name -> path) so a playbook can reference them.
    wg: dict[str, str] = {}
    for c in creds:
        if c.kind != "wireguard_key":
            continue
        secret = cred_store.read_secret(c.id)   # read+decrypt once per credential
        if secret:
            wg[c.name] = secret
    if wg:
        out["wireguard_keys"] = wg

    return out


def _error_text(f: dict) -> str:
    res = f.get("result") if isinstance(f, dict) else {}
    if not isinstance(res, dict):
        res = {}
    parts = [
        str(res.get("msg") or ""),
        str(f.get("error") or ""),
        str(f.get("stderr") or ""),
    ]
    return "\n".join(p for p in parts if p).lower()


# Each rule maps a lowercase error signature to a stable diagnostic code + hint.
# Phrased for non-expert operators: explain the cause first, then the fix.
# When adding a rule, prefer literal substrings from the actual Ansible/SSH/apt
# error text — these signatures hold across versions far better than regexes.
_DIAGNOSTIC_RULES: list[dict] = [
    {
        "code": "missing_sudo",
        "severity": "error",
        "match": lambda text, task: "sudo: not found" in text,
        "hint": "Controller or target lacks sudo. Install sudo and ensure the runtime user can elevate (become).",
    },
    {
        "code": "missing_python3_apt",
        "severity": "error",
        "match": lambda text, task: "python3-apt must be installed to use check mode" in text,
        "hint": "Apt tasks in --check need python3-apt on the executing host. Install python3-apt or avoid check mode for that task.",
    },
    {
        "code": "ssh_restart_lockout",
        "severity": "warning",
        "match": lambda text, task: (
            "connection refused" in text
            and "restart" in task.lower()
            and "ssh" in task.lower()
        ),
        "hint": "SSH became unreachable after restart. Use serial: 1 + wait_for_connection and validate sshd config before restarting.",
    },
    {
        "code": "firewall_permission_denied",
        "severity": "warning",
        "match": lambda text, task: (
            "could not fetch rule set generation id" in text
            or ("iptables" in text and "permission denied" in text)
        ),
        "hint": "Firewall task failed due to missing kernel/netfilter privileges. In Docker labs run targets with NET_ADMIN (and usually NET_RAW) or skip UFW tasks.",
    },
    {
        "code": "host_unreachable",
        "severity": "error",
        "match": lambda text, task: (
            "no route to host" in text
            or "connection timed out" in text
            or "host key verification failed" in text
            or "ssh: connect to host" in text
            or "failed to connect to the host via ssh" in text
        ),
        "hint": "Target host could not be reached over SSH. Check that the host is up, the inventory IP/port is right, and that the controller can SSH to it (host key + firewall).",
    },
    {
        "code": "ssh_auth_failed",
        "severity": "error",
        "match": lambda text, task: (
            "permission denied (publickey" in text
            or "permission denied (password" in text
            or "no authentication methods could be loaded" in text
        ),
        "hint": "SSH authentication failed. Verify the credential (key or password) is attached to this run, that ansible_user matches the target account, and that the key is authorized on the target.",
    },
    {
        "code": "dns_resolution_failed",
        "severity": "error",
        "match": lambda text, task: (
            "name or service not known" in text
            or "could not resolve hostname" in text
            or "temporary failure in name resolution" in text
        ),
        "hint": "DNS lookup for the target failed. Use an IP in the inventory, or make sure the controller can resolve the host (check /etc/hosts or DNS).",
    },
    {
        "code": "disk_full",
        "severity": "error",
        "match": lambda text, task: "no space left on device" in text,
        "hint": "The target ran out of disk space. Free space (logs, caches, old kernels) or expand the volume before retrying.",
    },
    {
        "code": "package_not_found",
        "severity": "error",
        "match": lambda text, task: (
            "unable to locate package" in text
            or "no package matching" in text
            or "no match for argument" in text
            or "could not find a match" in text
        ),
        "hint": "The package manager can't find the requested package. Check the package name for typos, refresh the cache (apt update / dnf makecache), or enable the right repository.",
    },
    {
        "code": "missing_collection",
        "severity": "error",
        "match": lambda text, task: (
            "couldn't resolve module/action" in text
            or "couldn't resolve module" in text
            or ("the collection" in text and "was not found" in text)
        ),
        "hint": "Ansible can't find the module's collection. Install it with ansible-galaxy collection install <name> (or add it to requirements.yml and use the Galaxy tab).",
    },
    {
        "code": "become_password_required",
        "severity": "error",
        "match": lambda text, task: (
            "missing sudo password" in text
            or "incorrect sudo password" in text
            or ("a password is required" in text and "sudo" in text)
        ),
        "hint": "Sudo on the target needs a password. Attach a 'become_password' credential to this run (or configure NOPASSWD on the target for the runtime user).",
    },
    {
        "code": "vault_password_required",
        "severity": "error",
        "match": lambda text, task: (
            "attempting to decrypt but no vault secrets found" in text
            or ("decryption failed" in text and "vault" in text)
        ),
        "hint": "The playbook uses Ansible Vault but no vault password was supplied. Attach a 'vault_password' credential to this run.",
    },
]


def _diagnose_failures(failures: list[dict], *, check_mode: bool) -> list[dict]:
    """Map common Ansible failures to actionable hints shown in API responses."""
    findings: list[dict] = []
    for f in (failures or []):
        text = _error_text(f)
        task = str((f or {}).get("task") or "")
        host = (f or {}).get("host")
        for rule in _DIAGNOSTIC_RULES:
            try:
                hit = bool(rule["match"](text, task))
            except Exception:
                hit = False
            if hit:
                findings.append({
                    "code": rule["code"],
                    "severity": rule["severity"],
                    "host": host,
                    "task": task,
                    "hint": rule["hint"],
                })

    # Keep deterministic, deduplicated diagnostics for stable UI rendering/tests.
    seen: set[tuple] = set()
    deduped: list[dict] = []
    for d in findings:
        key = (d.get("code"), d.get("host"), d.get("task"))
        if key in seen:
            continue
        seen.add(key)
        deduped.append(d)

    # If nothing matched and the run failed in check mode, return a generic nudge.
    if not deduped and check_mode and failures:
        deduped.append({
            "code": "check_mode_failure",
            "severity": "info",
            "host": None,
            "task": "",
            "hint": "Preview failed in --check mode. Re-run with /api/runs/preflight to verify controller and target prerequisites.",
        })
    return deduped


def _controller_preflight(check_mode: bool) -> dict:
    checks: list[dict] = []

    def add_bin(name: str, *, required: bool = True, hint: str = "") -> None:
        ok = shutil.which(name) is not None
        checks.append({"kind": "binary", "name": name, "ok": ok, "required": required, "hint": hint})

    def add_module(name: str, *, required: bool = True, hint: str = "") -> None:
        ok = importlib.util.find_spec(name) is not None
        checks.append({"kind": "python_module", "name": name, "ok": ok, "required": required, "hint": hint})

    def add_apt_module_probe() -> None:
        """`python3-apt` can exist only in system Python, while the app runs in /usr/local.

        Checking importlib on the app interpreter alone would report a false negative.
        Probe common controller realities in order:
        1) import in current interpreter,
        2) import with /usr/bin/python3,
        3) dpkg package presence fallback.
        """
        hint = "Apt module in --check mode requires python3-apt on the executing host."
        ok = importlib.util.find_spec("apt") is not None
        note = ""

        if not ok and shutil.which("/usr/bin/python3"):
            try:
                probe = subprocess.run(
                    ["/usr/bin/python3", "-c", "import apt"],
                    capture_output=True,
                    text=True,
                    timeout=3,
                    check=False,
                )
                if probe.returncode == 0:
                    ok = True
                    note = "python3-apt available via /usr/bin/python3"
            except (OSError, subprocess.TimeoutExpired):
                pass

        if not ok and shutil.which("dpkg-query"):
            try:
                probe = subprocess.run(
                    ["dpkg-query", "-W", "-f=${Status}", "python3-apt"],
                    capture_output=True,
                    text=True,
                    timeout=3,
                    check=False,
                )
                if probe.returncode == 0 and "installed" in (probe.stdout or ""):
                    ok = True
                    note = "python3-apt package installed (dpkg)"
            except (OSError, subprocess.TimeoutExpired):
                pass

        # Optional on the controller: only matters when an apt task runs against
        # localhost in --check mode. Most playbooks target remote hosts, where
        # python3-apt lives on the target (not here).
        item = {
            "kind": "python_module",
            "name": "apt",
            "ok": ok,
            "required": False,
            "hint": hint,
        }
        if note:
            item["note"] = note
        checks.append(item)

    add_bin("ansible", hint="Install ansible-core in the app container/host.")
    add_bin("ansible-playbook", hint="Install ansible-core in the app container/host.")
    # `sudo` runs on the target, not on the controller — keep it informational
    # so a slim controller image doesn't fail preflight for a remote-only playbook.
    add_bin("sudo", required=False,
            hint="Only needed on the controller for localhost tasks using become: yes.")
    add_bin("sshpass", required=False, hint="Needed only for password-based SSH auth.")
    add_module("passlib", required=False, hint="Required by password_hash filter in some playbooks.")
    if check_mode:
        add_apt_module_probe()

    failed_required = [c for c in checks if c["required"] and not c["ok"]]
    return {
        "ok": not failed_required,
        "checks": checks,
        "missing_required": [{"name": c["name"], "kind": c["kind"], "hint": c["hint"]} for c in failed_required],
    }


@router.post("")
async def start_run(payload: RunIn):
    """Run a playbook synchronously (waits for completion). For live output use the WebSocket."""
    req, environment_id = await _build_request(payload)
    async with SessionLocal() as session:
        run = Run(project_id=req.project_id, playbook=req.playbook, inventory=req.inventory,
                  tags=",".join(req.tags), status="running", environment_id=environment_id)
        session.add(run)
        await session.commit()
        await session.refresh(run)
        run_id = run.id

    result = await run_playbook(req)
    summary = summarize(result)
    artifacts = await _capture_artifacts(req.project_id, run_id)
    diagnostics = _diagnose_failures(summary.get("failures") or [], check_mode=bool(req.check))

    async with SessionLocal() as session:
        row = await session.get(Run, run_id)
        if row is not None:
            row.status = "canceled" if summary.get("status") == "canceled" else summary["overall"]
            row.ended_at = datetime.utcnow()
            row.stats_json = json.dumps(summary["hosts"])
            row.failures_json = json.dumps(summary["failures"])
            row.artifacts_json = json.dumps(artifacts)
            await session.commit()

    return {"run_id": run_id, "artifacts": artifacts, "diagnostics": diagnostics, **summary}


@router.get("")
async def list_runs(project_id: str | None = None, status: str | None = None,
                    playbook: str | None = None, limit: int = 100, offset: int = 0):
    """Global, filterable run history. Powers the /runs page."""
    async with SessionLocal() as session:
        stmt = (select(Run, Project.name, Environment.name)
                .join(Project, Run.project_id == Project.id)
                .outerjoin(Environment, Run.environment_id == Environment.id))
        if project_id:
            stmt = stmt.where(Run.project_id == project_id)
        if status:
            stmt = stmt.where(Run.status == status)
        if playbook:
            stmt = stmt.where(Run.playbook.contains(playbook))
        stmt = stmt.order_by(Run.started_at.desc()).limit(min(limit, 500)).offset(max(offset, 0))
        rows = (await session.execute(stmt)).all()
    return [
        {
            "id": r.id, "project_id": r.project_id, "project_name": pname,
            "playbook": r.playbook, "inventory": r.inventory, "tags": r.tags,
            "status": r.status,
            "environment_id": r.environment_id, "environment_name": ename,
            "started_at": r.started_at.isoformat() if r.started_at else None,
            "ended_at": r.ended_at.isoformat() if r.ended_at else None,
        }
        for r, pname, ename in rows
    ]


@router.delete("")
async def clear_runs(project_id: str | None = None, status: str | None = None,
                     older_than_days: int | None = None):
    """Delete run-history rows (the dashboard stats and /runs list are derived from
    them). Does NOT touch project files or run artifacts already committed to git.
    Optional filters scope the deletion; no filters = all runs."""
    from datetime import timedelta
    from sqlalchemy import delete as sa_delete
    async with SessionLocal() as session:
        stmt = sa_delete(Run)
        if project_id:
            stmt = stmt.where(Run.project_id == project_id)
        if status:
            stmt = stmt.where(Run.status == status)
        if older_than_days and older_than_days > 0:
            stmt = stmt.where(Run.started_at < datetime.utcnow() - timedelta(days=older_than_days))
        result = await session.execute(stmt)
        await session.commit()
    return {"deleted": getattr(result, "rowcount", 0)}


@router.get("/{run_id}")
async def run_detail(run_id: int):
    """Global run detail by id (without project prefix), useful for /runs views and API tooling."""
    async with SessionLocal() as session:
        row = (await session.execute(
            select(Run, Project.name, Environment.name)
            .join(Project, Run.project_id == Project.id)
            .outerjoin(Environment, Run.environment_id == Environment.id)
            .where(Run.id == run_id)
        )).first()
    if row is None:
        raise HTTPException(404, "run not found")
    run, pname, ename = row
    failures = json.loads(run.failures_json or "[]")
    return {
        "id": run.id,
        "project_id": run.project_id,
        "project_name": pname,
        "playbook": run.playbook,
        "inventory": run.inventory,
        "tags": run.tags,
        "status": run.status,
        "started_at": run.started_at.isoformat() if run.started_at else None,
        "ended_at": run.ended_at.isoformat() if run.ended_at else None,
        "stats": json.loads(run.stats_json or "{}"),
        "failures": failures,
        "artifacts": json.loads(run.artifacts_json or "[]"),
        "diagnostics": _diagnose_failures(failures, check_mode=False),
        "template_id": run.template_id,
        "environment_id": run.environment_id,
        "environment_name": ename,
    }


@router.post("/preflight")
async def preflight(payload: PreflightIn):
    """Validate controller prerequisites and optionally probe target reachability.

    This endpoint is side-effect free: it does not create a Run history row.
    """
    try:
        storage.paths_for(payload.project_id)
    except storage.StorageError as e:
        raise HTTPException(404, str(e))

    controller = _controller_preflight(payload.check)
    targets: dict | None = None
    if payload.include_targets:
        try:
            probe = await run_adhoc(
                payload.project_id,
                payload.host_pattern,
                "ping",
                "",
                payload.inventory,
            )
            targets = summarize(probe)
        except Exception as e:
            targets = {
                "overall": "failed",
                "status": "failed",
                "rc": -1,
                "hosts": {},
                "failures": [{"host": None, "task": "adhoc ping", "result": {"msg": str(e)}}],
            }

    overall_ok = controller["ok"] and (targets is None or targets.get("overall") == "ok")
    return {
        "ok": overall_ok,
        "controller": controller,
        "targets": targets,
    }


@router.post("/preview")
async def preview_run(payload: RunIn):
    """Dry-run in check mode: report which tasks would change on which hosts,
    without applying anything or writing a row to run history."""
    req, _ = await _build_request(payload)
    req.check = True
    req.syntax_check = False
    result = await run_playbook(req)
    summary = summarize(result)
    diagnostics = _diagnose_failures(summary.get("failures") or [], check_mode=True)
    return {"overall": summary["overall"], "status": summary["status"],
            "hosts": summary["hosts"], "failures": summary["failures"],
            "changes": result.changes, "diagnostics": diagnostics}


class AdhocIn(BaseModel):
    project_id: str
    host_pattern: str = "all"
    module: str = "ping"
    args: str = ""
    inventory: str = ""
    credential_ids: list[int] = []


@router.post("/adhoc")
async def adhoc(payload: AdhocIn):
    try:
        storage.paths_for(payload.project_id)
    except storage.StorageError as e:
        raise HTTPException(404, str(e))

    creds = await _resolve_credentials(payload.credential_ids)
    result = await run_adhoc(
        payload.project_id, payload.host_pattern,
        payload.module, payload.args, payload.inventory,
        ssh_key_content=creds.get("ssh_key_content", ""),
        ssh_password_content=creds.get("ssh_password_content", ""),
        become_password_content=creds.get("become_password_content", ""),
    )
    return summarize(result)


@router.websocket("/ws")
async def run_ws(ws: WebSocket):
    """Client sends a RunIn JSON, receives events; `{"action":"cancel"}` aborts.

    HTTP middleware doesn't cover WS scope, so the session cookie is re-checked
    here when auth is enabled — otherwise a LAN attacker could open a WS and
    run any playbook with the configured credentials.
    """
    if auth.auth_enabled() and not auth.verify_token(ws.cookies.get(auth.SESSION_COOKIE)):
        await ws.close(code=4401)  # app-range unauthorized; reject before accept()
        return
    await ws.accept()
    try:
        payload_dict = await ws.receive_json()
        run_in = RunIn(**payload_dict)
        req, environment_id = await _build_request(run_in)
    except HTTPException as e:
        await ws.send_json({"event": "error", "message": e.detail})
        await ws.close()
        return
    except (KeyError, TypeError, ValueError) as e:
        await ws.send_json({"event": "error", "message": f"bad request: {e}"})
        await ws.close()
        return

    queue: asyncio.Queue = asyncio.Queue()
    cancel_event = asyncio.Event()

    def push(event: dict) -> None:
        queue.put_nowait(event)

    async def pump() -> None:
        while True:
            event = await queue.get()
            if event is None:
                return
            try:
                await ws.send_json(event)
            except (WebSocketDisconnect, RuntimeError):
                return

    async def listen_for_control() -> None:
        """Read client messages while the run is in flight; flip cancel on request
        or on early disconnect."""
        while True:
            try:
                msg = await ws.receive_json()
            except (WebSocketDisconnect, RuntimeError):
                cancel_event.set()
                return
            if isinstance(msg, dict) and msg.get("action") == "cancel":
                cancel_event.set()
                return

    # Persist the run upfront so it shows up in history immediately and we have an
    # id to associate with the WS session. We finalize the row after the run ends.
    async with SessionLocal() as session:
        run_row = Run(
            project_id=req.project_id, playbook=req.playbook, inventory=req.inventory,
            tags=",".join(req.tags), status="running",
            template_id=run_in.template_id, environment_id=environment_id,
        )
        session.add(run_row)
        await session.commit()
        await session.refresh(run_row)
        run_id = run_row.id

    pumper = asyncio.create_task(pump())
    listener = asyncio.create_task(listen_for_control())
    try:
        result = await run_playbook(req, on_event=push, cancel_event=cancel_event)
        summary = summarize(result)
        diagnostics = _diagnose_failures(summary.get("failures") or [], check_mode=bool(req.check))
        if cancel_event.is_set():
            summary["canceled"] = True
        artifacts = await _capture_artifacts(req.project_id, run_id)
        async with SessionLocal() as session:
            run = await session.get(Run, run_id)
            if run is not None:
                run.status = "canceled" if summary.get("canceled") else summary["overall"]
                run.ended_at = datetime.utcnow()
                run.stats_json = json.dumps(summary["hosts"])
                run.failures_json = json.dumps(summary["failures"])
                run.artifacts_json = json.dumps(artifacts)
                await session.commit()
        await ws.send_json({"event": "summary", "run_id": run_id, "artifacts": artifacts,
                    "diagnostics": diagnostics, **summary})
    except Exception as e:
        async with SessionLocal() as session:
            run = await session.get(Run, run_id)
            if run is not None:
                run.status = "failed"
                run.ended_at = datetime.utcnow()
                await session.commit()
        await ws.send_json({"event": "error", "message": str(e)})
    finally:
        listener.cancel()
        queue.put_nowait(None)
        await pumper
        try:
            await ws.close()
        except RuntimeError:
            pass
