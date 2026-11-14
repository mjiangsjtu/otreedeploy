"""End-to-end suite: real Ubuntu, real systemd, real PostgreSQL, real bootstrap.sh.

Opt-in, because it needs a container runtime and network access:

    OTREEDEPLOY_E2E=1 .venv/bin/python -m pytest tests/e2e -v

The first run builds a base image, runs bootstrap.sh in a fresh container exactly as the
README says, and snapshots the result. Every test then gets its own container booted from
that snapshot, so tests never see each other's state.

Tests that assert a behaviour the paper claims but the submitted code does not deliver
are marked ``xfail(strict=True)`` with the referee item they reproduce. Strict means a
fix flips the test to XPASS, which fails the run until the marker is removed -- so the
list of known failures cannot silently go stale.
"""
from __future__ import annotations

import json
import os
import time
from pathlib import Path

import pytest

from .server import (
    BASE_IMAGE,
    BOOTSTRAPPED_IMAGE,
    PASSWORD,
    REPO,
    SRC_IN_CONTAINER,
    Server,
    http_status,
    runtime,
    wait_for,
)

ENABLED = os.environ.get("OTREEDEPLOY_E2E") == "1"
METRICS_FILE = Path(os.environ.get("OTREEDEPLOY_E2E_METRICS", REPO / "e2e-metrics.json"))


def pytest_collection_modifyitems(config, items):
    here = Path(__file__).parent
    for item in items:
        if Path(item.path).is_relative_to(here):
            item.add_marker(pytest.mark.e2e)
            if not ENABLED:
                item.add_marker(pytest.mark.skip(reason="set OTREEDEPLOY_E2E=1 to run"))


# ------------------------------------------------------------------------ metrics


class Metrics:
    """Timings and facts the paper reports (install time, upload-to-live, platforms)."""

    def __init__(self) -> None:
        self.data: dict = {"recorded_at": time.strftime("%Y-%m-%d %H:%M:%S"), "timings": {}}

    def time(self, key: str, seconds: float) -> None:
        self.data["timings"].setdefault(key, []).append(round(seconds, 1))

    def fact(self, key: str, value) -> None:
        self.data[key] = value


@pytest.fixture(scope="session")
def metrics():
    m = Metrics()
    yield m
    if ENABLED:
        METRICS_FILE.write_text(json.dumps(m.data, indent=2) + "\n")


# --------------------------------------------------------------------- the images


@pytest.fixture(scope="session")
def base_image() -> str:
    runtime("build", "-t", BASE_IMAGE, "-f", str(Path(__file__).parent / "Dockerfile"),
            str(Path(__file__).parent), timeout=1800)
    return BASE_IMAGE


@pytest.fixture(scope="session")
def fresh_install(base_image, metrics):
    """Run bootstrap.sh on a fresh machine, the documented way, and snapshot the result.

    Yields the bootstrap transcript so the install test can check what it printed.
    """
    server = Server(base_image)
    try:
        server.copy_tree_in(REPO, SRC_IN_CONTAINER)
        started = time.time()
        result = server.sh(
            f"cd {SRC_IN_CONTAINER} && ./bootstrap.sh",
            env={"OTREEDEPLOY_PASSWORD": PASSWORD},
            check=False,
        )
        metrics.time("bootstrap_fresh_seconds", time.time() - started)
        if not result.ok:
            pytest.exit(f"bootstrap.sh failed on a fresh machine:\n{result.out[-4000:]}", 3)

        facts = server.sh(
            ". /etc/os-release; echo \"$PRETTY_NAME\"; "
            "psql --version | awk '{print $3}'; python3 --version | awk '{print $2}'; "
            "uv --version | awk '{print $2}'; systemctl --version | head -1 | awk '{print $2}'"
        ).out.split("\n")
        metrics.fact("platform", {
            "os": facts[0], "postgresql": facts[1], "python": facts[2],
            "uv": facts[3], "systemd": facts[4],
        })

        # Warm uv's cache (it lives in the app user's home, /opt/otreedeploy/.cache) so
        # that every test does not download oTree again. This changes timing only.
        server.sh(
            "cd /tmp && uv venv -q /tmp/warm && "
            "uv pip install -q --python /tmp/warm/bin/python otree==6.0.15 "
            "psycopg2-binary sentry-sdk setuptools && rm -rf /tmp/warm",
            user="otreedeploy",
        )
        server.stop()
        server.commit(BOOTSTRAPPED_IMAGE)
        yield result.out
    finally:
        server.remove()


@pytest.fixture
def server(fresh_install):
    """A freshly booted copy of the installed server, private to one test."""
    s = Server(BOOTSTRAPPED_IMAGE)
    try:
        wait_for(lambda: http_status(f"http://{s.ip}:8000/login") == 200,
                 timeout=90, what="the management app")
        yield s
    finally:
        s.remove()


@pytest.fixture
def manager(server):
    return server.manager().login()


@pytest.fixture
def workdir(tmp_path):
    return tmp_path
