from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import JSONResponse
from pydantic import BaseModel

import security
from database import storage
from models import Developer

router = APIRouter(prefix="/OpenGeode", tags=["opentags"])


class TagPayload(BaseModel):
    name: str
    display_name: str
    readonly: bool = False


@router.get("", summary="Report OpenGeode support and capabilities")
def opengeode_info():
    return JSONResponse(content={"enabled": True})


@router.get("/tags", summary="Get OpenGeode tag definitions")
def get_tags(_admin: Developer = Depends(security.require_admin)):
    return {"error": "", "payload": storage.all_rows("tags")}


@router.post("/tags", summary="Add a new tag")
def create_tag(payload: TagPayload, _admin: Developer = Depends(security.require_admin)):
    name = payload.name.strip()
    display_name = payload.display_name.strip()
    if not name or not display_name:
        raise HTTPException(status_code=400, detail="name and display_name cannot be empty")
    if storage.find_one("tags", name=name) is not None:
        raise HTTPException(status_code=409, detail="Tag already exists")
    row = {
        "id": storage.next_id("tags"),
        "name": name,
        "display_name": display_name,
        "is_readonly": payload.readonly,
    }
    storage.insert("tags", row)
    return {"error": "", "payload": row}


@router.put("/tags/{tag_name}", summary="Update a tag")
def update_tag(tag_name: str, payload: TagPayload, _admin: Developer = Depends(security.require_admin)):
    old_name = tag_name.strip()
    new_name = payload.name.strip()
    display_name = payload.display_name.strip()
    if not old_name:
        raise HTTPException(status_code=400, detail="Tag name cannot be empty")
    if not new_name or not display_name:
        raise HTTPException(status_code=400, detail="name and display_name cannot be empty")
    existing = storage.find_one("tags", name=old_name)
    if existing is None:
        raise HTTPException(status_code=404, detail="Tag not found")
    if new_name != old_name and storage.find_one("tags", name=new_name) is not None:
        raise HTTPException(status_code=409, detail="Tag already exists")
    updated = storage.update_where(
        "tags", {"name": old_name},
        {"name": new_name, "display_name": display_name, "is_readonly": payload.readonly},
    )
    if new_name != old_name:
        for mod in storage.all_rows("mods"):
            tags = mod.get("tags") or []
            if old_name not in tags:
                continue
            storage.update_where("mods", {"id": mod["id"]}, {
                "tags": [new_name if tag == old_name else tag for tag in tags]
            })
    return {"error": "", "payload": updated}


@router.delete("/tags", summary="Delete a tag", status_code=204)
def delete_tag(name: str, _admin: Developer = Depends(security.require_admin)):
    name = name.strip()
    if storage.find_one("tags", name=name) is None:
        raise HTTPException(status_code=404, detail="Tag not found")
    storage.delete_where("tags", lambda d: d.get("name") == name)
    for mod in storage.all_rows("mods"):
        tags = mod.get("tags") or []
        if name not in tags:
            continue
        storage.update_where("mods", {"id": mod["id"]}, {
            "tags": [tag for tag in tags if tag != name]
        })
    return None
