from __future__ import annotations

import os
import random
import re
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import FileResponse

import mod_queries as mq
import security
import utils
from config import settings
from database import storage
from models import (
    AddDevPayload,
    CreateQueryParams,
    Developer,
    IndexSortType,
    ModVersionStatusEnum,
    UpdateModPayload,
    VerPlatform,
)
from pagination import page_params, paginate
from serializers import is_mod_developer, mod_public

router = APIRouter(prefix="/v1/mods", tags=["mods"])


@router.get("", summary="List all mods with optional filtering and pagination")
def list_mods(
    query: Optional[str] = Query(default=None),
    gd: Optional[str] = Query(default=None),
    platforms: Optional[str] = Query(default=None),
    sort: IndexSortType = Query(default=IndexSortType.downloads),
    geode: Optional[str] = Query(default=None),
    developer: Optional[str] = Query(default=None),
    tags: Optional[str] = Query(default=None),
    featured: Optional[bool] = Query(default=None),
    jitless: Optional[bool] = Query(default=None),
    status: Optional[ModVersionStatusEnum] = Query(default=None),
    page_and_size: tuple[int, int] = Depends(page_params),
    current_dev: Optional[Developer] = Depends(security.get_current_developer_optional),
):
    page, per_page = page_and_size
    visible_statuses = mq.resolve_visible_statuses(
        status, is_admin=bool(current_dev and current_dev.admin)
    )

    platform_list = mq.parse_platforms(platforms)
    tag_list = mq.parse_tags(tags)

    mods = storage.all_rows("mods")
    matches: list[tuple[dict, dict]] = []

    for mod in mods:
        if developer:
            dev_usernames = {
                d["username"].lower()
                for d in (storage.find_one("developers", id=x["developer_id"]) for x in mod["developers"])
                if d
            }
            if developer.lower() not in dev_usernames:
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
            version_names = [
                v["name"].lower() for v in storage.find_all("mod_versions", lambda v, mid=mod["id"]: v["mod_id"] == mid)
            ]
            if q not in mod["id"].lower() and not any(q in name for name in version_names):
                continue

        version = mq.select_best_version(mod["id"], visible_statuses, gd, platform_list, geode)
        if version is None:
            continue

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


@router.post("", status_code=201, summary="Create a new mod")
async def create_mod(
    body: CreateQueryParams,
    developer: Developer = Depends(security.get_current_developer),
):
    manifest = await utils.fetch_and_parse_geode_file(body.download_link)
    manifest["_download_link"] = body.download_link
    mod_id = manifest["id"]

    existing_mod = storage.find_one("mods", id=mod_id)

    if existing_mod is not None:
        # POST /v1/mods doubles as "submit a new version" once the mod
        # already exists, mirroring POST /v1/mods/{id}/versions -- same
        # permission and version-ordering rules apply.
        if not (developer.admin or is_mod_developer(existing_mod, developer.id)):
            raise HTTPException(
                status_code=403,
                detail="Only this mod's developers or an admin may submit new versions",
            )
        _validate_new_version(mod_id, manifest["version"])

        version_row = _build_version_row(manifest, mod_id, initial_status=_initial_status_for(developer))
        storage.insert("mod_versions", version_row)
        _ensure_submission(version_row["id"])
        mod_row = _refresh_mod_metadata(mod_id, manifest)
        _save_logo(mod_id, manifest)

        return {"error": "", "payload": mod_public(mod_row, [version_row], requester=developer)}

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

    version_row = _build_version_row(manifest, mod_id, initial_status=_initial_status_for(developer))
    storage.insert("mod_versions", version_row)
    _ensure_submission(version_row["id"])
    _save_logo(mod_id, manifest)

    return {"error": "", "payload": mod_public(mod_row, [version_row], requester=developer)}


