from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query, Response

import security
from database import storage
from models import CreateVersionBody, Developer, VerPlatform
from pagination import paginate

router = APIRouter(prefix="/v1/loader/versions", tags=["loader"])

_PLATFORM_TO_GD_FIELD = {
    VerPlatform.win: "win",
    VerPlatform.ios: "ios",
    VerPlatform.mac_intel: "mac-intel",
    VerPlatform.mac_arm: "mac-arm",
}


def _matches_platform(row: dict, platform: Optional[VerPlatform]) -> bool:
    if platform is None:
        return True
    if platform == VerPlatform.android:
        return bool(
            row["downloads"].get("android32")
            or row["downloads"].get("android64")
        )
    if platform == VerPlatform.android32:
        return bool(row["downloads"].get("android32"))
    if platform == VerPlatform.android64:
        return bool(row["downloads"].get("android64"))
    if platform == VerPlatform.mac:
        return bool(row["downloads"].get("mac"))
    gd_field = _PLATFORM_TO_GD_FIELD.get(platform)
    if gd_field:
        return bool(row["gd"].get(gd_field))
    return True


def _matches_gd(row: dict, gd: Optional[str]) -> bool:
    if not gd or gd == "*":
        return True
    return gd in row["gd"].values()


@router.get("", summary="Get all loader versions with optional filtering")
def list_loader_versions(
    gd: Optional[str] = Query(default=None),
    platform: Optional[VerPlatform] = Query(default=None),
    prerelease: bool = Query(default=False),
    page: int = Query(default=1, ge=1),
    per_page: int = Query(default=10, ge=1, le=50),
):
    rows = storage.all_rows("loader_versions")
    rows = [r for r in rows if r["prerelease"] == prerelease]
    rows = [r for r in rows if _matches_platform(r, platform) and _matches_gd(r, gd)]
    rows.sort(key=lambda r: r["created_at"], reverse=True)

    return {
        "error": "",
        "payload": paginate(rows, page, per_page),
    }


@router.post("", status_code=204, summary="Create a new loader version (admin only)")
def create_loader_version(
    body: CreateVersionBody,
    _admin: Developer = Depends(security.require_admin),
):
    tag = body.tag.strip().lstrip("v")
    if not tag:
        raise HTTPException(status_code=400, detail="Loader version tag cannot be empty")
    if storage.find_one("loader_versions", tag=tag) or storage.find_one("loader_versions", version=tag):
        raise HTTPException(status_code=400, detail="A loader version with this tag already exists")

    # The upstream server creates the version and asynchronously uploads its
    # release assets to S3. This Python reimplementation cannot reproduce the
    # S3 worker without configured object storage, so it preserves the API's
    # 204 response and records the version with empty download metadata rather
    # than fabricating GitHub asset URLs.
    row = {
        "tag": tag,
        "version": tag,
        "commit_hash": body.commit_hash,
        "prerelease": bool(body.prerelease),
        "created_at": storage.now_iso(),
        "gd": body.gd.model_dump(by_alias=True),
        "downloads": {
            "win": {"url": "", "hash": ""},
            "mac": {"url": "", "hash": ""},
            "android32": {"url": "", "hash": ""},
            "android64": {"url": "", "hash": ""},
            "ios": {"url": "", "hash": ""},
            "resources": {"url": "", "hash": ""},
        },
    }
    storage.insert("loader_versions", row)
    return Response(status_code=204)


@router.get("/{version}", summary="Get a specific loader version (or latest)")
def get_loader_version(
    version: str,
    platform: Optional[VerPlatform] = Query(default=None),
    gd: Optional[str] = Query(default=None),
    prerelease: bool = Query(default=False),
):
    rows = storage.all_rows("loader_versions")
    if not prerelease:
        rows = [r for r in rows if not r["prerelease"]]
    rows = [r for r in rows if _matches_platform(r, platform) and _matches_gd(r, gd)]

    if version == "latest":
        if not rows:
            raise HTTPException(status_code=404, detail="Latest version not found")
        rows.sort(key=lambda r: r["created_at"], reverse=True)
        return {"error": "", "payload": rows[0]}

    # The upstream server looks up an exact loader tag here. Do not silently
    # match an unrelated normalized version string.
    row = next((r for r in rows if r["tag"] == version), None)
    if row is None:
        raise HTTPException(status_code=404, detail="Not found")
    return {"error": "", "payload": row}
