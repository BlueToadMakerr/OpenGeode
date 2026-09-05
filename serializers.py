from __future__ import annotations

from typing import Optional

from database import storage


def developer_has_accepted_mod(developer_id: int) -> bool:
    """Whether this developer has at least one mod version that's been
    accepted -- across any mod they're a developer of, owner or not."""
    for mod in storage.all_rows("mods"):
        if not any(d["developer_id"] == developer_id for d in mod["developers"]):
            continue
        has_accepted = storage.find_one("mod_versions", mod_id=mod["id"], status="accepted")
        if has_accepted:
            return True
    return False


def developer_public(row: dict) -> dict:
    return {
        "id": row["id"],
        "username": row["username"],
        "display_name": row["display_name"],
        "verified": row["verified"],
        "admin": row["admin"],
        "github_id": row["github_id"],
        "has_accepted_mod": developer_has_accepted_mod(row["id"]),
    }


def mod_developers(mod_row: dict) -> list[dict]:
    out = []
    for d in mod_row["developers"]:
        dev = storage.find_one("developers", id=d["developer_id"])
        if dev is None:
            continue
        out.append(
            {
                "id": dev["id"],
                "username": dev["username"],
                "display_name": dev["display_name"],
                "is_owner": d["is_owner"],
            }
        )
    return out


def mod_version_public(
    v: dict,
    mod_row: Optional[dict] = None,
    *,
    context: str = "detail",
    requester: Optional[object] = None,
) -> dict:
    mod_row = mod_row or storage.find_one("mods", id=v["mod_id"])

    is_privileged = bool(
        requester
        and (getattr(requester, "admin", False) or (mod_row and is_mod_developer(mod_row, requester.id)))
    )

    out = {
        "name": v["name"],
        "version": v["version"],
        "download_link": v["download_link"],
        "hash": v["hash"],
        "geode": v["geode"],
        "early_load": v["early_load"],
        "api": v["api"],
        "mod_id": v["mod_id"],
        "status": v["status"],
        "download_count": v["download_count"],
        "requires_patching": v["requires_patching"],
        "gd": v["gd"],
        "description": v.get("description"),
        "created_at": v.get("created_at"),
        "updated_at": v.get("updated_at"),
    }

    if context != "embedded":
        out["dependencies"] = v.get("dependencies") or []
        out["incompatibilities"] = v.get("incompatibilities") or []

    if is_privileged:
        if v.get("direct_download_link"):
            out["direct_download_link"] = v["direct_download_link"]
        if v.get("info"):
            out["info"] = v["info"]

    return out


def mod_public(mod_row: dict, versions: list[dict], *, requester: Optional[object] = None) -> dict:
    return {
        "id": mod_row["id"],
        "repository": mod_row.get("repository"),
        "links": mod_row.get("links"),
        "tags": mod_row.get("tags", []),
        "featured": mod_row["featured"],
        "download_count": mod_row["download_count"],
        "developers": mod_developers(mod_row),
        "versions": [
            mod_version_public(v, mod_row, context="embedded", requester=requester) for v in versions
        ],
        "about": mod_row.get("about"),
        "changelog": mod_row.get("changelog"),
        "created_at": mod_row["created_at"],
        "updated_at": mod_row["updated_at"],
    }


def is_mod_developer(mod_row: dict, developer_id: int) -> bool:
    return any(d["developer_id"] == developer_id for d in mod_row["developers"])


def is_mod_owner(mod_row: dict, developer_id: int) -> bool:
    return any(
        d["developer_id"] == developer_id and d["is_owner"] for d in mod_row["developers"]
    )
