"""
Pydantic models for the Geode Index API.

This file mirrors the public API schemas used by the upstream Geode Index.
"""
from __future__ import annotations

from enum import Enum
from typing import List, Optional

from pydantic import BaseModel, Field


class ModVersionStatusEnum(str, Enum):
    pending = "pending"
    accepted = "accepted"
    rejected = "rejected"
    unlisted = "unlisted"


class ModVersionCompare(str, Enum):
    eq = "="
    gt = ">"
    gte = ">="
    lt = "<"
    lte = "<="


class IndexSortType(str, Enum):
    downloads = "downloads"
    recently_updated = "recently_updated"
    recently_published = "recently_published"
    oldest = "oldest"
    name = "name"
    name_reverse = "name_reverse"
    random = "random"


class DependencyImportance(str, Enum):
    suggested = "suggested"
    recommended = "recommended"
    required = "required"


class IncompatibilityImportance(str, Enum):
    breaking = "breaking"
    conflicting = "conflicting"
    superseded = "superseded"


class ModVersionSubmissionLock(str, Enum):
    none = "none"
    internal = "internal"
    locked = "locked"


class VerPlatform(str, Enum):
    android = "android"
    android32 = "android32"
    android64 = "android64"
    ios = "ios"
    mac = "mac"
    mac_arm = "mac-arm"
    mac_intel = "mac-intel"
    win = "win"


KNOWN_GD_VERSIONS = [
    "*", "2.113", "2.200", "2.204", "2.205", "2.206", "2.207", "2.2071",
    "2.2072", "2.2073", "2.2074", "2.208", "2.2081", "2.2082",
]


class StandardResponse(BaseModel):
    error: Optional[str] = None
    payload: Optional[dict] = None


class ApiResponse_String(BaseModel):
    error: Optional[str] = None
    payload: Optional[str] = None


class PaginatedData(BaseModel):
    data: List[dict]
    count: int


# Auth
class CallbackParams(BaseModel):
    code: str
    state: str


class PollParams(BaseModel):
    uuid: str
    expiry: Optional[bool] = None


class TokenLoginParams(BaseModel):
    token: str


class RefreshBody(BaseModel):
    refresh_token: str


class AuthTokens(BaseModel):
    access_token: str
    refresh_token: str


# Developers
class Developer(BaseModel):
    id: int
    username: str
    display_name: str
    verified: bool
    admin: bool
    github_id: int
    has_accepted_mod: bool


class ModDeveloper(BaseModel):
    id: int
    username: str
    display_name: str
    is_owner: bool


class DeveloperBan(BaseModel):
    id: int
    developer_id: int
    reason: Optional[str] = None
    admin_id: Optional[int] = None
    created_at: str
    revoked_at: Optional[str] = None


class AuditAction(str, Enum):
    created = "created"
    updated = "updated"
    deleted = "deleted"
    restored = "restored"


class AuditActionRow(BaseModel):
    action: AuditAction
    details: Optional[str] = None
    performed_by: Optional[int] = None
    performed_at: str


class DeveloperUpdatePayload(BaseModel):
    admin: Optional[bool] = None
    verified: Optional[bool] = None


class UploadProfilePayload(BaseModel):
    display_name: str


class PaginatedData_Developer(BaseModel):
    data: List[Developer]
    count: int


# GD versions / platforms
class DetailedGDVersion(BaseModel):
    win: Optional[str] = None
    ios: Optional[str] = None
    mac_intel: Optional[str] = Field(default=None, alias="mac-intel")
    mac_arm: Optional[str] = Field(default=None, alias="mac-arm")
    android32: Optional[str] = None
    android64: Optional[str] = None

    class Config:
        populate_by_name = True


class GDVersionAlias(BaseModel):
    version_name: str
    added_at: str
    android_manifest_id: Optional[int] = None
    ios_bundle_version: Optional[str] = None
    mac_arm_uuid: Optional[str] = None
    mac_intel_uuid: Optional[str] = None
    windows_timestamp: Optional[int] = None


# Tags
class Tag(BaseModel):
    id: int
    name: str
    display_name: str
    is_readonly: bool


# Loader
class LoaderDownload(BaseModel):
    url: str
    hash: str


class LoaderDownloads(BaseModel):
    win: LoaderDownload
    mac: LoaderDownload
    android32: LoaderDownload
    android64: LoaderDownload
    ios: LoaderDownload
    resources: LoaderDownload


