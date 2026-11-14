"""The management app as seen from the network, and the participant guard."""
from __future__ import annotations

import httpx
import pytest

from . import projects
from .server import deploy_live


@pytest.mark.xfail(strict=True, reason="R1 3.1 / N6: ?error= is passed through str.format")
def test_the_login_page_does_not_let_a_url_choose_the_response_size(server):
    r = httpx.get(f"http://{server.ip}:8000/login",
                  params={"error": "{port:>20000000}"}, timeout=60)
    assert len(r.content) < 200_000


@pytest.mark.xfail(strict=True, reason="m1: no CSRF protection; SameSite ignores the port")
def test_a_form_posted_from_the_experiment_origin_is_refused(server, manager, workdir):
    deploy_live(server, manager, projects.labdemo(workdir))
    r = manager.http.post(
        "/p/labdemo/restart",
        data={"force": "true"},
        headers={"Origin": f"http://{server.ip}", "Referer": f"http://{server.ip}/p/x"},
    )
    assert r.status_code == 403


@pytest.mark.xfail(strict=True, reason="m8: rollback joins an unvalidated release into a path")
def test_rollback_only_accepts_this_projects_own_releases(server, manager, workdir):
    deploy_live(server, manager, projects.labdemo(workdir))
    manager.upload(projects.renamed(projects.labdemo(workdir / "o"), workdir / "o2",
                                    "other.otreezip")).assert_success()
    other_release = server.sh("ls /srv/otree/other/releases").out.split()[0]

    outcome = manager.post("/p/labdemo/rollback",
                           release=f"../../other/releases/{other_release}")

    assert outcome.error
    target = server.sh("readlink -f /srv/otree/labdemo/current").out.strip()
    assert target.startswith("/srv/otree/labdemo/")


def test_a_participant_who_just_loaded_a_page_blocks_stop(server, manager, workdir):
    experiment, codes = deploy_live(server, manager, projects.labdemo(workdir), sessions=1)
    experiment.open_as_participant(experiment.participant_codes(codes[0])[0])

    outcome = manager.post("/p/labdemo/stop")

    assert outcome.error and "active" in outcome.message
    assert server.unit_active("labdemo")


@pytest.mark.xfail(strict=True, reason="m2: the UI offers no override for Stop/Restart/Roll back")
def test_the_project_page_offers_an_explicit_override_when_stop_is_blocked(
    server, manager, workdir
):
    experiment, codes = deploy_live(server, manager, projects.labdemo(workdir), sessions=1)
    experiment.open_as_participant(experiment.participant_codes(codes[0])[0])
    blocked = manager.post("/p/labdemo/stop")

    page = manager.get(blocked.location).text

    stop_forms = [chunk for chunk in page.split("<form")[1:] if "/p/labdemo/stop" in chunk]
    assert any('name="force"' in form for form in stop_forms)
