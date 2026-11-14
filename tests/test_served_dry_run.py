"""A dry run that is given a port really serves the live instance.

Simulating systemd normally means "make this live" sets a flag and nothing listens --
correct for the fast tests, but it leaves the experiment itself impossible to click
through off the container. Setting OTREEDEPLOY_OTREE_PORT turns the simulated supervisor
into a real one on an unprivileged port.
"""

from __future__ import annotations

import socket
from pathlib import Path

import pytest

from otreedeploy import config, db, deploy, environment, health, systemd

from .conftest import REAL_OTREEZIP

pytestmark = pytest.mark.skipif(
    not REAL_OTREEZIP.exists(), reason="real otreezip fixture not present"
)


def _free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


# ------------------------------------------------------------------ configuration


def test_port_defaults_to_80(root, monkeypatch):
    monkeypatch.delenv("OTREEDEPLOY_OTREE_PORT", raising=False)
    assert config.otree_port() == 80
    assert config.dry_run_serves() is False


def test_port_is_overridable(root, monkeypatch):
    monkeypatch.setenv("OTREEDEPLOY_OTREE_PORT", "8080")
    assert config.otree_port() == 8080
    assert config.dry_run_serves() is True


def test_serving_requires_dry_run(monkeypatch):
    """Outside a dry run the port is still honoured, but systemd stays in charge."""
    monkeypatch.setenv("OTREEDEPLOY_OTREE_PORT", "8080")
    monkeypatch.delenv("OTREEDEPLOY_DRY_RUN", raising=False)
    assert config.otree_port() == 8080
    assert config.dry_run_serves() is False


@pytest.mark.parametrize("bad", ["nope", "0", "-1", "70000", "8080.5"])
def test_rejects_an_unusable_port(root, monkeypatch, bad):
    monkeypatch.setenv("OTREEDEPLOY_OTREE_PORT", bad)
    with pytest.raises(ValueError):
        config.otree_port()


def test_the_env_file_records_the_port_otree_is_told_to_use(root, monkeypatch):
    """oTree reads its port from the environment, so the override has to reach it."""
    monkeypatch.setenv("OTREEDEPLOY_OTREE_PORT", "8080")
    db.init()
    project = db.create_project("x", "X")

    assert environment.build(project)["PORT"] == "8080"
    assert 'PORT="8080"' in environment.write(project).read_text()


# ------------------------------------------------------------------ really serving


@pytest.fixture
def served(root, monkeypatch):
    port = _free_port()
    monkeypatch.setenv("OTREEDEPLOY_OTREE_PORT", str(port))
    systemd.reset_fake_state()
    db.init()
    yield port
    systemd.reset_fake_state()
    from otreedeploy import otreeapi

    otreeapi.pool.shutdown()


@pytest.mark.slow
def test_activating_actually_serves_the_experiment(served):
    port = served
    job = deploy.new_job("serve")
    slug = deploy.deploy(job, REAL_OTREEZIP, "study.otreezip")

    assert not health.responds(port, timeout=2.0), "nothing should serve before activation"

    deploy.activate(slug)
    assert deploy.active_slug() == slug
    assert systemd.is_active(slug)
    assert health.responds(port, timeout=10.0), "the live instance must answer on its port"

    deploy.stop(slug)
    assert not systemd.is_active(slug)
    assert not health.responds(port, timeout=2.0), "stopping must free the port"


@pytest.mark.slow
def test_a_died_instance_reports_inactive(served):
    """systemd would notice the unit died; the stand-in has to as well."""
    job = deploy.new_job("serve")
    slug = deploy.deploy(job, REAL_OTREEZIP, "study.otreezip")
    deploy.activate(slug)
    assert systemd.is_active(slug)

    systemd._fake.instances[slug].process.kill()
    systemd._fake.instances[slug].process.wait(timeout=15)

    assert not systemd.is_active(slug)


# ------------------------------------------------------------ what the UI says

TEMPLATES = Path(__file__).parent.parent / "templates"


def test_no_template_hardcodes_the_port():
    """A page naming the wrong port sends people to a refused connection."""
    offenders = [
        f"{path.name}:{n}: {line.strip()}"
        for path in sorted(TEMPLATES.glob("*.html"))
        for n, line in enumerate(path.read_text().splitlines(), 1)
        if "port 80" in line.lower()
    ]
    assert not offenders, "templates must render otree_port(), not a literal:\n" + "\n".join(
        offenders
    )


def test_the_landing_page_names_the_configured_port(root, monkeypatch):
    monkeypatch.setenv("OTREEDEPLOY_OTREE_PORT", "8080")
    systemd.reset_fake_state()
    db.init()
    from fastapi.testclient import TestClient

    from otreedeploy.app import app

    with TestClient(app, headers={"Origin": "http://testserver"}) as client:
        body = client.get("/").text
    assert "port 8080" in body
    assert "port 80." not in body


@pytest.mark.slow
def test_the_project_page_links_to_the_port_that_is_really_serving(served):
    port = served
    from fastapi.testclient import TestClient

    from otreedeploy.app import app

    with TestClient(app, headers={"Origin": "http://testserver"}) as client:
        job = deploy.new_job("serve")
        slug = deploy.deploy(job, REAL_OTREEZIP, "study.otreezip")
        deploy.activate(slug)

        body = client.get(f"/p/{slug}").text
        assert f"port {port}" in body
        assert f"http://testserver:{port}/" in body, "the admin link must carry the port"