class LoaderVersion(BaseModel):
    tag: str
    version: str
    commit_hash: str
    prerelease: bool
    created_at: str
    gd: DetailedGDVersion
    downloads: LoaderDownloads


class CreateVersionBody(BaseModel):
    tag: str
    commit_hash: str
    gd: DetailedGDVersion
    prerelease: Optional[bool] = False


class PaginatedData_LoaderVersion(BaseModel):
    data: List[LoaderVersion]
    count: int


# Dependencies / incompatibilities / replacement
class ResponseDependency(BaseModel):
    mod_id: str
    version: str
    importance: DependencyImportance
    required: bool


class ResponseIncompatibility(BaseModel):
    mod_id: str
    version: str
    importance: IncompatibilityImportance
    breaking: bool


class Replacement(BaseModel):
    id: str
    version: str
    download_link: str
    dependencies: List[ResponseDependency]
    incompatibilities: List[ResponseIncompatibility]


class ModUpdate(BaseModel):
    id: str
    version: str
    download_link: str
    dependencies: List[ResponseDependency]
    incompatibilities: List[ResponseIncompatibility]
    replacement: Optional[Replacement] = None


# Mods / mod versions
class ModLinks(BaseModel):
    homepage: Optional[str] = None
    community: Optional[str] = None
    source: Optional[str] = None


class ModVersion(BaseModel):
    name: str
    version: str
    download_link: str
    direct_download_link: Optional[str] = None
    hash: str
    geode: str
    early_load: bool
    api: bool
    mod_id: str
    status: ModVersionStatusEnum
    download_count: int
    requires_patching: bool
    info: Optional[str] = None
    gd: DetailedGDVersion
    description: Optional[str] = None
    dependencies: Optional[List[ResponseDependency]] = None
    incompatibilities: Optional[List[ResponseIncompatibility]] = None
    developers: Optional[List[ModDeveloper]] = None
    tags: Optional[List[str]] = None
    created_at: Optional[str] = None
    updated_at: Optional[str] = None


class Mod(BaseModel):
    id: str
    repository: Optional[str] = None
    links: Optional[ModLinks] = None
    tags: List[str]
    featured: bool
    download_count: int
    developers: List[ModDeveloper]
    versions: List[ModVersion]
    about: Optional[str] = None
    changelog: Optional[str] = None
    created_at: str
    updated_at: str


class CreateQueryParams(BaseModel):
    download_link: str = Field(max_length=1024)


class UpdateModPayload(BaseModel):
    featured: bool


class UpdatePayload(BaseModel):
    status: ModVersionStatusEnum
    info: Optional[str] = None


class AddDevPayload(BaseModel):
    username: str


class SimpleDevModVersion(BaseModel):
    name: str
    version: str
    download_count: int
    validated: bool
    info: Optional[str] = None
    status: ModVersionStatusEnum


class SimpleDevMod(BaseModel):
    id: str
    featured: bool
    download_count: int
    versions: List[SimpleDevModVersion]
    developers: List[ModDeveloper]


class PaginatedData_Mod(BaseModel):
    data: List[Mod]
    count: int


# Deprecations
class Deprecation(BaseModel):
    id: int
    mod_id: str
    reason: str
    by: List[str]


class CreateDeprecationData(BaseModel):
    reason: str
    by: List[str]


class UpdateDeprecationData(BaseModel):
    reason: Optional[str] = None
    by: Optional[List[str]] = None


# Submissions / comments / attachments
class ModVersionSubmission(BaseModel):
    mod_version_id: int
    lock: ModVersionSubmissionLock
    locked_by: Optional[Developer] = None
    created_at: str
    updated_at: str


class UpdateSubmissionPayload(BaseModel):
    lock: ModVersionSubmissionLock


class ModVersionSubmissionCommentAttachment(BaseModel):
    id: int
    url: str


class ModVersionSubmissionComment(BaseModel):
    id: int
    submission_id: int
    author: Developer
    comment: str
    attachments: List[ModVersionSubmissionCommentAttachment]
    created_at: str
    updated_at: Optional[str] = None


class CreateCommentPayload(BaseModel):
    comment: str


class UpdateCommentPayload(BaseModel):
    comment: str


class ModVersionSubmissionAttachment(BaseModel):
    id: int
    comment_id: int
    url: str
    created_at: str


class UploadAttachmentsForm(BaseModel):
    image: List[str]


# Stats
class Stats(BaseModel):
    total_mod_count: int
    total_mod_downloads: int
    total_geode_downloads: int
    total_registered_developers: int