@router.get("/updates", summary="Get available mod updates for a list of installed mods")
def get_mod_updates(
    ids: str = Query(...),
    gd: str = Query(...),
    platform: VerPlatform = Query(...),
    geode: str = Query(...),
):
    updates = []
    deprecations_out = []

    # Confirmed from real traffic: ids are semicolon-separated bare mod ids
    # (e.g. "a.mod;b.mod;c.mod"), not comma-separated "id@version" pairs.
    # The client doesn't tell the server what it currently has installed --
    # it just gets back the current best-matching version for every id it
    # asks about (minus deprecated/unmatched ones) and presumably diffs
    # that against its own local state itself.
    for chunk in re.split(r"[;,]", ids):
        mod_id = chunk.strip()
        if not mod_id:
            continue
        # Still tolerate an "id@version" shape defensively, in case some
        # client does send one -- just ignored, since it's not used to
        # filter anything.
        mod_id = mod_id.partition("@")[0]

        mod_deprecations = storage.find_all("deprecations", lambda d, mid=mod_id: d["mod_id"] == mid)
        if mod_deprecations:
            deprecations_out.extend(mod_deprecations)
            continue

        best = mq.select_best_version(
            mod_id, [ModVersionStatusEnum.accepted.value], gd, [platform], geode
        )
        if best is None:
            continue

        updates.append(
            {
                "id": mod_id,
                "version": best["version"],
                # Confirmed from real traffic: this endpoint always returns
                # an empty string here, even for mods whose normal
                # download_link (e.g. from GET /v1/mods/{id}) is populated.
                "download_link": "",
                "dependencies": best.get("dependencies") or [],
                "incompatibilities": best.get("incompatibilities") or [],
                "replacement": None,
            }
        )

    return {"error": "", "payload": {"updates": updates, "deprecations": deprecations_out}}


@router.get("/{id}", summary="Get a specific mod by ID")
def get_mod(
    id: str,
    current_dev: Optional[Developer] = Depends(security.get_current_developer_optional),
):
    mod_row = storage.find_one("mods", id=id)
    if mod_row is None:
        raise HTTPException(status_code=404, detail="Mod not found")

    is_admin = bool(current_dev and current_dev.admin)
    all_versions = mq.versions_for_mod(id)
    if is_admin:
        versions = all_versions
    else:
        versions = [v for v in all_versions if v["status"] != ModVersionStatusEnum.rejected.value]
        if not versions and all_versions:
            # Every version of this mod has been rejected -- treat the mod
            # itself as invisible to the public, same as if it didn't exist.
            # Admins still get the 200 response above with the full history.
            raise HTTPException(status_code=404, detail="Mod not found")

    versions.sort(key=lambda v: mq.compare_versions_key(v["version"]), reverse=True)
    return {"error": "", "payload": mod_public(mod_row, versions, requester=current_dev)}


@router.put("/{id}", status_code=204, summary="Update a mod (admin only)")
def update_mod(
    id: str,
    body: UpdateModPayload,
    _admin: Developer = Depends(security.require_admin),
):
    mod_row = storage.find_one("mods", id=id)
    if mod_row is None:
        raise HTTPException(status_code=404, detail="Mod not found")
    storage.update_where("mods", {"id": id}, {"featured": body.featured, "updated_at": storage.now_iso()})
    return None


@router.get("/{id}/logo", summary="Get the logo image for a mod")
def get_mod_logo(id: str):
    mod_row = storage.find_one("mods", id=id)
    if mod_row is None:
        raise HTTPException(status_code=404, detail="Logo not found")
    path = os.path.join(settings.LOGOS_DIR, f"{id}.png")
    if not os.path.exists(path):
        raise HTTPException(status_code=404, detail="Logo not found")
    return FileResponse(path, media_type="image/png")


