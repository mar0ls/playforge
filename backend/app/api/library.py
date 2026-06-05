"""Read-only library endpoints (starter playbook templates)."""
from __future__ import annotations

from fastapi import APIRouter, HTTPException

from app.core import playbook_templates

router = APIRouter(prefix="/api/library", tags=["library"])


@router.get("/playbook-templates")
async def playbook_templates_list():
    return playbook_templates.catalog()


@router.get("/playbook-templates/{template_id}")
async def playbook_template_detail(template_id: str):
    tpl = playbook_templates.get(template_id)
    if tpl is None:
        raise HTTPException(404, f"template not found: {template_id}")
    return tpl
