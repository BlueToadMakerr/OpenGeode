from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query

import security
from database import storage
from models import (
    Developer,
    DeveloperUpdatePayload,
    ModDeveloper,
    ModVersionStatusEnum,
    SimpleDevMod,
    SimpleDevModVersion,
    UploadProfilePayload,
)
from pagination import page_params, paginate
from serializers import developer_public as _developer_public

router = APIRouter(tags=["developers"])


# NOTE: order matters -- /v1/me and /v1/me/mods must be declared before the
# /v1/developers/{id} routes to avoid "me" being parsed as an id.

@router.get("/v1/me", summary="Get the current developer's profile")
def get_me(developer: Developer = Depends(security.get_current_developer)):
    return {"error": "", "payload": developer.model_dump()}


@router.put("/v1/me", summary="Update the current developer's profile")
def update_me(
    body: UploadProfilePayload,
    developer: Developer = Depends(security.get_current_developer),
):
    if not body.display_name or not body.display_name.strip():
        raise HTTPException(status_code=400, detail="display_name cannot be empty")

    updated = storage.update_where(
        "developers", {"id": developer.id}, {"display_name": body.display_name.strip()}
    )
    return {"error": "", "payload": _developer_public(updated)}


@router.get("/v1/me/mods", summary="Get all mods owned by the current developer")
def get_my_mods(
    status: Optional[ModVersionStatusEnum] = Query(default=None),
    only_owner: bool = Query(default=False),
    developer: Developer = Depends(security.get_current_developer),
):
    mods = storage.find_all(
        "mods",
        lambda m: any(
            d["developer_id"] == developer.id and (not only_owner or d["is_owner"])
            for d in m["developers"]
        ),
    )

    result = []
    for mod in mods:
        versions = storage.find_all("mod_versions", lambda v, mid=mod["id"]: v["mod_id"] == mid)
        target_status = status.value if status is not None else ModVersionStatusEnum.accepted.value
        versions = [v for v in versions if v["status"] == target_status]
        versions.sort(key=lambda v: v.get("created_at") or "", reverse=True)

        dev_rows = [storage.find_one("developers", id=d["developer_id"]) for d in mod["developers"]]
        mod_developers = [
            ModDeveloper(
                id=dr["id"],
                username=dr["username"],
                display_name=dr["display_name"],
                is_owner=next(d["is_owner"] for d in mod["developers"] if d["developer_id"] == dr["id"]),
            )
            for dr in dev_rows
            if dr
        ]

        result.append(
            SimpleDevMod(
                id=mod["id"],
                featured=mod["featured"],
                download_count=mod["download_count"],
                versions=[
                    SimpleDevModVersion(
                        name=v["name"],
                        version=v["version"],
                        download_count=v["download_count"],
                        validated=v["status"] == ModVersionStatusEnum.accepted.value,
                        info=v.get("info") or "",
                        status=v["status"],
                    )
                    for v in versions
                ],
                developers=mod_developers,
            ).model_dump()
        )

    return {"error": "", "payload": result}


@router.delete("/v1/me/token", status_code=204, summary="Delete the current API token")
def delete_current_token(session_id: str = Depends(security.get_current_session_id)):
    security.revoke_session(session_id)
    return None


@router.delete("/v1/me/tokens", status_code=204, summary="Delete all API tokens for the current developer")
def delete_all_tokens(developer: Developer = Depends(security.get_current_developer)):
    security.revoke_all_sessions(developer.id)
    return None


@router.get("/v1/developers", summary="List all developers with optional search and pagination")
def list_developers(
    query: Optional[str] = Query(default=None),
    page_and_size: tuple[int, int] = Depends(page_params),
):
    page, per_page = page_and_size
    rows = storage.all_rows("developers")
    if query:
        q = query.lower()
        rows = [r for r in rows if q in r["username"].lower() or q in r["display_name"].lower()]
    rows.sort(key=lambda r: r["id"])

    result = paginate(rows, page, per_page)
    result["data"] = [_developer_public(r) for r in result["data"]]
    return {"error": "", "payload": result}


@router.get("/v1/developers/{id}", summary="Get a specific developer by ID")
def get_developer(id: int):
    row = storage.find_one("developers", id=id)
    if row is None:
        raise HTTPException(status_code=404, detail="Developer not found")
    return {"error": "", "payload": _developer_public(row)}


@router.put(
    "/v1/developers/{id}",
    summary="Update a developer's admin/verified status (admin only)",
)
def update_developer(
    id: int,
    body: DeveloperUpdatePayload,
    _admin: Developer = Depends(security.require_admin),
):
    row = storage.find_one("developers", id=id)
    if row is None:
        raise HTTPException(status_code=404, detail="Developer not found")

    patch = {}
    if body.admin is not None:
        patch["admin"] = body.admin
    if body.verified is not None:
        patch["verified"] = body.verified
    if not patch:
        raise HTTPException(status_code=400, detail="No fields to update")

    updated = storage.update_where("developers", {"id": id}, patch)
    return {"error": "", "payload": _developer_public(updated)}


# NOTE: POST /v1/mods/{id}/developers and DELETE /v1/mods/{id}/developers/{username}
# are tagged "developers" in the OpenAPI spec but live in routers/mods.py,
# since they need the same mod-ownership permission logic as the rest of the
# mods routes.
