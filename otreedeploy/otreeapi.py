"""Booting a project outside systemd, and talking to oTree's REST API.

This is what lets an experimenter download data from a study that is not the one
currently on port 80: we start the project on a loopback port, pull the CSV through
oTree's key-authenticated export endpoints, and shut it back down.

The endpoints used (verified against oTree 6.0.15, otree/views/export.py) authenticate
with the ``otree-rest-key`` header rather than a login form, which is what keeps this
free of session-cookie scraping.
"""

from __future__ import annotations

import os
import signal
import subprocess
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

import httpx

from . import builder, config, environment, health, sandbox
from .db import Project

Logger = Callable[[str], None]

EXPORTS = {
    "wide": ("/api/export_wide", "all_apps_wide"),
    "page_times": ("/api/export_page_times", "page_times"),
    "chat": ("/api/export_chat", "chat_messages"),
    "app": ("/api/export_app", "app_data"),
}


class OtreeError(Exception):
    pass


@dataclass
class Instance:
    """A project running under our own supervision on a loopback port.

    In a real install it is a transient systemd unit running as the project's own user
    (``unit``); in a dry run, a child process of this app (``process``).
    """

    slug: str
    port: int
    process: subprocess.Popen | None = None
    unit: str | None = None
    last_used: float = field(default_factory=time.monotonic)

    @property
    def alive(self) -> bool:
        if self.unit:
            return sandbox.boot_alive(self.unit)
        return self.process is not None and self.process.poll() is None

    def stop(self) -> None:
        if self.unit:
            # Stopping the unit stops every process in it, worker included.
            sandbox.unboot(self.slug, self.port)
            return
        self._stop_process()

    def _stop_process(self) -> None:
        """Terminate the whole process group.

        `otree prodserver` forks `otree timeoutsubprocess` as a child, so signalling only
        the parent would leave that child holding the port and a database connection.
        We start each instance in its own session precisely so we can signal the group.
        """
        if self.process is None or self.process.poll() is not None:
            return
        try:
            group = os.getpgid(self.process.pid)
        except OSError:
            group = None

        def signal_group(sig: int) -> None:
            if group is not None:
                try:
                    os.killpg(group, sig)
                    return
                except OSError:
                    pass
            self.process.send_signal(sig)

        signal_group(signal.SIGTERM)
        try:
            self.process.wait(timeout=15)
        except subprocess.TimeoutExpired:
            signal_group(signal.SIGKILL)
            try:
                self.process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                pass


def launch(
    project: Project,
    release_dir: Path,
    port: int,
    log: Logger | None = None,
    host: str = "127.0.0.1",
) -> Instance:
    """Start `otree prodserver` for a release and wait until it answers."""
    otree = builder.venv_otree(release_dir)
    if not otree.exists():
        raise OtreeError(
            f"the release at {release_dir} has no virtualenv; it was never built"
        )
    if not config.dry_run():
        return _launch_confined(project, release_dir, port, log)

    process = subprocess.Popen(
        [str(otree), "prodserver", f"{host}:{port}"],
        cwd=release_dir,
        env=environment.subprocess_env(project, port, release_dir),
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        bufsize=1,
        start_new_session=True,
    )

    output: list[str] = []

    def drain() -> None:
        assert process.stdout is not None
        for line in process.stdout:
            line = line.rstrip("\n")
            output.append(line)
            if log:
                log(line)

    reader = threading.Thread(target=drain, daemon=True)
    reader.start()

    instance = Instance(slug=project.slug, port=port, process=process)
    ready = health.wait_until_ready(
        port, host=host, should_continue=lambda: process.poll() is None
    )
    if not ready:
        instance.stop()
        reader.join(timeout=5)
        tail = "\n".join(output[-40:]) or "(no output)"
        if process.returncode not in (None, 0):
            raise OtreeError(
                f"oTree exited immediately (code {process.returncode}). Output:\n{tail}"
            )
        raise OtreeError(
            f"oTree did not start responding within "
            f"{config.BOOT_TIMEOUT_SECONDS}s. Output:\n{tail}"
        )
    return instance


def _launch_confined(
    project: Project, release_dir: Path, port: int, log: Logger | None
) -> Instance:
    """Boot as the project's own user, in a transient unit, and wait until it answers."""
    unit = None
    for attempt in range(5):
        if attempt:
            port = health.free_port(*config.EPHEMERAL_PORT_RANGE)
        try:
            unit = sandbox.boot(project.slug, release_dir, port)
            break
        except sandbox.SandboxError as exc:
            # The unit is named after its port, so a port another boot already holds
            # is refused by systemd itself; try another one.
            if "already" not in str(exc).lower() or attempt == 4:
                raise OtreeError(f"could not start the project: {exc}") from exc
    assert unit is not None

    instance = Instance(slug=project.slug, port=port, unit=unit)
    ready = health.wait_until_ready(
        port, host="127.0.0.1", should_continue=lambda: sandbox.boot_alive(unit)
    )
    if ready:
        return instance
    exited = not sandbox.boot_alive(unit)
    instance.stop()
    tail = sandbox.boot_log(unit) or "(no output)"
    if log:
        for line in tail.splitlines():
            log(line)
    if exited:
        raise OtreeError(f"oTree exited immediately. Output:\n{tail}")
    raise OtreeError(
        f"oTree did not start responding within {config.BOOT_TIMEOUT_SECONDS}s. "
        f"Output:\n{tail}"
    )


