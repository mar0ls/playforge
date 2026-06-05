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

VALID_KINDS = {"ssh_key", "vault_password", "become_password", "wireguard_key"}


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
        public_part=c.public_part, has_secret=cred_store.secret_path(c.id).is_file(),
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
