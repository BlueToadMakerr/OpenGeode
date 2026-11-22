from __future__ import annotations

import re
from typing import Optional

from fastapi import HTTPException

from database import storage
from models import KNOWN_GD_VERSIONS, ModVersionStatusEnum, VerPlatform
from utils import _parse_version, compare_versions, version_matches

_PLATFORM_FIELDS = {
    VerPlatform.win: ("win",),
    VerPlatform.ios: ("ios",),
    VerPlatform.mac: ("mac-intel", "mac-arm"),
    VerPlatform.mac_intel: ("mac-intel",),
    VerPlatform.mac_arm: ("mac-arm",),
    VerPlatform.android: ("android32", "android64"),
    VerPlatform.android32: ("android32",),
    VerPlatform.android64: ("android64",),
}

_COMPARE_RE = re.compile(r"^(>=|<=|>|<|=)?\s*(.+)$")

PUBLICLY_VISIBLE_STATUSES = [
    ModVersionStatusEnum.accepted.value,
    ModVersionStatusEnum.pending.value,
    ModVersionStatusEnum.unlisted.value,
]


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
    out: list[VerPlatform] = []
    for chunk in raw.split(","):
        chunk = chunk.strip().lower()
        if not chunk:
            continue
        # Match the upstream aliases exactly, including windows/macos.
        if chunk in ("windows",):
            chunk = "win"
        elif chunk in ("macos",):
            chunk = "mac"
        try:
            platform = VerPlatform(chunk)
        except ValueError:
            raise HTTPException(status_code=400, detail=f"Invalid platform {chunk}")

        # The upstream server expands aggregate platform names before
        # querying the version rows.
        if platform == VerPlatform.android:
            out.extend((VerPlatform.android32, VerPlatform.android64))
        elif platform == VerPlatform.mac:
            out.extend((VerPlatform.mac_arm, VerPlatform.mac_intel))
        else:
            out.append(platform)

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
        raise HTTPException(status_code=400, detail=f"Bad compare string {raw}")
    op, ver = match.groups()
    if not ver.strip():
        raise HTTPException(status_code=400, detail=f"Bad compare string {raw}")
    try:
        compare_versions("0.0.0", ver.strip())
    except ValueError:
        raise HTTPException(status_code=400, detail=f"Bad compare string {raw}")
    return (op or "=", ver.strip())


def validate_gd(gd: Optional[str]) -> None:
    if gd and gd not in KNOWN_GD_VERSIONS:
        raise HTTPException(status_code=400, detail="Invalid gd")


def version_matches_gd(v: dict, gd: Optional[str]) -> bool:
    validate_gd(gd)
    if not gd or gd == "*":
        return True
    return gd in [val for val in v.get("gd", {}).values() if val]


def version_matches_platforms(v: dict, platforms: Optional[list[VerPlatform]]) -> bool:
    if not platforms:
        return True
    gd = v.get("gd", {})
    return any(gd.get(field) for platform in platforms for field in _PLATFORM_FIELDS.get(platform, ()))


def validate_geode(geode: Optional[str]) -> None:
    if geode:
        try:
            _parse_version(geode)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail="Invalid geode version") from exc


def version_matches_geode(
    v: dict,
    geode: Optional[str],
    for_update: bool = False,
) -> bool:
    if not geode:
        return True
    validate_geode(geode)
    installed = _parse_version(geode)
    try:
        required = _parse_version(v["geode"])
    except (KeyError, ValueError):
        return False

    if required.major != installed.major or required.minor > installed.minor:
        return False

    installed_pre = ".".join(str(part) for part in installed.prerelease) if installed.prerelease else None
    required_pre = ".".join(str(part) for part in required.prerelease) if required.prerelease else None
    required_is_alpha = bool(required_pre and required_pre.lower().startswith("alpha"))

    if for_update:
        if installed_pre is None:
            return required_pre is None or not required_is_alpha
        if installed_pre.lower().startswith("alpha"):
            return (
                (required.minor == installed.minor and required.patch == installed.patch and required_pre == installed_pre)
                or required_pre is None
                or required.minor < installed.minor
                or required.patch < installed.patch
                or (
                    not required_is_alpha
                    and installed_pre.lower() >= (required_pre or "").lower()
                )
            )
        return (
            required_pre is None
            or required.minor < installed.minor
            or required.patch < installed.patch
            or (
                not required_is_alpha
                and installed_pre.lower() >= (required_pre or "").lower()
            )
        )

    if installed_pre and installed_pre.lower().startswith("alpha"):
        return (
            (required.minor == installed.minor and required.patch == installed.patch and required_pre == installed_pre)
            or required_pre is None
            or required.minor < installed.minor
        )
    if installed_pre is not None:
        return required_pre is None or (
            not required_is_alpha
            and installed_pre.lower() >= (required_pre or "").lower()
        )
    return required_pre is None or not required_is_alpha


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
    major: Optional[int] = None,
    update_mode: bool = False,
) -> Optional[dict]:
    validate_geode(geode)
    candidates = [
        v
        for v in versions_for_mod(mod_id, statuses)
        if version_matches_gd(v, gd)
        and version_matches_platforms(v, platforms)
        and version_matches_geode(v, geode, for_update=update_mode)
        and (
            major is None
            or (v.get("version", "").split(".", 1)[0].isdigit()
                and int(v["version"].split(".", 1)[0]) == major)
        )
    ]
    if not candidates:
        return None
    candidates.sort(key=lambda v: compare_versions_key(v["version"]), reverse=True)
    return candidates[0]


def compare_versions_key(version: str):
    from utils import _version_key
    return _version_key(version)


def filter_submittable_tags(tag_names) -> list[str]:
    if not tag_names:
        return []
    valid_by_lower = {
        t["name"].lower(): t["name"]
        for t in storage.all_rows("tags")
        if not t["is_readonly"]
    }
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
