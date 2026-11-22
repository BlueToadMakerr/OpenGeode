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
from PIL import Image, UnidentifiedImageError
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
    denied_networks = (
        ipaddress.ip_network("0.0.0.0/8"),
        ipaddress.ip_network("192.0.0.0/24"),
        ipaddress.ip_network("198.18.0.0/15"),
        ipaddress.ip_network("240.0.0.0/4"),
    )
    if isinstance(address, ipaddress.IPv4Address) and any(address in network for network in denied_networks):
        return False
    return address.is_global and not address.is_multicast


def _resolve_public_addresses(host: str, port: int) -> list[str]:
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
    return addresses


def _download_http_hop(url: str, max_size: int):
    try:
        parsed = urlsplit(url)
        scheme = parsed.scheme.lower()
        host = parsed.hostname
        port = parsed.port if parsed.port is not None else (443 if scheme == "https" else 80)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail="Invalid download URL") from exc

    if (
        scheme not in {"http", "https"}
        or not host
        or parsed.username is not None
        or parsed.password is not None
        or "%" in host
        or port < 1
    ):
        raise HTTPException(status_code=400, detail="Invalid download URL")

    pinned_ips = _resolve_public_addresses(host, port)
    connection_type = _PinnedHTTPSConnection if scheme == "https" else _PinnedHTTPConnection
    host_header = f"[{host}]" if ":" in host else host
    if port != (443 if scheme == "https" else 80):
        host_header = f"{host_header}:{port}"
    target = parsed.path or "/"
    if parsed.query:
        target += f"?{parsed.query}"

    last_error = None
    for pinned_ip in pinned_ips:
        connection = connection_type(host, port, pinned_ip)
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

            if 300 <= response.status < 400 or response.status != 200:
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
        except (OSError, http.client.HTTPException, ssl.SSLError) as exc:
            last_error = exc
        finally:
            connection.close()

    if last_error is not None:
        raise last_error
    raise HTTPException(status_code=400, detail="Could not connect to download URL")


async def download_bytes(url: str, max_size: int) -> bytes:
    current_url = url
    for _ in range(10):
        try:
            status, headers, data = await asyncio.wait_for(
                asyncio.to_thread(_download_http_hop, current_url, max_size),
                timeout=60,
            )
        except HTTPException:
            raise
        except (OSError, http.client.HTTPException, ssl.SSLError, TimeoutError) as exc:
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
    max_mod_json_size = 128 * 1024
    max_markdown_size = 1024 * 1024
    max_logo_size = 5 * 1024 * 1024
    max_uncompressed_size = 512 * 1024 * 1024

    try:
        with zipfile.ZipFile(io.BytesIO(file_bytes)) as zf:
            infos = zf.infolist()
            if sum(info.file_size for info in infos) > max_uncompressed_size:
                raise HTTPException(status_code=400, detail="Package exceeds the uncompressed size limit")

            manifest_info = next((info for info in infos if info.filename == "mod.json"), None)
            if manifest_info is None:
                raise HTTPException(status_code=400, detail="Package does not contain a root mod.json")
            if manifest_info.file_size > max_mod_json_size:
                raise HTTPException(status_code=400, detail="mod.json exceeds the 128 KB size limit")
            with zf.open(manifest_info) as f:
                manifest = json.load(f)

            root_files = {
                info.filename.lower(): info
                for info in infos
                if "/" not in info.filename and not info.filename.endswith("/")
            }

            def read_root_file(*candidate_names: str) -> Optional[str]:
                for candidate in candidate_names:
                    info = root_files.get(candidate.lower())
                    if info:
                        if info.file_size > max_markdown_size:
                            raise HTTPException(status_code=400, detail=f"{info.filename} exceeds the 1 MB size limit")
                        with zf.open(info) as rf:
                            return rf.read().decode("utf-8", errors="replace")
                return None

            def read_root_file_bytes(*candidate_names: str) -> Optional[bytes]:
                for candidate in candidate_names:
                    info = root_files.get(candidate.lower())
                    if info:
                        if info.file_size > max_logo_size:
                            raise HTTPException(status_code=400, detail="logo.png exceeds the 5 MB size limit")
                        with zf.open(info) as rf:
                            return rf.read()
                return None

            manifest["_about"] = read_root_file("about.md", "README.md")
            manifest["_changelog"] = read_root_file("changelog.md")
            logo_bytes = read_root_file_bytes("logo.png")
            if logo_bytes:
                try:
                    with Image.open(io.BytesIO(logo_bytes)) as logo:
                        if logo.format != "PNG":
                            raise HTTPException(status_code=400, detail="logo.png must be a PNG image")
                        width, height = logo.size
                        if width != height:
                            raise HTTPException(status_code=400, detail="Mod logo must have a 1:1 aspect ratio")
                        if width > 1024 or height > 1024:
                            raise HTTPException(status_code=400, detail="Mod logo dimensions may not exceed 1024x1024")
                        logo.verify()
                except (UnidentifiedImageError, OSError, Image.DecompressionBombError) as exc:
                    raise HTTPException(status_code=400, detail="logo.png is not a valid PNG image") from exc
            manifest["_logo_bytes"] = logo_bytes
    except zipfile.BadZipFile:
        raise HTTPException(status_code=400, detail="download_link did not point to a valid .geode package")
    except json.JSONDecodeError:
        raise HTTPException(status_code=400, detail="mod.json in the package is not valid JSON")
    except UnicodeDecodeError:
        raise HTTPException(status_code=400, detail="mod.json in the package is not valid UTF-8")

    missing = [field for field in REQUIRED_MOD_JSON_FIELDS if field not in manifest]
    if missing:
        raise HTTPException(status_code=400, detail=f"mod.json missing required field(s): {', '.join(missing)}")
    if not MOD_ID_RE.match(manifest["id"]):
        raise HTTPException(status_code=400, detail="mod.json 'id' must look like '<developer>.<mod-name>', e.g. 'geode.node-ids'")
    if manifest["id"] == "geode.loader":
        raise HTTPException(status_code=400, detail="The geode.loader mod ID is reserved")
    if not manifest.get("developer") and not manifest.get("developers"):
        raise HTTPException(status_code=400, detail="No developer specified on mod.json")
    if not all(ord(char) < 128 and (char.isprintable() or char == " ") for char in str(manifest["name"])):
        raise HTTPException(status_code=400, detail="Mod name contains non-printable characters")
    try:
        _parse_version(str(manifest["version"]))
        _parse_version(str(manifest["geode"]))
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
