from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException

import security
from database import storage
from models import Developer

router = APIRouter(prefix="/OpenGeode", tags=["opentags"])

@router.post("/tags", summary="Add a new tag")
def create_tag(
    name: str,
    display_name: str,
    readonly: bool,
    _admin: Developer = Depends(security.require_admin)
):
    if not name.strip() or not display_name.strip():
        raise HTTPException(status_code=400, detail="name cannot be empty")
    row = {
        "id": storage.next_id("tags"),
        "name": name,
        "display_name": display_name,
        "readonly": readonly
    }
    storage.insert("tags", row)
    return {"error": "", "payload": row}

@router.delete("/tags", summary="Delete a tag", status_code=204)
def delete_tag(
    name: str,
    _admin: Developer = Depends(security.require_admin)
):
    row = storage.find_one("tags", name=name)
    if row is None:
        raise HTTPException(status_code=404, detail="Tag not found")
    storage.delete_where("tags", lambda d: d["name"] == name)
    return None