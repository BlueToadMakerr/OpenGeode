"""
Misc helpers: simple semver-ish comparison, mod-id / version validation,
and downloading + parsing .geode files
"""
from __future__ import annotations

import hashlib
import io
import json
import re
import zipfile
from typing import Optional

import httpx
from fastapi import HTTPException

from config import settings

MOD_ID_RE = re.compile(r"^[a-z0-9_\-]+\.[a-z0-9_\-]+$")


# Version comparison

def _version_key(version: str) -> tuple:
    """Turn 'v1.2.3-beta.1' into a comparable tuple.

    This is a pragmatic subset of semver: numeric release components compare
    numerically, and any prerelease suffix (after '-') sorts before the
    equivalent release version.
    """
    v = version.strip()
    if v.startswith("v"):
        v = v[1:]
    release, _, prerelease = v.partition("-")
    parts = []
    for chunk in release.split("."):
        parts.append(int(chunk) if chunk.isdigit() else 0)
    has_prerelease = 1 if prerelease else 0
    prerelease_parts = tuple(
        int(p) if p.isdigit() else p for p in prerelease.split(".")
    ) if prerelease else ()
    return (tuple(parts), 0 if has_prerelease else 1, prerelease_parts)


def compare_versions(a: str, b: str) -> int:
    """Return -1, 0, or 1 comparing version strings a and b."""
    ka, kb = _version_key(a), _version_key(b)
    if ka < kb:
        return -1
    if ka > kb:
        return 1
    return 0


def version_matches(version: str, target: str, op: str) -> bool:
    cmp = compare_versions(version, target)
    return {
        "=": cmp == 0,
        ">": cmp > 0,
        ">=": cmp >= 0,
        "<": cmp < 0,
        "<=": cmp <= 0,
    }.get(op, False)


# .geode file download + parse

REQUIRED_MOD_JSON_FIELDS = ["id", "name", "version", "geode"]


async def download_bytes(url: str, max_size: int) -> bytes:
    try:
        async with httpx.AsyncClient(timeout=30, follow_redirects=True) as client:
            async with client.stream("GET", url) as resp:
                if resp.status_code != 200:
                    raise HTTPException(
                        status_code=400,
                        detail=f"Failed to download file from download_link (status {resp.status_code})",
                    )
                chunks = bytearray()
                async for chunk in resp.aiter_bytes():
                    chunks.extend(chunk)
                    if len(chunks) > max_size:
                        raise HTTPException(status_code=400, detail="Downloaded file exceeds size limit")
                return bytes(chunks)
    except httpx.RequestError as exc:
        raise HTTPException(status_code=400, detail=f"Could not reach download_link: {exc}") from exc


def parse_geode_package(file_bytes: bytes) -> dict:
    """Parse a .geode package.

    Also pulls out, all from the package root (case-insensitive, not
    searched in subdirectories):
      - `about.md`, the mod's long-form Markdown description. Falls back to
        `README.md` if `about.md` is missing (matching Geode CLI >=3.5.0).
      - `changelog.md`, the mod's changelog.
      - `logo.png`, the mod's icon.

    Returns the mod.json contents plus `_hash` (sha256 of the whole
    package), `_size`, `_about`, `_changelog`, and `_logo_bytes`.
    """
    try:
        with zipfile.ZipFile(io.BytesIO(file_bytes)) as zf:
            names = zf.namelist()
            manifest_name = next((n for n in names if n == "mod.json" or n.endswith("/mod.json")), None)
            if manifest_name is None:
                raise HTTPException(status_code=400, detail="Package does not contain a mod.json")
            with zf.open(manifest_name) as f:
                manifest = json.load(f)

            root_files = {n.lower(): n for n in names if "/" not in n and not n.endswith("/")}

            def read_root_file(*candidate_names: str) -> Optional[str]:
                for candidate in candidate_names:
                    real_name = root_files.get(candidate.lower())
                    if real_name:
                        with zf.open(real_name) as rf:
                            return rf.read().decode("utf-8", errors="replace")
                return None

            def read_root_file_bytes(*candidate_names: str) -> Optional[bytes]:
                for candidate in candidate_names:
                    real_name = root_files.get(candidate.lower())
                    if real_name:
                        with zf.open(real_name) as rf:
                            return rf.read()
                return None

            about_text = read_root_file("about.md", "README.md")
            changelog_text = read_root_file("changelog.md")
            logo_bytes = read_root_file_bytes("logo.png")
    except zipfile.BadZipFile:
        raise HTTPException(status_code=400, detail="download_link did not point to a valid .geode package")
    except json.JSONDecodeError:
        raise HTTPException(status_code=400, detail="mod.json in the package is not valid JSON")

    missing = [field for field in REQUIRED_MOD_JSON_FIELDS if field not in manifest]
    if missing:
        raise HTTPException(status_code=400, detail=f"mod.json missing required field(s): {', '.join(missing)}")

    if not MOD_ID_RE.match(manifest["id"]):
        raise HTTPException(
            status_code=400,
            detail="mod.json 'id' must look like '<developer>.<mod-name>', e.g. 'geode.node-ids'",
        )

    manifest["_hash"] = hashlib.sha256(file_bytes).hexdigest()
    manifest["_size"] = len(file_bytes)
    manifest["_about"] = about_text
    manifest["_changelog"] = changelog_text
    manifest["_logo_bytes"] = logo_bytes
    return manifest


async def fetch_and_parse_geode_file(download_link: str) -> dict:
    file_bytes = await download_bytes(download_link, settings.MAX_GEODE_FILE_SIZE_BYTES)
    return parse_geode_package(file_bytes)

_DEPENDENCY_IMPORTANCE_VALUES = {"suggested", "recommended", "required"}
_INCOMPATIBILITY_IMPORTANCE_VALUES = {"breaking", "conflicting", "superseded"}


def _parse_dep_like(
    raw,
    *,
    flag_key: str,
    importance_values: set,
    default_importance_true: str,
    default_importance_false: str,
) -> list[dict]:
    if not raw:
        return []
    if isinstance(raw, list):
        return raw
    if not isinstance(raw, dict):
        return []

    out = []
    for mod_id, spec in raw.items():
        if isinstance(spec, str):
            version, flag, importance = spec, True, None
        elif isinstance(spec, dict):
            version = spec.get("version", "*")
            flag = spec.get(flag_key, True)
            importance = spec.get("importance")
        else:
            continue

        if importance not in importance_values:
            importance = default_importance_true if flag else default_importance_false

        out.append({"mod_id": mod_id, "version": version, "importance": importance, flag_key: bool(flag)})
    return out


def parse_dependencies(raw) -> list[dict]:
    """mod.json dependencies object -> array of ResponseDependency dicts.
    Required by default per the docs ("If this is not specified, the
    dependency is marked as required")."""
    return _parse_dep_like(
        raw,
        flag_key="required",
        importance_values=_DEPENDENCY_IMPORTANCE_VALUES,
        default_importance_true="required",
        default_importance_false="recommended",
    )


def parse_incompatibilities(raw) -> list[dict]:
    """mod.json incompatibilities object -> array of ResponseIncompatibility
    dicts. Defaults to "breaking" when unspecified, mirroring dependencies
    defaulting to "required" when unspecified."""
    return _parse_dep_like(
        raw,
        flag_key="breaking",
        importance_values=_INCOMPATIBILITY_IMPORTANCE_VALUES,
        default_importance_true="breaking",
        default_importance_false="conflicting",
    )


# Misc

def parse_gd_query(value: Optional[str]) -> Optional[str]:
    if value is None:
        return None
    return value.strip()
