from __future__ import annotations

import os
import random
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import FileResponse

import mod_queries as mq
import security
import utils
from config import settings
from database import storage
from models import AddDevPayload, CreateQueryParams, Developer, IndexSortType, ModVersionStatusEnum, UpdateModPayload, VerPlatform
from pagination import page_params, paginate
from serializers import is_mod_developer, is_mod_owner, mod_public

router = APIRouter(prefix="/v1/mods", tags=["mods"])


@router.get("", summary="List all mods with optional filtering and pagination")
def list_mods(
    query: Optional[str] = Query(default=None), gd: Optional[str] = Query(default=None), platforms: Optional[str] = Query(default=None),
    sort: IndexSortType = Query(default=IndexSortType.downloads), geode: Optional[str] = Query(default=None), developer: Optional[str] = Query(default=None),
    tags: Optional[str] = Query(default=None), featured: Optional[bool] = Query(default=None), jitless: Optional[bool] = Query(default=None),
    status: Optional[ModVersionStatusEnum] = Query(default=None), page_and_size: tuple[int, int] = Depends(page_params),
    current_dev: Optional[Developer] = Depends(security.get_current_developer_optional),
):
    page, per_page = page_and_size
    visible_statuses = mq.resolve_visible_statuses(status, is_admin=bool(current_dev and current_dev.admin))
    platform_list = mq.parse_platforms(platforms)
    tag_list = mq.parse_tags(tags)
    matches: list[tuple[dict, dict]] = []

    for mod in storage.all_rows("mods"):
        if developer:
            usernames = {
                d["username"].lower()
                for d in (storage.find_one("developers", id=x["developer_id"]) for x in mod["developers"])
                if d
            }
            if developer.lower() not in usernames:
                continue
        if featured is not None and mod["featured"] != featured:
            continue
        mod_tags = {t.lower() for t in mod.get("tags", [])}
        if tag_list and not set(tag_list).issubset(mod_tags):
            continue
        if jitless is not None and ("jitless" in mod_tags) != jitless:
            continue
        if query:
            q = query.lower()
            version_names = [v["name"].lower() for v in storage.find_all("mod_versions", lambda v, mid=mod["id"]: v["mod_id"] == mid)]
            if q not in mod["id"].lower() and not any(q in name for name in version_names):
                continue
        version = mq.select_best_version(mod["id"], visible_statuses, gd, platform_list, geode)
        if version is not None:
            matches.append((mod, version))

    if sort == IndexSortType.downloads:
        matches.sort(key=lambda pair: pair[0]["download_count"], reverse=True)
    elif sort == IndexSortType.recently_updated:
        matches.sort(key=lambda pair: pair[0]["updated_at"], reverse=True)
    elif sort == IndexSortType.recently_published:
        matches.sort(key=lambda pair: pair[0]["created_at"], reverse=True)
    elif sort == IndexSortType.oldest:
        matches.sort(key=lambda pair: pair[0]["created_at"])
    elif sort == IndexSortType.name:
        matches.sort(key=lambda pair: pair[1]["name"].lower())
    elif sort == IndexSortType.name_reverse:
        matches.sort(key=lambda pair: pair[1]["name"].lower(), reverse=True)
    elif sort == IndexSortType.random:
        random.shuffle(matches)

    paged = paginate(matches, page, per_page)
    paged["data"] = [mod_public(mod, [version], requester=current_dev) for mod, version in paged["data"]]
    return {"error": "", "payload": paged}


