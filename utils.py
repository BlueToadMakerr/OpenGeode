"""
Misc helpers: semver-compatible comparison, mod-id / version validation,
and downloading + parsing .geode files
"""
from __future__ import annotations

import asyncio
import hashlib
import http.client
import io
import ipaddress
import json
import re
import socket
import ssl
import zipfile
from typing import Optional
from urllib.parse import urljoin, urlsplit

from fastapi import HTTPException
from semantic_version import Version

from config import settings

MOD_ID_RE = re.compile(r"^[a-z0-9_\-]+\.[a-z0-9_\-]+$")


def _parse_version(version: str) -> Version:
    """Parse a strict SemVer version, matching the Rust server's semver crate."""
    value = version.strip()
    if value.startswith(("v", "V")):
        value = value[1:]
    try:
        return Version(value)
    except ValueError as exc:
        raise ValueError(f"Invalid semantic version: {version}") from exc


def _version_key(version: str):
    """Return the SemVer precedence key used by semver precedence rules.

    Build metadata must not affect precedence, so use semantic_version's
    precedence key rather than its full comparison key.
    """
    return _parse_version(version)._cmp_precedence_key


def compare_versions(a: str, b: str) -> int:
    va, vb = _parse_version(a), _parse_version(b)
    ka, kb = va._cmp_precedence_key, vb._cmp_precedence_key
    if ka < kb:
        return -1
    if ka > kb:
        return 1
    return 0


def version_matches(version: str, target: str, op: str) -> bool:
    try:
        cmp = compare_versions(version, target)
    except ValueError:
        return False
    return {"=": cmp == 0, ">": cmp > 0, ">=": cmp >= 0, "<": cmp < 0, "<=": cmp <= 0}.get(op, False)


REQUIRED_MOD_JSON_FIELDS = ["id", "name", "version", "geode"]


class _PinnedHTTPConnection(http.client.HTTPConnection):
    def __init__(self, host: str, port: int, pinned_ip: str):
        super().__init__(host, port, timeout=30)
        self.pinned_ip = pinned_ip

    def connect(self):
        self.sock = socket.create_connection((self.pinned_ip, self.port), self.timeout)


class _PinnedHTTPSConnection(http.client.HTTPSConnection):
    def __init__(self, host: str, port: int, pinned_ip: str):
        super().__init__(host, port, timeout=30, context=ssl.create_default_context())
        self.pinned_ip = pinned_ip

    def connect(self):
        self.sock = socket.create_connection((self.pinned_ip, self.port), self.timeout)
        self.sock = self._context.wrap_socket(self.sock, server_hostname=self.host)


def _is_public_ip(value: str) -> bool:
    try:
        address = ipaddress.ip_address(value)
    except ValueError:
        return False
    if isinstance(address, ipaddress.IPv6Address) and address.ipv4_mapped:
        address = address.ipv4_mapped
    return address.is_global


def _resolve_public_address(host: str, port: int) -> str:
    normalized_host = host.rstrip(".").lower()
    denied_suffixes = (".host", ".lan", ".local", ".internal")
    if (
        normalized_host == "localhost"
        or normalized_host in {"host", "lan", "local", "internal"}
        or normalized_host.endswith(denied_suffixes)
    ):
        raise HTTPException(status_code=400, detail="Invalid download URL")

    try:
        literal = ipaddress.ip_address(normalized_host)
    except ValueError:
        try:
            records = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
        except OSError as exc:
            raise HTTPException(status_code=400, detail="Could not resolve download URL host") from exc
        addresses = list(dict.fromkeys(record[4][0] for record in records))
    else:
        addresses = [str(literal)]

    if not addresses or any(not _is_public_ip(address) for address in addresses):
        raise HTTPException(status_code=400, detail="Download URL resolves to a disallowed IP address")
    return addresses[0]


