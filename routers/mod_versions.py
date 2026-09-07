from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import RedirectResponse

import mod_queries as mq
import security
import utils
from database import storage
from models import CreateQueryParams, Developer, ModVersionStatusEnum, UpdatePayload, VerPlatform
from pagination import page_params, paginate
from routers.mods import _build_version_row, _ensure_submission, _refresh_mod_metadata, _save_logo, _validate_new_version
from serializers import is_mod_developer, mod_version_public

router = APIRouter(prefix="/v1/mods/{id}/versions", tags=["mod_versions"])


def _get_mod_or_404(id: str) -> dict:
    mod_row = storage.find_one("mods", id=id)
    if mod_row is None:
        raise HTTPException(status_code=404, detail="Mod not found")
    return mod_row


def _new_version_status(id: str, developer: Developer) -> bool:
    """Return whether a new version should be immediately accepted.

    The upstream server only auto-accepts a verified developer's subsequent
    version after that mod already has an accepted or unlisted version. The
    first version is always pending.
    """
    if not developer.verified:
        return False
    versions = mq.versions_for_mod(id)
    return any(v["status"] in (ModVersionStatusEnum.accepted.value, ModVersionStatusEnum.unlisted.value) for v in versions)


@router.get("", summary="List all versions for a mod")
def list_versions(
    id: str,
    gd: Optional[str] = Query(default=None),
    platforms: Optional[str] = Query(default=None),
    status: Optional[ModVersionStatusEnum] = Query(default=None),
    compare: Optional[str] = Query(default=None),
    page_and_size: tuple[int, int] = Depends(page_params),
    current_dev: Optional[Developer] = Depends(security.get_current_developer_optional),
):
    page, per_page = page_and_size
    mod_row = _get_mod_or_404(id)
    platform_list = mq.parse_platforms(platforms)
    compare_parsed = mq.parse_compare(compare)
    visible_statuses = mq.resolve_visible_statuses(status, is_admin=bool(current_dev and current_dev.admin))
    versions = mq.filter_versions(id, visible_statuses, gd, platform_list, compare_parsed)
    paged = paginate(versions, page, min(per_page, 50))
    paged["data"] = [mod_version_public(v, mod_row, context="list", requester=current_dev) for v in paged["data"]]
    return {"error": "", "payload": paged}


@router.post("", status_code=201, summary="Create a new version for a mod")
async def create_version(
    id: str,
    body: CreateQueryParams,
    developer: Developer = Depends(security.get_current_developer),
):
    mod_row = _get_mod_or_404(id)
    if not (developer.admin or is_mod_developer(mod_row, developer.id)):
        raise HTTPException(status_code=403, detail="Only mod developers or admins may add versions")

    manifest = await utils.fetch_and_parse_geode_file(body.download_link)
    manifest["_download_link"] = body.download_link
    if manifest["id"] != id:
        raise HTTPException(status_code=400, detail=f"Request id {id} does not match mod.json id {manifest['id']}")

    _validate_new_version(id, manifest["version"])

    make_accepted = _new_version_status(id, developer)
    new_row = _build_version_row(
        manifest,
        id,
        initial_status=ModVersionStatusEnum.accepted.value if make_accepted else ModVersionStatusEnum.pending.value,
    )

    versions = mq.versions_for_mod(id)
    versions.sort(key=lambda v: mq.compare_versions_key(v["version"]), reverse=True)
    if versions and versions[0]["status"] == ModVersionStatusEnum.pending.value:
        # Upstream replaces an existing pending submission rather than adding
        # a second pending version. Preserve the version/submission id.
        new_row["id"] = versions[0]["id"]
        new_row["created_at"] = versions[0].get("created_at") or new_row["created_at"]
        storage.update_where("mod_versions", {"id": versions[0]["id"]}, new_row)
        version_row = storage.find_one("mod_versions", id=versions[0]["id"])
    else:
        storage.insert("mod_versions", new_row)
        version_row = new_row

    if not make_accepted:
        _ensure_submission(version_row["id"])
        # Pending submissions must not change the public mod metadata.
    else:
        _refresh_mod_metadata(id, manifest)
        _save_logo(id, manifest)

    return {"error": "", "payload": mod_version_public(version_row, mod_row, requester=developer)}


@router.get("/{version}", summary="Get a specific version of a mod")
def get_version(
    id: str,
    version: str,
    platforms: Optional[str] = Query(default=None),
    gd: Optional[str] = Query(default=None),
    major: Optional[int] = Query(default=None),
    current_dev: Optional[Developer] = Depends(security.get_current_developer_optional),
):
    mod_row = _get_mod_or_404(id)
    platform_list = mq.parse_platforms(platforms)
    if version == "latest":
        row = mq.select_best_version(id, [ModVersionStatusEnum.accepted.value], gd, platform_list, None)
        if major is not None and row is not None:
            try:
                row_major = int(row["version"].split(".", 1)[0])
            except (ValueError, IndexError):
                row = None
            if row is not None and row_major != major:
                candidates = [
                    v for v in mq.versions_for_mod(id, [ModVersionStatusEnum.accepted.value])
                    if mq.version_matches_gd(v, gd) and mq.version_matches_platforms(v, platform_list)
                    and v["version"].split(".", 1)[0].isdigit() and int(v["version"].split(".", 1)[0]) == major
                ]
                candidates.sort(key=lambda v: mq.compare_versions_key(v["version"]), reverse=True)
                row = candidates[0] if candidates else None
    else:
        row = storage.find_one("mod_versions", mod_id=id, version=version)
        if row is not None and row["status"] == ModVersionStatusEnum.rejected.value and not (current_dev and current_dev.admin):
            raise HTTPException(status_code=404, detail="Couldn't find valid mod version for given filters")

    if row is None:
        raise HTTPException(status_code=404, detail="Couldn't find valid mod version for given filters")
    return {"error": "", "payload": mod_version_public(row, mod_row, context="list", requester=current_dev)}


@router.put("/{version}", status_code=204, summary="Update a mod version status (admin only)")
def update_version_status(
    id: str,
    version: str,
    body: UpdatePayload,
    _admin: Developer = Depends(security.require_admin),
):
    _get_mod_or_404(id)
    row = storage.find_one("mod_versions", mod_id=id, version=version)
    if row is None:
        raise HTTPException(status_code=404, detail="Mod or version not found")
    storage.update_where("mod_versions", {"id": row["id"]}, {"status": body.status.value, "info": body.info, "updated_at": storage.now_iso()})
    return None


@router.get("/{version}/download", summary="Download a specific version of a mod (redirects to download URL)")
def download_version(
    id: str,
    version: str,
    gd: Optional[str] = Query(default=None),
    platforms: Optional[str] = Query(default=None),
    major: Optional[int] = Query(default=None),
):
    _get_mod_or_404(id)
    platform_list = mq.parse_platforms(platforms)
    if version == "latest":
        row = mq.select_best_version(id, [ModVersionStatusEnum.accepted.value], gd, platform_list, None)
    else:
        row = storage.find_one("mod_versions", mod_id=id, version=version)
        if row is not None and row["status"] != ModVersionStatusEnum.accepted.value:
            # Match the upstream route: pending/unlisted/rejected versions
            # still resolve if the exact version exists; the upstream storage
            # layer controls whether it can be downloaded.
            pass

    if row is None:
        raise HTTPException(status_code=404, detail="Couldn't find valid mod version for given filters")

    target = row.get("direct_download_link") or row["download_link"]
    return RedirectResponse(url=target, status_code=302)
