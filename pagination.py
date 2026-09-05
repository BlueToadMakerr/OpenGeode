from __future__ import annotations

from typing import Optional, TypeVar

from fastapi import Query

from config import settings

T = TypeVar("T")


def page_params(
    page: Optional[int] = Query(default=1, ge=1),
    per_page: Optional[int] = Query(default=None, ge=1),
):
    page = page or 1
    per_page = per_page or settings.DEFAULT_PER_PAGE
    per_page = min(per_page, settings.MAX_PER_PAGE)
    return page, per_page


def paginate(items: list[T], page: int, per_page: int) -> dict:
    total = len(items)
    start = (page - 1) * per_page
    end = start + per_page
    return {"data": items[start:end], "count": total}
