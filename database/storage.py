"""
SQLite-backed storage engine.

This replaced an earlier version of this module that kept the whole
database as a single JSON file, rewritten from scratch on every write. That
was fine for a handful of test mods, but doesn't scale: every write cost
grew with the *total* size of the database (not just the row being
changed), there was no indexing, and a crash mid-write could corrupt the
entire file.

This version stores each "table" as a real SQLite table (a single file,
`database.sqlite3`, still no external service required) with the row
itself kept as a JSON blob in a `data` column -- routers already treat rows
as plain dicts with heterogeneous/nested content (a mod's `developers` list,
a version's `gd` object, etc.), and reshaping all of that into fully
normalized relational tables would mean rewriting most of the routers, not
just this module. What SQLite gets us instead:

  - Real indexes (via generated columns, see TABLES below) on every field
    routers actually look rows up by, so find_one() is an indexed lookup
    rather than a linear scan of a giant in-memory dict.
  - Real transactions and crash-safe writes (WAL mode), instead of a
    temp-file-then-rename dance over the whole database.
  - Each write only touches the rows it changes, not the entire dataset.

Two things this deliberately does NOT do, to keep the change bounded to
this one file:
  - find_all()'s `predicate` is an arbitrary Python callable (routers pass
    lambdas everywhere), which can't be pushed down into SQL in general.
    find_all() still pulls every row of the *relevant table only* and
    filters in Python -- much better than before (which pulled the entire
    multi-table database), but not a fully query-pushed-down engine.
  - No SQL-level FOREIGN KEY constraints. The relationships (mod_id,
    developer_id, etc.) are still just plain fields inside the JSON blob,
    enforced by application code the same way they were before.

If you outgrow *this*, the natural next step is normalizing the hot tables
(mod_versions, mods) into real columns and adding foreign keys -- but that's
a schema-and-router-level redesign, not a config change.
"""
from __future__ import annotations

import json
import os
import sqlite3
import threading
from datetime import datetime, timezone
from typing import Callable, Optional

from config import settings

_lock = threading.RLock()
_conn: Optional[sqlite3.Connection] = None

# Every "table" this app uses, and which of its JSON fields get a real,
# indexed SQLite column generated from them. These are exactly the fields
# routers actually call find_one()/update_where() with -- add to this list
# if you add a new lookup elsewhere.
TABLES: dict[str, list[str]] = {
    "developers": ["id", "github_id", "username"],
    "mods": ["id"],
    "mod_versions": ["id", "mod_id", "version", "status"],
    "deprecations": ["id", "mod_id"],
    "tags": ["id", "name"],
    "loader_versions": ["tag", "version"],
    "submissions": ["mod_version_id"],
    "submission_comments": ["id", "submission_id"],
    "submission_attachments": ["id", "comment_id"],
    "tokens": ["id", "developer_id", "refresh_token_hash"],
    "login_attempts": ["uuid"],
    "oauth_states": ["state"],
}

# Default set of tags the real index ships with. is_readonly tags can only
# be assigned by admins (e.g. curated event tags); the rest are free for
# mod developers to pick from when they submit a mod. Confirmed from a live
# GET /v1/detailed-tags response against https://api.geode-sdk.org --
# including the exact ids and (non-numeric) order, not just the names.
_DEFAULT_TAGS = [
    {"id": 1, "name": "universal", "display_name": "Universal", "is_readonly": False},
    {"id": 2, "name": "gameplay", "display_name": "Gameplay", "is_readonly": False},
    {"id": 3, "name": "editor", "display_name": "Editor", "is_readonly": False},
    {"id": 4, "name": "offline", "display_name": "Offline", "is_readonly": False},
    {"id": 5, "name": "online", "display_name": "Online", "is_readonly": False},
    {"id": 6, "name": "enhancement", "display_name": "Enhancement", "is_readonly": False},
    {"id": 7, "name": "music", "display_name": "Music", "is_readonly": False},
    {"id": 8, "name": "interface", "display_name": "Interface", "is_readonly": False},
    {"id": 9, "name": "bugfix", "display_name": "Bugfix", "is_readonly": False},
    {"id": 10, "name": "utility", "display_name": "Utility", "is_readonly": False},
    {"id": 11, "name": "performance", "display_name": "Performance", "is_readonly": False},
    {"id": 12, "name": "customization", "display_name": "Customization", "is_readonly": False},
    {"id": 13, "name": "content", "display_name": "Content", "is_readonly": False},
    {"id": 14, "name": "developer", "display_name": "Developer", "is_readonly": False},
    {"id": 15, "name": "cheat", "display_name": "Cheat", "is_readonly": False},
    {"id": 16, "name": "paid", "display_name": "Paid", "is_readonly": False},
    {"id": 17, "name": "joke", "display_name": "Joke", "is_readonly": False},
    {"id": 19, "name": "modtober24winner", "display_name": "Modtober 2024 Winner", "is_readonly": True},
    {"id": 20, "name": "api", "display_name": "API", "is_readonly": False},
    {"id": 22, "name": "modtober25winner", "display_name": "Modtober 2025 Winner", "is_readonly": True},
    {"id": 18, "name": "modtober24", "display_name": "Modtober 2024", "is_readonly": True},
    {"id": 21, "name": "modtober25", "display_name": "Modtober 2025", "is_readonly": True},
]


