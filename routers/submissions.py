from __future__ import annotations

import io
import os
import uuid
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query, UploadFile, File
from PIL import Image, UnidentifiedImageError

import security
from config import settings
from database import storage
from models import (
    CreateCommentPayload,
    Developer,
    ModVersionSubmissionLock,
    UpdateCommentPayload,
    UpdateSubmissionPayload,
    AuditAction,
    AuditActionRow,
)
from pagination import page_params, paginate
from serializers import developer_public, developer_has_accepted_mod, is_mod_developer

router = APIRouter(prefix="/v1/mods/{id}/versions/{version}/submission", tags=["mod_version_submissions"])


# ---------------------------------------------------------------------------
# Shared lookups
# ---------------------------------------------------------------------------

def _get_mod_and_version(id: str, version: str) -> tuple[dict, dict]:
    mod_row = storage.find_one("mods", id=id)
    if mod_row is None:
        raise HTTPException(status_code=404, detail="Mod not found")
    version_row = storage.find_one("mod_versions", mod_id=id, version=version)
    if version_row is None:
        raise HTTPException(status_code=404, detail="Version not found")
    return mod_row, version_row


def _submission_public(row: dict) -> dict:
    locked_by = None
    if row.get("locked_by"):
        dev = storage.find_one("developers", id=row["locked_by"])
        locked_by = developer_public(dev) if dev else None
    return {
        "mod_version_id": row["mod_version_id"],
        "lock": row["lock"],
        "locked_by": locked_by,
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
    }


def _comment_public(row: dict) -> dict:
    author = storage.find_one("developers", id=row["author_id"])
    attachments = storage.find_all("submission_attachments", lambda a: a["comment_id"] == row["id"])
    return {
        "id": row["id"],
        "submission_id": row["submission_id"],
        "author": developer_public(author) if author else None,
        "comment": row["comment"],
        "attachments": [{"id": a["id"], "url": a["url"]} for a in attachments],
        "created_at": row["created_at"],
        "updated_at": row.get("updated_at"),
    }


def _attachment_public(row: dict) -> dict:
    return {"id": row["id"], "comment_id": row["comment_id"], "url": row["url"], "created_at": row["created_at"]}




def _audit_row(action: AuditAction, details: Optional[str], performed_by: Optional[int]) -> dict:
    return {
        "action": action.value,
        "details": details,
        "performed_by": performed_by,
        "performed_at": storage.now_iso(),
    }


def _add_submission_audit(submission_id: int, action: AuditAction, details: Optional[str] = None, performed_by: Optional[int] = None) -> None:
    storage.insert("submission_audit", {
        "id": storage.next_id("submission_audit"),
        "submission_id": submission_id,
        **_audit_row(action, details, performed_by),
    })


def _add_comment_audit(comment_id: int, action: AuditAction, details: Optional[str] = None, performed_by: Optional[int] = None) -> None:
    storage.insert("comment_audit", {
        "id": storage.next_id("comment_audit"),
        "comment_id": comment_id,
        **_audit_row(action, details, performed_by),
    })


# ---------------------------------------------------------------------------
# Submission
# ---------------------------------------------------------------------------

@router.get(
    "",
    summary="Get the submission for a mod version, or create one if the mod version is pending and has no submission",
)
def get_submission(
    id: str,
    version: str,
    _developer: Developer = Depends(security.get_current_developer),
):
    mod_row, version_row = _get_mod_and_version(id, version)
    submission = storage.find_one("submissions", mod_version_id=version_row["id"])

    if submission is None:
        if version_row["status"] != "pending":
            raise HTTPException(status_code=404, detail="This version has no review submission")
        now = storage.now_iso()
        submission = storage.insert(
            "submissions",
            {
                "mod_version_id": version_row["id"],
                "lock": ModVersionSubmissionLock.none.value,
                "locked_by": None,
                "created_at": now,
                "updated_at": now,
            },
        )
        _add_submission_audit(submission["mod_version_id"], AuditAction.created)

    return {"error": "", "payload": _submission_public(submission)}