@router.post("", status_code=201, summary="Create a new mod or submit a new version for an existing mod")
async def create_mod(body: CreateQueryParams, developer: Developer = Depends(security.require_not_banned)):
    manifest = await utils.fetch_and_parse_geode_file(body.download_link)
    manifest["_download_link"] = body.download_link
    mod_id = manifest["id"]
    existing_mod = storage.find_one("mods", id=mod_id)

    if existing_mod is not None:
        if not (developer.admin or is_mod_developer(existing_mod, developer.id)):
            raise HTTPException(status_code=403, detail="Forbidden")

        _validate_new_version(mod_id, manifest["version"])
        make_accepted = _new_version_status(mod_id, developer)
        version_row = _build_version_row(
            manifest,
            mod_id,
            initial_status=ModVersionStatusEnum.accepted.value if make_accepted else ModVersionStatusEnum.pending.value,
        )

        versions = mq.versions_for_mod(mod_id)
        versions.sort(key=lambda v: mq.compare_versions_key(v["version"]), reverse=True)
        if versions and versions[0]["status"] == ModVersionStatusEnum.pending.value:
            version_row["id"] = versions[0]["id"]
            version_row["created_at"] = versions[0].get("created_at") or version_row["created_at"]
            storage.update_where("mod_versions", {"id": versions[0]["id"]}, version_row)
            version_row = storage.find_one("mod_versions", id=versions[0]["id"])
        else:
            storage.insert("mod_versions", version_row)

        if not make_accepted:
            _ensure_submission(version_row["id"])
        else:
            _refresh_mod_metadata(mod_id, manifest)
            _save_logo(mod_id, manifest)

        return {"error": "", "payload": mod_public(existing_mod, [version_row], requester=developer)}

    now = storage.now_iso()
    mod_row = {
        "id": mod_id,
        "repository": manifest.get("repository"),
        "links": {
            "homepage": (manifest.get("links") or {}).get("homepage"),
            "community": (manifest.get("links") or {}).get("community"),
            "source": manifest.get("repository") or (manifest.get("links") or {}).get("source"),
        },
        "tags": mq.filter_submittable_tags(manifest.get("tags", [])),
        "featured": False,
        "download_count": 0,
        "developers": [{"developer_id": developer.id, "is_owner": True}],
        "about": manifest.get("_about"),
        "changelog": manifest.get("_changelog"),
        "created_at": now,
        "updated_at": now,
    }
    storage.insert("mods", mod_row)

    version_row = _build_version_row(manifest, mod_id, initial_status=ModVersionStatusEnum.pending.value)
    storage.insert("mod_versions", version_row)
    _ensure_submission(version_row["id"])
    mod_row = _refresh_mod_metadata(mod_id, manifest)
    _save_logo(mod_id, manifest)
    return {"error": "", "payload": mod_public(mod_row, [version_row], requester=developer)}


@router.get("/updates", summary="Get available mod updates for a list of installed mods")
def get_mod_updates(ids: str = Query(...), gd: str = Query(...), platform: VerPlatform = Query(...), geode: str = Query(...)):
    updates, deprecations_out = [], []
    for mod_id in ids.split(";")[:200]:
        mod_id = mod_id.strip()
        if not mod_id:
            continue
        mod_deprecations = storage.find_all("deprecations", lambda d, mid=mod_id: d["mod_id"] == mid)
        if mod_deprecations:
            deprecations_out.extend(mod_deprecations)
            continue
        best = mq.select_best_version(mod_id, [ModVersionStatusEnum.accepted.value], gd, [platform], geode)
        if best is None:
            continue
        updates.append({
            "id": mod_id,
            "version": best["version"],
            "download_link": best["download_link"],
            "dependencies": best.get("dependencies") or [],
            "incompatibilities": best.get("incompatibilities") or [],
            "replacement": None,
        })
    return {"error": "", "payload": {"updates": updates, "deprecations": deprecations_out}}


