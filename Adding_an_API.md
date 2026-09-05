Here's how to extend this API — using your actual project's conventions, with a full worked example at the end.

## The two pieces of a new endpoint

Every route in this project is: a plain function decorated on an `APIRouter`, doing DB work through `storage.py`, returning a dict shaped like `{"error": "", "payload": ...}`. FastAPI picks up type hints and docstrings automatically for Swagger — you don't register anything separately for docs.

```python
# routers/whatever.py
from fastapi import APIRouter, Depends, HTTPException
import security
from database import storage
from models import Developer

router = APIRouter(prefix="/v1/whatever", tags=["whatever"])

@router.get("/{id}", summary="Get a widget")
def get_widget(id: int, developer: Developer = Depends(security.get_current_developer)):
    row = storage.find_one("widgets", id=id)
    if row is None:
        raise HTTPException(status_code=404, detail="Widget not found")
    return {"error": "", "payload": row}
```

That's it — Swagger UI at `/docs` picks it up immediately on next restart. More on making it look good there below.

## Using the DB

`database/storage.py` exposes eight functions; every router only ever talks to the DB through these:

| Function | Use |
|---|---|
| `storage.next_id(table)` | Get the next auto-increment int id for a table |
| `storage.insert(table, row_dict)` | Insert a new row |
| `storage.find_one(table, **filters)` | Get one row by exact-match field(s), or `None` |
| `storage.find_all(table, predicate=None)` | Get rows matching a Python lambda (or all rows) |
| `storage.all_rows(table)` | Every row in a table |
| `storage.update_where(table, match_dict, patch_dict)` | Update the first matching row, merging `patch_dict` in |
| `storage.delete_where(table, predicate)` | Delete rows matching a lambda, returns count deleted |
| `storage.now_iso()` | Current timestamp in the exact format the real API uses |

Rows are just plain dicts — no ORM, no model classes to define for storage itself.

```python
# Insert
row = storage.insert("widgets", {
    "id": storage.next_id("widgets"),
    "name": "Sprocket",
    "owner_id": developer.id,
    "created_at": storage.now_iso(),
})

# Read
widget = storage.find_one("widgets", id=5)
mine = storage.find_all("widgets", lambda w: w["owner_id"] == developer.id)

# Modify
storage.update_where("widgets", {"id": 5}, {"name": "New Name"})

# Delete
storage.delete_where("widgets", lambda w: w["id"] == 5)
```

**If you add a brand-new table**, that's all you need — a table is created automatically on first `insert()`... actually, one catch: tables and their indexes are declared upfront in `TABLES` in `storage.py`, so a genuinely new table needs one entry there:

```python
# database/storage.py
TABLES: dict[str, list[str]] = {
    "developers": ["id", "github_id", "username"],
    ...
    "widgets": ["id", "owner_id"],   # <- add this; list the fields you'll query by
}
```

The strings listed are the fields that get a real SQLite index — list whatever you'll call `find_one`/`update_where` with. If you skip this, `find_one`/`find_all` still work, just without an index (a table scan on that field).

## Response shape

Every success response in this API is `{"error": "", "payload": <data>}` — confirmed from the real server, `error` is always a string (never `null`), empty on success. Errors are handled globally in `main.py`: just `raise HTTPException(status_code=..., detail="message")` and it comes back as `{"error": "message", "payload": ""}` automatically — you never build error responses by hand.

```python
raise HTTPException(status_code=404, detail="Widget not found")
raise HTTPException(status_code=403, detail="Not your widget")
```

For a "no content" response (like the `POST /v1/mods/{id}/developers` pattern), set `status_code=204` on the decorator and `return None`.

## Request bodies: use a Pydantic model

Anything with a JSON body should be a model in `models.py` — this is what gives you both validation and a documented request schema in Swagger:

```python
# models.py
class CreateWidgetPayload(BaseModel):
    name: str
    color: Optional[str] = None
```