class Pool:
    """Keeps ephemerally-booted projects alive briefly so repeat downloads reuse a boot."""

    def __init__(self) -> None:
        self._instances: dict[str, Instance] = {}
        self._lock = threading.Lock()
        self._sweeper: threading.Thread | None = None

    def acquire(self, project: Project, release_dir: Path) -> Instance:
        with self._lock:
            existing = self._instances.get(project.slug)
            if existing and existing.alive:
                existing.last_used = time.monotonic()
                return existing
            if existing:
                self._instances.pop(project.slug, None)

        port = health.free_port(*config.EPHEMERAL_PORT_RANGE)
        instance = launch(project, release_dir, port)

        with self._lock:
            self._instances[project.slug] = instance
            self._ensure_sweeper()
        return instance

    def release(self, slug: str) -> None:
        with self._lock:
            instance = self._instances.pop(slug, None)
        if instance:
            instance.stop()

    def shutdown(self) -> None:
        with self._lock:
            instances = list(self._instances.values())
            self._instances.clear()
        for instance in instances:
            instance.stop()

    def _ensure_sweeper(self) -> None:
        if self._sweeper and self._sweeper.is_alive():
            return
        self._sweeper = threading.Thread(target=self._sweep, daemon=True)
        self._sweeper.start()

    def _sweep(self) -> None:
        while True:
            time.sleep(15)
            now = time.monotonic()
            expired: list[Instance] = []
            with self._lock:
                for slug, instance in list(self._instances.items()):
                    idle = now - instance.last_used
                    if not instance.alive or idle > config.EPHEMERAL_IDLE_SECONDS:
                        expired.append(self._instances.pop(slug))
                if not self._instances:
                    self._sweeper = None
                    stop = True
                else:
                    stop = False
            for instance in expired:
                instance.stop()
            if stop:
                return


pool = Pool()


# ------------------------------------------------------------------------- REST calls


def _get(port: int, path: str, rest_key: str, params: dict | None = None) -> httpx.Response:
    try:
        response = httpx.get(
            f"http://127.0.0.1:{port}{path}",
            params=params or {},
            headers={"otree-rest-key": rest_key},
            timeout=300.0,
        )
    except httpx.HTTPError as exc:
        # The instance can go away between the readiness probe and this call -- oTree
        # crashes, or the idle sweeper reaps an ephemeral boot. Reported as OtreeError so
        # the best-effort callers below degrade instead of 500-ing a page.
        raise OtreeError(f"could not reach oTree on port {port}: {exc}") from exc
    if response.status_code == 403:
        raise OtreeError(f"oTree rejected the REST key: {response.text.strip()}")
    if response.status_code >= 400:
        raise OtreeError(
            f"oTree returned {response.status_code} for {path}: {response.text[:400]}"
        )
    return response


def export_csv(
    port: int, rest_key: str, kind: str, session_code: str | None = None,
    app_name: str | None = None,
) -> tuple[bytes, str]:
    """Fetch one of oTree's CSV exports. Returns (body, filename_stem)."""
    try:
        path, stem = EXPORTS[kind]
    except KeyError:
        raise OtreeError(f"unknown export type {kind!r}") from None

    params: dict[str, str] = {}
    if session_code:
        params["session_code"] = session_code
    if app_name:
        params["app_name"] = app_name
    response = _get(port, path, rest_key, params)
    return response.content, stem


def serves_project(port: int, rest_key: str, host: str = "127.0.0.1") -> bool:
    """True only if the server on this port is the project holding this REST key.

    "Something answers on port 80" is not enough to call a restart successful: another
    server can hold the port while oTree crash-loops behind it ("address already in
    use"), or a different project can still be answering. oTree checks the REST key on
    every REST endpoint, and each project has its own key, so a 200 here identifies it.
    """
    try:
        response = httpx.get(
            f"http://{host}:{port}/api/otree_version",
            headers={"otree-rest-key": rest_key},
            timeout=5.0,
        )
        return response.status_code == 200 and "version" in response.json()
    except (httpx.HTTPError, ValueError):
        return False


def otree_version(port: int, rest_key: str) -> str | None:
    try:
        return _get(port, "/api/otree_version", rest_key).json().get("version")
    except (OtreeError, ValueError):
        return None


def session_configs(port: int, rest_key: str) -> list[dict]:
    try:
        return _get(port, "/api/session_configs", rest_key).json()
    except (OtreeError, ValueError):
        return []


def apps_with_data(port: int, rest_key: str) -> list[str]:
    try:
        payload = _get(port, "/api/apps", rest_key).json()
    except (OtreeError, ValueError):
        return []
    if isinstance(payload, list):
        return [str(item) for item in payload]
    return []