def _download_http_hop(url: str, max_size: int):
    try:
        parsed = urlsplit(url)
        scheme = parsed.scheme.lower()
        host = parsed.hostname
        port = parsed.port or (443 if scheme == "https" else 80)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail="Invalid download URL") from exc

    if (
        scheme not in {"http", "https"}
        or not host
        or parsed.username is not None
        or parsed.password is not None
        or "%" in host
    ):
        raise HTTPException(status_code=400, detail="Invalid download URL")

    pinned_ip = _resolve_public_address(host, port)
    connection_type = _PinnedHTTPSConnection if scheme == "https" else _PinnedHTTPConnection
    connection = connection_type(host, port, pinned_ip)
    host_header = f"[{host}]" if ":" in host else host
    if port != (443 if scheme == "https" else 80):
        host_header = f"{host_header}:{port}"
    target = parsed.path or "/"
    if parsed.query:
        target += f"?{parsed.query}"

    try:
        connection.request(
            "GET",
            target,
            headers={
                "Host": host_header,
                "User-Agent": "OpenGeode",
                "Accept-Encoding": "identity",
                "Connection": "close",
            },
        )
        response = connection.getresponse()
        headers = {key.lower(): value for key, value in response.getheaders()}

        if 300 <= response.status < 400:
            return response.status, headers, b""
        if response.status != 200:
            return response.status, headers, b""

        content_length = headers.get("content-length")
        if content_length:
            try:
                if int(content_length) > max_size:
                    raise HTTPException(status_code=400, detail="Downloaded file exceeds size limit")
            except ValueError:
                pass

        chunks = bytearray()
        while True:
            chunk = response.read(min(64 * 1024, max_size + 1 - len(chunks)))
            if not chunk:
                break
            chunks.extend(chunk)
            if len(chunks) > max_size:
                raise HTTPException(status_code=400, detail="Downloaded file exceeds size limit")
        return response.status, headers, bytes(chunks)
    finally:
        connection.close()


async def download_bytes(url: str, max_size: int) -> bytes:
    current_url = url
    for _ in range(10):
        try:
            status, headers, data = await asyncio.to_thread(
                _download_http_hop, current_url, max_size
            )
        except HTTPException:
            raise
        except (OSError, http.client.HTTPException, ssl.SSLError) as exc:
            raise HTTPException(status_code=400, detail=f"Could not reach download_link: {exc}") from exc

        if 300 <= status < 400:
            location = headers.get("location")
            if not location:
                raise HTTPException(status_code=400, detail="Invalid redirect from download_link")
            current_url = urljoin(current_url, location)
            continue
        if status != 200:
            raise HTTPException(
                status_code=400,
                detail=f"Failed to download file from download_link (status {status})",
            )
        return data

    raise HTTPException(status_code=400, detail="Too many redirects when downloading .geode file")


def parse_geode_package(file_bytes: bytes) -> dict:
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

            manifest["_about"] = read_root_file("about.md", "README.md")
            manifest["_changelog"] = read_root_file("changelog.md")
            manifest["_logo_bytes"] = read_root_file_bytes("logo.png")
    except zipfile.BadZipFile:
        raise HTTPException(status_code=400, detail="download_link did not point to a valid .geode package")
    except json.JSONDecodeError:
        raise HTTPException(status_code=400, detail="mod.json in the package is not valid JSON")

    missing = [field for field in REQUIRED_MOD_JSON_FIELDS if field not in manifest]
    if missing:
        raise HTTPException(status_code=400, detail=f"mod.json missing required field(s): {', '.join(missing)}")
    if not MOD_ID_RE.match(manifest["id"]):
        raise HTTPException(status_code=400, detail="mod.json 'id' must look like '<developer>.<mod-name>', e.g. 'geode.node-ids'")
    try:
        _parse_version(str(manifest["version"]))
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    manifest["_hash"] = hashlib.sha256(file_bytes).hexdigest()
    manifest["_size"] = len(file_bytes)
    return manifest


async def fetch_and_parse_geode_file(download_link: str) -> dict:
    return parse_geode_package(await download_bytes(download_link, settings.MAX_GEODE_FILE_SIZE_BYTES))


_DEPENDENCY_IMPORTANCE_VALUES = {"suggested", "recommended", "required"}
_INCOMPATIBILITY_IMPORTANCE_VALUES = {"breaking", "conflicting", "superseded"}


def _parse_dep_like(raw, *, flag_key: str, importance_values: set, default_importance_true: str, default_importance_false: str) -> list[dict]:
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
    return _parse_dep_like(raw, flag_key="required", importance_values=_DEPENDENCY_IMPORTANCE_VALUES, default_importance_true="required", default_importance_false="recommended")


def parse_incompatibilities(raw) -> list[dict]:
    return _parse_dep_like(raw, flag_key="breaking", importance_values=_INCOMPATIBILITY_IMPORTANCE_VALUES, default_importance_true="breaking", default_importance_false="conflicting")


def parse_gd_query(value: Optional[str]) -> Optional[str]:
    return None if value is None else value.strip()
