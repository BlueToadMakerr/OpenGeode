from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query

import security
from database import storage
from models import CreateVersionBody, Developer, VerPlatform
from pagination import page_params, paginate
from utils import compare_versions

router = APIRouter(prefix="/v1/loader/versions", tags=["loader"])

_PLATFORM_TO_GD_FIELD = {
    VerPlatform.win: "win",
    VerPlatform.ios: "ios",
    VerPlatform.mac: "mac-intel",
    VerPlatform.mac_intel: "mac-intel",
    VerPlatform.mac_arm: "mac-arm",
    # android has no per-platform GD field in DetailedGDVersion; it just
    # needs a loader build to exist for it (checked against `downloads`).
}


def _matches_platform(row: dict, platform: Optional[VerPlatform]) -> bool:
    if platform is None:
        return True
    if platform == VerPlatform.android:
        return bool(row["downloads"].get("android32") or row["downloads"].get("android64"))
    if platform in (VerPlatform.android32, VerPlatform.android64):
        return bool(row["downloads"].get(platform.value))
    gd_field = _PLATFORM_TO_GD_FIELD.get(platform)
    return bool(row["gd"].get(gd_field)) if gd_field else True


def _matches_gd(row: dict, gd: Optional[str]) -> bool:
    if not gd or gd == "*":
        return True
    return gd in row["gd"].values()


@router.get("", summary="Get all loader versions with optional filtering")
def list_loader_versions(
    gd: Optional[str] = Query(default=None),
    platform: Optional[VerPlatform] = Query(default=None),
    prerelease: Optional[bool] = Query(default=None),
    page_and_size: tuple[int, int] = Depends(page_params),
):
    page, per_page = page_and_size
    rows = storage.all_rows("loader_versions")

    if prerelease is not None:
        rows = [r for r in rows if r["prerelease"] == prerelease]
    rows = [r for r in rows if _matches_platform(r, platform) and _matches_gd(r, gd)]

    rows.sort(key=lambda r: r["created_at"], reverse=True)

    result = paginate(rows, page, per_page)
    return {"error": "", "payload": result}


@router.post("", status_code=201, summary="Create a new loader version (admin only)")
def create_loader_version(
    body: CreateVersionBody,
    _admin: Developer = Depends(security.require_admin),
):
    if storage.find_one("loader_versions", tag=body.tag):
        raise HTTPException(status_code=400, detail="A loader version with this tag already exists")

    row = {
        "tag": body.tag,
        "version": body.tag.lstrip("v"),
        "commit_hash": body.commit_hash,
        "prerelease": bool(body.prerelease),
        "created_at": storage.now_iso(),
        "gd": body.gd.model_dump(by_alias=True),
        # Download URLs aren't part of CreateVersionBody -- the real server
        # derives them from the tag against a fixed GitHub release asset
        # naming convention. We do the same here, pointing at the
        # geode-sdk/geode GitHub releases for this tag; swap this out if you
        # host build artifacts elsewhere.
        "downloads": {
            platform: {
                "url": f"https://github.com/geode-sdk/geode/releases/download/{body.tag}/{asset}",
                "hash": "",
            }
            for platform, asset in {
                "win": "geode-installer-win.exe",
                "mac": "geode-installer-mac.pkg",
                "android32": "geode-installer-android32.apk",
                "android64": "geode-installer-android64.apk",
                "ios": "geode-installer-ios.deb",
                "resources": "resources.zip",
            }.items()
        },
    }
    storage.insert("loader_versions", row)
    return {"error": "", "payload": row}


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
            raise HTTPException(status_code=404, detail="No loader versions available")
        rows.sort(key=lambda r: r["created_at"], reverse=True)
        return {"error": "", "payload": rows[0]}

    row = next((r for r in rows if r["tag"] == version or r["version"] == version), None)
    if row is None:
        raise HTTPException(status_code=404, detail="Version not found")
    return {"error": "", "payload": row}
