"""How long backups and stored uploads are kept, and the sweep that enforces it.

Backups and uploads hold participants' data (a backup is a full copy of the database),
so "kept forever" should be a decision, not an accident. Until an admin sets a period
on the System page nothing is deleted -- losing data by surprise is worse -- but the
page says plainly that no period is set.

The sweep never deletes the newest backup of a project that still exists: whatever the
setting, the last safety net stays. Files of deleted projects (``backups/_deleted``)
are swept by the same periods, with no such exception. Every deletion is recorded in
the audit trail.
"""

from __future__ import annotations

import shutil
import threading
import time
from pathlib import Path

from . import audit, config, db

BACKUP_KEY = "backup_retention_days"
UPLOAD_KEY = "upload_retention_days"
DELETED_DIR = "_deleted"
SWEEP_INTERVAL = 3600


def get() -> dict[str, int | None]:
    def days(key: str) -> int | None:
        value = db.get_setting(key)
        return int(value) if value else None

    return {"backups": days(BACKUP_KEY), "uploads": days(UPLOAD_KEY)}


def set_periods(backups: int | None, uploads: int | None) -> None:
    for key, value in ((BACKUP_KEY, backups), (UPLOAD_KEY, uploads)):
        if value is not None and value < 1:
            raise ValueError("a retention period is at least one day")
        db.set_setting(key, str(value) if value else None)


def _expired(directory: Path, days: int | None, now: float, keep_newest: bool) -> list[Path]:
    if days is None or not directory.is_dir():
        return []
    files = sorted((p for p in directory.iterdir() if p.is_file()),
                   key=lambda p: p.stat().st_mtime)
    if keep_newest and files:
        files = files[:-1]
    cutoff = now - days * 86400
    return [p for p in files if p.stat().st_mtime < cutoff]


def sweep(now: float | None = None) -> list[Path]:
    """Delete what has outlived its period. Returns the files removed."""
    now = time.time() if now is None else now
    periods = get()
    settings = config.get_settings()
    existing = {p.slug for p in db.list_projects()}
    removed: list[Path] = []

    for kind, root in (("backups", settings.backups_dir), ("uploads", settings.uploads_dir)):
        if not root.is_dir():
            continue
        for directory in sorted(root.iterdir()):
            if not directory.is_dir() or directory.name == DELETED_DIR:
                continue
            for path in _expired(directory, periods[kind], now,
                                 keep_newest=directory.name in existing):
                path.unlink(missing_ok=True)
                removed.append(path)
                audit.record("retention_delete", "system", directory.name,
                             f"{kind}/{path.name}")

    deleted_root = settings.backups_dir / DELETED_DIR
    if deleted_root.is_dir():
        for archive in sorted(deleted_root.iterdir()):
            for kind in ("backups", "uploads"):
                for path in _expired(archive / kind, periods[kind], now, keep_newest=False):
                    path.unlink(missing_ok=True)
                    removed.append(path)
                    audit.record("retention_delete", "system", archive.name,
                                 f"deleted project {kind}/{path.name}")
            if archive.is_dir() and not any(p.is_file() for p in archive.rglob("*")):
                shutil.rmtree(archive, ignore_errors=True)
    return removed


def deleted_archives() -> list[dict]:
    """What is left of deleted projects, for admins to see and remove."""
    root = config.get_settings().backups_dir / DELETED_DIR
    if not root.is_dir():
        return []
    archives = []
    for archive in sorted(root.iterdir(), reverse=True):
        files = [p for p in archive.rglob("*") if p.is_file()]
        archives.append({"name": archive.name, "files": len(files),
                         "size": sum(p.stat().st_size for p in files)})
    return archives


def remove_archive(name: str) -> None:
    root = config.get_settings().backups_dir / DELETED_DIR
    target = root / name
    if "/" in name or not name or target.parent != root or not target.is_dir():
        raise ValueError(f"there is no deleted project called '{name}'")
    shutil.rmtree(target)


_sweeper: threading.Thread | None = None


def start_sweeper() -> None:
    global _sweeper
    if _sweeper and _sweeper.is_alive():
        return

    def loop() -> None:
        while True:
            try:
                sweep()
            except Exception as exc:  # a failed sweep must not kill the thread
                print(f"otreedeploy: retention sweep failed: {exc!r}")
            time.sleep(SWEEP_INTERVAL)

    _sweeper = threading.Thread(target=loop, daemon=True, name="retention")
    _sweeper.start()
