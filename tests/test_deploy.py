"""End-to-end exercise of the pipeline in dry-run mode.

Dry run swaps PostgreSQL for a real SQLite file and systemd for an in-memory record, but
everything else is genuine: the archive is really extracted, uv really builds a venv, and
oTree is really booted for the smoke test. These are slow but they are the tests that
would actually have caught a broken deploy.
"""

from __future__ import annotations

import pytest

from otreedeploy import config, db, deploy, postgres, systemd

from .conftest import REAL_OTREEZIP, create_session

pytestmark = pytest.mark.skipif(
    not REAL_OTREEZIP.exists(), reason="real otreezip fixture not present"
)


@pytest.fixture
def clean(root):
    systemd.reset_fake_state()
    db.init()
    yield root
    from otreedeploy import otreeapi

    otreeapi.pool.shutdown()


@pytest.mark.slow
def test_full_deploy(clean):
    job = deploy.new_job("test deploy")
    slug = deploy.deploy(job, REAL_OTREEZIP, "Trust Game v2.otreezip")

    assert slug == "trust_game_v2"
    settings = config.get_settings()

    # The release is on disk with its own virtualenv, and 'current' points at it.
    current = settings.current_link(slug)
    assert current.is_symlink()
    release_dir = current.resolve()
    assert (release_dir / "settings.py").exists()
    assert (release_dir / ".venv/bin/otree").exists()

    # requirements were normalized away from the source-only psycopg2 pin
    assert "psycopg2-binary" in (release_dir / "requirements.txt").read_text()

    # The schema was created and is readable through the same code path the UI uses.
    assert postgres.has_otree_tables(slug)
    assert postgres.list_sessions(slug) == []
    assert postgres.count_active_participants(slug) == 0

    # The service environment names a real database rather than letting oTree
    # silently fall back to a stray sqlite file.
    env_text = settings.env_file(slug).read_text()
    assert "DATABASE_URL=" in env_text
    assert "OTREE_REST_KEY=" in env_text
    # group-readable for the otree service user, never world-readable
    assert settings.env_file(slug).stat().st_mode & 0o007 == 0

    # It is recorded as a successful release, with the oTree version detected.
    releases = db.list_releases(slug)
    assert len(releases) == 1
    assert releases[0].status == "success"
    assert releases[0].otree_version

    # The upload itself was kept verbatim.
    assert list(settings.upload_dir(slug).glob("*.otreezip"))


@pytest.mark.slow
def test_redeploy_keeps_the_database(clean):
    """The safety property that matters most: re-uploading must not wipe data."""
    job = deploy.new_job("first")
    slug = deploy.deploy(job, REAL_OTREEZIP, "study.otreezip")

    # Real collected data, created through oTree's own API.
    code = create_session(slug)

    assert [s.code for s in postgres.list_sessions(slug)] == [code]

    job2 = deploy.new_job("second")
    deploy.deploy(job2, REAL_OTREEZIP, "study.otreezip", slug=slug)

    assert [s.code for s in postgres.list_sessions(slug)] == [code], (
        "re-uploading a project must not destroy collected data"
    )
    assert len(db.list_releases(slug)) == 2


@pytest.mark.slow
def test_activate_moves_port_80(clean):
    job = deploy.new_job("a")
    first = deploy.deploy(job, REAL_OTREEZIP, "alpha.otreezip")
    job2 = deploy.new_job("b")
    second = deploy.deploy(job2, REAL_OTREEZIP, "beta.otreezip")

    assert deploy.active_slug() is None
    deploy.activate(first)
    assert deploy.active_slug() == first

    deploy.activate(second)
    assert deploy.active_slug() == second, "activating must take port 80 from the incumbent"
    assert not systemd.is_enabled(first)

    deploy.stop(second)
    assert deploy.active_slug() is None


@pytest.mark.slow
def test_rollback(clean):
    job = deploy.new_job("v1")
    slug = deploy.deploy(job, REAL_OTREEZIP, "study.otreezip")
    first_release = db.list_releases(slug)[0].release

    job2 = deploy.new_job("v2")
    deploy.deploy(job2, REAL_OTREEZIP, "study.otreezip", slug=slug)
    assert config.get_settings().current_link(slug).resolve().name != first_release

    deploy.rollback(slug, first_release)
    assert config.get_settings().current_link(slug).resolve().name == first_release


def test_rollback_refuses_a_release_name_that_is_not_one_of_this_projects_own(clean):
    """The release name comes from a form; it must not be able to walk the filesystem."""
    settings = config.get_settings()
    db.create_project("victim", "victim")
    db.create_project("other", "other")
    stolen = settings.release_dir("other", "20260101-000000")
    (stolen / ".venv").mkdir(parents=True)
    db.finish_release(
        db.create_release("other", "20260101-000000", "o.otreezip", "x"), "success", ""
    )

    for name in ("../../other/releases/20260101-000000", "20260101-000000", ""):
        with pytest.raises(deploy.DeployError, match="not a successful release"):
            deploy.rollback("victim", name)
    assert not settings.current_link("victim").is_symlink()