```python
@router.post("", status_code=201)
def create_widget(body: CreateWidgetPayload, developer: Developer = Depends(security.get_current_developer)):
    if not body.name.strip():
        raise HTTPException(status_code=400, detail="name cannot be empty")
    ...
```

Response bodies, by contrast, are built as plain dicts (not `response_model=`) throughout this project — see the note in the README under "A note on response typing" for why, and how to opt back into `response_model=` per-route if you want stricter response docs/validation for something you build.

## Auth & permissions

Three dependencies from `security.py`, drop into any route's parameters:

- `Depends(security.get_current_developer)` — 401s if there's no valid bearer token; gives you the `Developer`.
- `Depends(security.get_current_developer_optional)` — same, but returns `None` instead of 401ing (for endpoints that behave differently for logged-in vs anonymous users, like `GET /v1/mods`).
- `Depends(security.require_admin)` — 403s if the caller isn't an admin.

## Swagger UI — what makes it look good

FastAPI auto-generates the OpenAPI spec (and `/docs`) from your route signatures — nothing extra to "turn on." To make an endpoint's Swagger entry actually useful:

- `summary="..."` on the decorator — short description shown in the endpoint list.
- `tags=["whatever"]` — groups endpoints into collapsible sections (set once via `APIRouter(tags=[...])`, or per-route).
- Type hints on every parameter — a `str`, `int`, `Optional[X]`, or `Enum` gets its type/constraints documented automatically; `Query(default=..., description="...")` lets you document query params individually.
- A Pydantic request model — automatically renders as an expandable schema with an "Example Value" Swagger can prefill for "Try it out."
- `status_code=` on the decorator — shows the right response code instead of defaulting to 200.

## Full worked example: a "favorite a mod" feature

New table, new model, new router, wired in — showing every piece together.

**1. Add the table to `storage.py`:**
```python
TABLES: dict[str, list[str]] = {
    ...
    "favorites": ["developer_id", "mod_id"],
}
```

**2. `routers/favorites.py`:**
```python
from fastapi import APIRouter, Depends, HTTPException
import security
from database import storage
from models import Developer
from pagination import page_params, paginate
from serializers import mod_public
import mod_queries as mq
from models import ModVersionStatusEnum

router = APIRouter(prefix="/v1", tags=["favorites"])


@router.post("/mods/{id}/favorite", status_code=204, summary="Favorite a mod")
def favorite_mod(id: str, developer: Developer = Depends(security.get_current_developer)):
    if storage.find_one("mods", id=id) is None:
        raise HTTPException(status_code=404, detail="Mod not found")
    if storage.find_one("favorites", developer_id=developer.id, mod_id=id):
        return None  # already favorited -- idempotent, no error
    storage.insert("favorites", {"developer_id": developer.id, "mod_id": id, "created_at": storage.now_iso()})
    return None


@router.delete("/mods/{id}/favorite", status_code=204, summary="Unfavorite a mod")
def unfavorite_mod(id: str, developer: Developer = Depends(security.get_current_developer)):
    storage.delete_where("favorites", lambda f: f["developer_id"] == developer.id and f["mod_id"] == id)
    return None


@router.get("/me/favorites", summary="List the current developer's favorited mods")
def list_favorites(
    developer: Developer = Depends(security.get_current_developer),
    page_and_size: tuple[int, int] = Depends(page_params),
):
    page, per_page = page_and_size
    favorite_ids = [f["mod_id"] for f in storage.find_all("favorites", lambda f: f["developer_id"] == developer.id)]

    mods = []
    for mod_id in favorite_ids:
        mod_row = storage.find_one("mods", id=mod_id)
        if mod_row is None:
            continue
        best = mq.select_best_version(mod_id, [ModVersionStatusEnum.accepted.value])
        mods.append(mod_public(mod_row, [best] if best else [], requester=developer))

    result = paginate(mods, page, per_page)
    return {"error": "", "payload": result}
```

Restart the server, open `/docs`, and you'll see a new "favorites" section with all three endpoints, fully documented and testable via "Try it out" — no extra config anywhere.