@router.put("", summary="Update (lock / unlock) a submission (admin only)")
def update_submission(
    id: str,
    version: str,
    body: UpdateSubmissionPayload,
    admin: Developer = Depends(security.require_admin),
):
    mod_row, version_row = _get_mod_and_version(id, version)
    submission = storage.find_one("submissions", mod_version_id=version_row["id"])
    if submission is None:
        raise HTTPException(status_code=404, detail="Submission not found")

    locked_by = admin.id if body.lock != ModVersionSubmissionLock.none else None
    _add_submission_audit(version_row["id"], AuditAction.updated, f"Submission {body.lock.value}", admin.id)
    updated = storage.update_where(
        "submissions",
        {"mod_version_id": version_row["id"]},
        {"lock": body.lock.value, "locked_by": locked_by, "updated_at": storage.now_iso()},
    )
    return {"error": "", "payload": _submission_public(updated)}


def _get_submission_or_404(version_row: dict) -> dict:
    submission = storage.find_one("submissions", mod_version_id=version_row["id"])
    if submission is None:
        raise HTTPException(status_code=404, detail="Submission not found")
    return submission


def _ensure_unlocked_for(submission: dict, developer: Developer) -> None:
    if security.get_active_ban(developer.id) is not None:
        raise HTTPException(status_code=403, detail="You are banned from accessing this resource")
    if submission["lock"] == ModVersionSubmissionLock.none.value:
        return
    if submission["lock"] == ModVersionSubmissionLock.locked.value and not developer.admin:
        raise HTTPException(status_code=400, detail="This submission is locked")
    if submission["lock"] == ModVersionSubmissionLock.internal.value and not developer.admin:
        raise HTTPException(status_code=400, detail="This submission is locked to index team discussion")


# ---------------------------------------------------------------------------
# Comments
# ---------------------------------------------------------------------------

@router.get("/comments", summary="List comments for a mod version submission")
def list_comments(
    id: str,
    version: str,
    page_and_size: tuple[int, int] = Depends(page_params),
    _developer: Developer = Depends(security.get_current_developer),
):
    page, per_page = page_and_size
    mod_row, version_row = _get_mod_and_version(id, version)
    submission = _get_submission_or_404(version_row)

    rows = storage.find_all("submission_comments", lambda c: c["submission_id"] == submission["mod_version_id"])
    rows.sort(key=lambda c: c["created_at"])

    paged = paginate(rows, page, per_page)
    paged["data"] = [_comment_public(r) for r in paged["data"]]
    return {"error": "", "payload": paged}


@router.post("/comments", status_code=201, summary="Add a comment to a mod version submission")
def create_comment(
    id: str,
    version: str,
    body: CreateCommentPayload,
    developer: Developer = Depends(security.get_current_developer),
):
    mod_row, version_row = _get_mod_and_version(id, version)
    submission = _get_submission_or_404(version_row)
    _ensure_unlocked_for(submission, developer)

    if not body.comment.strip():
        raise HTTPException(status_code=400, detail="comment cannot be empty")

    now = storage.now_iso()
    row = {
        "id": storage.next_id("submission_comments"),
        "submission_id": submission["mod_version_id"],
        "author_id": developer.id,
        "comment": body.comment,
        "created_at": now,
        "updated_at": None,
    }
    storage.insert("submission_comments", row)
    _add_comment_audit(row["id"], AuditAction.created, performed_by=developer.id)
    storage.update_where("submissions", {"mod_version_id": submission["mod_version_id"]}, {"updated_at": now})
    return {"error": "", "payload": _comment_public(row)}


def _get_comment_or_404(submission_id: int, comment_id: int) -> dict:
    row = storage.find_one("submission_comments", id=comment_id, submission_id=submission_id)
    if row is None:
        raise HTTPException(status_code=404, detail="Comment not found")
    return row


