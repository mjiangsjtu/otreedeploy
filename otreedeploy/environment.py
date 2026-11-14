"""Rendering the systemd EnvironmentFile for a project.

oTree is configured entirely through the environment. The one that matters most is
DATABASE_URL: if it is missing, oTree silently falls back to a SQLite file inside the
release directory, so a study would appear to work while writing its data somewhere that
the next deploy throws away. We therefore always write it explicitly.
"""

from __future__ import annotations

import os
from pathlib import Path

from . import config, postgres
from .db import Project

AUTH_LEVELS = ("STUDY", "DEMO")


def build(project: Project, port: int | None = None) -> dict[str, str]:
    port = config.otree_port() if port is None else port
    return {
        "DATABASE_URL": postgres.database_url(project.slug),
        "OTREE_ADMIN_PASSWORD": project.admin_password,
        "OTREE_REST_KEY": project.rest_key,
        "OTREE_AUTH_LEVEL": project.auth_level,
        "OTREE_PRODUCTION": "1",
        # The unit runs `otree prodserver ${PORT}`, and prodserver takes "port" or
        # "address:port" there; behind the HTTPS proxy this is loopback-only.
        "PORT": config.otree_addrport(port),
    }


def render(values: dict[str, str]) -> str:
    # systemd splits on the first '=' and strips surrounding double quotes, so quoting
    # keeps values such as the DATABASE_URL query string unambiguous.
    return "".join(f'{key}="{value}"\n' for key, value in sorted(values.items()))


def write(project: Project, port: int | None = None) -> Path:
    port = config.otree_port() if port is None else port
    settings = config.get_settings()
    path = settings.env_file(project.slug)
    path.parent.mkdir(parents=True, exist_ok=True)
    # Contains the admin password and REST key: readable by the otree service user's
    # group, never world-readable.
    tmp = path.with_suffix(".tmp")
    tmp.write_text(render(build(project, port=port)))
    os.chmod(tmp, 0o640)
    tmp.replace(path)
    return path


def subprocess_env(
    project: Project, port: int, release_dir: Path | None = None
) -> dict[str, str]:
    """Environment for booting a project ourselves, outside systemd.

    The venv's bin directory must be on PATH, not merely used to locate the entry point:
    `otree prodserver` re-invokes itself as the bare name `otree` to spawn its timeout
    worker, so an absolute ExecStart alone leaves that child unfindable and the server
    exits immediately. The systemd unit sets the same PATH for the same reason.
    """
    env = {**os.environ, **build(project, port=port)}
    if release_dir is not None:
        bin_dir = str((release_dir / ".venv/bin").resolve())
        env["PATH"] = bin_dir + os.pathsep + env.get("PATH", "")
        env["VIRTUAL_ENV"] = str((release_dir / ".venv").resolve())
    return env
