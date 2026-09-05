from __future__ import annotations

import re
from typing import Optional

from fastapi import HTTPException

from database import storage
from models import ModVersionStatusEnum, VerPlatform
from utils import compare_versions, version_matches

_ANDROID_PLATFORMS = {VerPlatform.android, VerPlatform.android32, VerPlatform.android64}

_GD_FIELD_BY_PLATFORM = {
    VerPlatform.win: "win",
    VerPlatform.ios: "ios",
    VerPlatform.mac: "mac-intel",
    VerPlatform.mac_intel: "mac-intel",
    VerPlatform.mac_arm: "mac-arm",
}

_COMPARE_RE = re.compile(r"^(>=|<=|>|<|=)?\s*(.+)$")

# Rejected mods are not viewable for denied users (non admins)
PUBLICLY_VISIBLE_STATUSES = [s.value for s in ModVersionStatusEnum if s != ModVersionStatusEnum.rejected]


def resolve_visible_statuses(
    status: Optional[ModVersionStatusEnum],
    is_admin: bool = False,
) -> list[str]:
    if status is None:
        return [ModVersionStatusEnum.accepted.value]
    if status == ModVersionStatusEnum.rejected and not is_admin:
        raise HTTPException(status_code=403, detail="Rejected mods cannot be viewed")
    return [status.value]


def parse_platforms(raw: Optional[str]) -> Optional[list[VerPlatform]]:
    if not raw:
        return None
    out = []
    for chunk in raw.split(","):
        chunk = chunk.strip()
        if not chunk:
            continue
        try:
            out.append(VerPlatform(chunk))
        except ValueError:
            continue
    return out or None


def parse_tags(raw: Optional[str]) -> Optional[list[str]]:
    if not raw:
        return None
    return [t.strip().lower() for t in raw.split(",") if t.strip()]


def parse_compare(raw: Optional[str]) -> Optional[tuple[str, str]]:
    if not raw:
        return None
    match = _COMPARE_RE.match(raw.strip())
    if not match:
        return None
    op, ver = match.groups()
    return (op or "=", ver)


def version_matches_gd(v: dict, gd: Optional[str]) -> bool:
    if not gd or gd == "*":
        return True
    return gd in [val for val in v["gd"].values() if val]


def version_matches_platforms(v: dict, platforms: Optional[list[VerPlatform]]) -> bool:
    if not platforms:
        return True
    for platform in platforms:
        if platform in _ANDROID_PLATFORMS:
            return True
        gd_field = _GD_FIELD_BY_PLATFORM.get(platform)
        if gd_field and v["gd"].get(gd_field):
            return True
    return False


def version_matches_geode(v: dict, geode: Optional[str]) -> bool:
    if not geode:
        return True
    return compare_versions(v["geode"], geode) <= 0


def versions_for_mod(mod_id: str, statuses: Optional[list[str]] = None) -> list[dict]:
    rows = storage.find_all("mod_versions", lambda v: v["mod_id"] == mod_id)
    if statuses is not None:
        rows = [r for r in rows if r["status"] in statuses]
    return rows


def select_best_version(
    mod_id: str,
    statuses: Optional[list[str]],
    gd: Optional[str] = None,
    platforms: Optional[list[VerPlatform]] = None,
    geode: Optional[str] = None,
) -> Optional[dict]:
    candidates = [
        v
        for v in versions_for_mod(mod_id, statuses)
        if version_matches_gd(v, gd)
        and version_matches_platforms(v, platforms)
        and version_matches_geode(v, geode)
    ]
    if not candidates:
        return None
    candidates.sort(key=lambda v: compare_versions_key(v["version"]), reverse=True)
    return candidates[0]


def compare_versions_key(version: str):
    from utils import _version_key

    return _version_key(version)

# TODO: Allow changing if valid tags are required in opengeode.json
def filter_submittable_tags(tag_names) -> list[str]:
    if not tag_names:
        return []
    valid_by_lower = {t["name"].lower(): t["name"] for t in storage.all_rows("tags") if not t["is_readonly"]}

    result = []
    for name in tag_names:
        if not isinstance(name, str):
            continue
        canonical = valid_by_lower.get(name.strip().lower())
        if canonical and canonical not in result:
            result.append(canonical)
    return result


def filter_versions(
    mod_id: str,
    statuses: Optional[list[str]],
    gd: Optional[str],
    platforms: Optional[list[VerPlatform]],
    compare: Optional[tuple[str, str]],
) -> list[dict]:
    rows = [
        v
        for v in versions_for_mod(mod_id, statuses)
        if version_matches_gd(v, gd) and version_matches_platforms(v, platforms)
    ]
    if compare:
        op, target = compare
        rows = [v for v in rows if version_matches(v["version"], target, op)]
    rows.sort(key=lambda v: compare_versions_key(v["version"]), reverse=True)
    return rows


def highest_existing_version(mod_id: str) -> Optional[str]:
    versions = versions_for_mod(mod_id)
    if not versions:
        return None
    versions.sort(key=lambda v: compare_versions_key(v["version"]), reverse=True)
    return versions[0]["version"]
