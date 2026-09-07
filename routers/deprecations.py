from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException

import security
from database import storage
from models import CreateDeprecationData, Developer, UpdateDeprecationData
from serializers import is_mod_owner

router = APIRouter(prefix="/v1/mods/{id}/deprecations", tags=["deprecations"])

MAX_MODS_PER_DEPRECATION = 20


def _get_mod_or_404(id: str) -> dict:
    mod_row = storage.find_one("mods", id=id)
    if mod_row is None:
        raise HTTPException(status_code=404, detail=f"Mod id {id} not found")
    return mod_row


def _check_mod_list(ids: list[str]) -> None:
    if len(ids) > MAX_MODS_PER_DEPRECATION:
        raise HTTPException(
            status_code=400,
            detail=f"Max {MAX_MODS_PER_DEPRECATION} mods allowed per deprecation",
        )
    missing = [mid for mid in ids if storage.find_one("mods", id=mid) is None]
    if missing:
        raise HTTPException(
            status_code=400,
            detail=f"The following mods don't exist on the index: {', '.join(missing)}",
        )


@router.get("", summary="Fetch all deprecations for a mod")
def list_deprecations(id: str):
    _get_mod_or_404(id)
    rows = storage.find_all("deprecations", lambda d: d["mod_id"] == id)
    return {"error": "", "payload": rows}


@router.post("", status_code=201, summary="Insert one deprecation for a mod")
def create_deprecation(
    id: str,
    body: CreateDeprecationData,
    developer: Developer = Depends(security.get_current_developer),
):
    mod = _get_mod_or_404(id)
    if not (developer.admin or is_mod_owner(mod, developer.id)):
        raise HTTPException(status_code=403, detail="Forbidden")
    _check_mod_list(body.by)
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
    developer: Developer = Depends(security.get_current_developer),
):
    mod = _get_mod_or_404(id)
    if not (developer.admin or is_mod_owner(mod, developer.id)):
        raise HTTPException(status_code=403, detail="Forbidden")
    storage.delete_where("deprecations", lambda d: d["mod_id"] == id)
    return None


@router.put("/{deprecation_id}", summary="Update a deprecation")
def update_deprecation(
    id: str,
    deprecation_id: int,
    body: UpdateDeprecationData,
    developer: Developer = Depends(security.get_current_developer),
):
    mod = _get_mod_or_404(id)
    row = storage.find_one("deprecations", id=deprecation_id)
    if row is None or row["mod_id"] != id:
        raise HTTPException(status_code=404, detail=f"Deprecation id {deprecation_id} not found")
    if not (developer.admin or is_mod_owner(mod, developer.id)):
        raise HTTPException(status_code=403, detail="Forbidden")

    if body.by is not None:
        _check_mod_list(body.by)
    if body.reason is not None and not body.reason.strip():
        raise HTTPException(status_code=400, detail="reason cannot be empty")

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
    developer: Developer = Depends(security.get_current_developer),
):
    mod = _get_mod_or_404(id)
    row = storage.find_one("deprecations", id=deprecation_id)
    if row is None or row["mod_id"] != id:
        raise HTTPException(status_code=404, detail=f"Deprecation id {deprecation_id} not found")
    if not (developer.admin or is_mod_owner(mod, developer.id)):
        raise HTTPException(status_code=403, detail="Forbidden")
    storage.delete_where("deprecations", lambda d: d["id"] == deprecation_id)
    return None
