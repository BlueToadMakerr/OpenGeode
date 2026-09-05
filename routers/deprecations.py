from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException

import security
from database import storage
from models import CreateDeprecationData, Developer, UpdateDeprecationData

router = APIRouter(prefix="/v1/mods/{id}/deprecations", tags=["deprecations"])


def _get_mod_or_404(id: str) -> dict:
    mod_row = storage.find_one("mods", id=id)
    if mod_row is None:
        raise HTTPException(status_code=404, detail="Mod not found")
    return mod_row


@router.get("", summary="Fetch all deprecations for a mod")
def list_deprecations(id: str):
    _get_mod_or_404(id)
    rows = storage.find_all("deprecations", lambda d: d["mod_id"] == id)
    return {"error": "", "payload": rows}


@router.post("", status_code=201, summary="Insert one deprecation for a mod")
def create_deprecation(
    id: str,
    body: CreateDeprecationData,
    _admin: Developer = Depends(security.require_admin),
):
    _get_mod_or_404(id)
    if not body.reason.strip():
        raise HTTPException(status_code=400, detail="reason cannot be empty")

    row = {
        "id": storage.next_id("deprecations"),
        "mod_id": id,
        "reason": body.reason,
        "by": body.by,
    }
    storage.insert("deprecations", row)
    return {"error": "", "payload": row}


@router.delete("", status_code=204, summary="Delete all deprecations for a mod")
def delete_all_deprecations(
    id: str,
    _admin: Developer = Depends(security.require_admin),
):
    _get_mod_or_404(id)
    storage.delete_where("deprecations", lambda d: d["mod_id"] == id)
    return None


@router.put("/{deprecation_id}", summary="Update a deprecation")
def update_deprecation(
    id: str,
    deprecation_id: int,
    body: UpdateDeprecationData,
    _admin: Developer = Depends(security.require_admin),
):
    _get_mod_or_404(id)
    row = storage.find_one("deprecations", id=deprecation_id, mod_id=id)
    if row is None:
        raise HTTPException(status_code=404, detail="Deprecation not found")

    patch = {}
    if body.reason is not None:
        patch["reason"] = body.reason
    if body.by is not None:
        patch["by"] = body.by
    updated = storage.update_where("deprecations", {"id": deprecation_id}, patch) if patch else row
    return {"error": "", "payload": updated}


@router.delete("/{deprecation_id}", status_code=204, summary="Delete a deprecation")
def delete_deprecation(
    id: str,
    deprecation_id: int,
    _admin: Developer = Depends(security.require_admin),
):
    _get_mod_or_404(id)
    deleted = storage.delete_where("deprecations", lambda d: d["id"] == deprecation_id and d["mod_id"] == id)
    if not deleted:
        raise HTTPException(status_code=404, detail="Deprecation not found")
    return None
