"""The management app's own state, in SQLite.

Deliberately separate from the PostgreSQL databases the experiments use: this file is
metadata about deployments, and losing it should never risk experiment data.

SQLite is the source of truth for a project's settings; the systemd EnvironmentFile is
rendered from it.
"""

from __future__ import annotations

import contextvars
import secrets
import sqlite3
import time
from dataclasses import dataclass
from typing import Any

from . import config

SCHEMA = """
CREATE TABLE IF NOT EXISTS projects (
    slug            TEXT PRIMARY KEY,
    display_name    TEXT NOT NULL,
    created_at      INTEGER NOT NULL,
    admin_password  TEXT NOT NULL,
    rest_key        TEXT NOT NULL,
    auth_level      TEXT NOT NULL DEFAULT 'STUDY',
    current_release TEXT
);

CREATE TABLE IF NOT EXISTS releases (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    slug          TEXT NOT NULL REFERENCES projects(slug) ON DELETE CASCADE,
    release       TEXT NOT NULL,
    created_at    INTEGER NOT NULL,
    filename      TEXT,
    sha256        TEXT,
    otree_version TEXT,
    status        TEXT NOT NULL DEFAULT 'building',
    log           TEXT NOT NULL DEFAULT '',
    UNIQUE (slug, release)
);

CREATE TABLE IF NOT EXISTS events (
    id      INTEGER PRIMARY KEY AUTOINCREMENT,
    slug    TEXT,
    at      INTEGER NOT NULL,
    kind    TEXT NOT NULL,
    message TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS settings (
    key   TEXT PRIMARY KEY,
    value TEXT
);

CREATE INDEX IF NOT EXISTS events_at ON events(at DESC);
CREATE INDEX IF NOT EXISTS releases_slug ON releases(slug, created_at DESC);
"""


@dataclass(frozen=True)
class Project:
    slug: str
    display_name: str
    created_at: int
    admin_password: str
    rest_key: str
    auth_level: str
    current_release: str | None


@dataclass(frozen=True)
class Release:
    id: int
    slug: str
    release: str
    created_at: int
    filename: str | None
    sha256: str | None
    otree_version: str | None
    status: str
    log: str


class _Connection(sqlite3.Connection):
    """A connection that closes when its ``with`` block ends.

    sqlite3's own context manager commits or rolls back but leaves the connection
    open; every call here opens a fresh one, so they would otherwise pile up until
    garbage collection.
    """

    def __exit__(self, *exc):
        try:
            return super().__exit__(*exc)
        finally:
            self.close()


def connect() -> sqlite3.Connection:
    settings = config.get_settings()
    settings.state_dir.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(settings.db_path, timeout=30, factory=_Connection)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    return conn


def init() -> None:
    with connect() as conn:
        conn.executescript(SCHEMA)
        # Added with accounts: who did it. Older databases lack the column.
        columns = {r["name"] for r in conn.execute("PRAGMA table_info(events)")}
        if "user" not in columns:
            conn.execute("ALTER TABLE events ADD COLUMN user TEXT")


def get_setting(key: str) -> str | None:
    with connect() as conn:
        row = conn.execute("SELECT value FROM settings WHERE key = ?", (key,)).fetchone()
    return row["value"] if row else None


def set_setting(key: str, value: str | None) -> None:
    with connect() as conn:
        if value is None:
            conn.execute("DELETE FROM settings WHERE key = ?", (key,))
        else:
            conn.execute("INSERT INTO settings (key, value) VALUES (?, ?) "
                         "ON CONFLICT(key) DO UPDATE SET value = excluded.value", (key, value))


def new_secret(length: int = 24) -> str:
    return secrets.token_urlsafe(length)


# --------------------------------------------------------------------------- projects


def create_project(slug: str, display_name: str) -> Project:
    now = int(time.time())
    with connect() as conn:
        conn.execute(
            "INSERT INTO projects (slug, display_name, created_at, admin_password, "
            "rest_key, auth_level) VALUES (?, ?, ?, ?, ?, 'STUDY')",
            (slug, display_name, now, new_secret(9), new_secret(24)),
        )
    project = get_project(slug)
    assert project is not None
    return project


