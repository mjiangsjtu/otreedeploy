"""People: user accounts, who may see which project, and signed-in sessions.

A lab server is usually shared by several groups whose data-protection promises differ
(different ethics approvals, different data-management plans). So each person signs in
as themselves, sees only the projects they are a member of, and everything they do is
attributed to them in the audit trail (``audit``). Admins see every project and manage
the accounts.

Access is deliberately one level: a member of a project can do everything with it, and
anybody else cannot even see that it exists. Finer roles were considered and rejected
-- in a lab, the people on a study are the people responsible for it.

Stored in its own SQLite file, ``accounts.db`` (mode 0600), not in ``state.db``: that
one gets copied around when somebody debugs, and a copy of it should carry no password
hashes. Sessions are kept here too, server-side, so signing out, disabling somebody or
changing a password ends their sessions at once.

Before accounts existed there was one shared password in ``auth.json``. ``init()``
turns it into the admin account ``admin`` with the very same hash, so the existing
password keeps working.
"""

from __future__ import annotations

import argparse
import getpass
import hashlib
import hmac
import json
import os
import re
import secrets
import sqlite3
import sys
import time
from dataclasses import dataclass
from pathlib import Path

from . import config
from .auth import ALGORITHM, ITERATIONS, MIN_PASSWORD_LENGTH, _derive

SESSION_MAX_AGE = 60 * 60 * 24 * 30
# Refreshing last_seen on every request would write to the database on every page.
_TOUCH_INTERVAL = 300
NAME_RE = re.compile(r"^[a-z][a-z0-9._-]{1,31}$")

SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    name        TEXT PRIMARY KEY,
    algorithm   TEXT NOT NULL,
    iterations  INTEGER NOT NULL,
    salt        TEXT NOT NULL,
    hash        TEXT NOT NULL,
    is_admin    INTEGER NOT NULL DEFAULT 0,
    disabled    INTEGER NOT NULL DEFAULT 0,
    created_at  INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS members (
    name  TEXT NOT NULL REFERENCES users(name) ON DELETE CASCADE,
    slug  TEXT NOT NULL,
    PRIMARY KEY (name, slug)
);