@router.put("/comments/{comment_id}", summary="Update a comment on a mod version submission")
def update_comment(
    id: str,
    version: str,
    comment_id: int,
    body: UpdateCommentPayload,
    developer: Developer = Depends(security.get_current_developer),
):
    mod_row, version_row = _get_mod_and_version(id, version)
    submission = _get_submission_or_404(version_row)
    comment = _get_comment_or_404(submission["mod_version_id"], comment_id)

    if comment["author_id"] != developer.id and not developer.admin:
        raise HTTPException(status_code=403, detail="You may only edit your own comments")
    _ensure_unlocked_for(submission, developer)

    if not body.comment.strip():
        raise HTTPException(status_code=400, detail="comment cannot be empty")

    _add_comment_audit(comment_id, AuditAction.updated, f"Updated comment. Previously: {comment["comment"]}", developer.id)
    updated = storage.update_where(
        "submission_comments",
        {"id": comment_id},
        {"comment": body.comment, "updated_at": storage.now_iso()},
    )
    return {"error": "", "payload": _comment_public(updated)}


@router.delete("/comments/{comment_id}", status_code=204, summary="Delete a comment on a mod version submission")
def delete_comment(
    id: str,
    version: str,
    comment_id: int,
    developer: Developer = Depends(security.get_current_developer),
):
    mod_row, version_row = _get_mod_and_version(id, version)
    submission = _get_submission_or_404(version_row)
    comment = _get_comment_or_404(submission["mod_version_id"], comment_id)

    if comment["author_id"] != developer.id and not developer.admin:
        raise HTTPException(status_code=403, detail="You may only delete your own comments")

    _add_comment_audit(comment_id, AuditAction.deleted, performed_by=developer.id)
    attachments = storage.find_all("submission_attachments", lambda a: a["comment_id"] == comment_id)
    for att in attachments:
        _delete_attachment_file(att)
    storage.delete_where("submission_attachments", lambda a: a["comment_id"] == comment_id)
    storage.delete_where("submission_comments", lambda c: c["id"] == comment_id)
    return None


# ---------------------------------------------------------------------------
# Attachments
# ---------------------------------------------------------------------------

def _attachment_dir(comment_id: int) -> str:
    path = os.path.join(settings.ATTACHMENTS_DIR, str(comment_id))
    os.makedirs(path, exist_ok=True)
    return path


def _delete_attachment_file(row: dict) -> None:
    path = row.get("_path")
    if path and os.path.exists(path):
        try:
            os.remove(path)
        except OSError:
            pass


@router.get("/comments/{comment_id}/attachments", summary="List attachments for a submission comment")
def list_attachments(
    id: str,
    version: str,
    comment_id: int,
    _developer: Developer = Depends(security.get_current_developer),
):
    mod_row, version_row = _get_mod_and_version(id, version)
    submission = _get_submission_or_404(version_row)
    _get_comment_or_404(submission["mod_version_id"], comment_id)

    rows = storage.find_all("submission_attachments", lambda a: a["comment_id"] == comment_id)
    return {"error": "", "payload": [_attachment_public(r) for r in rows]}