@router.post("/{id}/developers", status_code=204, summary="Add a developer to a mod", tags=["developers"])
def add_mod_developer(
    id: str,
    body: AddDevPayload,
    developer: Developer = Depends(security.get_current_developer),
):
    mod_row = storage.find_one("mods", id=id)
    if mod_row is None:
        raise HTTPException(status_code=404, detail="Mod not found")
    if not (developer.admin or is_mod_developer(mod_row, developer.id)):
        raise HTTPException(
            status_code=403,
            detail="Only this mod's owner, one of its developers, or an admin can add developers",
        )

    target = storage.find_one("developers", username=body.username)
    if target is None:
        raise HTTPException(status_code=404, detail="Developer not found")

    if any(d["developer_id"] == target["id"] for d in mod_row["developers"]):
        raise HTTPException(status_code=400, detail="Developer already added to this mod")

    new_developers = mod_row["developers"] + [{"developer_id": target["id"], "is_owner": False}]
    storage.update_where("mods", {"id": id}, {"developers": new_developers, "updated_at": storage.now_iso()})
    return None


@router.delete("/{id}/developers/{username}", status_code=204, summary="Remove a developer from a mod", tags=["developers"])
def remove_mod_developer(
    id: str,
    username: str,
    developer: Developer = Depends(security.get_current_developer),
):
    mod_row = storage.find_one("mods", id=id)
    if mod_row is None:
        raise HTTPException(status_code=404, detail="Mod not found")
    if not (developer.admin or is_mod_developer(mod_row, developer.id)):
        raise HTTPException(
            status_code=403,
            detail="Only this mod's owner, one of its developers, or an admin can remove developers",
        )

    target = storage.find_one("developers", username=username)
    if target is None:
        raise HTTPException(status_code=404, detail="Developer not found")

    # Nobody -- not even an admin -- can remove themselves from a mod
    # through this endpoint; have someone else do it instead.
    if target["id"] == developer.id:
        raise HTTPException(status_code=409, detail="Cannot remove yourself from a mod")

    remaining = [d for d in mod_row["developers"] if d["developer_id"] != target["id"]]
    if len(remaining) == len(mod_row["developers"]):
        raise HTTPException(status_code=404, detail="Developer is not attached to this mod")

    storage.update_where("mods", {"id": id}, {"developers": remaining, "updated_at": storage.now_iso()})
    return None


# ---------------------------------------------------------------------------
# Shared helpers used by both mod creation (above) and version creation
# (routers/mod_versions.py imports this).
# ---------------------------------------------------------------------------

def _initial_status_for(developer: Developer) -> str:
    """Verified developers' submissions are auto-accepted instead of going
    through the normal pending-review queue."""
    return ModVersionStatusEnum.accepted.value if developer.verified else ModVersionStatusEnum.pending.value


def _normalize_version(version: str) -> str:
    """The real API stores/returns versions without a leading 'v' (e.g.
    mod.json's "v1.1.1" becomes "1.1.1") -- confirmed from live data."""
    if len(version) > 1 and version[0] in "vV" and version[1].isdigit():
        return version[1:]
    return version


