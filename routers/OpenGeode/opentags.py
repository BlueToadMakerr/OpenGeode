from __future__ import annotations

import hashlib
import secrets
import string
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import JSONResponse
from pydantic import BaseModel

import security
from config import settings
from database import storage
from models import AuthTokens, Developer

router = APIRouter(prefix="/OpenGeode", tags=["opentags"])


class TagPayload(BaseModel):
    name: str
    display_name: str
    readonly: bool = False


class GdLoginCodePayload(BaseModel):
    code: str


def _code_hash(code: str) -> str:
    return hashlib.sha256(code.encode("utf-8")).hexdigest()


def _cleanup_login_codes() -> None:
    now = datetime.now(timezone.utc)
    storage.delete_where(
        "gd_login_codes",
        lambda row: storage.parse_iso(row["expires_at"]) <= now,
    )


@router.get("", summary="Report OpenGeode support and capabilities")
def opengeode_info():
    return JSONResponse(content={
        "enabled": True,
        "allowGdLogin": settings.ALLOW_GD_LOGIN,
    })


@router.post("/login-code", summary="Generate a short-lived Geometry Dash login code")
def generate_gd_login_code(developer: Developer = Depends(security.get_current_developer)):
    if not settings.ALLOW_GD_LOGIN:
        raise HTTPException(status_code=403, detail="Geometry Dash login is disabled on this server")

    _cleanup_login_codes()
    storage.delete_where("gd_login_codes", lambda row: row["developer_id"] == developer.id)

    alphabet = string.ascii_uppercase + string.digits
    code = "".join(secrets.choice(alphabet) for _ in range(4))
    now = datetime.now(timezone.utc)
    expires_at = now + timedelta(seconds=settings.GD_LOGIN_CODE_EXPIRE_SECONDS)

    storage.insert(
        "gd_login_codes",
        {
            "id": storage.next_id("gd_login_codes"),
            "code_hash": _code_hash(code),
            "developer_id": developer.id,
            "created_at": storage.format_iso(now),
            "expires_at": storage.format_iso(expires_at),
        },
    )

    return {
        "error": "",
        "payload": {
            "code": code,
            "expires_at": storage.format_iso(expires_at),
            "expires_in": settings.GD_LOGIN_CODE_EXPIRE_SECONDS,
        },
    }


@router.post("/login-code/login", summary="Exchange a Geometry Dash login code for API tokens")
def login_with_gd_code(payload: GdLoginCodePayload):
    if not settings.ALLOW_GD_LOGIN:
        raise HTTPException(status_code=403, detail="Geometry Dash login is disabled on this server")

    code = payload.code.strip().upper()
    if len(code) != 4 or any(ch not in string.ascii_uppercase + string.digits for ch in code):
        raise HTTPException(status_code=400, detail="Invalid login code")

    _cleanup_login_codes()
    row = storage.find_one("gd_login_codes", code_hash=_code_hash(code))
    if row is None:
        raise HTTPException(status_code=401, detail="Invalid or expired login code")

    developer = storage.find_one("developers", id=row["developer_id"])
    if developer is None:
        storage.delete_where("gd_login_codes", lambda item: item["id"] == row["id"])
        raise HTTPException(status_code=401, detail="Invalid login code")

    # Codes are one-time credentials. Delete before issuing the session so a
    # successful code can never be reused, even if the client retries.
    storage.delete_where("gd_login_codes", lambda item: item["id"] == row["id"])
    access_token, refresh_token = security.issue_session(developer["id"])
    tokens = AuthTokens(access_token=access_token, refresh_token=refresh_token)
    return {"error": "", "payload": tokens.model_dump()}


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
