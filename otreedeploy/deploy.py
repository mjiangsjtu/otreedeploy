"""The deploy pipeline and the project lifecycle operations.

Everything that can disrupt a running experiment funnels through here so that the same
two protections always apply: refuse to interrupt participants who are mid-session
unless explicitly forced, and take a database backup before anything destructive.
"""

from __future__ import annotations

import queue
import shutil
import subprocess
import threading
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Iterator

from . import (
    archive,
    builder,
    config,
    db,
    environment,
    health,
    otreeapi,
    postgres,
    projects,
    systemd,
)
from .db import Project

SENTINEL = "\0__done__"


class DeployError(Exception):
    pass


class ParticipantsActive(DeployError):
    """Raised when an action would interrupt people who are currently in a session."""

    def __init__(self, slug: str, count: int) -> None:
        self.slug = slug
        self.count = count
        super().__init__(
            f"{count} participant{'s are' if count != 1 else ' is'} currently active in "
            f"'{slug}'. Interrupting now would lose their progress."
        )


# ------------------------------------------------------------------------------- jobs


@dataclass
class Job:
    """A running deploy, with a log that the browser tails over SSE."""

    id: str
    slug: str | None = None
    title: str = "Deploying"
    lines: list[str] = field(default_factory=list)
    status: str = "running"  # running | success | failed
    error: str | None = None
    started_at: float = field(default_factory=time.time)
    _subscribers: list[queue.Queue] = field(default_factory=list)
    _lock: threading.Lock = field(default_factory=threading.Lock)

    def log(self, line: str) -> None:
        with self._lock:
            self.lines.append(line)
            subscribers = list(self._subscribers)
        for subscriber in subscribers:
            subscriber.put(line)

    def finish(self, status: str, error: str | None = None) -> None:
        with self._lock:
            self.status = status
            self.error = error
            subscribers = list(self._subscribers)
        for subscriber in subscribers:
            subscriber.put(SENTINEL)

    def subscribe(self) -> Iterator[str]:
        """Replay what has happened so far, then follow along live."""
        channel: queue.Queue = queue.Queue()
        with self._lock:
            backlog = list(self.lines)
            finished = self.status != "running"
            self._subscribers.append(channel)
        try:
            for line in backlog:
                yield line
            if finished:
                return
            while True:
                line = channel.get()
                if line == SENTINEL:
                    return
                yield line
        finally:
            with self._lock:
                if channel in self._subscribers:
                    self._subscribers.remove(channel)


_jobs: dict[str, Job] = {}
_jobs_lock = threading.Lock()


def new_job(title: str, slug: str | None = None) -> Job:
    job = Job(id=uuid.uuid4().hex[:12], slug=slug, title=title)
    with _jobs_lock:
        _jobs[job.id] = job
        # Keep the registry from growing without bound across a long-lived process.
        if len(_jobs) > 50:
            for stale in sorted(_jobs.values(), key=lambda j: j.started_at)[:10]:
                if stale.status != "running":
                    _jobs.pop(stale.id, None)
    return job


def get_job(job_id: str) -> Job | None:
    with _jobs_lock:
        return _jobs.get(job_id)


# ------------------------------------------------------------------------------ state


def active_slug() -> str | None:
    """The project that owns port 80. systemd's 'enabled' flag is the source of truth,
    so the answer survives a reboot and any out-of-band systemctl use."""
    for project in db.list_projects():
        if systemd.is_enabled(project.slug):
            return project.slug
    return None


def status_of(slug: str) -> str:
    if systemd.is_active(slug):
        return "running"
    if systemd.is_enabled(slug):
        return "enabled"
    return "stopped"


def current_release_dir(slug: str) -> Path | None:
    link = config.get_settings().current_link(slug)
    if not link.exists():
        return None
    return link.resolve()


def guard_disruption(slug: str, force: bool) -> None:
    """Refuse to disturb a study with people currently in it, unless forced."""
    if force:
        return
    count = postgres.count_active_participants(slug)
    if count:
        raise ParticipantsActive(slug, count)


