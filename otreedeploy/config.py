"""Filesystem layout and tunables.

Every path is derived from ``OTREEDEPLOY_ROOT`` (default ``/``) so the whole app can be
pointed at a throwaway directory in tests without touching the real server layout.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

# Ports
OTREE_PORT = 80
DEFAULT_APP_PORT = 8000

# The live experiment binds 0.0.0.0 -- the systemd unit passes only a port to
# `otree prodserver`, and participants reach it from other machines.
OTREE_BIND_HOST = "0.0.0.0"

# Ephemeral loopback ports used when briefly booting a stopped project for data export.
#
# Deliberately below the kernel's own ephemeral range (net.ipv4.ip_local_port_range,
# 32768-60999 by default). Inside it, two things go wrong: an unrelated outbound
# connection can take the port between free_port() picking it and oTree binding it, and a
# readiness probe against the not-yet-bound port can be handed the same number as its
# source port and connect to itself.
EPHEMERAL_PORT_RANGE = (18000, 18900)

# How long an ephemerally-booted project stays up after its last download, so that
# several exports in a row reuse one boot instead of paying startup cost each time.
EPHEMERAL_IDLE_SECONDS = 120

# Readiness probing.
BOOT_TIMEOUT_SECONDS = 90
PROBE_PATH = "/favicon.ico"

# A participant that has hit the server within this window counts as "currently active",
# which is what blocks a disruptive action.
PARTICIPANT_ACTIVE_SECONDS = 120
# oTree only records a participant's time when they load a page, so somebody reading
# a long instructions page, or waiting for others, goes quiet. A participant part-way
# through a session who acted within this window also counts as active; somebody who
# abandoned a session stops counting once it passes.
PARTICIPANT_IN_PROGRESS_SECONDS = 30 * 60

MAX_UPLOAD_BYTES = 512 * 1024 * 1024
KEEP_RELEASES = 5


@dataclass(frozen=True)
class Settings:
    root: Path

    @property
    def state_dir(self) -> Path:
        return self.root / "var/lib/otreedeploy"

    @property
    def db_path(self) -> Path:
        return self.state_dir / "state.db"

    @property
    def uploads_dir(self) -> Path:
        return self.state_dir / "uploads"

    @property
    def backups_dir(self) -> Path:
        return self.state_dir / "backups"

    @property
    def otree_home(self) -> Path:
        return self.root / "srv/otree"

    def project_dir(self, slug: str) -> Path:
        return self.otree_home / slug

    def releases_dir(self, slug: str) -> Path:
        return self.project_dir(slug) / "releases"

    def release_dir(self, slug: str, release: str) -> Path:
        return self.releases_dir(slug) / release

    def current_link(self, slug: str) -> Path:
        return self.project_dir(slug) / "current"

    def env_file(self, slug: str) -> Path:
        return self.project_dir(slug) / "env"

    def upload_dir(self, slug: str) -> Path:
        return self.uploads_dir / slug

    def backup_dir(self, slug: str) -> Path:
        return self.backups_dir / slug


def get_settings() -> Settings:
    """Read the layout root fresh each call so tests can repoint it via the environment."""
    return Settings(root=Path(os.environ.get("OTREEDEPLOY_ROOT", "/")))


def otree_port() -> int:
    """The port the live experiment serves on -- 80 in production.

    Overridable because binding 80 needs CAP_NET_BIND_SERVICE, which the systemd unit
    grants and a developer shell does not. See ``dry_run_serves``.
    """
    raw = os.environ.get("OTREEDEPLOY_OTREE_PORT", "").strip()
    if not raw:
        return OTREE_PORT
    try:
        port = int(raw)
    except ValueError:
        raise ValueError(f"OTREEDEPLOY_OTREE_PORT is not a number: {raw!r}") from None
    if not 1 <= port <= 65535:
        raise ValueError(f"OTREEDEPLOY_OTREE_PORT is out of range: {port}")
    return port


def public_url() -> str | None:
    """The HTTPS address of this server, when ``bootstrap.sh`` put it behind Caddy.

    Set (as ``OTREEDEPLOY_PUBLIC_URL``, e.g. ``https://lab.example.edu``) only in HTTPS
    mode. Then both this app and the live experiment listen on 127.0.0.1 alone, Caddy
    is the only thing reachable from the network, and this app trusts its
    X-Forwarded-* headers for the client's address and scheme.
    """
    value = os.environ.get("OTREEDEPLOY_PUBLIC_URL", "").strip().rstrip("/")
    return value or None


def behind_proxy() -> bool:
    return public_url() is not None


def otree_addrport(port: int | None = None) -> str:
    """The argument to `otree prodserver` for the live experiment.

    Just the port on a lab network, which oTree binds on every interface; behind the
    HTTPS proxy, loopback only, so the plain-HTTP experiment cannot be reached around it.
    """
    port = otree_port() if port is None else port
    return f"127.0.0.1:{port}" if behind_proxy() else str(port)


def public_addresses(ip_json: str | None = None) -> list[str]:
    """This machine's addresses that are reachable from the internet, if any.

    Plain HTTP is fine on a closed lab network and not fine across the internet, so the
    app warns when it finds one of these while not behind the HTTPS proxy. Private,
    link-local and loopback addresses are not "public".
    """
    import ipaddress
    import json
    import subprocess

    if ip_json is None:
        if dry_run():
            return []
        try:
            ip_json = subprocess.run(["ip", "-j", "addr"], capture_output=True, text=True,
                                     timeout=10).stdout
        except (OSError, subprocess.SubprocessError):
            return []
    try:
        interfaces = json.loads(ip_json or "[]")
    except ValueError:
        return []
    found = []
    for interface in interfaces:
        for info in interface.get("addr_info", []):
            try:
                address = ipaddress.ip_address(info.get("local", ""))
            except ValueError:
                continue
            if address.is_global:
                found.append(str(address))
    return found


def dry_run_serves() -> bool:
    """Whether a dry run really boots the live instance instead of only recording it.

    Simulating systemd normally means "make this live" changes a flag and nothing
    listens, which is fine for tests but leaves the experiment itself untestable. Giving
    a dry run an unprivileged port to serve on turns the simulated supervisor into a real
    one, so the live study can actually be clicked through.
    """
    return dry_run() and bool(os.environ.get("OTREEDEPLOY_OTREE_PORT", "").strip())


def dry_run() -> bool:
    """When set, privileged side effects (systemctl, postgres) are stubbed.

    Lets the full pipeline and UI run on a workstation with no container.
    """
    return os.environ.get("OTREEDEPLOY_DRY_RUN", "").lower() in {"1", "true", "yes"}
