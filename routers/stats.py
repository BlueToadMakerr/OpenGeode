from __future__ import annotations

from fastapi import APIRouter

from database import storage

router = APIRouter(prefix="/v1", tags=["stats"])


@router.get("/stats", summary="Get global index statistics")
def get_stats():
    mods = storage.all_rows("mods")
    versions = storage.all_rows("mod_versions")
    developers = storage.all_rows("developers")

    total_mod_downloads = sum(v["download_count"] for v in versions)

    payload = {
        "total_mod_count": len(mods),
        "total_mod_downloads": total_mod_downloads,
        # NOTE: the upstream server tracks a running counter of loader
        # (Geode itself) downloads server-side. This lightweight storage
        # layer doesn't have a natural place to increment that outside of a
        # dedicated download-redirect endpoint for the loader, which isn't
        # part of this OpenAPI spec, so we report 0 here. Wire this up to a
        # real counter if you add loader download tracking.
        "total_geode_downloads": 0,
        "total_registered_developers": len(developers),
    }
    return {"error": "", "payload": payload}
