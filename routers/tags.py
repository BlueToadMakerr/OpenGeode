from __future__ import annotations

from fastapi import APIRouter

from database import storage

router = APIRouter(prefix="/v1", tags=["tags"])


@router.get("/tags", summary="Get all available tags")
def list_tags():
    # The upstream endpoint returns only writable tags. Read-only/curated
    # tags are still exposed by /detailed-tags.
    names = [
        t["name"]
        for t in storage.all_rows("tags")
        if not t.get("is_readonly", False)
    ]
    return {"error": "", "payload": names}


@router.get("/detailed-tags", summary="Get all available tags with detailed information")
def list_detailed_tags():
    return {"error": "", "payload": storage.all_rows("tags")}
