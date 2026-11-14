"""Running uploaded code -- always as the project's own user, never as this app.

An uploaded project is arbitrary Python written by somebody else, and it runs at more
moments than just "live": an sdist's build hooks run while its virtualenv is built,
`otree resetdb` imports the models, and the smoke test and every data export boot the
whole project. This app holds the management password, every project's data and its
own source code, so none of that may run as this app's user.

In a real install, each function here asks the root wrapper ``otree-ctl`` to do the
work as the project's own system user (see ``projects.project_user``), with the same
systemd hardening as the live service. That user owns the project's database and can
reach nothing else: not this app's files, not other projects' code or data.

In a dry run there are no such users, so the same steps run locally as before. The
behaviour the unit tests see is unchanged; what they cannot see -- the identity the code
runs under -- is exercised by tests/e2e.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path
from typing import Callable

from . import builder, config, postgres, projects, systemd

Logger = Callable[[str], None]

JOURNALCTL = "/usr/bin/journalctl"


class SandboxError(Exception):
    pass


def _ctl(*args: str, timeout: float = 300) -> str:
    try:
        return systemd._run([*args], timeout=timeout).stdout
    except systemd.SystemdError as exc:
        raise SandboxError(str(exc)) from exc


def _ctl_stream(args: list[str], log: Logger) -> int:
    """Run an otree-ctl verb, passing its output line by line to the deploy log."""
    process = subprocess.Popen(
        ["sudo", "-n", systemd.OTREE_CTL, *args],
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        stdin=subprocess.DEVNULL,
        text=True,
        bufsize=1,
    )
    assert process.stdout is not None
    for line in process.stdout:
        log(line.rstrip("\n"))
    return process.wait()


def _release_name(slug: str, release_dir: Path) -> str:
    """The release's name, after checking it really is one of this project's releases."""
    expected_parent = config.get_settings().releases_dir(slug)
    if release_dir.parent != expected_parent:
        raise SandboxError(f"{release_dir} is not a release of '{slug}'")
    return release_dir.name


# --------------------------------------------------------------------- identity


def provision(slug: str) -> None:
    """Create (or repair) the project's user, database, directories and unit identity."""
    projects.validate(slug)
    if config.dry_run():
        config.get_settings().releases_dir(slug).mkdir(parents=True, exist_ok=True)
        postgres.create_database(slug)
        return
    _ctl("provision", slug)


def unprovision(slug: str) -> None:
    """Remove the project's units, database, user and files. Backups are not touched."""
    projects.validate(slug)
    if config.dry_run():
        postgres.drop_database(slug)
        shutil.rmtree(config.get_settings().project_dir(slug), ignore_errors=True)
        return
    _ctl("unprovision", slug)


# ------------------------------------------------------------ running its code


def build(slug: str, release_dir: Path, log: Logger) -> None:
    """Build the release's virtualenv from its requirements.txt."""
    if config.dry_run():
        builder.build_venv(release_dir, log)
        return
    release = _release_name(slug, release_dir)
    log(f"$ uv venv .venv && uv pip install -r requirements.txt"
        f"   (as {projects.project_user(slug)}, confined)")
    code = _ctl_stream(["build", slug, release], log)
    if code != 0:
        raise builder.BuildError(
            f"building the virtualenv failed (exit {code}); see the log above for details"
        )


def resetdb(slug: str, release_dir: Path, env: dict[str, str], log: Logger) -> None:
    """Create oTree's tables. ``env`` is used only in a dry run; a real install reads
    the project's own environment file, exactly as the live service does."""
    if config.dry_run():
        otree = builder.venv_otree(release_dir)
        process = subprocess.Popen(
            [str(otree), "resetdb", "--noinput"],
            cwd=release_dir,
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1,
        )
        assert process.stdout is not None
        for line in process.stdout:
            log(f"  {line.rstrip()}")
        code = process.wait()
    else:
        code = _ctl_stream(
            ["resetdb", slug, _release_name(slug, release_dir)], lambda l: log(f"  {l}")
        )
    if code != 0:
        raise SandboxError("otree resetdb failed; the database schema could not be created")


def clean(slug: str, release_dir: Path) -> None:
    """Remove what the project's user created inside a release, so it can be deleted.

    Best effort: a release that cannot be cleaned is left on disk, which wastes space
    but harms nothing.
    """
    if config.dry_run() or not release_dir.is_dir():
        return
    try:
        _ctl("clean", slug, _release_name(slug, release_dir))
    except SandboxError:
        pass


# --------------------------------------------------------------- private boots


def boot_unit(slug: str, port: int) -> str:
    return f"otree-boot-{slug}-{port}.service"


def boot(slug: str, release_dir: Path, port: int) -> str:
    """Start the release on a loopback port as a transient unit; returns the unit name.

    Raises SandboxError if the port's unit name is already taken, which is how two
    boots are kept off the same port.
    """
    _ctl("boot", slug, _release_name(slug, release_dir), str(port))
    return boot_unit(slug, port)


def unboot(slug: str, port: int) -> None:
    try:
        _ctl("unboot", slug, str(port), timeout=60)
    except SandboxError:
        pass  # already gone


def unboot_all() -> None:
    """Stop private boots a previous run of this app left behind."""
    if config.dry_run():
        return
    try:
        _ctl("unboot-all", timeout=60)
    except SandboxError:
        pass


def boot_alive(unit: str) -> bool:
    # Reading unit state needs no privilege.
    result = subprocess.run(
        ["systemctl", "is-active", "--quiet", unit], capture_output=True, timeout=30
    )
    return result.returncode == 0


def boot_log(unit: str, lines: int = 40) -> str:
    try:
        result = subprocess.run(
            [JOURNALCTL, "-u", unit, "-n", str(lines), "--no-pager", "-o", "cat"],
            capture_output=True,
            text=True,
            timeout=30,
        )
    except (OSError, subprocess.SubprocessError):
        return ""
    return result.stdout.strip()
