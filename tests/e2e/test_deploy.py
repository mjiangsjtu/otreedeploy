"""The deploy pipeline's central promise: a broken upload never breaks a running study."""
from __future__ import annotations

import pytest

from . import projects
from .server import deploy_live, http_status, wait_for


def test_a_first_upload_goes_live_and_takes_participants(server, manager, workdir, metrics):
    experiment, codes = deploy_live(server, manager, projects.labdemo(workdir),
                                    metrics=metrics, sessions=2)
    for code in codes:
        for participant in experiment.participant_codes(code):
            page = experiment.open_as_participant(participant)
            assert page.status_code == 200, page.text[:2000]
    assert server.foreign_owned("labdemo") == []
    # Invariant 3: the experiment writes to PostgreSQL, never to a stray SQLite file.
    assert server.psql("otree_labdemo", "SELECT count(*) FROM otree_session") == "2"


def test_a_build_that_fails_leaves_the_live_study_serving_the_old_release(
    server, manager, workdir
):
    experiment, codes = deploy_live(server, manager, projects.labdemo(workdir), sessions=1)
    before = server.sh("readlink /srv/otree/labdemo/current").out.strip()

    job = manager.upload(projects.with_unbuildable_requirement(workdir / "broken"),
                         slug="labdemo")

    assert job.status == "failed"
    assert server.sh("readlink /srv/otree/labdemo/current").out.strip() == before
    participant = experiment.participant_codes(codes[0])[0]
    assert experiment.open_as_participant(participant).status_code == 200


def test_adding_a_model_field_never_publishes_a_release_that_errors_for_participants(
    server, manager, workdir
):
    experiment, codes = deploy_live(server, manager, projects.labdemo(workdir), sessions=1)

    job = manager.upload(projects.with_extra_field(workdir / "v2"), slug="labdemo")

    # Either outcome is acceptable: the change is applied, or it is refused before going
    # live. What must not happen is "Finished successfully" and then HTTP 500.
    participant = experiment.participant_codes(codes[0])[0]
    page = experiment.open_as_participant(participant)
    assert page.status_code == 200, (
        f"deploy reported {job.status!r}, participants now get {page.status_code}\n{job.log}"
    )
    data = manager.get("/p/labdemo/data/wide.csv")
    assert data.status_code == 200, data.text[:1000]


def test_adding_an_app_to_a_live_project_still_lets_sessions_be_created(
    server, manager, workdir
):
    deploy_live(server, manager, projects.labdemo(workdir), sessions=1)

    manager.upload(projects.with_new_app(workdir / "v2"), slug="labdemo").assert_success()

    assert server.foreign_owned("labdemo") == []
    experiment = server.experiment("labdemo")
    response = experiment.create_session()
    assert response.status_code == 200, response.text[:2000]


def test_activating_a_project_that_cannot_start_keeps_the_previous_one_live(
    server, manager, workdir
):
    experiment, codes = deploy_live(server, manager, projects.labdemo(workdir), sessions=1)
    other = projects.renamed(projects.labdemo(workdir / "o"), workdir / "o2", "other.otreezip")
    manager.upload(other).assert_success()
    # A release that built and passed its smoke test, but whose server will not start.
    server.sh("rm /srv/otree/other/current/.venv/bin/otree")

    outcome = manager.activate("other")

    assert outcome.error
    assert "is live" not in outcome.message
    assert server.unit_enabled("labdemo") and not server.unit_enabled("other")
    wait_for(lambda: server.unit_active("labdemo"), timeout=60, what="labdemo restored")
    participant = experiment.participant_codes(codes[0])[0]
    assert experiment.open_as_participant(participant).status_code == 200


def test_a_restart_is_not_reported_successful_when_another_server_holds_the_port(
    server, manager, workdir
):
    deploy_live(server, manager, projects.labdemo(workdir))
    manager.post("/p/labdemo/stop").assert_ok()
    server.sh("systemd-run --unit=squatter -p AmbientCapabilities=CAP_NET_BIND_SERVICE "
              "python3 -m http.server 80")
    wait_for(lambda: http_status(f"http://{server.ip}/") == 200, timeout=30, what="squatter")

    outcome = manager.activate("labdemo")

    assert outcome.error, "activation reported success while oTree could not bind port 80"


def test_only_five_releases_are_kept(server, manager, workdir):
    for i in range(7):
        manager.upload(projects.labdemo(workdir / str(i)), slug="labdemo" if i else None
                       ).assert_success()
        # Release names have one-second resolution (m6).
        __import__("time").sleep(1.1)
    releases = server.sh("ls /srv/otree/labdemo/releases").out.split()
    assert len(releases) == 5
    current = server.sh("readlink -f /srv/otree/labdemo/current").out.strip()
    assert current.rsplit("/", 1)[-1] == max(releases)


def test_a_field_whose_type_changes_is_refused_and_the_live_study_is_untouched(
    server, manager, workdir
):
    experiment, codes = deploy_live(server, manager, projects.labdemo(workdir), sessions=1)
    before = server.sh("readlink /srv/otree/labdemo/current").out.strip()

    job = manager.upload(projects.with_changed_field_type(workdir / "v2"), slug="labdemo")

    assert job.status == "failed"
    assert "changes from integer to text" in job.log
    assert server.sh("readlink /srv/otree/labdemo/current").out.strip() == before
    participant = experiment.participant_codes(codes[0])[0]
    assert experiment.open_as_participant(participant).status_code == 200
