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


@pytest.mark.slow
def test_resetdb_hands_the_tables_to_the_otree_role(clean, monkeypatch):
    """The experiment runs as `otree`, but this app runs resetdb -- so without a handover
    every table is owned by the app's role and the experiment cannot read its own data.

    Dry run uses SQLite, which has no roles, so the wiring is what is asserted here: that
    the deploy actually asks for the handover. The SQL itself only runs on PostgreSQL.
    """
    calls: list[str] = []
    monkeypatch.setattr(
        "otreedeploy.postgres.align_ownership", lambda slug: calls.append(slug) or 0
    )

    job = deploy.new_job("ownership")
    slug = deploy.deploy(job, REAL_OTREEZIP, "study.otreezip")
    assert calls == [slug], "resetdb must be followed by the ownership handover"

    # A redeploy keeps the database, and must still repair one built by an older version.
    calls.clear()
    deploy.deploy(deploy.new_job("again"), REAL_OTREEZIP, "study.otreezip", slug=slug)
    assert calls == [slug], "an existing database must still be repaired"


def test_align_ownership_is_a_noop_in_dry_run(clean):
    from otreedeploy import postgres

    assert postgres.align_ownership("anything") == 0