@router.get("/{id}", summary="Get a specific mod by ID")
def get_mod(id: str, current_dev: Optional[Developer] = Depends(security.get_current_developer_optional)):
    mod_row = storage.find_one("mods", id=id)
    if mod_row is None:
        raise HTTPException(status_code=404, detail="Mod not found")
    all_versions = mq.versions_for_mod(id)
    if current_dev and current_dev.admin:
        versions = all_versions
    else:
        versions = [v for v in all_versions if v["status"] in (ModVersionStatusEnum.accepted.value, ModVersionStatusEnum.pending.value)]
        if not versions:
            raise HTTPException(status_code=404, detail="Mod not found")
    versions.sort(key=lambda v: mq.compare_versions_key(v["version"]), reverse=True)
    return {"error": "", "payload": mod_public(mod_row, versions, requester=current_dev)}


@router.put("/{id}", status_code=204, summary="Update a mod (admin only)")
def update_mod(id: str, body: UpdateModPayload, _admin: Developer = Depends(security.require_admin)):
    if storage.find_one("mods", id=id) is None:
        raise HTTPException(status_code=404, detail="Mod not found")
    storage.update_where("mods", {"id": id}, {"featured": body.featured, "updated_at": storage.now_iso()})
    return None


@router.get("/{id}/logo", summary="Get the logo image for a mod")
def get_mod_logo(id: str):
    if storage.find_one("mods", id=id) is None:
        raise HTTPException(status_code=404, detail="Logo not found")
    path = os.path.join(settings.LOGOS_DIR, f"{id}.png")
    if not os.path.exists(path):
        raise HTTPException(status_code=404, detail="Logo not found")
    return FileResponse(path, media_type="image/png")


@router.post("/{id}/developers", status_code=204, summary="Add a developer to a mod", tags=["developers"])
def add_mod_developer(id: str, body: AddDevPayload, developer: Developer = Depends(security.require_not_banned)):
    mod_row = storage.find_one("mods", id=id)
    if mod_row is None:
        raise HTTPException(status_code=404, detail="Mod not found")
    if not is_mod_owner(mod_row, developer.id):
        raise HTTPException(status_code=403, detail="Forbidden")
    target = storage.find_one("developers", username=body.username)
    if target is not None and security.get_active_ban(target["id"]) is not None:
        raise HTTPException(status_code=403, detail="The developer being added is banned")
    if target is None:
        raise HTTPException(status_code=400, detail=f"No developer found with username {body.username}")
    if any(d["developer_id"] == target["id"] for d in mod_row["developers"]):
        raise HTTPException(status_code=400, detail="Developer already added to this mod")
    storage.update_where("mods", {"id": id}, {"developers": mod_row["developers"] + [{"developer_id": target["id"], "is_owner": False}], "updated_at": storage.now_iso()})
    return None


@router.delete("/{id}/developers/{username}", status_code=204, summary="Remove a developer from a mod", tags=["developers"])
def remove_mod_developer(id: str, username: str, developer: Developer = Depends(security.get_current_developer)):
    mod_row = storage.find_one("mods", id=id)
    if mod_row is None:
        raise HTTPException(status_code=404, detail="Mod not found")
    if not is_mod_owner(mod_row, developer.id):
        raise HTTPException(status_code=403, detail="Forbidden")
    target = storage.find_one("developers", username=username)
    if target is None:
        raise HTTPException(status_code=404, detail=f"No developer found with username {username}")
    if target["id"] == developer.id:
        raise HTTPException(status_code=409, detail="Cannot remove self from mod developer list")
    if not any(d["developer_id"] == target["id"] for d in mod_row["developers"]):
        raise HTTPException(status_code=404, detail=f"{username} is not a developer for this mod")
    storage.update_where("mods", {"id": id}, {"developers": [d for d in mod_row["developers"] if d["developer_id"] != target["id"]], "updated_at": storage.now_iso()})
    return None


def _initial_status_for(developer: Developer) -> str:
    return ModVersionStatusEnum.pending.value


def _normalize_version(version: str) -> str:
    if len(version) > 1 and version[0] in "vV" and version[1].isdigit():
        return version[1:]
    return version