def _build_version_row(manifest: dict, mod_id: str, initial_status: str = ModVersionStatusEnum.pending.value) -> dict:
    now = storage.now_iso()
    gd = manifest.get("gd") or {}
    if isinstance(gd, str):
        # Some mod.json manifests use a shorthand like "gd": "2.2074" that
        # applies to every platform instead of a per-platform object.
        gd = {"win": gd, "mac": gd, "ios": gd, "android": gd}

    # Current Geode mod.json only exposes combined "mac" and "android" keys
    # (no more per-architecture split at the manifest level) -- newer GD
    # releases don't let a mod submit partial platform support anymore, so
    # "mac" covers both mac-intel/mac-arm and "android" covers both
    # android32/android64. Still honor explicit split keys if a manifest
    # provides them directly, falling back to the combined key otherwise.
    mac = gd.get("mac")
    android = gd.get("android")

    version = _normalize_version(manifest["version"])
    return {
        "id": storage.next_id("mod_versions"),
        "mod_id": mod_id,
        "name": manifest.get("name", mod_id),
        "version": version,
        # The real API's `download_link` always points back at its own
        # /download redirect route rather than the raw URL the developer
        # submitted (confirmed from live responses) -- the original URL is
        # kept as `direct_download_link` instead.
        "download_link": f"{settings.BASE_URL}/v1/mods/{mod_id}/versions/{version}/download",
        "direct_download_link": manifest.get("_download_link"),
        "hash": manifest["_hash"],
        "geode": manifest["geode"],
        "early_load": bool(manifest.get("early-load", False)),
        "api": bool(manifest.get("api", False)) or "api" in manifest,
        "status": initial_status,
        "download_count": 0,
        # Most mods hook through Geode's API and don't touch the GD binary
        # directly, so "no patching needed" is the sane default when a
        # manifest doesn't say either way (confirmed against a real mod that
        # omits the field entirely and comes back `requires_patching: false`).
        "requires_patching": bool(manifest.get("requires_patching", False)),
        "info": None,
        "gd": {
            "win": gd.get("win"),
            "ios": gd.get("ios"),
            "mac-intel": gd.get("mac-intel", mac),
            "mac-arm": gd.get("mac-arm", mac),
            "android32": gd.get("android32", android),
            "android64": gd.get("android64", android),
        },
        "description": manifest.get("description"),
        "dependencies": utils.parse_dependencies(manifest.get("dependencies")),
        "incompatibilities": utils.parse_incompatibilities(manifest.get("incompatibilities")),
        "tags": mq.filter_submittable_tags(manifest.get("tags", [])),
        "created_at": now,
        "updated_at": now,
    }


def _validate_new_version(mod_id: str, new_version: str) -> None:
    """A submitted version must be strictly newer than every version that
    already exists for this mod (across any status) -- used by both
    POST /v1/mods (submitting to an existing mod) and
    POST /v1/mods/{id}/versions."""
    highest = mq.highest_existing_version(mod_id)
    if highest is not None and utils.compare_versions(new_version, highest) <= 0:
        raise HTTPException(
            status_code=409,
            detail=(
                f"Version {new_version} is not newer than this mod's latest submitted "
                f"version ({highest}) -- submit a higher version number"
            ),
        )


def _refresh_mod_metadata(mod_id: str, manifest: dict) -> dict:
    """Keep a mod's top-level about/changelog/tags/links/repository in sync
    with the latest submitted version's about.md/changelog.md/mod.json,
    without wiping out existing values when the new submission doesn't
    provide something (e.g. an update that only touches changelog.md)."""
    patch: dict = {"updated_at": storage.now_iso()}

    tags = mq.filter_submittable_tags(manifest.get("tags", []))
    if tags:
        patch["tags"] = tags

    links = manifest.get("links") or {}
    if links or manifest.get("repository"):
        patch["links"] = {
            "homepage": links.get("homepage"),
            "community": links.get("community"),
            "source": manifest.get("repository") or links.get("source"),
        }
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
    row = {
        "mod_version_id": mod_version_id,
        "lock": "none",
        "locked_by": None,
        "created_at": now,
        "updated_at": now,
    }
    return storage.insert("submissions", row)


def _save_logo(mod_id: str, manifest: dict) -> None:
    """Persist a package's logo.png (extracted by utils.parse_geode_package)
    to disk, keyed by mod id, so GET /v1/mods/{id}/logo can serve it. Only
    overwrites the stored logo when this submission actually included one
    -- a version that doesn't repackage logo.png shouldn't wipe out the
    existing icon."""
    logo_bytes = manifest.get("_logo_bytes")
    if not logo_bytes:
        return
    os.makedirs(settings.LOGOS_DIR, exist_ok=True)
    path = os.path.join(settings.LOGOS_DIR, f"{mod_id}.png")
    with open(path, "wb") as f:
        f.write(logo_bytes)