def now_iso() -> str:
    """RFC3339 UTC timestamp matching the real API's format exactly, e.g.
    "2026-03-08T19:05:13Z" -- no fractional seconds, "Z" suffix rather than
    "+00:00"."""
    return format_iso(datetime.now(timezone.utc))


def format_iso(dt: datetime) -> str:
    """Format an arbitrary datetime the same way now_iso() does."""
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    else:
        dt = dt.astimezone(timezone.utc)
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_iso(s: str) -> datetime:
    """Parse a timestamp produced by now_iso()/format_iso() -- or an older
    stored value using Python's "+00:00"-style isoformat() -- back into an
    aware UTC datetime."""
    if s.endswith("Z"):
        s = s[:-1] + "+00:00"
    dt = datetime.fromisoformat(s)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


# ---------------------------------------------------------------------------
# Setup / migration
# ---------------------------------------------------------------------------

def init_db() -> None:
    global _conn

    os.makedirs(settings.DATABASE_DIR, exist_ok=True)
    os.makedirs(settings.MODS_STORAGE_DIR, exist_ok=True)
    os.makedirs(settings.LOGOS_DIR, exist_ok=True)
    os.makedirs(settings.ATTACHMENTS_DIR, exist_ok=True)

    with _lock:
        is_new_db = not os.path.exists(settings.DATABASE_SQLITE_FILE)

        conn = sqlite3.connect(settings.DATABASE_SQLITE_FILE, check_same_thread=False)
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA foreign_keys=OFF")
        _conn = conn

        _create_schema(conn)

        if is_new_db:
            _migrate_from_legacy_json(conn)

        if not all_rows("tags"):
            _seed_default_tags(conn)

        conn.commit()


def _create_schema(conn: sqlite3.Connection) -> None:
    conn.execute("CREATE TABLE IF NOT EXISTS _meta (table_name TEXT PRIMARY KEY, next_id INTEGER NOT NULL)")

    for table, indexed_fields in TABLES.items():
        columns = ["_rowid INTEGER PRIMARY KEY AUTOINCREMENT", "data TEXT NOT NULL"]
        for field in indexed_fields:
            # STORED (not VIRTUAL) so the index below is a real B-tree over
            # materialized values, not recomputed from `data` on every
            # lookup.
            columns.append(f'"{field}" GENERATED ALWAYS AS (json_extract(data, \'$.{field}\')) STORED')
        conn.execute(f'CREATE TABLE IF NOT EXISTS "{table}" ({", ".join(columns)})')
        for field in indexed_fields:
            conn.execute(f'CREATE INDEX IF NOT EXISTS "idx_{table}_{field}" ON "{table}"("{field}")')


def _seed_default_tags(conn: sqlite3.Connection) -> None:
    for tag in _DEFAULT_TAGS:
        conn.execute("INSERT INTO tags (data) VALUES (?)", (json.dumps(tag),))
    _set_next_id(conn, "tags", max(t["id"] for t in _DEFAULT_TAGS) + 1)


def _migrate_from_legacy_json(conn: sqlite3.Connection) -> None:
    """One-time import of an old database.json (from before the SQLite
    migration) into the new SQLite tables. No-op if there's nothing to
    migrate. The old file is renamed (not deleted) once migrated, so it
    won't be re-imported on a later startup and isn't silently lost."""
    legacy_path = settings.DATABASE_FILE
    if not os.path.exists(legacy_path):
        return

    with open(legacy_path, "r", encoding="utf-8") as f:
        try:
            legacy = json.load(f)
        except json.JSONDecodeError:
            return

    migrated_any = False
    for table in TABLES:
        rows = legacy.get(table) or []
        for row in rows:
            conn.execute(f'INSERT INTO "{table}" (data) VALUES (?)', (json.dumps(row, default=str),))
            migrated_any = True

    next_ids = (legacy.get("_meta") or {}).get("next_ids") or {}
    for table, next_id_value in next_ids.items():
        if table in TABLES:
            _set_next_id(conn, table, next_id_value)

    if migrated_any:
        conn.commit()
        os.replace(legacy_path, legacy_path + ".migrated")


