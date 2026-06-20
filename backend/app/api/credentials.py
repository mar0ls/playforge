"""CRUD for reusable credentials (SSH keys, vault passwords, etc.).

Secrets are write-only over the API: you can post a new value, but GET only
returns metadata + public material. The actual secret is stored on disk and
referenced by file path at run time.
"""
from __future__ import annotations

from datetime import datetime

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel
from sqlalchemy import select

from app.core import credentials as cred_store
from app.models.db import SessionLocal, Credential


router = APIRouter(prefix="/api/credentials", tags=["credentials"])

VALID_KINDS = {"ssh_key", "ssh_password", "vault_password", "become_password", "wireguard_key"}


class CredIn(BaseModel):
    kind: str
    name: str
    description: str = ""
    secret: str
    public_part: str = ""


class CredUpdate(BaseModel):
    name: str | None = None
    description: str | None = None
    secret: str | None = None
    public_part: str | None = None


class CredOut(BaseModel):
    id: int
    kind: str
    name: str
    description: str
    public_part: str
    has_secret: bool
    created_at: datetime
    updated_at: datetime


def _to_out(c: Credential) -> CredOut:
    return CredOut(
        id=c.id, kind=c.kind, name=c.name, description=c.description,
        public_part=c.public_part, has_secret=cred_store.has_secret(c.id),
        created_at=c.created_at, updated_at=c.updated_at,
    )


@router.get("", response_model=list[CredOut])
async def list_credentials():
    async with SessionLocal() as session:
        rows = (await session.execute(select(Credential).order_by(Credential.name))).scalars().all()
    return [_to_out(c) for c in rows]


@router.post("", response_model=CredOut)
async def create_credential(payload: CredIn):
    if payload.kind not in VALID_KINDS:
        raise HTTPException(400, f"kind must be one of: {sorted(VALID_KINDS)}")
    if not payload.secret.strip():
        raise HTTPException(400, "secret is required")
    async with SessionLocal() as session:
        c = Credential(
            kind=payload.kind, name=payload.name, description=payload.description,
            public_part=payload.public_part,
        )
        session.add(c)
        await session.commit()
        await session.refresh(c)
        cred_store.write_secret(c.id, payload.secret)
        return _to_out(c)


@router.patch("/{cred_id}", response_model=CredOut)
async def update_credential(cred_id: int, payload: CredUpdate):
    async with SessionLocal() as session:
        c = await session.get(Credential, cred_id)
        if c is None:
            raise HTTPException(404, "credential not found")
        if payload.name is not None: c.name = payload.name
        if payload.description is not None: c.description = payload.description
        if payload.public_part is not None: c.public_part = payload.public_part
        if payload.secret is not None and payload.secret.strip():
            cred_store.write_secret(cred_id, payload.secret)
        c.updated_at = datetime.utcnow()
        await session.commit()
        await session.refresh(c)
        return _to_out(c)


@router.delete("/{cred_id}")
async def delete_credential(cred_id: int):
    async with SessionLocal() as session:
        c = await session.get(Credential, cred_id)
        if c is None:
            raise HTTPException(404, "credential not found")
        await session.delete(c)
        await session.commit()
    cred_store.delete_secret(cred_id)
    return {"deleted": cred_id}


class CredTestIn(BaseModel):
    project_id: str
    inventory: str = ""        # rel path; falls back to project default
    host_pattern: str = "all"  # which hosts to probe — usually a group or single host


@router.post("/{cred_id}/test")
async def test_credential(cred_id: int, payload: CredTestIn):
    """Probe a credential via a one-task playbook. Per-host ✓/✗ for the UI."""
    import uuid as _uuid

    from app.core import storage
    from app.core.runner import RunRequest, run_playbook

    async with SessionLocal() as session:
        c = await session.get(Credential, cred_id)
    if c is None:
        raise HTTPException(404, "credential not found")
    try:
        storage.paths_for(payload.project_id)
    except storage.StorageError as e:
        raise HTTPException(404, str(e))

    secret = cred_store.read_secret(cred_id)
    if not secret:
        raise HTTPException(400, "credential has no stored secret")

    # Use the playbook runner (not run_adhoc) — it already wires up every
    # credential kind; ad-hoc would need its own credential plumbing. Unique
    # filename per request so concurrent probes (or an overlapping run) don't clobber.
    playbook_rel = f"_cred_probe_{_uuid.uuid4().hex[:8]}.yml"
    paths = storage.paths_for(payload.project_id)
    probe_path = paths.root / playbook_rel
    try:
        if c.kind == "ssh_key":
            probe_path.write_text(
                "---\n"
                f"- hosts: {payload.host_pattern}\n"
                "  gather_facts: false\n"
                "  tasks:\n"
                "    - name: probe ssh\n"
                "      ansible.builtin.ping:\n"
            )
            req = RunRequest(project_id=payload.project_id, playbook=playbook_rel,
                             inventory=payload.inventory, ssh_key_content=secret)
        elif c.kind == "ssh_password":
            probe_path.write_text(
                "---\n"
                f"- hosts: {payload.host_pattern}\n"
                "  gather_facts: false\n"
                "  tasks:\n"
                "    - name: probe ssh\n"
                "      ansible.builtin.ping:\n"
            )
            req = RunRequest(project_id=payload.project_id, playbook=playbook_rel,
                             inventory=payload.inventory,
                             ssh_password_content=secret.rstrip("\n"))
        elif c.kind == "become_password":
            probe_path.write_text(
                "---\n"
                f"- hosts: {payload.host_pattern}\n"
                "  gather_facts: false\n"
                "  become: true\n"
                "  tasks:\n"
                "    - name: probe sudo\n"
                "      ansible.builtin.command: id\n"
                "      changed_when: false\n"
            )
            req = RunRequest(project_id=payload.project_id, playbook=playbook_rel,
                             inventory=payload.inventory,
                             become_password_content=secret.rstrip("\n"))
        elif c.kind == "vault_password":
            # No standalone probe — vault passwords only validate against a file.
            return {"kind": c.kind, "ok": None,
                    "note": "vault passwords are validated when decrypting an encrypted file. "
                            "Open a vault-encrypted file in the editor and try Decrypt."}
        else:
            return {"kind": c.kind, "ok": None,
                    "note": f"no probe defined for credential kind '{c.kind}'"}

        try:
            result = await run_playbook(req)
        finally:
            try:
                probe_path.unlink()
            except OSError:
                pass

        stats = result.stats or {}
        host_status: dict[str, str] = {}
        for host in (stats.get("ok") or {}):
            host_status[host] = "ok"
        for host in (stats.get("unreachable") or {}):
            host_status[host] = "unreachable"
        for host in (stats.get("failures") or {}):
            host_status[host] = "failed"
        ok = bool(host_status) and all(s == "ok" for s in host_status.values())
        return {"kind": c.kind, "ok": ok, "hosts": host_status,
                "failures": [{"host": f.get("host"),
                              "msg": (f.get("result") or {}).get("msg") or f.get("error") or ""}
                             for f in result.failures]}
    finally:
        # The probe playbook is unlinked in the inner finally; nothing else to clean.
        pass