def test_rollback_refuses_a_release_whose_build_failed(clean):
    settings = config.get_settings()
    db.create_project("study", "study")
    (settings.release_dir("study", "20260101-000000") / ".venv").mkdir(parents=True)
    db.finish_release(
        db.create_release("study", "20260101-000000", "s.otreezip", "x"), "failed", ""
    )
    with pytest.raises(deploy.DeployError, match="not a successful release"):
        deploy.rollback("study", "20260101-000000")


def test_rejects_bad_archive(clean, tmp_path):
    bogus = tmp_path / "notazip.otreezip"
    bogus.write_bytes(b"nope")
    job = deploy.new_job("bad")
    with pytest.raises(deploy.DeployError, match="could not read this file"):
        deploy.deploy(job, bogus, "notazip.otreezip")


def test_rejects_non_otree_archive(clean, make_archive):
    path = make_archive("x.otreezip", [("file", "/README.md", "hi\n")])
    job = deploy.new_job("bad")
    with pytest.raises(deploy.DeployError, match="does not look like an oTree project"):
        deploy.deploy(job, path, "x.otreezip")


def test_guard_blocks_when_participants_active(clean, monkeypatch):
    monkeypatch.setattr(postgres, "count_active_participants", lambda slug, **kw: 7)
    with pytest.raises(deploy.ParticipantsActive) as excinfo:
        deploy.guard_disruption("anything", force=False)
    assert "7 participants are currently active" in str(excinfo.value)
    # ...and force overrides it.
    deploy.guard_disruption("anything", force=True)


def test_job_log_replays_backlog_to_late_subscribers():
    job = deploy.new_job("t")
    job.log("one")
    job.log("two")
    job.finish("success")
    assert list(job.subscribe()) == ["one", "two"]


def test_the_otree_version_is_read_from_metadata_without_running_the_projects_code(
    clean, tmp_path
):
    """The app must never execute a release's Python: that is the project's own code."""
    release = tmp_path / "release"
    dist = release / ".venv/lib/python3.12/site-packages/otree-6.0.15.dist-info"
    dist.mkdir(parents=True)
    (dist / "METADATA").write_text("Metadata-Version: 2.1\nName: otree\nVersion: 6.0.15\n\nbody\n")
    # A python that would betray being run.
    (release / ".venv/bin").mkdir(parents=True)
    trap = release / ".venv/bin/python"
    trap.write_text("#!/bin/sh\ntouch " + str(tmp_path / "executed") + "\n")
    trap.chmod(0o755)

    from otreedeploy import builder

    assert builder.otree_version(release) == "6.0.15"
    assert not (tmp_path / "executed").exists()


@pytest.mark.slow
@pytest.mark.parametrize("rollback_works", [True, False])
def test_a_failed_live_restart_says_whether_participants_are_affected(
    clean, monkeypatch, rollback_works
):
    slug = deploy.deploy(deploy.new_job("v1"), REAL_OTREEZIP, "study.otreezip")
    deploy.activate(slug)
    first = config.get_settings().current_link(slug).resolve()

    attempts: list[int] = []

    def restart(_slug):
        attempts.append(1)
        if len(attempts) == 1 or not rollback_works:
            raise deploy.DeployError("did not respond")

    monkeypatch.setattr(deploy, "_restart_and_verify", restart)
    job = deploy.new_job("v2")
    with pytest.raises(deploy.DeployError):
        deploy.deploy(job, REAL_OTREEZIP, "study.otreezip", slug=slug)

    assert config.get_settings().current_link(slug).resolve() == first
    assert job.live_down is (not rollback_works)


@pytest.mark.slow
def test_activating_a_project_that_cannot_start_gives_port_80_back_to_the_incumbent(
    clean, monkeypatch
):
    incumbent = deploy.deploy(deploy.new_job("a"), REAL_OTREEZIP, "live.otreezip")
    deploy.activate(incumbent)
    broken = deploy.deploy(deploy.new_job("b"), REAL_OTREEZIP, "broken.otreezip")

    def restart(slug):
        systemd.restart(slug)
        if slug == broken:
            raise deploy.DeployError("did not start")

    monkeypatch.setattr(deploy, "_restart_and_verify", restart)
    with pytest.raises(deploy.DeployError, match="'live' is serving again"):
        deploy.activate(broken)

    assert deploy.active_slug() == incumbent
    assert systemd.is_active(incumbent)
    assert not systemd.is_enabled(broken) and not systemd.is_active(broken)


@pytest.mark.slow
def test_stopping_or_restarting_a_unit_that_is_not_running_needs_no_override(
    clean, monkeypatch
):
    slug = deploy.deploy(deploy.new_job("a"), REAL_OTREEZIP, "study.otreezip")
    monkeypatch.setattr(postgres, "count_active_participants", lambda slug: 3)
    assert not systemd.is_active(slug)

    # Nobody can be interrupted by (re)starting or stopping a study that is down.
    deploy.restart(slug)
    systemd.stop(slug)
    deploy.stop(slug)

    # Once it is running, the guard applies again.
    systemd.start(slug)
    with pytest.raises(deploy.ParticipantsActive):
        deploy.restart(slug)