# ---------------------------------------------------------------------------
# next_id
# ---------------------------------------------------------------------------

def _set_next_id(conn: sqlite3.Connection, table: str, value: int) -> None:
    conn.execute(
        "INSERT INTO _meta (table_name, next_id) VALUES (?, ?) "
        "ON CONFLICT(table_name) DO UPDATE SET next_id = excluded.next_id",
        (table, value),
    )


def next_id(table: str) -> int:
    """Return the next autoincrement integer id for a table."""
    with _lock:
        row = _conn.execute("SELECT next_id FROM _meta WHERE table_name = ?", (table,)).fetchone()
        if row is not None:
            current = row[0]
        else:
            # Fall back to (max existing id) + 1 if this table's counter was
            # never initialized (e.g. a table that predates this counter).
            max_row = _conn.execute(f'SELECT MAX(json_extract(data, \'$.id\')) FROM "{table}"').fetchone()
            current = (max_row[0] or 0) + 1
        _set_next_id(_conn, table, current + 1)
        _conn.commit()
        return current


# ---------------------------------------------------------------------------
# CRUD
# ---------------------------------------------------------------------------

def all_rows(table: str) -> list[dict]:
    with _lock:
        cur = _conn.execute(f'SELECT data FROM "{table}"')
        return [json.loads(r[0]) for r in cur.fetchall()]


def _quote_column(table: str, field: str) -> str:
    """Use the indexed generated column when one exists for this field (a
    fast, real index lookup); otherwise fall back to an inline json_extract
    (correct, just not index-backed) so callers aren't restricted to only
    querying pre-declared fields."""
    if field in TABLES.get(table, []):
        return f'"{field}"'
    return f"json_extract(data, '$.{field}')"


def find_one(table: str, **filters) -> Optional[dict]:
    with _lock:
        if not filters:
            cur = _conn.execute(f'SELECT data FROM "{table}" LIMIT 1')
        else:
            where = " AND ".join(f"{_quote_column(table, k)} = ?" for k in filters)
            cur = _conn.execute(f'SELECT data FROM "{table}" WHERE {where} LIMIT 1', list(filters.values()))
        row = cur.fetchone()
        return json.loads(row[0]) if row else None


def find_all(table: str, predicate: Optional[Callable[[dict], bool]] = None) -> list[dict]:
    rows = all_rows(table)
    if predicate is None:
        return rows
    return [r for r in rows if predicate(r)]


def insert(table: str, row: dict) -> dict:
    with _lock:
        _conn.execute(f'INSERT INTO "{table}" (data) VALUES (?)', (json.dumps(row, default=str),))
        _conn.commit()
        return dict(row)


def update_where(table: str, match: dict, patch: dict) -> Optional[dict]:
    """Update the first row matching `match` with `patch`. Returns the
    updated row."""
    with _lock:
        where = " AND ".join(f"{_quote_column(table, k)} = ?" for k in match)
        cur = _conn.execute(
            f'SELECT _rowid, data FROM "{table}" WHERE {where} LIMIT 1', list(match.values())
        )
        row = cur.fetchone()
        if row is None:
            return None
        rowid, data = row
        updated = json.loads(data)
        updated.update(patch)
        _conn.execute(
            f'UPDATE "{table}" SET data = ? WHERE _rowid = ?', (json.dumps(updated, default=str), rowid)
        )
        _conn.commit()
        return updated


def delete_where(table: str, predicate: Callable[[dict], bool]) -> int:
    """Delete all rows matching predicate. Returns number of rows deleted."""
    with _lock:
        cur = _conn.execute(f'SELECT _rowid, data FROM "{table}"')
        to_delete = [rowid for rowid, data in cur.fetchall() if predicate(json.loads(data))]
        if not to_delete:
            return 0
        placeholders = ",".join("?" * len(to_delete))
        _conn.execute(f'DELETE FROM "{table}" WHERE _rowid IN ({placeholders})', to_delete)
        _conn.commit()
        return len(to_delete)
