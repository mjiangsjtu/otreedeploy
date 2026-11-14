"""Exercises the actual HTTP endpoints, including a real upload -> deploy -> download."""

from __future__ import annotations

import time

import pytest
from fastapi.testclient import TestClient

from otreedeploy import config, db, deploy, postgres, systemd

from .conftest import REAL_OTREEZIP, create_session


@pytest.fixture
def client(root):
    systemd.reset_fake_state()
    db.init()
    from otreedeploy.app import app

    with TestClient(app, headers={"Origin": "http://testserver"}) as test_client:
        yield test_client
    from otreedeploy import otreeapi

    otreeapi.pool.shutdown()


def _wait_for_job(job_id: str, timeout: float = 300) -> deploy.Job:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        job = deploy.get_job(job_id)
        assert job is not None
        if job.status != "running":
            return job
        time.sleep(0.25)
    raise AssertionError("deploy job did not finish in time")


def _upload(client, path, filename="study.otreezip", **data):
    with open(path, "rb") as fh:
        return client.post(
            "/upload",
            files={"file": (filename, fh, "application/octet-stream")},
            data=data,
            follow_redirects=False,
        )


def test_empty_state(client):
    response = client.get("/")
    assert response.status_code == 200
    assert "No experiments yet" in response.text


def test_pages_render(client):
    assert client.get("/upload").status_code == 200
    assert client.get("/system").status_code == 200


def test_unknown_project_redirects(client):
    response = client.get("/p/nosuchthing", follow_redirects=True)
    assert "no project called" in response.text


@pytest.mark.parametrize(
    "slug", ["..%2F..%2Fetc", "evil;DROP", "UPPER", "a" * 60, "otree"]
)
def test_invalid_slug_is_rejected_not_crashed(client, slug):
    """A slug that could never be valid must be refused, never interpolated anywhere."""
    response = client.get(f"/p/{slug}", follow_redirects=True)
    assert response.status_code in (200, 404)
    if response.status_code == 200:
        assert "no project called" in response.text


def test_upload_rejects_empty_file(client, tmp_path):
    empty = tmp_path / "empty.otreezip"
    empty.write_bytes(b"")
    response = _upload(client, empty)
    assert response.status_code == 303
    assert "no%20file" in response.headers["location"]


def test_upload_rejects_junk(client, tmp_path):
    junk = tmp_path / "junk.otreezip"
    junk.write_bytes(b"not a tarball at all")
    response = _upload(client, junk)
    job_id = response.headers["location"].rsplit("/", 1)[-1]
    job = _wait_for_job(job_id)
    assert job.status == "failed"
    assert "could not read this file" in "\n".join(job.lines)


def test_backup_download_rejects_traversal(client):
    db.create_project("demo", "Demo")
    response = client.get("/p/demo/backups/..%2F..%2F..%2Fetc%2Fpasswd")
    assert response.status_code == 404


def test_reset_requires_typed_confirmation(client):
    db.create_project("demo", "Demo")
    response = client.post("/p/demo/reset-db", data={"confirm": "wrong"},
                           follow_redirects=False)
    assert "type%20the%20project%20name" in response.headers["location"]


def test_delete_requires_typed_confirmation(client):
    db.create_project("demo", "Demo")
    response = client.post("/p/demo/delete", data={"confirm": ""}, follow_redirects=False)
    assert "type%20the%20project%20name" in response.headers["location"]


def test_settings_rejects_bad_auth_level(client):
    db.create_project("demo", "Demo")
    response = client.post(
        "/p/demo/settings",
        data={"display_name": "Demo", "admin_password": "x", "auth_level": "WIDE_OPEN"},
        follow_redirects=False,
    )
    assert "unknown%20access%20level" in response.headers["location"]


def test_settings_rejects_empty_password(client):
    db.create_project("demo", "Demo")
    response = client.post(
        "/p/demo/settings",
        data={"display_name": "Demo", "admin_password": "  ", "auth_level": "STUDY"},
        follow_redirects=False,
    )
    assert "password%20cannot%20be%20empty" in response.headers["location"]


