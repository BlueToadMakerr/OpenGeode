"""
SQLite-backed storage engine.

This replaced an earlier version of this module that kept the whole
database as a single JSON file, rewritten from scratch on every write. That
was fine for a handful of test mods, but doesn't scale: every write cost
grew with the *total* size of the database (not just the row being
changed), there was no indexing, and a crash mid-write could corrupt the
entire file.

This version stores each "table" as a real SQLite table (a single file,
`database.sqlite3`, still no external service required) with the row itself
kept as a JSON blob in a `data` column. SQLite supplies real indexes,
transactions, and crash-safe writes while the routers continue to work with
plain dictionaries.
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
    return format_iso(datetime.now(timezone.utc))


def format_iso(dt: datetime) -> str:
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    else:
        dt = dt.astimezone(timezone.utc)
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_iso(s: str) -> datetime:
    if s.endswith("Z"):
        s = s[:-1] + "+00:00"
    dt = datetime.fromisoformat(s)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


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
        # Remove the old Geometry Dash login-code table from existing installs.
        conn.execute("DROP TABLE IF EXISTS gd_login_codes")
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
            columns.append(f'"{field}" GENERATED ALWAYS AS (json_extract(data, \'$.{field}\')) STORED')
        conn.execute(f'CREATE TABLE IF NOT EXISTS "{table}" ({", ".join(columns)})')
        for field in indexed_fields:
            conn.execute(f'CREATE INDEX IF NOT EXISTS "idx_{table}_{field}" ON "{table}"("{field}")')


def _seed_default_tags(conn: sqlite3.Connection) -> None:
    for tag in _DEFAULT_TAGS:
        conn.execute("INSERT INTO tags (data) VALUES (?)", (json.dumps(tag),))
    _set_next_id(conn, "tags", max(t["id"] for t in _DEFAULT_TAGS) + 1)


def _migrate_from_legacy_json(conn: sqlite3.Connection) -> None:
    legacy_path = getattr(settings, "DATABASE_FILE", os.path.join(settings.DATABASE_DIR, "database.json"))
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


def _set_next_id(conn: sqlite3.Connection, table: str, value: int) -> None:
    conn.execute(
        "INSERT INTO _meta (table_name, next_id) VALUES (?, ?) ON CONFLICT(table_name) DO UPDATE SET next_id = excluded.next_id",
        (table, value),
    )


def next_id(table: str) -> int:
    with _lock:
        row = _conn.execute("SELECT next_id FROM _meta WHERE table_name = ?", (table,)).fetchone()
        if row is not None:
            current = row[0]
        else:
            max_row = _conn.execute(f'SELECT MAX(json_extract(data, \'$.id\')) FROM "{table}"').fetchone()
            current = (max_row[0] or 0) + 1
        _set_next_id(_conn, table, current + 1)
        _conn.commit()
        return current


def all_rows(table: str) -> list[dict]:
    with _lock:
        cur = _conn.execute(f'SELECT data FROM "{table}"')
        return [json.loads(r[0]) for r in cur.fetchall()]
