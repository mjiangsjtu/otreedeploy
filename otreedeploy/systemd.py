"""systemd control for oTree instances.

The app runs unprivileged and reaches systemd through a narrow sudoers rule. The unit
template ``otree@.service`` is installed once by bootstrap.sh; this module never writes
to /etc, it only instantiates and controls the template.

"Enabled" is the definition of the active project: exactly one instance is enabled at a
time, so a reboot brings back whichever study was live.

Journal reading does not use sudo -- bootstrap puts the service user in the
``systemd-journal`` group instead.
"""

from __future__ import annotations

import shutil
import subprocess
import threading
import time
from dataclasses import dataclass, field
from typing import Iterator

from typing import TYPE_CHECKING

from . import config
from .projects import unit_name, validate

if TYPE_CHECKING:  # pragma: no cover
    from . import otreeapi

# The single privileged command this app may run. It validates its own arguments and is
# root-owned, so the sudoers grant needs no wildcards. See deploy/otree-ctl.
OTREE_CTL = "/usr/local/sbin/otree-ctl"
JOURNALCTL = "/usr/bin/journalctl"


class SystemdError(Exception):
    pass


@dataclass
class _FakeState:
    """Stands in for systemd when OTREEDEPLOY_DRY_RUN is set.

    ``instances`` is only populated when the dry run was given a port to serve on; see
    ``config.dry_run_serves``. Otherwise the sets alone are the whole simulation.
    """

    active: set[str] = field(default_factory=set)
    enabled: set[str] = field(default_factory=set)
    instances: dict[str, "otreeapi.Instance"] = field(default_factory=dict)


_fake = _FakeState()

# Every systemctl query costs a sudo + exec. The listing asks about active/enabled state
# for each project several times while rendering one page, so answers are held very
# briefly -- long enough to collapse a single render, too short to show stale state after
# an action. Any mutating call clears it outright.
_CACHE_TTL = 1.5
_cache: dict[str, tuple[float, bool]] = {}
_cache_lock = threading.Lock()


def _cached(key: str, compute) -> bool:
    now = time.monotonic()
    with _cache_lock:
        hit = _cache.get(key)
        if hit and now - hit[0] < _CACHE_TTL:
            return hit[1]
    value = compute()
    with _cache_lock:
        _cache[key] = (now, value)
    return value


def invalidate() -> None:
    with _cache_lock:
        _cache.clear()


def _run(args: list[str], check: bool = True) -> subprocess.CompletedProcess:
    cmd = ["sudo", "-n", OTREE_CTL, *args]
    result = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
    if check and result.returncode != 0:
        raise SystemdError(
            f"`{' '.join(cmd)}` failed (exit {result.returncode}): "
            f"{(result.stderr or result.stdout).strip()}"
        )
    return result


def daemon_reload() -> None:
    if config.dry_run():
        return
    _run(["daemon-reload"])


def _fake_stop(slug: str) -> None:
    _fake.active.discard(slug)
    instance = _fake.instances.pop(slug, None)
    if instance is not None:
        instance.stop()


def _fake_start(slug: str) -> None:
    """Record the unit as active and, when the dry run serves, really boot it.

    Imports are local: ``deploy`` imports this module, so pulling it in at import time
    would be a cycle. By the time anything can call this, both modules are loaded.
    """
    _fake_stop(slug)
    _fake.active.add(slug)
    if not config.dry_run_serves():
        return

    from . import db, deploy, otreeapi

    project = db.get_project(slug)
    release_dir = deploy.current_release_dir(slug)
    if project is None or release_dir is None:
        _fake.active.discard(slug)
        raise SystemdError(f"{slug} has no built release to start")
    try:
        _fake.instances[slug] = otreeapi.launch(
            project, release_dir, config.otree_port(), host=config.OTREE_BIND_HOST
        )
    except otreeapi.OtreeError as exc:
        _fake.active.discard(slug)
        raise SystemdError(str(exc)) from exc


def start(slug: str) -> None:
    validate(slug)
    if config.dry_run():
        _fake_start(slug)
        return
    _run(["start", slug])
    invalidate()


def stop(slug: str) -> None:
    validate(slug)
    if config.dry_run():
        _fake_stop(slug)
        return
    _run(["stop", slug])
    invalidate()


def restart(slug: str) -> None:
    validate(slug)
    if config.dry_run():
        _fake_start(slug)
        return
    _run(["restart", slug])
    invalidate()


def enable(slug: str) -> None:
    validate(slug)
    if config.dry_run():
        _fake.enabled.add(slug)
        return
    _run(["enable", slug])
    invalidate()


def disable(slug: str) -> None:
    validate(slug)
    if config.dry_run():
        _fake.enabled.discard(slug)
        return
    _run(["disable", slug])
    invalidate()


def is_active(slug: str) -> bool:
    validate(slug)
    if config.dry_run():
        # A served dry run can have the process die under it, exactly as systemd would
        # notice. Report what is true rather than what was last asked for.
        instance = _fake.instances.get(slug)
        if instance is not None and not instance.alive:
            _fake_stop(slug)
        return slug in _fake.active
    # `is-active` exits non-zero for inactive units, which is not an error here.
    return _run(["is-active", slug], check=False).stdout.strip() == "active"


def is_enabled(slug: str) -> bool:
    validate(slug)
    if config.dry_run():
        return slug in _fake.enabled
    return _cached(
        f"enabled:{slug}",
        lambda: _run(["is-enabled", slug], check=False).stdout.strip() == "enabled",
    )


def show(slug: str, *properties: str) -> dict[str, str]:
    """Read unit properties, e.g. ActiveState, SubState, ExecMainStartTimestamp."""
    validate(slug)
    if config.dry_run():
        return {
            "ActiveState": "active" if slug in _fake.active else "inactive",
            "SubState": "running" if slug in _fake.active else "dead",
        }
    result = _run(["show", slug, *properties], check=False)
    values: dict[str, str] = {}
    for line in result.stdout.splitlines():
        if "=" in line:
            key, _, value = line.partition("=")
            values[key] = value
    return values


def enabled_slugs(known: list[str]) -> list[str]:
    """Which of the known projects are enabled (i.e. own port 80 across reboots)."""
    return [slug for slug in known if is_enabled(slug)]


def journal(slug: str, lines: int = 200) -> str:
    validate(slug)
    if config.dry_run():
        return f"(dry run: no journal for {slug})\n"
    journalctl = shutil.which("journalctl") or JOURNALCTL
    result = subprocess.run(
        [journalctl, "-u", unit_name(slug), "-n", str(lines), "--no-pager"],
        capture_output=True,
        text=True,
        timeout=30,
    )
    return result.stdout or result.stderr


def follow_journal(slug: str, lines: int = 100) -> Iterator[str]:
    """Yield journal lines as they arrive, for the live log view."""
    validate(slug)
    if config.dry_run():
        yield f"(dry run: no journal for {slug})"
        return
    journalctl = shutil.which("journalctl") or JOURNALCTL
    process = subprocess.Popen(
        [journalctl, "-u", unit_name(slug), "-n", str(lines), "-f", "--no-pager"],
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        bufsize=1,
    )
    try:
        assert process.stdout is not None
        for line in process.stdout:
            yield line.rstrip("\n")
    finally:
        process.terminate()
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:  # pragma: no cover
            process.kill()


def reset_fake_state() -> None:
    """Test helper; also the shutdown path for a served dry run."""
    for slug in list(_fake.instances):
        _fake_stop(slug)
    _fake.active.clear()
    _fake.enabled.clear()
    invalidate()