@router.post("/comments/{comment_id}/attachments", status_code=201, summary="Upload attachments to a submission comment")
async def upload_attachments(
    id: str,
    version: str,
    comment_id: int,
    image: list[UploadFile] = File(...),
    developer: Developer = Depends(security.get_current_developer),
):
    mod_row, version_row = _get_mod_and_version(id, version)
    submission = _get_submission_or_404(version_row)
    comment = _get_comment_or_404(submission["mod_version_id"], comment_id)

    permission = settings.ATTACHMENT_PERMISSIONS
    allowed = (
        permission == 1
        or (permission == 2 and (developer.admin or developer.verified))
        or (permission == 3 and (developer.admin or developer_has_accepted_mod(developer.id)))
        or (permission == 4 and developer.admin)
    )

    if permission < 1 or permission > 5:
        raise HTTPException(status_code=500, detail="Invalid attachment permission setting")

    if not allowed:
        raise HTTPException(status_code=403, detail="You do not have permission to upload attachments")

    existing = storage.find_all("submission_attachments", lambda a: a["comment_id"] == comment_id)
    if not image:
        raise HTTPException(status_code=400, detail="No images provided")
    if len(existing) + len(image) > settings.MAX_ATTACHMENTS_PER_COMMENT:
        raise HTTPException(
            status_code=400,
            detail=f"A comment may have at most {settings.MAX_ATTACHMENTS_PER_COMMENT} attachments",
        )

    created = []
    directory = _attachment_dir(comment_id)
    for upload in image:
        data = await upload.read()
        if len(data) > settings.MAX_ATTACHMENT_SIZE_BYTES:
            raise HTTPException(status_code=400, detail=f"{upload.filename} exceeds the size limit")

        try:
            img = Image.open(io.BytesIO(data))
            img.verify()
            ext = (img.format or "png").lower()
        except (UnidentifiedImageError, OSError):
            raise HTTPException(status_code=400, detail=f"{upload.filename} is not a valid image")

        attachment_id = storage.next_id("submission_attachments")
        filename = f"{attachment_id}_{uuid.uuid4().hex}.{ext}"
        path = os.path.join(directory, filename)
        with open(path, "wb") as f:
            f.write(data)

        url = f"{settings.BASE_URL}/uploads/attachments/{comment_id}/{filename}"
        row = {
            "id": attachment_id,
            "comment_id": comment_id,
            "url": url,
            "created_at": storage.now_iso(),
            "_path": path,
        }
        storage.insert("submission_attachments", row)
        created.append(row)

    _add_comment_audit(comment_id, AuditAction.updated, f"Attached {len(created)} file{'s' if len(created) != 1 else ''}", developer.id)

    return {"error": "", "payload": [_attachment_public(r) for r in created]}


@router.delete("/comments/{comment_id}/attachments/{attachment_id}", status_code=204, summary="Delete an attachment from a submission comment")
def delete_attachment(
    id: str,
    version: str,
    comment_id: int,
    attachment_id: int,
    developer: Developer = Depends(security.get_current_developer),
):
    mod_row, version_row = _get_mod_and_version(id, version)
    submission = _get_submission_or_404(version_row)
    comment = _get_comment_or_404(submission["mod_version_id"], comment_id)

    if comment["author_id"] != developer.id and not developer.admin:
        raise HTTPException(status_code=403, detail="You may only manage attachments on your own comments")

    row = storage.find_one("submission_attachments", id=attachment_id, comment_id=comment_id)
    if row is None:
        raise HTTPException(status_code=404, detail="Attachment not found")

    _delete_attachment_file(row)
    storage.delete_where("submission_attachments", lambda a: a["id"] == attachment_id)
    _add_comment_audit(comment_id, AuditAction.updated, "Removed an attachment", developer.id)
    return None


@router.get("/audit", summary="Get submission audit (admin only)")
def get_submission_audit(
    id: str,
    version: str,
    _admin: Developer = Depends(security.require_admin),
):
    _mod, version_row = _get_mod_and_version(id, version)
    submission = _get_submission_or_404(version_row)
    rows = storage.find_all("submission_audit", lambda a: a["submission_id"] == submission["mod_version_id"])
    rows.sort(key=lambda a: a["performed_at"])
    return {"error": "", "payload": [AuditActionRow(**{k: row.get(k) for k in ("action", "details", "performed_by", "performed_at")}).model_dump() for row in rows]}


@router.get("/comments/{comment_id}/audit", summary="Get comment audit (admin only)")
def get_comment_audit(
    id: str,
    version: str,
    comment_id: int,
    _admin: Developer = Depends(security.require_admin),
):
    _mod, version_row = _get_mod_and_version(id, version)
    submission = _get_submission_or_404(version_row)
    comment = _get_comment_or_404(submission["mod_version_id"], comment_id)
    rows = storage.find_all("comment_audit", lambda a: a["comment_id"] == comment_id)
    rows.sort(key=lambda a: a["performed_at"])
    return {"error": "", "payload": [AuditActionRow(**{k: row.get(k) for k in ("action", "details", "performed_by", "performed_at")}).model_dump() for row in rows]}