def _build_version_row(manifest: dict, mod_id: str, initial_status: str = ModVersionStatusEnum.pending.value) -> dict:
    now = storage.now_iso()
    gd = manifest.get("gd") or {}
    if isinstance(gd, str):
        gd = {"win": gd, "mac": gd, "ios": gd, "android": gd}
    mac, android = gd.get("mac"), gd.get("android")
    version = _normalize_version(manifest["version"])
    return {
        "id": storage.next_id("mod_versions"), "mod_id": mod_id, "name": manifest.get("name", mod_id), "version": version,
        "download_link": f"{settings.BASE_URL}/v1/mods/{mod_id}/versions/{version}/download",
        "direct_download_link": manifest.get("_download_link"), "hash": manifest["_hash"], "geode": manifest["geode"],
        "early_load": bool(manifest.get("early-load", False)), "api": bool(manifest.get("api", False)) or "api" in manifest,
        "status": initial_status, "download_count": 0, "requires_patching": bool(manifest.get("requires_patching", False)), "info": None,
        "gd": {"win": gd.get("win"), "ios": gd.get("ios"), "mac-intel": gd.get("mac-intel", mac), "mac-arm": gd.get("mac-arm", mac), "android32": gd.get("android32", android), "android64": gd.get("android64", android)},
        "description": manifest.get("description"), "dependencies": utils.parse_dependencies(manifest.get("dependencies")),
        "incompatibilities": utils.parse_incompatibilities(manifest.get("incompatibilities")), "tags": mq.filter_submittable_tags(manifest.get("tags", [])),
        "created_at": now, "updated_at": now,
    }


def _validate_new_version(mod_id: str, new_version: str) -> None:
    highest = mq.highest_existing_version(mod_id)
    if highest is not None:
        try:
            valid = utils.compare_versions(new_version, highest) > 0
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        if not valid:
            raise HTTPException(status_code=409, detail=f"Version {new_version} is not newer than this mod's latest submitted version ({highest}) -- submit a higher version number")


def _new_version_status(id: str, developer: Developer) -> bool:
    if not developer.verified:
        return False
    versions = mq.versions_for_mod(id)
    return any(v["status"] in (ModVersionStatusEnum.accepted.value, ModVersionStatusEnum.unlisted.value) for v in versions)


def _refresh_mod_metadata(mod_id: str, manifest: dict) -> dict:
    patch: dict = {"updated_at": storage.now_iso()}
    tags = mq.filter_submittable_tags(manifest.get("tags", []))
    if tags:
        patch["tags"] = tags
    links = manifest.get("links") or {}
    if links or manifest.get("repository"):
        patch["links"] = {"homepage": links.get("homepage"), "community": links.get("community"), "source": manifest.get("repository") or links.get("source")}
    if manifest.get("repository"):
        patch["repository"] = manifest["repository"]
    if manifest.get("_about"):
        patch["about"] = manifest["_about"]
    if manifest.get("_changelog"):
        patch["changelog"] = manifest["_changelog"]
    return storage.update_where("mods", {"id": mod_id}, patch)


def _ensure_submission(mod_version_id: int) -> dict:
    existing = storage.find_one("submissions", mod_version_id=mod_version_id)
    if existing:
        return existing
    now = storage.now_iso()
    row = storage.insert("submissions", {"mod_version_id": mod_version_id, "lock": "none", "locked_by": None, "created_at": now, "updated_at": now})
    storage.insert("submission_audit", {"id": storage.next_id("submission_audit"), "submission_id": mod_version_id, "action": "created", "details": None, "performed_by": None, "performed_at": now})
    return row


def _save_logo(mod_id: str, manifest: dict) -> None:
    logo_bytes = manifest.get("_logo_bytes")
    if not logo_bytes:
        return
    os.makedirs(settings.LOGOS_DIR, exist_ok=True)
    with open(os.path.join(settings.LOGOS_DIR, f"{mod_id}.png"), "wb") as f:
        f.write(logo_bytes)
