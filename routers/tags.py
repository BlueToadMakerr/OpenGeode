from __future__ import annotations

from fastapi import APIRouter

from database import storage

router = APIRouter(prefix="/v1", tags=["tags"])


@router.get("/tags", summary="Get all available tags")
def list_tags():
    names = [t["name"] for t in storage.all_rows("tags")]
    return {"error": "", "payload": names}


@router.get("/detailed-tags", summary="Get all available tags with detailed information")
def list_detailed_tags():
    rows = storage.all_rows("tags")
    return {"error": "", "payload": rows}