def backup(slug: str, reason: str) -> Path | None:
    """Snapshot the database. Called before every destructive action, unconditionally."""
    if not postgres.database_exists(slug):
        return None
    settings = config.get_settings()
    stamp = time.strftime("%Y%m%d-%H%M%S")
    destination = settings.backup_dir(slug) / f"{stamp}-{reason}.sql.gz"
    return postgres.dump(slug, destination)


# ------------------------------------------------------------------------------ deploy


def _new_release_name() -> str:
    return time.strftime("%Y%m%d-%H%M%S")


def deploy(
    job: Job,
    archive_path: Path,
    filename: str,
    slug: str | None = None,
    force: bool = False,
) -> str:
    """Run the whole pipeline. Returns the slug. Raises DeployError on failure."""
    settings = config.get_settings()
    log = job.log

    # 1. Identify the project ------------------------------------------------------
    existing = {p.slug for p in db.list_projects()}
    if slug is None:
        desired = projects.slugify(filename)
        slug = desired if desired in existing else projects.unique_slug(desired, existing)
    projects.validate(slug)
    job.slug = slug

    is_new = slug not in existing
    log(f"Project: {slug} ({'new' if is_new else 'existing'})")

    if not is_new and status_of(slug) == "running":
        guard_disruption(slug, force)

    # 2. Vet and extract -----------------------------------------------------------
    log("Checking the uploaded file…")
    try:
        inspection = archive.inspect(archive_path)
    except archive.ArchiveError as exc:
        raise DeployError(str(exc)) from exc
    log(
        f"  looks like an oTree project: {inspection.file_count} files, "
        f"{inspection.total_size / 1024:.0f} KB"
    )

    digest = archive.sha256(archive_path)
    release = _new_release_name()
    release_dir = settings.release_dir(slug, release)

    project = db.get_project(slug) or db.create_project(slug, _display_name(filename))
    release_id = db.create_release(slug, release, filename, digest)

    # Keep the upload verbatim, so a deploy can always be reproduced from what arrived.
    upload_copy = settings.upload_dir(slug) / f"{release}.otreezip"
    upload_copy.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(archive_path, upload_copy)

    try:
        log(f"Extracting to {release_dir}…")
        release_dir.parent.mkdir(parents=True, exist_ok=True)
        archive.extract(archive_path, release_dir)

        # 3. Normalize -------------------------------------------------------------
        normalization = builder.normalize_release(release_dir)
        for note in normalization.notes:
            log(f"  {note}")

        # 4. Build the virtualenv --------------------------------------------------
        log("Building the virtualenv with uv…")
        builder.build_venv(release_dir, log)
        version = builder.otree_version(release_dir)
        log(f"  oTree {version or 'version unknown'}")

        # 5. Database and environment ----------------------------------------------
        if postgres.create_database(slug):
            log(f"Created database {projects.db_name(slug)}")
        postgres.prepare_release(slug, release_dir)
        environment.write(project)
        log("Wrote the service environment file")

        if not postgres.has_otree_tables(slug):
            log("Empty database: creating oTree's tables (otree resetdb)…")
            _run_resetdb(project, release_dir, log)
        else:
            log("Existing database kept — your collected data is untouched")
            repaired = postgres.align_ownership(slug)
            if repaired:
                log(f"  repaired ownership of {repaired} object(s) left by an older deploy")

        # 6. Smoke test ------------------------------------------------------------
        log("Starting a test copy on a private port to check it works…")
        _smoke_test(project, release_dir, log)
        log("  it starts and responds")

        # 7. Publish ---------------------------------------------------------------
        previous = current_release_dir(slug)
        _point_current_at(slug, release_dir)
        log(f"Published release {release}")

        if status_of(slug) in {"running", "enabled"}:
            log("Restarting the live service…")
            try:
                _restart_and_verify(slug)
                log(f"  live on port {config.otree_port()}")
            except DeployError as exc:
                log(f"  FAILED: {exc}")
                if previous:
                    log(f"Rolling back to {previous.name}…")
                    _point_current_at(slug, previous)
                    try:
                        _restart_and_verify(slug)
                        log("  rolled back; the previous version is serving again")
                    except DeployError as inner:
                        log(f"  rollback also failed: {inner}")
                raise

        db.finish_release(release_id, "success", "\n".join(job.lines), version)
        db.update_project(slug, current_release=release)
        db.log_event("deploy", f"deployed release {release}", slug)
        _prune_releases(slug, log)
        return slug

    except Exception as exc:
        db.finish_release(release_id, "failed", "\n".join(job.lines))
        db.log_event("deploy_failed", f"release {release}: {exc}", slug)
        # Leave a failed build on disk only if it is not the published one; otherwise it
        # is dead weight that will confuse the next person to look.
        if current_release_dir(slug) != release_dir:
            shutil.rmtree(release_dir, ignore_errors=True)
        raise