CREATE TABLE IF NOT EXISTS sessions (
    id          TEXT PRIMARY KEY,  -- sha256 of the cookie value, never the value itself
    name        TEXT NOT NULL REFERENCES users(name) ON DELETE CASCADE,
    created_at  INTEGER NOT NULL,
    expires_at  INTEGER NOT NULL,
    last_seen   INTEGER NOT NULL
);
"""


class AccountError(Exception):
    """A request about accounts that cannot be carried out, in words for the user."""


@dataclass(frozen=True)
class User:
    name: str
    is_admin: bool
    disabled: bool = False
    created_at: int = 0


# The stand-in for a dry run on a laptop with no accounts set up: everything is open,
# as it always was. A real install with no accounts is locked instead (see ``access``).
DEV_USER = User(name="dev", is_admin=True)


def _path() -> Path:
    return config.get_settings().state_dir / "accounts.db"


class _Connection(sqlite3.Connection):
    def __exit__(self, *exc):
        try:
            return super().__exit__(*exc)
        finally:
            self.close()


def _connect() -> sqlite3.Connection:
    path = _path()
    path.parent.mkdir(parents=True, exist_ok=True)
    if not path.exists():
        # Created 0600 before SQLite writes anything into it.
        os.close(os.open(path, os.O_WRONLY | os.O_CREAT, 0o600))
    conn = sqlite3.connect(path, timeout=30, factory=_Connection)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    if path not in _schema_ready:
        conn.executescript(SCHEMA)
        _schema_ready.add(path)
    return conn


_initialised: set[Path] = set()
_schema_ready: set[Path] = set()


def init() -> None:
    """Create the tables, and adopt a password from before accounts existed.

    Called on every request, so it does nothing once this database is set up -- unless
    a pre-accounts password file has (re)appeared, which is then migrated.
    """
    path = _path()
    legacy = path.parent / "auth.json"
    if path in _initialised and path.exists() and not legacy.exists():
        return
    with _connect() as conn:
        conn.executescript(SCHEMA)
    _migrate_single_password()
    _initialised.add(path)


def has_users() -> bool:
    if not _path().exists():
        return False
    with _connect() as conn:
        return conn.execute("SELECT 1 FROM users LIMIT 1").fetchone() is not None


def validate_name(name: str) -> str:
    if not NAME_RE.match(name or ""):
        raise AccountError(
            "a user name is 2-32 characters: lower-case letters, digits, '.', '-' or "
            "'_', starting with a letter"
        )
    return name


def _check_password(password: str) -> None:
    if len(password) < MIN_PASSWORD_LENGTH:
        raise AccountError(
            f"the password must be at least {MIN_PASSWORD_LENGTH} characters long"
        )


def _hash(password: str) -> tuple[str, str]:
    salt = secrets.token_bytes(16)
    return salt.hex(), _derive(password, salt, ITERATIONS).hex()


def _user(row) -> User:
    return User(name=row["name"], is_admin=bool(row["is_admin"]),
                disabled=bool(row["disabled"]), created_at=row["created_at"])


# ------------------------------------------------------------------------- users


def create_user(name: str, password: str, is_admin: bool = False) -> User:
    validate_name(name)
    _check_password(password)
    salt, digest = _hash(password)
    try:
        with _connect() as conn:
            conn.execute(
                "INSERT INTO users (name, algorithm, iterations, salt, hash, is_admin, "
                "created_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (name, ALGORITHM, ITERATIONS, salt, digest, int(is_admin), int(time.time())),
            )
    except sqlite3.IntegrityError:
        raise AccountError(f"there is already a user called '{name}'") from None
    return User(name=name, is_admin=is_admin)


def get_user(name: str) -> User | None:
    with _connect() as conn:
        row = conn.execute("SELECT * FROM users WHERE name = ?", (name,)).fetchone()
    return _user(row) if row else None


def list_users() -> list[User]:
    with _connect() as conn:
        rows = conn.execute("SELECT * FROM users ORDER BY name").fetchall()
    return [_user(r) for r in rows]


# A real hash to compare against when the name does not exist, so that "no such user"
# and "wrong password" take the same time and cannot be told apart.
_DUMMY_SALT = secrets.token_bytes(16)


def verify(name: str, password: str) -> User | None:
    """The user, if the name exists, is enabled and the password is right."""
    with _connect() as conn:
        row = conn.execute("SELECT * FROM users WHERE name = ?", (name,)).fetchone()
    if row is None or row["algorithm"] != ALGORITHM:
        _derive(password, _DUMMY_SALT, ITERATIONS)
        return None
    candidate = _derive(password, bytes.fromhex(row["salt"]), row["iterations"])
    if not hmac.compare_digest(candidate, bytes.fromhex(row["hash"])):
        return None
    if row["disabled"]:
        return None
    return _user(row)


def set_password(name: str, password: str) -> None:
    """Replace a password. Every session of that user ends."""
    _check_password(password)
    salt, digest = _hash(password)
    with _connect() as conn:
        changed = conn.execute(
            "UPDATE users SET algorithm = ?, iterations = ?, salt = ?, hash = ? "
            "WHERE name = ?",
            (ALGORITHM, ITERATIONS, salt, digest, name),
        ).rowcount
        if not changed:
            raise AccountError(f"there is no user called '{name}'")
        conn.execute("DELETE FROM sessions WHERE name = ?", (name,))


def _admins(conn, excluding: str) -> int:
    return conn.execute(
        "SELECT count(*) FROM users WHERE is_admin = 1 AND disabled = 0 AND name <> ?",
        (excluding,),
    ).fetchone()[0]


def set_admin(name: str, is_admin: bool) -> None:
    with _connect() as conn:
        if not is_admin and not _admins(conn, name):
            raise AccountError("there must always be at least one admin")
        if not conn.execute("UPDATE users SET is_admin = ? WHERE name = ?",
                            (int(is_admin), name)).rowcount:
            raise AccountError(f"there is no user called '{name}'")


def set_disabled(name: str, disabled: bool) -> None:
    """Disabling keeps the account (and its place in the audit trail) but ends its
    sessions and refuses its password."""
    with _connect() as conn:
        if disabled and not _admins(conn, name):
            row = conn.execute("SELECT is_admin FROM users WHERE name = ?", (name,)).fetchone()
            if row and row["is_admin"]:
                raise AccountError("there must always be at least one admin")
        if not conn.execute("UPDATE users SET disabled = ? WHERE name = ?",
                            (int(disabled), name)).rowcount:
            raise AccountError(f"there is no user called '{name}'")
        if disabled:
            conn.execute("DELETE FROM sessions WHERE name = ?", (name,))


def generate_password() -> str:
    """For an admin creating or resetting an account: shown once, then changed."""
    return secrets.token_urlsafe(12)


# ----------------------------------------------------------------------- members


def grant(name: str, slug: str) -> None:
    with _connect() as conn:
        if conn.execute("SELECT 1 FROM users WHERE name = ?", (name,)).fetchone() is None:
            raise AccountError(f"there is no user called '{name}'")
        conn.execute("INSERT OR IGNORE INTO members (name, slug) VALUES (?, ?)", (name, slug))


def revoke(name: str, slug: str) -> None:
    with _connect() as conn:
        conn.execute("DELETE FROM members WHERE name = ? AND slug = ?", (name, slug))


def forget_project(slug: str) -> None:
    """A deleted project's memberships go with it, so a new project that reuses the
    name does not inherit them."""
    with _connect() as conn:
        conn.execute("DELETE FROM members WHERE slug = ?", (slug,))


def members_of(slug: str) -> list[str]:
    with _connect() as conn:
        rows = conn.execute("SELECT name FROM members WHERE slug = ? ORDER BY name",
                            (slug,)).fetchall()
    return [r["name"] for r in rows]


def projects_of(name: str) -> set[str]:
    with _connect() as conn:
        rows = conn.execute("SELECT slug FROM members WHERE name = ?", (name,)).fetchall()
    return {r["slug"] for r in rows}


def can_access(user: User | None, slug: str | None) -> bool:
    if user is None or slug is None:
        return False
    if user.is_admin:
        return True
    with _connect() as conn:
        return conn.execute("SELECT 1 FROM members WHERE name = ? AND slug = ?",
                            (user.name, slug)).fetchone() is not None


# ---------------------------------------------------------------------- sessions


def _session_id(token: str) -> str:
    # Only a hash is stored: a copy of accounts.db does not let anyone take over a
    # signed-in browser.
    return hashlib.sha256(token.encode()).hexdigest()


def start_session(name: str) -> str:
    token = secrets.token_urlsafe(32)
    now = int(time.time())
    with _connect() as conn:
        conn.execute(
            "INSERT INTO sessions (id, name, created_at, expires_at, last_seen) "
            "VALUES (?, ?, ?, ?, ?)",
            (_session_id(token), name, now, now + SESSION_MAX_AGE, now),
        )
        # Expired sessions are only ever looked up to be refused; drop them here.
        conn.execute("DELETE FROM sessions WHERE expires_at < ?", (now,))
    return token


def session_user(token: str | None) -> User | None:
    if not token or not _path().exists():
        return None
    now = int(time.time())
    with _connect() as conn:
        row = conn.execute(
            "SELECT s.id, s.expires_at, s.last_seen, u.* FROM sessions s "
            "JOIN users u ON u.name = s.name WHERE s.id = ?",
            (_session_id(token),),
        ).fetchone()
        if row is None or row["expires_at"] < now or row["disabled"]:
            return None
        if now - row["last_seen"] > _TOUCH_INTERVAL:
            conn.execute("UPDATE sessions SET last_seen = ? WHERE id = ?", (now, row["id"]))
    return _user(row)


def end_session(token: str | None) -> None:
    if not token or not _path().exists():
        return
    with _connect() as conn:
        conn.execute("DELETE FROM sessions WHERE id = ?", (_session_id(token),))


# --------------------------------------------------------------------- migration


def _migrate_single_password() -> None:
    """Turn the pre-accounts shared password into the admin account ``admin``.

    The hash is copied as it is, so the existing password keeps working; the old file
    is renamed rather than deleted, so the step can be inspected afterwards.
    """
    old = config.get_settings().state_dir / "auth.json"
    if not old.exists() or has_users():
        return
    try:
        raw = json.loads(old.read_text())
        if raw["algorithm"] != ALGORITHM:
            raise ValueError(raw["algorithm"])
        values = (str(raw["salt"]), str(raw["hash"]), int(raw["iterations"]))
        bytes.fromhex(values[0]), bytes.fromhex(values[1])
    except (OSError, ValueError, KeyError, TypeError) as exc:
        # A damaged old file must not leave the app open: with no users a real install
        # refuses every page, which is the locked state wanted here.
        raise AccountError(f"{old} could not be migrated: {exc}") from exc
    from . import db

    with _connect() as conn:
        conn.execute(
            "INSERT INTO users (name, algorithm, iterations, salt, hash, is_admin, "
            "created_at) VALUES ('admin', ?, ?, ?, ?, 1, ?)",
            (ALGORITHM, values[2], values[0], values[1], int(time.time())),
        )
        for project in db.list_projects():
            conn.execute("INSERT OR IGNORE INTO members (name, slug) VALUES ('admin', ?)",
                         (project.slug,))
    old.replace(old.with_name("auth.json.migrated"))


# --------------------------------------------------------------------------- CLI


def _read_password(args) -> str:
    if args.stdin:
        return sys.stdin.readline().rstrip("\n")
    password = getpass.getpass("New password: ")
    if password != getpass.getpass("Repeat password: "):
        raise AccountError("those did not match")
    return password


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(
        prog="otreedeploy-users",
        description="Manage the accounts that sign in to the otreedeploy management page. "
        "Run on the server as the app user: sudo -u otreedeploy otreedeploy-users ...",
    )
    parser.add_argument("--check", action="store_true",
                        help="exit 0 if at least one account exists, 1 if not")
    sub = parser.add_subparsers(dest="command")
    add = sub.add_parser("add", help="create an account")
    add.add_argument("name")
    add.add_argument("--admin", action="store_true", help="may manage accounts and see all projects")
    add.add_argument("--stdin", action="store_true", help="read the password from standard input")
    passwd = sub.add_parser("passwd", help="set an account's password (no old one needed)")
    passwd.add_argument("name")
    passwd.add_argument("--stdin", action="store_true")
    admin = sub.add_parser("admin", help="give or take away admin rights")
    admin.add_argument("name")
    admin.add_argument("state", choices=["on", "off"])
    for verb in ("disable", "enable"):
        sub.add_parser(verb, help=f"{verb} an account").add_argument("name")
    sub.add_parser("list", help="list accounts")
    args = parser.parse_args(argv)

    try:
        init()
        if args.check:
            raise SystemExit(0 if has_users() else 1)
        if args.command == "add":
            create_user(args.name, _read_password(args), is_admin=args.admin)
            print(f"Created {'admin ' if args.admin else ''}account '{args.name}'.")
        elif args.command == "passwd":
            set_password(args.name, _read_password(args))
            print(f"Password set for '{args.name}'; their other sessions have ended.")
        elif args.command == "admin":
            set_admin(args.name, args.state == "on")
            print(f"'{args.name}' is {'now' if args.state == 'on' else 'no longer'} an admin.")
        elif args.command in {"disable", "enable"}:
            set_disabled(args.name, args.command == "disable")
            print(f"'{args.name}' is {args.command}d.")
        elif args.command == "list":
            for user in list_users():
                flags = ", ".join(f for f, on in (("admin", user.is_admin),
                                                  ("disabled", user.disabled)) if on)
                projects = ", ".join(sorted(projects_of(user.name))) or "-"
                print(f"{user.name}{f' ({flags})' if flags else ''}: {projects}")
        else:
            parser.print_help()
    except AccountError as exc:
        print(f"Could not do that: {exc}", file=sys.stderr)
        raise SystemExit(1) from None


if __name__ == "__main__":
    main()