@pytest.mark.slow
@pytest.mark.skipif(not REAL_OTREEZIP.exists(), reason="no otreezip fixture")
def test_upload_deploy_activate_and_download(client):
    # 1. Upload -> a deploy job runs to completion
    response = _upload(client, REAL_OTREEZIP, "Trust Game.otreezip")
    assert response.status_code == 303
    job_id = response.headers["location"].rsplit("/", 1)[-1]
    job = _wait_for_job(job_id)
    assert job.status == "success", "\n".join(job.lines)
    slug = "trust_game"

    # The job page and its replayed log render.
    assert client.get(f"/jobs/{job_id}").status_code == 200

    # 2. It shows up, stopped, with a release
    listing = client.get("/").text
    assert "Trust Game" in listing
    assert slug in listing

    # 3. Activating gives it port 80
    activated = client.post(f"/p/{slug}/activate", follow_redirects=False)
    assert deploy.active_slug() == slug
    # The notice travels as its untranslated catalogue key. It used to be an f-string,
    # which left the Chinese entry keyed on "{port}" permanently unmatched.
    assert "now%20serving%20on%20port%20%7Bport%7D" in activated.headers["location"]
    assert "port 80" in client.get("/").text

    # 4. Project page renders with everything on it
    page = client.get(f"/p/{slug}").text
    assert "Releases" in page and "Danger zone" in page

    # 5. Data page: no data yet
    assert "No data yet" in client.get(f"/p/{slug}/data").text

    # 6. With a session present, it is listed and downloadable while STOPPED --
    #    the property that makes exports work without disturbing port 80.
    code = create_session(slug)
    deploy.stop(slug)
    assert deploy.active_slug() is None

    data_page = client.get(f"/p/{slug}/data").text
    assert code in data_page
    assert "not currently running" in data_page

    download = client.get(f"/p/{slug}/data/wide.csv")
    assert download.status_code == 200, download.text[:2000]
    assert "participant.code" in download.text or "participant" in download.text
    assert download.headers["content-type"].startswith("text/csv")
    assert "attachment" in download.headers["content-disposition"]

    download_one = client.get(f"/p/{slug}/data/wide.csv", params={"session_code": code})
    assert download_one.status_code == 200


@pytest.mark.slow
@pytest.mark.skipif(not REAL_OTREEZIP.exists(), reason="no otreezip fixture")
def test_reset_database_takes_a_backup(client):
    response = _upload(client, REAL_OTREEZIP, "study.otreezip")
    job = _wait_for_job(response.headers["location"].rsplit("/", 1)[-1])
    assert job.status == "success", "\n".join(job.lines)
    slug = "study"

    create_session(slug)
    assert len(postgres.list_sessions(slug)) == 1

    client.post(f"/p/{slug}/reset-db", data={"confirm": slug}, follow_redirects=False)

    assert postgres.list_sessions(slug) == [], "reset should have cleared the sessions"
    backups = list(config.get_settings().backup_dir(slug).glob("*.sql.gz"))
    assert backups, "a backup must be taken before wiping data"

    # And that backup is downloadable through the UI.
    got = client.get(f"/p/{slug}/backups/{backups[0].name}")
    assert got.status_code == 200


def _forms(page: str, action: str) -> list[str]:
    return [chunk.split("</form>")[0] for chunk in page.split("<form")[1:] if action in chunk]


@pytest.mark.slow
def test_every_blocked_action_offers_an_explicit_override(client, monkeypatch):
    """The page promises an override for each blocked action (m2)."""
    job_id = _upload(client, REAL_OTREEZIP, "study.otreezip").headers["location"].split("/")[-1]
    assert _wait_for_job(job_id).status == "success"
    deploy.activate("study")
    monkeypatch.setattr(postgres, "count_active_participants", lambda slug, *a: 2)
    monkeypatch.setattr(
        postgres, "project_stats",
        lambda slug, *a: postgres.Stats(has_tables=True, session_count=1, active_participants=2),
    )
    # A second release, so there is something to roll back to.
    job_id = _upload(client, REAL_OTREEZIP, "study.otreezip", slug="study", force="true"
                     ).headers["location"].split("/")[-1]
    assert _wait_for_job(job_id).status == "success"

    page = client.get("/p/study").text
    for action in ("/p/study/stop", "/p/study/restart", "/p/study/rollback"):
        assert any('name="force"' in form for form in _forms(page, action)), action
    assert 'name="force"' in client.get("/upload?slug=study").text


@pytest.mark.slow
def test_activate_anyway_appears_for_the_live_projects_participants_not_this_ones(
    client, monkeypatch
):
    for name in ("live", "next"):
        job_id = _upload(client, REAL_OTREEZIP, f"{name}.otreezip").headers["location"].split("/")[-1]
        assert _wait_for_job(job_id).status == "success"
    deploy.activate("live")
    active = {"live": 3, "next": 0}
    monkeypatch.setattr(postgres, "count_active_participants",
                        lambda slug, *a: active.get(slug, 0))

    page = client.get("/p/next").text
    assert any('name="force"' in form for form in _forms(page, "/p/next/activate"))
    assert "3 participant(s) in live" in page

    active["live"] = 0
    page = client.get("/p/next").text
    assert not any('name="force"' in form for form in _forms(page, "/p/next/activate"))


def test_the_participant_address_is_escaped_even_if_the_host_header_is_not(client):
    """N7: the address is built from the request's Host header and shown on the page."""
    from otreedeploy import app as app_module

    class FakeURL:
        hostname = '"><script>alert(1)</script>'

    origin = app_module._experiment_origin(type("R", (), {"url": FakeURL()})())
    rendered = app_module.templates.env.from_string(
        "{{ ('<span>' ~ (o | e) ~ '</span>') | safe }}").render(o=origin)
    assert "<script>" not in rendered and "&lt;script&gt;" in rendered