def _display_name(filename: str) -> str:
    stem = filename.rsplit("/", 1)[-1].rsplit("\\", 1)[-1]
    for suffix in (".otreezip", ".tar.gz", ".tgz", ".zip"):
        if stem.lower().endswith(suffix):
            return stem[: -len(suffix)]
    return stem


def _run_resetdb(project: Project, release_dir: Path, log: Callable[[str], None]) -> None:
    otree = builder.venv_otree(release_dir)
    process = subprocess.Popen(
        [str(otree), "resetdb", "--noinput"],
        cwd=release_dir,
        env=environment.subprocess_env(project, config.otree_port(), release_dir),
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        bufsize=1,
    )
    assert process.stdout is not None
    for line in process.stdout:
        log(f"  {line.rstrip()}")
    if process.wait() != 0:
        raise DeployError(
            "otree resetdb failed; the database schema could not be created"
        )
    # resetdb ran as this app's role, so the tables are owned by it rather than by the
    # user the experiment runs as. Hand them over before anything tries to serve.
    moved = postgres.align_ownership(project.slug)
    if moved:
        log(f"  handed {moved} object(s) to the '{postgres.OTREE_ROLE}' role")


def _smoke_test(project: Project, release_dir: Path, log: Callable[[str], None]) -> None:
    """Boot the new code privately before letting it near port 80."""
    port = health.free_port(*config.EPHEMERAL_PORT_RANGE)
    try:
        instance = otreeapi.launch(project, release_dir, port)
    except otreeapi.OtreeError as exc:
        raise DeployError(
            f"the uploaded project did not start.\n\n{exc}\n\n"
            "If you changed your models (added or removed fields), the existing database "
            "no longer matches your code — use “Reset database” on the project page, "
            "which takes a backup first."
        ) from exc
    instance.stop()


def _point_current_at(slug: str, release_dir: Path) -> None:
    """Atomically repoint the 'current' symlink."""
    link = config.get_settings().current_link(slug)
    link.parent.mkdir(parents=True, exist_ok=True)
    temporary = link.with_name(link.name + ".swap")
    if temporary.is_symlink() or temporary.exists():
        temporary.unlink()
    temporary.symlink_to(release_dir)
    temporary.replace(link)


def _restart_and_verify(slug: str) -> None:
    systemd.restart(slug)
    if config.dry_run() and not config.dry_run_serves():
        return
    if not health.wait_until_ready(config.otree_port()):
        raise DeployError(
            f"the service did not respond on port {config.otree_port()} after restarting"
        )


def _prune_releases(slug: str, log: Callable[[str], None]) -> None:
    settings = config.get_settings()
    releases_dir = settings.releases_dir(slug)
    if not releases_dir.exists():
        return
    keep_current = current_release_dir(slug)
    all_releases = sorted(
        (p for p in releases_dir.iterdir() if p.is_dir()), key=lambda p: p.name
    )
    for stale in all_releases[: -config.KEEP_RELEASES]:
        if stale == keep_current:
            continue
        shutil.rmtree(stale, ignore_errors=True)
        db.delete_release(slug, stale.name)
        log(f"Removed old release {stale.name}")