def get_project(slug: str) -> Project | None:
    with connect() as conn:
        row = conn.execute("SELECT * FROM projects WHERE slug = ?", (slug,)).fetchone()
    return _project(row) if row else None


def list_projects() -> list[Project]:
    with connect() as conn:
        rows = conn.execute("SELECT * FROM projects ORDER BY display_name").fetchall()
    return [_project(r) for r in rows]


def update_project(slug: str, **fields: Any) -> None:
    if not fields:
        return
    allowed = {"display_name", "admin_password", "auth_level", "current_release", "rest_key"}
    unknown = set(fields) - allowed
    if unknown:
        raise ValueError(f"cannot update unknown field(s): {sorted(unknown)}")
    assignments = ", ".join(f"{k} = ?" for k in fields)
    with connect() as conn:
        conn.execute(
            f"UPDATE projects SET {assignments} WHERE slug = ?",
            (*fields.values(), slug),
        )


def delete_project(slug: str) -> None:
    with connect() as conn:
        conn.execute("DELETE FROM releases WHERE slug = ?", (slug,))
        conn.execute("DELETE FROM projects WHERE slug = ?", (slug,))


def _project(row: sqlite3.Row) -> Project:
    return Project(
        slug=row["slug"],
        display_name=row["display_name"],
        created_at=row["created_at"],
        admin_password=row["admin_password"],
        rest_key=row["rest_key"],
        auth_level=row["auth_level"],
        current_release=row["current_release"],
    )


# --------------------------------------------------------------------------- releases


def create_release(slug: str, release: str, filename: str | None, sha256: str | None) -> int:
    with connect() as conn:
        cursor = conn.execute(
            "INSERT INTO releases (slug, release, created_at, filename, sha256) "
            "VALUES (?, ?, ?, ?, ?)",
            (slug, release, int(time.time()), filename, sha256),
        )
    return int(cursor.lastrowid)


def finish_release(
    release_id: int, status: str, log: str, otree_version: str | None = None
) -> None:
    with connect() as conn:
        conn.execute(
            "UPDATE releases SET status = ?, log = ?, otree_version = ? WHERE id = ?",
            (status, log, otree_version, release_id),
        )


def list_releases(slug: str, limit: int = 50) -> list[Release]:
    with connect() as conn:
        rows = conn.execute(
            "SELECT * FROM releases WHERE slug = ? ORDER BY created_at DESC LIMIT ?",
            (slug, limit),
        ).fetchall()
    return [_release(r) for r in rows]


def get_release(slug: str, release: str) -> Release | None:
    with connect() as conn:
        row = conn.execute(
            "SELECT * FROM releases WHERE slug = ? AND release = ?", (slug, release)
        ).fetchone()
    return _release(row) if row else None


def delete_release(slug: str, release: str) -> None:
    with connect() as conn:
        conn.execute(
            "DELETE FROM releases WHERE slug = ? AND release = ?", (slug, release)
        )


def _release(row: sqlite3.Row) -> Release:
    return Release(
        id=row["id"],
        slug=row["slug"],
        release=row["release"],
        created_at=row["created_at"],
        filename=row["filename"],
        sha256=row["sha256"],
        otree_version=row["otree_version"],
        status=row["status"],
        log=row["log"],
    )


# ----------------------------------------------------------------------------- events


# Who is acting, set per request by access.EnforceAccess (and carried into deploy
# threads), so that the history records it without threading a user through every call.
actor: contextvars.ContextVar[str | None] = contextvars.ContextVar("actor", default=None)


def log_event(kind: str, message: str, slug: str | None = None) -> None:
    with connect() as conn:
        conn.execute(
            "INSERT INTO events (slug, at, kind, message, user) VALUES (?, ?, ?, ?, ?)",
            (slug, int(time.time()), kind, message, actor.get()),
        )


def recent_events(limit: int = 50, slug: str | None = None) -> list[dict]:
    with connect() as conn:
        if slug:
            rows = conn.execute(
                "SELECT * FROM events WHERE slug = ? ORDER BY at DESC LIMIT ?",
                (slug, limit),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM events ORDER BY at DESC LIMIT ?", (limit,)
            ).fetchall()
    return [dict(r) for r in rows]