@pytest.mark.slow
def test_restoring_a_backup_brings_back_the_data_a_reset_removed(clean):
    slug = deploy.deploy(deploy.new_job("a"), REAL_OTREEZIP, "study.otreezip")
    code = create_session(slug)
    snapshot = deploy.reset_database(slug, force=True)
    assert [s.code for s in postgres.list_sessions(slug)] == []

    before_restore = deploy.restore_backup(slug, snapshot.name)

    assert [s.code for s in postgres.list_sessions(slug)] == [code]
    assert before_restore is not None and "before-restore" in before_restore.name


@pytest.mark.parametrize(
    "name", ["../other/x.sql.gz", "x.sql.gz", "notabackup.txt", "", "/etc/passwd"]
)
def test_restore_only_accepts_this_projects_own_backups(clean, name):
    db.create_project("study", "study")
    other = config.get_settings().backup_dir("other")
    other.mkdir(parents=True)
    (other / "x.sql.gz").write_bytes(b"")
    with pytest.raises(deploy.DeployError, match="no backup called"):
        deploy.restore_backup("study", name)


# ------------------------------------------------------------- schema changes (M1)


def _session_participant_page(slug):
    """Boot the current release and fetch a participant page of a fresh session."""
    import httpx

    from otreedeploy import otreeapi

    code = create_session(slug)
    project = db.get_project(slug)
    instance = otreeapi.pool.acquire(project, deploy.current_release_dir(slug))
    base = f"http://127.0.0.1:{instance.port}"
    participants = httpx.get(f"{base}/api/sessions/{code}",
                             headers={"otree-rest-key": project.rest_key}).json()["participants"]
    page = httpx.get(f"{base}/InitializeParticipant/{participants[0]['code']}",
                     follow_redirects=True, timeout=60)
    otreeapi.pool.release(slug)
    return page


@pytest.mark.slow
def test_a_new_model_field_is_added_to_the_existing_database_and_the_study_keeps_working(
    clean, tmp_path
):
    from .e2e import projects as variants

    slug = deploy.deploy(deploy.new_job("v1"), variants.labdemo(tmp_path / "v1"), "labdemo.otreezip")
    create_session(slug)
    job = deploy.new_job("v2")
    deploy.deploy(job, variants.with_extra_field(tmp_path / "v2"), "labdemo.otreezip", slug=slug)

    log = "\n".join(job.lines)
    assert "added column simple_task_player.extra" in log
    assert len(postgres.list_sessions(slug)) == 1, "the collected data must survive"
    assert _session_participant_page(slug).status_code == 200


@pytest.mark.slow
def test_a_field_whose_type_changes_is_refused_before_anything_goes_live(clean, tmp_path):
    from .e2e import projects as variants

    slug = deploy.deploy(deploy.new_job("v1"), variants.labdemo(tmp_path / "v1"), "labdemo.otreezip")
    deploy.activate(slug)
    create_session(slug)
    before = config.get_settings().current_link(slug).resolve()

    with pytest.raises(deploy.SchemaChangeRefused, match="simple_task_player.answer changes from integer to text"):
        deploy.deploy(deploy.new_job("v2"), variants.with_changed_field_type(tmp_path / "v2"),
                      "labdemo.otreezip", slug=slug)

    assert config.get_settings().current_link(slug).resolve() == before
    assert systemd.is_active(slug)
    assert len(postgres.list_sessions(slug)) == 1


@pytest.mark.slow
def test_redeploying_unchanged_models_reports_that_the_database_already_fits(clean, tmp_path):
    from .e2e import projects as variants

    slug = deploy.deploy(deploy.new_job("v1"), variants.labdemo(tmp_path / "v1"), "labdemo.otreezip")
    job = deploy.new_job("v2")
    deploy.deploy(job, variants.labdemo(tmp_path / "v2"), "labdemo.otreezip", slug=slug)
    assert "the database already fits this version" in "\n".join(job.lines)


@pytest.mark.slow
def test_a_unit_that_systemd_refuses_to_start_still_gives_port_80_back(clean, monkeypatch):
    """systemctl fails immediately when the unit cannot even be executed; that must
    trigger the same rollback as a unit that starts but never answers."""
    incumbent = deploy.deploy(deploy.new_job("a"), REAL_OTREEZIP, "live.otreezip")
    deploy.activate(incumbent)
    broken = deploy.deploy(deploy.new_job("b"), REAL_OTREEZIP, "broken.otreezip")
    real_restart = systemd.restart

    def restart(slug):
        if slug == broken:
            raise systemd.SystemdError("Job for otree@broken.service failed")
        real_restart(slug)

    monkeypatch.setattr(systemd, "restart", restart)
    with pytest.raises(deploy.DeployError, match="'live' is serving again"):
        deploy.activate(broken)
    assert deploy.active_slug() == incumbent and systemd.is_active(incumbent)