# --------------------------------------------------------------------------- lifecycle


def activate(slug: str, force: bool = False) -> None:
    """Give this project port 80, taking it from whoever holds it."""
    projects.validate(slug)
    if current_release_dir(slug) is None:
        raise DeployError(f"'{slug}' has no successful deploy to activate")

    incumbent = active_slug()
    if incumbent == slug and systemd.is_active(slug):
        return
    if incumbent and incumbent != slug:
        guard_disruption(incumbent, force)
        systemd.stop(incumbent)
        systemd.disable(incumbent)
        db.log_event("deactivate", f"stopped to make room for {slug}", incumbent)

    systemd.enable(slug)
    _restart_and_verify(slug)
    db.log_event("activate", f"now serving on port {config.otree_port()}", slug)


def stop(slug: str, force: bool = False) -> None:
    projects.validate(slug)
    guard_disruption(slug, force)
    systemd.stop(slug)
    systemd.disable(slug)
    db.log_event("stop", "stopped", slug)


def restart(slug: str, force: bool = False) -> None:
    projects.validate(slug)
    guard_disruption(slug, force)
    _restart_and_verify(slug)
    db.log_event("restart", "restarted", slug)


def rollback(slug: str, release: str, force: bool = False) -> None:
    projects.validate(slug)
    # `release` arrives from a form. Only a name this project itself recorded as a
    # successful build is accepted: joined into a path unchecked, "../../other/releases/x"
    # would point this project's `current` at another project's code.
    record = db.get_release(slug, release)
    if record is None or record.status != "success":
        raise DeployError(f"'{release}' is not a successful release of '{slug}'")
    settings = config.get_settings()
    release_dir = settings.release_dir(slug, release)
    if release_dir.parent != settings.releases_dir(slug) or not (release_dir / ".venv").exists():
        raise DeployError(f"release {release} is not on disk any more")
    guard_disruption(slug, force)

    _point_current_at(slug, release_dir)
    db.update_project(slug, current_release=release)
    if status_of(slug) in {"running", "enabled"}:
        _restart_and_verify(slug)
    db.log_event("rollback", f"rolled back to {release}", slug)


def reset_database(slug: str, force: bool = False) -> Path | None:
    """Wipe and recreate the schema. Always takes a backup first."""
    project = db.get_project(slug)
    if project is None:
        raise DeployError(f"unknown project '{slug}'")
    guard_disruption(slug, force)

    snapshot = backup(slug, "before-reset")

    # resetdb blocks on any other open connection, so clear them out first.
    was_running = systemd.is_active(slug)
    otreeapi.pool.release(slug)
    if was_running:
        systemd.stop(slug)

    release_dir = current_release_dir(slug)
    if release_dir is None:
        raise DeployError(f"'{slug}' has no deployed release")

    postgres.create_database(slug)
    job = new_job(f"Resetting {slug}", slug)
    try:
        _run_resetdb(project, release_dir, job.log)
        job.finish("success")
    except Exception as exc:
        job.finish("failed", str(exc))
        raise
    finally:
        if was_running:
            systemd.start(slug)

    db.log_event("reset_db", f"database reset (backup: {snapshot})", slug)
    return snapshot


def delete_project(slug: str, force: bool = False) -> Path | None:
    """Remove a project entirely, after one last backup."""
    projects.validate(slug)
    guard_disruption(slug, force)

    snapshot = backup(slug, "before-delete")
    otreeapi.pool.release(slug)
    if systemd.is_active(slug):
        systemd.stop(slug)
    if systemd.is_enabled(slug):
        systemd.disable(slug)

    postgres.drop_database(slug)
    settings = config.get_settings()
    shutil.rmtree(settings.project_dir(slug), ignore_errors=True)
    db.delete_project(slug)
    db.log_event("delete", f"project deleted (backup: {snapshot})", slug)
    return snapshot
