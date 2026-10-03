from __future__ import annotations

from enum import Enum
from urllib.parse import quote

from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import RedirectResponse

from config import settings
from database import storage

router = APIRouter(prefix="/v1/mods", tags=["mods"])


class StatusBadgeStat(str, Enum):
    version = "version"
    gd_version = "gd_version"
    geode_version = "geode_version"
    downloads = "downloads"


@router.get("/{id}/status_badge", summary="Get a dynamic Shields.io status badge for a mod")
def status_badge(id: str, stat: StatusBadgeStat = Query(...)):
    mod = storage.find_one("mods", id=id)
    if mod is None:
        raise HTTPException(status_code=404, detail="Mod not found")

    version = None
    versions = storage.find_all("mod_versions", lambda v: v.get("mod_id") == id and v.get("status") == "accepted")
    versions.sort(key=lambda v: v.get("created_at") or "", reverse=True)
    if versions:
        version = versions[0]

    if stat == StatusBadgeStat.version:
        value = (version or {}).get("version", "unknown")
        label = "Version"
    elif stat == StatusBadgeStat.gd_version:
        value = ((version or {}).get("gd") or {}).get("win", "unknown")
        label = "Geometry Dash"
    elif stat == StatusBadgeStat.geode_version:
        value = (version or {}).get("geode", "unknown")
        label = "Geode"
    else:
        value = mod.get("download_count", 0)
        label = "Downloads"

    api_url = f"{settings.BASE_URL}/v1/mods/{quote(id, safe='')}?abbreviate=true"
    mod_link = f"{settings.FRONT_URL.rstrip('/')}/mods/{quote(id, safe='')}"
    # Keep the same Shields dynamic-JSON mechanism as the upstream endpoint.
    query_map = {
        StatusBadgeStat.version: "payload.versions[0].version",
        StatusBadgeStat.gd_version: "payload.versions[0].gd.win",
        StatusBadgeStat.geode_version: "payload.versions[0].geode",
        StatusBadgeStat.downloads: "payload.download_count",
    }
    shields_url = (
        "https://img.shields.io/badge/dynamic/json"
        f"?url={quote(api_url, safe='')}"
        f"&query={quote(query_map[stat], safe='')}"
        f"&label={quote(label, safe='')}"
        "&labelColor=%230c0811&color=%235f3d84"
        f"&link={quote(mod_link, safe='')}"
        "&style=plastic"
    )
    return RedirectResponse(shields_url, status_code=302)
