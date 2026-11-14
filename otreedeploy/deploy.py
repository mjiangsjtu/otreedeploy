"""The deploy pipeline and the project lifecycle operations.

Everything that can disrupt a running experiment funnels through here so that the same
two protections always apply: refuse to interrupt participants who are mid-session
unless explicitly forced, and take a database backup before anything destructive.
"""

from __future__ import annotations

import contextlib
import functools
import queue
import shutil
import threading
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Iterator

from . import (
    accounts,
    archive,
    builder,
    config,
    db,
    environment,
    health,
    otreeapi,
    postgres,
    projects,
    sandbox,
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
    status: str = "running"  # running | success | failed | blocked | live_down
    user: str | None = None  # who started it; they may follow it even before membership
    # Set when the failure took the live experiment down with it, so the page does not
    # tell the experimenter that "nothing was changed for participants".
    live_down: bool = False
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


def new_job(title: str, slug: str | None = None, user: str | None = None) -> Job:
    job = Job(id=uuid.uuid4().hex[:12], slug=slug, title=title, user=user)
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


def _new_release_name(slug: str) -> str:
    """A timestamp, with a suffix if this second is already taken (m6).

    The suffix sorts after the plain name and before the next second, so releases
    still sort in the order they were made.
    """
    base = time.strftime("%Y%m%d-%H%M%S")
    candidate, n = base, 1
    while (config.get_settings().release_dir(slug, candidate).exists()
           or db.get_release(slug, candidate) is not None):
        candidate = f"{base}-{n}"
        n += 1
    return candidate


# ------------------------------------------------------------------------- one at a time


class Busy(DeployError):
    """Another action on the same project is still running."""


_locks: dict[str, threading.Lock] = {}
_locks_guard = threading.Lock()


@contextlib.contextmanager
def _exclusive(*slugs: str | None):
    """Hold the projects' locks for the duration, or refuse at once (m6).

    Two deploys, a deploy and a reset, or a reset and a restore of the same project
    would otherwise interleave -- both repointing `current`, both restarting the
    service. Refusing is better than queueing: the second person is told why instead
    of waiting on a page that seems to hang. Locks are taken in a fixed order, so two
    actions that each need two projects cannot deadlock.
    """
    held: list[threading.Lock] = []
    try:
        for name in sorted({s for s in slugs if s}):
            with _locks_guard:
                lock = _locks.setdefault(name, threading.Lock())
            if not lock.acquire(blocking=False):
                raise Busy(
                    f"something else is already being done to '{name}' -- a deploy, "
                    "reset or restore, perhaps by a colleague. Try again once it has "
                    "finished."
                )
            held.append(lock)
        yield
    finally:
        for lock in reversed(held):
            lock.release()


def _one_at_a_time(func):
    @functools.wraps(func)
    def wrapper(slug, *args, **kwargs):
        with _exclusive(slug):
            return func(slug, *args, **kwargs)

    return wrapper


def deploy(
    job: Job,
    archive_path: Path,
    filename: str,
    slug: str | None = None,
    force: bool = False,
) -> str:
    """Run the whole pipeline. Returns the slug. Raises DeployError on failure."""
    if slug is None:
        existing = {p.slug for p in db.list_projects()}
        desired = projects.slugify(filename)
        slug = desired if desired in existing else projects.unique_slug(desired, existing)
    with _exclusive(slug):
        return _deploy(job, archive_path, filename, slug, force)


def _deploy(job: Job, archive_path: Path, filename: str, slug: str, force: bool) -> str:
    settings = config.get_settings()
    log = job.log

    # 1. Identify the project ------------------------------------------------------
    existing = {p.slug for p in db.list_projects()}
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

    # The project's own system user, database role and database. Everything its code
    # does from here on -- build hooks, resetdb, the smoke test -- runs as that user.
    try:
        sandbox.provision(slug)
    except sandbox.SandboxError as exc:
        raise DeployError(f"could not prepare the project's user and database: {exc}") from exc
    if not config.dry_run():
        log(f"Project runs as its own user: {projects.project_user(slug)}")

    release = _new_release_name(slug)
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
        _open_to_project(release_dir)

        # 3. Normalize -------------------------------------------------------------
        normalization = builder.normalize_release(release_dir)
        for note in normalization.notes:
            log(f"  {note}")

        # 4. Build the virtualenv --------------------------------------------------
        log("Building the virtualenv with uv…")
        sandbox.build(slug, release_dir, log)
        version = builder.otree_version(release_dir)
        packages = builder.installed_packages(release_dir)
        lock = upload_copy.with_suffix(".packages.txt")
        lock.write_text(
            f"# Exactly what release {release} of {slug} was built with.\n"
            "# To rebuild it this way, use this file as the project's requirements.txt.\n"
            + "".join(f"{p}\n" for p in packages)
        )
        log(f"  recorded the {len(packages)} installed packages beside the upload")
        log(f"  oTree {version or 'version unknown'}")

        # 5. Database and environment ----------------------------------------------
        postgres.prepare_release(slug, release_dir)
        environment.write(project)
        log("Wrote the service environment file")

        if not postgres.has_otree_tables(slug):
            log("Empty database: creating oTree's tables (otree resetdb)…")
            _run_resetdb(project, release_dir, log)
        else:
            log("Existing database kept — your collected data is untouched")
            _reconcile_schema(project, release_dir, log)

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
                job.live_down = True
                if previous:
                    log(f"Rolling back to {previous.name}…")
                    _point_current_at(slug, previous)
                    try:
                        _restart_and_verify(slug)
                        job.live_down = False
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
            sandbox.clean(slug, release_dir)
            shutil.rmtree(release_dir, ignore_errors=True)
        raise


def _open_to_project(release_dir: Path) -> None:
    """Let the project's own user write inside its release.

    The build runs as that user and writes here: its .venv, and any local package listed
    in requirements.txt is built in place. The group is already the project's, inherited
    from the setgid project directory; this adds group write. Directories also lose the
    setgid bit: anything the project user created in them would inherit it, and its
    hardening (RestrictSUIDSGID) then refuses the chmods a build performs. No other
    project can reach any of this -- the project directory is closed to them.
    """
    if config.dry_run():
        return
    release_dir.chmod(0o770)
    for path in release_dir.rglob("*"):
        if path.is_symlink():
            continue
        if path.is_dir():
            path.chmod(0o770)
        else:
            path.chmod((path.stat().st_mode & 0o777) | 0o060)


def _display_name(filename: str) -> str:
    stem = filename.rsplit("/", 1)[-1].rsplit("\\", 1)[-1]
    for suffix in (".otreezip", ".tar.gz", ".tgz", ".zip"):
        if stem.lower().endswith(suffix):
            return stem[: -len(suffix)]
    return stem


def _run_resetdb(project: Project, release_dir: Path, log: Callable[[str], None]) -> None:
    """Create oTree's tables, as the project's own user -- so they are also owned by
    the project's role, which the experiment connects as."""
    try:
        sandbox.resetdb(
            project.slug,
            release_dir,
            environment.subprocess_env(project, config.otree_port(), release_dir),
            log,
        )
    except sandbox.SandboxError as exc:
        raise DeployError(str(exc)) from exc


class SchemaChangeRefused(DeployError):
    pass


def _reconcile_schema(project: Project, release_dir: Path, log: Callable[[str], None]) -> None:
    """Make the existing database fit the new code, or refuse before anything goes live.

    oTree has no migrations and its start-up never compares columns, so without this a
    new model field deploys "successfully" and then fails for every participant.
    """
    log("Comparing the new code with the existing database…")
    env = environment.subprocess_env(project, config.otree_port(), release_dir)
    try:
        report = sandbox.schema(project.slug, release_dir, env, "check", log)
    except sandbox.SandboxError as exc:
        raise DeployError(str(exc)) from exc

    if report["blocking"]:
        problems = "\n".join(f"  - {item}" for item in report["blocking"])
        raise SchemaChangeRefused(
            "this version changes the database in a way the data already collected "
            f"cannot follow:\n{problems}\n\n"
            "Nothing was changed. Either keep the old field definitions, or use "
            "“Reset database” on the project page (a backup is taken first, and the "
            "collected data is deleted), or upload this version as a new project."
        )
    for name in report["new_tables"]:
        log(f"  new table {name} (created when it starts)")
    for name in report["removed"]:
        log(f"  {name} is no longer used by this version; its data is kept")
    if not report["added"]:
        log("  the database already fits this version")
        return
    try:
        sandbox.schema(project.slug, release_dir, env, "apply", log)
    except sandbox.SandboxError as exc:
        raise DeployError(str(exc)) from exc
    for change in report["added"]:
        log(f"  added column {change['table']}.{change['column']} "
            "(empty for rows collected before)")


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
    """Restart the project's unit and wait until *this project* serves on the live port.

    Every way of failing raises DeployError -- including systemctl itself refusing,
    which happens at once when the unit cannot even be executed -- so callers that roll
    back on failure always get the chance to.
    """
    try:
        systemd.restart(slug)
    except systemd.SystemdError as exc:
        raise DeployError(f"'{slug}' could not be started: {exc}") from exc
    if config.dry_run() and not config.dry_run_serves():
        return
    project = db.get_project(slug)
    port = config.otree_port()
    if project is None or not health.wait_for(
        lambda: otreeapi.serves_project(port, project.rest_key)
    ):
        raise DeployError(
            f"'{slug}' did not start serving on port {port} within "
            f"{config.BOOT_TIMEOUT_SECONDS} seconds. Either the project fails while "
            f"starting, or another program is using port {port}; its log on the project "
            "page shows which."
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
        sandbox.clean(slug, stale)
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
    with _exclusive(slug, incumbent):
        _activate(slug, incumbent, force)


def _activate(slug: str, incumbent: str | None, force: bool) -> None:
    if incumbent == slug and systemd.is_active(slug):
        return
    if incumbent and incumbent != slug:
        guard_disruption(incumbent, force)
        systemd.stop(incumbent)
        systemd.disable(incumbent)
        db.log_event("deactivate", f"stopped to make room for {slug}", incumbent)

    systemd.enable(slug)
    try:
        _restart_and_verify(slug)
    except DeployError as exc:
        # Put things back as they were: nothing is worse than an experiment that was
        # serving fine being stopped for one that cannot start.
        systemd.stop(slug)
        systemd.disable(slug)
        db.log_event("activate_failed", str(exc), slug)
        if not incumbent or incumbent == slug:
            raise
        systemd.enable(incumbent)
        try:
            _restart_and_verify(incumbent)
        except DeployError:
            raise DeployError(
                f"{exc} Restarting '{incumbent}' failed as well, so nothing is serving on "
                f"port {config.otree_port()} now."
            ) from exc
        db.log_event("activate", f"restored after '{slug}' failed to start", incumbent)
        raise DeployError(f"{exc} '{incumbent}' is serving again, as before.") from exc
    db.log_event("activate", f"now serving on port {config.otree_port()}", slug)


@_one_at_a_time
def stop(slug: str, force: bool = False) -> None:
    projects.validate(slug)
    # A unit that is not running interrupts nobody, so it needs no guard.
    if systemd.is_active(slug):
        guard_disruption(slug, force)
    systemd.stop(slug)
    systemd.disable(slug)
    db.log_event("stop", "stopped", slug)


@_one_at_a_time
def restart(slug: str, force: bool = False) -> None:
    projects.validate(slug)
    # Restarting a unit that is down (say, after PostgreSQL went away) interrupts
    # nobody; refusing it would only keep the study down longer.
    if systemd.is_active(slug):
        guard_disruption(slug, force)
    _restart_and_verify(slug)
    db.log_event("restart", "restarted", slug)


@_one_at_a_time
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


@_one_at_a_time
def reset_database(slug: str, force: bool = False) -> Path | None:
    """Wipe and recreate the schema. Always takes a backup first."""
    project = db.get_project(slug)
    if project is None:
        raise DeployError(f"unknown project '{slug}'")
    guard_disruption(slug, force)

    # Check everything that can fail cheaply before touching the live service, so a
    # refused reset leaves the experiment exactly as it was.
    release_dir = current_release_dir(slug)
    if release_dir is None:
        raise DeployError(f"'{slug}' has no deployed release")

    snapshot = backup(slug, "before-reset")

    # resetdb blocks on any other open connection, so clear them out first.
    was_running = systemd.is_active(slug)
    otreeapi.pool.release(slug)
    job = new_job(f"Resetting {slug}", slug)
    try:
        if was_running:
            systemd.stop(slug)
        sandbox.provision(slug)
        _run_resetdb(project, release_dir, job.log)
        job.finish("success")
    except sandbox.SandboxError as exc:
        job.finish("failed", str(exc))
        raise DeployError(str(exc)) from exc
    except Exception as exc:
        job.finish("failed", str(exc))
        raise
    finally:
        if was_running:
            systemd.start(slug)

    db.log_event("reset_db", f"database reset (backup: {snapshot})", slug)
    return snapshot


def backup_path(slug: str, name: str) -> Path:
    """A backup of this project by file name, refusing anything that is not one."""
    directory = config.get_settings().backup_dir(slug)
    candidate = directory / name
    if "/" in name or not name.endswith(".sql.gz") or candidate.parent != directory \
            or not candidate.is_file():
        raise DeployError(f"there is no backup called '{name}' for '{slug}'")
    return candidate


@_one_at_a_time
def restore_backup(slug: str, name: str, force: bool = False) -> Path | None:
    """Put a backup back in place of the current data. Takes a backup first, so the
    restore itself can be undone the same way."""
    projects.validate(slug)
    if db.get_project(slug) is None:
        raise DeployError(f"unknown project '{slug}'")
    source = backup_path(slug, name)
    guard_disruption(slug, force)

    snapshot = backup(slug, "before-restore")
    otreeapi.pool.release(slug)
    was_running = systemd.is_active(slug)
    try:
        if config.dry_run() and was_running:
            systemd.stop(slug)
        sandbox.restore(slug, source)
    except sandbox.SandboxError as exc:
        raise DeployError(f"the backup was not restored: {exc}") from exc
    finally:
        # A real install stops and starts the service inside otree-ctl itself.
        if config.dry_run() and was_running:
            systemd.start(slug)
    db.log_event("restore", f"restored {name} (previous data saved as {snapshot})", slug)
    return snapshot


@_one_at_a_time
def delete_project(slug: str, force: bool = False, purge: bool = False) -> Path | None:
    """Remove a project entirely.

    Without ``purge``, one last backup is taken, and the project's backups and stored
    uploads move to ``backups/_deleted/<slug>-<time>/``: out of reach of a new project
    that reuses the name, visible to admins, and subject to the retention periods.
    Returns where that last backup now is.

    With ``purge``, nothing of the project's data is kept -- no last backup, no
    earlier backups, no uploads. That is what a request to erase the data needs.
    """
    projects.validate(slug)
    guard_disruption(slug, force)

    snapshot = None if purge else backup(slug, "before-delete")
    otreeapi.pool.release(slug)
    if systemd.is_active(slug):
        systemd.stop(slug)
    if systemd.is_enabled(slug):
        systemd.disable(slug)

    # Database, role, system user, unit identity and files.
    sandbox.unprovision(slug)
    db.delete_project(slug)
    accounts.forget_project(slug)

    settings = config.get_settings()
    kept = [d for d in (settings.backup_dir(slug), settings.upload_dir(slug)) if d.exists()]
    if purge:
        for directory in kept:
            shutil.rmtree(directory)
        db.log_event("delete", "project deleted, with all its backups and uploads", slug)
        return None

    archive = settings.backups_dir / "_deleted" / f"{slug}-{time.strftime('%Y%m%d-%H%M%S')}"
    archive.mkdir(parents=True)
    for directory, name in ((settings.backup_dir(slug), "backups"),
                            (settings.upload_dir(slug), "uploads")):
        if directory.exists():
            directory.rename(archive / name)
    if snapshot is not None:
        snapshot = archive / "backups" / snapshot.name
    db.log_event("delete", f"project deleted (backups kept in {archive.name})", slug)
    return snapshot
