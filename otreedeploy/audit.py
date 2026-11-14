"""The audit trail: who did what, to which project, and which data left the server.

Written to the system journal rather than to a file or table this app owns. A process
that is not root can add journal entries but never change or remove them, so neither
this app nor anything that compromises it can rewrite history. journald also stamps
each entry with the sender's user id (``_UID``) itself, which the sender cannot
choose; the trail is read back filtered on this app's own uid, so an entry forged by
any other process -- a project's own code included -- is never shown.

Entries carry structured fields (AUDIT_USER, AUDIT_ACTION, AUDIT_PROJECT,
AUDIT_DETAIL, AUDIT_CLIENT) and the identifier ``otreedeploy-audit``, so they can be
queried outside the app too:

    journalctl -t otreedeploy-audit _UID=$(id -u otreedeploy) -o verbose

How long the journal keeps them is the system's setting (``MaxRetentionSec`` and the
size limits in journald.conf), not this app's.

In a dry run there is no system journal to rely on, so entries go to ``audit.log`` in
the state directory instead.
"""

from __future__ import annotations

import json
import os
import socket
import subprocess
import time
from dataclasses import dataclass

from . import config

IDENTIFIER = "otreedeploy-audit"
JOURNAL_SOCKET = "/run/systemd/journal/socket"
JOURNALCTL = "/usr/bin/journalctl"


@dataclass(frozen=True)
class Entry:
    at: float
    user: str
    action: str
    project: str
    detail: str
    client: str


def _clean(value: object) -> str:
    # The simple journal protocol is one KEY=value per line, so values stay on one line.
    return str(value if value is not None else "").replace("\n", " ").replace("\r", " ")[:2000]


def record(action: str, user: str | None, project: str | None = None,
           detail: str = "", client: str | None = None) -> None:
    """Append one entry. Never raises: failing to audit must not fail the action
    itself, but it is reported on stderr, which the journal also keeps."""
    fields = {
        "AUDIT_ACTION": action,
        "AUDIT_USER": user or "-",
        "AUDIT_PROJECT": project or "",
        "AUDIT_DETAIL": detail,
        "AUDIT_CLIENT": client or "",
    }
    message = f"{fields['AUDIT_USER']} {action}" + (f" {project}" if project else "") + (
        f": {detail}" if detail else "")
    try:
        if config.dry_run() or not os.path.exists(JOURNAL_SOCKET):
            _record_file(fields)
        else:
            _record_journal(message, fields)
    except OSError as exc:
        import sys

        print(f"otreedeploy: could not write the audit entry {message!r}: {exc}",
              file=sys.stderr)


def _record_journal(message: str, fields: dict) -> None:
    payload = {"MESSAGE": message, "PRIORITY": "6", "SYSLOG_IDENTIFIER": IDENTIFIER,
               **fields}
    data = "".join(f"{key}={_clean(value)}\n" for key, value in payload.items())
    with socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM) as sock:
        sock.sendto(data.encode(), JOURNAL_SOCKET)


def _file() -> os.PathLike:
    return config.get_settings().state_dir / "audit.log"


def _record_file(fields: dict) -> None:
    path = _file()
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a", encoding="utf-8") as out:
        out.write(json.dumps({"at": time.time(), **{k: _clean(v) for k, v in fields.items()}})
                  + "\n")


def read(limit: int = 200, project: str | None = None, user: str | None = None) -> list[Entry]:
    """The newest entries first, optionally only those about one project or user."""
    if config.dry_run() or not os.path.exists(JOURNAL_SOCKET):
        records = _read_file()
    else:
        records = _read_journal(limit * 4 if (project or user) else limit, project, user)
    entries = []
    for raw in records:
        entry = Entry(
            at=float(raw.get("at", 0)),
            user=raw.get("AUDIT_USER", ""),
            action=raw.get("AUDIT_ACTION", ""),
            project=raw.get("AUDIT_PROJECT", ""),
            detail=raw.get("AUDIT_DETAIL", ""),
            client=raw.get("AUDIT_CLIENT", ""),
        )
        if project and entry.project != project:
            continue
        if user and entry.user != user:
            continue
        entries.append(entry)
    entries.sort(key=lambda e: e.at, reverse=True)
    return entries[:limit]


def _read_file() -> list[dict]:
    path = _file()
    if not os.path.exists(path):
        return []
    with open(path, encoding="utf-8") as handle:
        return [json.loads(line) for line in handle if line.strip()]


def _read_journal(limit: int, project: str | None, user: str | None) -> list[dict]:
    # _UID is set by journald from the sending socket's credentials. Matching on it is
    # what makes an entry forged by another process (same identifier, same fields)
    # invisible here.
    args = [JOURNALCTL, "--no-pager", "-o", "json", "-r", "-n", str(limit),
            "-t", IDENTIFIER, f"_UID={os.getuid()}"]
    if project:
        args.append(f"AUDIT_PROJECT={project}")
    if user:
        args.append(f"AUDIT_USER={user}")
    try:
        out = subprocess.run(args, capture_output=True, text=True, timeout=30).stdout
    except (OSError, subprocess.SubprocessError):
        return []
    records = []
    for line in out.splitlines():
        try:
            raw = json.loads(line)
        except ValueError:
            continue
        raw["at"] = int(raw.get("__REALTIME_TIMESTAMP", 0)) / 1_000_000
        records.append(raw)
    return records
