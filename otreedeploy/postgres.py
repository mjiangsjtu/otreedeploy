"""Database provisioning and read-only inspection of experiment databases.

Each project gets its own PostgreSQL database, ``otree_<slug>``, owned by the ``otree``
role, so resetting one study can never touch another's data.

No sudo is involved: bootstrap.sh creates a PostgreSQL role matching this app's OS user
with CREATEDB, so we connect over the unix socket under peer authentication. The app role
is granted membership in ``otree``, which is what lets it read session tables and run
pg_dump against databases oTree owns.

In dry-run mode (``OTREEDEPLOY_DRY_RUN=1``) each project is backed by a real SQLite file
instead. That is not a stub: the pipeline genuinely deploys and runs oTree, so the app can
be developed and exercised on a workstation with no PostgreSQL and no container.

We never unpickle anything out of these databases. oTree stores session config as a
base64 pickle; the plain columns are enough to identify a session for export.
"""

from __future__ import annotations

import gzip
import os
import shutil
import sqlite3
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path

import psycopg

from . import config
from .projects import db_name

MAINTENANCE_DB = "postgres"

SESSION_TABLE = "otree_session"
PARTICIPANT_TABLE = "otree_participant"

SESSION_QUERY = (
    f"SELECT code, label, num_participants, _created FROM {SESSION_TABLE} "
    "ORDER BY _created DESC"
)
ACTIVE_QUERY = (
    f"SELECT count(*) FROM {PARTICIPANT_TABLE} "
    "WHERE _last_request_timestamp IS NOT NULL AND _last_request_timestamp > "
)


class PostgresError(Exception):
    pass


@dataclass(frozen=True)
class SessionRow:
    code: str
    label: str | None
    num_participants: int | None
    created: int | None

    @property
    def created_iso(self) -> str:
        if not self.created:
            return "—"
        return time.strftime("%Y-%m-%d %H:%M", time.localtime(self.created))


# ------------------------------------------------------------------ sqlite (dry run)


def _sqlite_path(slug: str) -> Path:
    return config.get_settings().project_dir(slug) / "db.sqlite3"


def _sqlite_query(slug: str, query: str) -> list[tuple] | None:
    path = _sqlite_path(slug)
    if not path.exists():
        return None
    try:
        conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=5)
        try:
            return conn.execute(query).fetchall()
        finally:
            conn.close()
    except sqlite3.Error:
        return None


# -------------------------------------------------------------------------- postgres


def _connect(dbname: str) -> psycopg.Connection:
    try:
        return psycopg.connect(dbname=dbname, autocommit=True)
    except psycopg.Error as exc:
        raise PostgresError(f"could not connect to database {dbname!r}: {exc}") from exc


# ----------------------------------------------------------------------- provisioning


def database_url(slug: str) -> str:
    """The value handed to oTree as DATABASE_URL; SQLAlchemy parses it directly."""
    if config.dry_run():
        return f"sqlite:///{_sqlite_path(slug)}"
    return f"postgresql:///{db_name(slug)}?host=/var/run/postgresql"


def prepare_release(slug: str, release_dir: Path) -> None:
    """Give a freshly extracted release access to the project's database.

    Only meaningful in dry-run mode. oTree ignores the path in a ``sqlite://`` URL and
    always opens ``db.sqlite3`` relative to its working directory, so without this the
    database would live inside the release and be discarded on the next deploy. Linking
    it to the project-level file reproduces production's behaviour, where redeploying
    leaves collected data untouched.
    """
    if not config.dry_run():
        return
    target = _sqlite_path(slug)
    target.parent.mkdir(parents=True, exist_ok=True)
    link = release_dir / "db.sqlite3"
    if link.is_symlink() or link.exists():
        link.unlink()
    link.symlink_to(target)


def database_exists(slug: str) -> bool:
    if config.dry_run():
        return _sqlite_path(slug).exists()
    name = db_name(slug)
    with _connect(MAINTENANCE_DB) as conn:
        row = conn.execute(
            "SELECT 1 FROM pg_database WHERE datname = %s", (name,)
        ).fetchone()
    return row is not None


def create_database(slug: str) -> bool:
    """Dry run only: make room for the project's SQLite file.

    A real install creates each database through ``otree-ctl provision``, owned by the
    project's own role -- this app's role cannot create databases at all.
    """
    if not config.dry_run():
        raise PostgresError("databases are created by otree-ctl provision")
    # oTree's resetdb creates the SQLite file itself; just make sure the directory it
    # lives in is there.
    _sqlite_path(slug).parent.mkdir(parents=True, exist_ok=True)
    return not _sqlite_path(slug).exists()


def drop_database(slug: str) -> None:
    """Dry run only; a real install drops it with ``otree-ctl unprovision``."""
    if not config.dry_run():
        raise PostgresError("databases are dropped by otree-ctl unprovision")
    _sqlite_path(slug).unlink(missing_ok=True)


# ------------------------------------------------------------------------ inspection


def has_otree_tables(slug: str) -> bool:
    """True once `otree resetdb` has built the schema."""
    if config.dry_run():
        rows = _sqlite_query(
            slug,
            f"SELECT name FROM sqlite_master WHERE type='table' AND name='{SESSION_TABLE}'",
        )
        return bool(rows)
    if not database_exists(slug):
        return False
    with _connect(db_name(slug)) as conn:
        row = conn.execute("SELECT to_regclass(%s)", (SESSION_TABLE,)).fetchone()
    return bool(row and row[0])


def list_sessions(slug: str) -> list[SessionRow]:
    """Read the session list directly, so it works while the project is stopped."""
    if not has_otree_tables(slug):
        return []
    if config.dry_run():
        rows = _sqlite_query(slug, SESSION_QUERY) or []
    else:
        try:
            with _connect(db_name(slug)) as conn:
                rows = conn.execute(SESSION_QUERY).fetchall()
        except PostgresError:
            return []
    return [
        SessionRow(code=r[0], label=r[1], num_participants=r[2], created=r[3])
        for r in rows
    ]


@dataclass(frozen=True)
class Stats:
    has_tables: bool
    session_count: int
    active_participants: int


def project_stats(slug: str, window: int = config.PARTICIPANT_ACTIVE_SECONDS) -> Stats:
    """Everything the listing shows about one project's data, over one connection.

    The listing renders these for every project, so doing it as separate queries meant a
    fresh connection per project per field.
    """
    empty = Stats(has_tables=False, session_count=0, active_participants=0)
    if not has_otree_tables(slug):
        return empty
    cutoff = int(time.time()) - window
    count_sessions = f"SELECT count(*) FROM {SESSION_TABLE}"

    if config.dry_run():
        sessions = _sqlite_query(slug, count_sessions)
        active = _sqlite_query(slug, ACTIVE_QUERY + str(cutoff))
        return Stats(
            has_tables=True,
            session_count=int(sessions[0][0]) if sessions else 0,
            active_participants=int(active[0][0]) if active else 0,
        )

    try:
        with _connect(db_name(slug)) as conn:
            sessions = conn.execute(count_sessions).fetchone()
            active = conn.execute(ACTIVE_QUERY + "%s", (cutoff,)).fetchone()
    except PostgresError:
        return empty
    return Stats(
        has_tables=True,
        session_count=int(sessions[0]) if sessions else 0,
        active_participants=int(active[0]) if active else 0,
    )


def count_active_participants(
    slug: str, window: int = config.PARTICIPANT_ACTIVE_SECONDS
) -> int:
    """Participants who hit the server recently -- the signal that a study is live.

    This is what stands between a careless click and a ruined session.
    """
    if not has_otree_tables(slug):
        return 0
    cutoff = int(time.time()) - window
    if config.dry_run():
        rows = _sqlite_query(slug, ACTIVE_QUERY + str(cutoff))
        return int(rows[0][0]) if rows else 0
    try:
        with _connect(db_name(slug)) as conn:
            row = conn.execute(ACTIVE_QUERY + "%s", (cutoff,)).fetchone()
    except PostgresError:
        return 0
    return int(row[0]) if row else 0


# --------------------------------------------------------------------------- backups


def dump(slug: str, destination: Path) -> Path:
    """Snapshot the database to a gzipped file. Runs before anything destructive."""
    destination.parent.mkdir(parents=True, exist_ok=True)

    if config.dry_run():
        source = _sqlite_path(slug)
        payload = source.read_bytes() if source.exists() else b""
        destination.write_bytes(gzip.compress(payload))
        return destination

    pg_dump = shutil.which("pg_dump") or "/usr/bin/pg_dump"
    with open(destination, "wb") as out:
        gz = gzip.GzipFile(fileobj=out, mode="wb")
        process = subprocess.Popen(
            [pg_dump, "--no-owner", "--no-privileges", db_name(slug)],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env={**os.environ, "PGDATABASE": db_name(slug)},
        )
        assert process.stdout is not None
        while chunk := process.stdout.read(1024 * 256):
            gz.write(chunk)
        gz.close()
        _, stderr = process.communicate()
    if process.returncode != 0:
        destination.unlink(missing_ok=True)
        raise PostgresError(
            f"pg_dump failed (exit {process.returncode}): "
            f"{stderr.decode(errors='replace').strip()}"
        )
    return destination


def server_version() -> str | None:
    if config.dry_run():
        return f"sqlite {sqlite3.sqlite_version} (dry run)"
    try:
        with _connect(MAINTENANCE_DB) as conn:
            row = conn.execute("SHOW server_version").fetchone()
        return row[0] if row else None
    except PostgresError:
        return None


def database_size(slug: str) -> int | None:
    if config.dry_run():
        path = _sqlite_path(slug)
        return path.stat().st_size if path.exists() else None
    if not database_exists(slug):
        return None
    try:
        with _connect(MAINTENANCE_DB) as conn:
            row = conn.execute("SELECT pg_database_size(%s)", (db_name(slug),)).fetchone()
        return int(row[0]) if row else None
    except PostgresError:
        return None
