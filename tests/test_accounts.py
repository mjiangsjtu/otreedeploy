"""Accounts, per-project access, the audit trail and retention.

The threat model is a shared lab server: several groups, each with its own studies and
its own promises about who sees the data, and participants at machines on the same
network. So these tests care most about the ways a page might be reachable by the
wrong person -- signed out, signed in but not a member, a member but not an admin.
"""

from __future__ import annotations

import gzip
import io
import json
import os
import time
import types

import pytest
from fastapi.testclient import TestClient

from otreedeploy import access, accounts, audit, auth, config, db, deploy, retention, systemd

PASSWORD = "correct horse battery"


@pytest.fixture(autouse=True)
def fast_hashing(monkeypatch):
    """The real iteration count costs ~0.3s per sign-in, on purpose; the route matrix
    signs in hundreds of times. Hashes made here still verify the same way."""
    monkeypatch.setattr(accounts, "ITERATIONS", 1000)


@pytest.fixture(autouse=True)
def clean_rate_limits():
    auth.reset_rate_limits()
    yield
    auth.reset_rate_limits()


@pytest.fixture
def app_client(root):
    systemd.reset_fake_state()
    db.init()
    accounts._initialised.clear()
    accounts.init()
    from otreedeploy.app import app

    def make():
        return TestClient(app, follow_redirects=False, headers={"Origin": "http://testserver"})

    with make() as client:
        client.make = make  # type: ignore[attr-defined]
        yield client


@pytest.fixture
def people(app_client):
    """ada (admin), bob (member of alpha), eve (member of beta)."""
    accounts.create_user("ada", PASSWORD, is_admin=True)
    accounts.create_user("bob", PASSWORD)
    accounts.create_user("eve", PASSWORD)
    for slug in ("alpha", "beta"):
        db.create_project(slug, slug)
    accounts.grant("bob", "alpha")
    accounts.grant("eve", "beta")
    return app_client


def sign_in(client, name, password=PASSWORD):
    return client.post("/login", data={"username": name, "password": password})


def as_user(client, name):
    other = client.make()
    other.__enter__()
    assert sign_in(other, name).status_code == 303
    return other


# --------------------------------------------------------------- the route table


def _concrete(path: str) -> str:
    return (path.replace("{slug}", "alpha").replace("{code}", "en")
            .replace("{job_id}", "nosuchjob").replace("{kind}", "wide")
            .replace("{name}", "bob" if path.startswith("/users") else "x.sql.gz"))


def test_every_route_has_an_access_rule():
    """A page added without deciding who may see it is a page open to everybody."""
    from otreedeploy.app import app

    missing = []
    for route in app.routes:
        for method in getattr(route, "methods", None) or {"GET"}:
            if method != "HEAD" and (method, route.path) not in access.RULES:
                missing.append(f"{method} {route.path}")
    assert missing == []


def test_a_route_without_a_rule_is_refused(people, monkeypatch):
    monkeypatch.delitem(access.RULES, ("GET", "/account"))
    client = as_user(people, "ada")
    response = client.get("/account")
    assert response.status_code == 500
    assert "no access rule" in response.text


MIDDLEWARE_REFUSALS = {"There is no such project.", "Only an admin can use this page."}


def _refused(response) -> bool:
    # The sign-in redirect always says where to come back to; signing out, which also
    # ends on the sign-in page, does not.
    if response.status_code == 303 and response.headers["location"].startswith("/login?next="):
        return True
    if response.status_code == 401:
        return True
    return response.text in MIDDLEWARE_REFUSALS


@pytest.mark.parametrize("method,path", sorted(access.RULES))
@pytest.mark.parametrize("who", ["nobody", "eve", "bob", "ada"])
def test_each_route_admits_exactly_whom_its_rule_names(people, method, path, who):
    rule = access.RULES[(method, path)]
    client = people.make() if who == "nobody" else as_user(people, who)
    if who == "nobody":
        client.__enter__()
    before = (len(db.list_projects()), len(accounts.list_users()))
    url = _concrete(path)
    if path == "/static":
        url = "/static/style.css"
    response = client.request(method, url, headers={"Accept": "text/html"})

    allowed = {
        access.PUBLIC: True,
        access.SIGNED_IN: who != "nobody",
        access.MEMBER: who in {"bob", "ada"},
        access.ADMIN: who == "ada",
    }[rule]
    assert _refused(response) is (not allowed), (who, method, path, response.status_code,
                                                 response.text[:200])
    if not allowed and method == "POST":
        assert (len(db.list_projects()), len(accounts.list_users())) == before


def test_a_log_stream_gets_a_status_rather_than_a_sign_in_page(people):
    response = people.get("/p/alpha/logs/stream", headers={"Accept": "text/event-stream"})
    assert response.status_code == 401


def test_the_project_list_shows_only_your_own_projects(people):
    page = as_user(people, "eve").get("/").text
    assert "beta" in page and "alpha" not in page
    page = as_user(people, "ada").get("/").text
    assert "alpha" in page and "beta" in page


# ------------------------------------------------------------------ signing in


def test_signing_in_with_the_right_password_opens_the_app(people):
    assert sign_in(people, "bob").status_code == 303
    assert people.get("/").status_code == 200


@pytest.mark.parametrize("name,password", [("bob", "wrong password"), ("nobody", PASSWORD),
                                           ("", PASSWORD)])
def test_a_wrong_name_or_password_does_not_sign_you_in(people, name, password):
    response = sign_in(people, name, password)
    assert response.status_code == 200
    assert "wrong user name or password" in response.text
    assert people.get("/").status_code == 303


def test_a_disabled_account_cannot_sign_in(people):
    accounts.set_disabled("bob", True)
    assert sign_in(people, "bob").status_code == 200


@pytest.mark.parametrize("target", ["//evil.example/x", "https://evil.example", "/\\evil"])
def test_signing_in_never_redirects_off_this_app(people, target):
    response = people.post("/login", data={"username": "bob", "password": PASSWORD,
                                           "next": target})
    assert response.headers["location"] == "/"


def test_the_sign_in_page_can_be_read_in_chinese_before_signing_in(people):
    people.get("/lang/zh?next=/login")
    people.cookies.set("otreedeploy_lang", "zh")
    assert "登录" in people.get("/login").text


def test_signing_out_ends_the_session_for_good(people):
    client = as_user(people, "bob")
    token = client.cookies.get(access.COOKIE)
    client.post("/logout")
    replay = people.make()
    replay.__enter__()
    replay.cookies.set(access.COOKIE, token)
    assert replay.get("/").status_code == 303  # a copied cookie is dead too (N12)


def test_disabling_an_account_ends_its_sessions(people):
    client = as_user(people, "bob")
    accounts.set_disabled("bob", True)
    assert client.get("/").status_code == 303


def test_an_admin_resetting_a_password_ends_that_users_sessions(people):
    client = as_user(people, "bob")
    as_user(people, "ada").post("/users/bob/password")
    assert client.get("/").status_code == 303


def test_changing_your_own_password_keeps_you_signed_in_here_only(people):
    here = as_user(people, "bob")
    elsewhere = as_user(people, "bob")
    response = here.post("/account/password", data={
        "current_password": PASSWORD, "new_password": "another long one",
        "confirm_password": "another long one"})
    assert "error" not in response.headers["location"]
    assert here.get("/").status_code == 200
    assert elsewhere.get("/").status_code == 303
    assert accounts.verify("bob", "another long one")


def test_changing_your_password_needs_the_current_one(people):
    client = as_user(people, "bob")
    response = client.post("/account/password", data={
        "current_password": "not it", "new_password": "another long one",
        "confirm_password": "another long one"})
    assert "error" in response.headers["location"]
    assert accounts.verify("bob", PASSWORD)


def test_guessing_one_name_does_not_lock_out_a_colleague(people):
    for _ in range(auth.MAX_FAILURES):
        sign_in(people, "bob", "wrong guess")
    assert "too many attempts" in sign_in(people, "bob").text  # even the right one
    assert sign_in(people, "eve").status_code == 303


def test_password_hashes_are_not_kept_beside_the_deployment_metadata(people):
    path = config.get_settings().state_dir / "accounts.db"
    assert path.stat().st_mode & 0o777 == 0o600
    assert PASSWORD.encode() not in path.read_bytes()
    assert b"pbkdf2" not in config.get_settings().db_path.read_bytes()


def test_there_must_always_be_an_admin(people):
    with pytest.raises(accounts.AccountError, match="at least one admin"):
        accounts.set_admin("ada", False)
    with pytest.raises(accounts.AccountError, match="at least one admin"):
        accounts.set_disabled("ada", True)


# --------------------------------------------------------------------- migration


def _legacy_password_file(password: str) -> None:
    salt = os.urandom(16)
    path = config.get_settings().state_dir / "auth.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({
        "algorithm": auth.ALGORITHM, "iterations": 1000, "salt": salt.hex(),
        "hash": auth._derive(password, salt, 1000).hex(), "secret": os.urandom(32).hex()}))


def test_the_shared_password_of_an_older_install_becomes_the_admin_account(root):
    db.init()
    db.create_project("study", "study")
    _legacy_password_file("the old shared one")
    accounts._initialised.clear()

    accounts.init()

    admin = accounts.verify("admin", "the old shared one")
    assert admin is not None and admin.is_admin
    assert "study" in accounts.projects_of("admin")
    state = config.get_settings().state_dir
    assert not (state / "auth.json").exists() and (state / "auth.json.migrated").exists()


def test_a_damaged_old_password_file_locks_the_app_rather_than_opening_it(root):
    db.init()
    (config.get_settings().state_dir / "auth.json").write_text("{not json")
    accounts._initialised.clear()
    from otreedeploy.app import app

    with TestClient(app, follow_redirects=False) as client:
        assert client.get("/").status_code == 503


def test_a_real_server_without_any_account_is_locked(root, monkeypatch):
    db.init()
    accounts._initialised.clear()
    monkeypatch.setattr(access, "config", types.SimpleNamespace(dry_run=lambda: False))
    from otreedeploy.app import app

    with TestClient(app, follow_redirects=False) as client:
        response = client.get("/")
        assert response.status_code == 503
        assert "otreedeploy-users add" in response.text
        assert client.get("/login").status_code == 503


def test_the_command_line_creates_and_lists_accounts(root, monkeypatch, capsys):
    db.init()
    accounts._initialised.clear()
    with pytest.raises(SystemExit) as exit_:
        accounts.main(["--check"])
    assert exit_.value.code == 1
    monkeypatch.setattr("sys.stdin", io.StringIO(PASSWORD + "\n"))
    accounts.main(["add", "ada", "--admin", "--stdin"])
    with pytest.raises(SystemExit) as exit_:
        accounts.main(["--check"])
    assert exit_.value.code == 0
    accounts.main(["list"])
    assert "ada (admin)" in capsys.readouterr().out


# ------------------------------------------------------------- projects and access


def test_a_new_upload_makes_you_a_member_of_its_project(people):
    client = as_user(people, "eve")
    response = client.post("/upload", files={"file": ("gamma.otreezip", b"x", "application/gzip")})
    job = deploy.get_job(response.headers["location"].rsplit("/", 1)[-1])
    assert job.slug == "gamma"
    assert "gamma" in accounts.projects_of("eve")


def test_uploading_a_file_named_after_somebody_elses_project_leaves_theirs_alone(people):
    client = as_user(people, "eve")
    response = client.post("/upload", files={"file": ("alpha.otreezip", b"x", "application/gzip")})
    job = deploy.get_job(response.headers["location"].rsplit("/", 1)[-1])
    assert job.slug != "alpha"
    assert "alpha" not in accounts.projects_of("eve")


def test_a_non_member_cannot_upload_a_new_version_of_a_project(people):
    client = as_user(people, "eve")
    response = client.post("/upload", data={"slug": "alpha"},
                           files={"file": ("x.otreezip", b"x", "application/gzip")})
    assert "/jobs/" not in response.headers["location"]


def test_a_deploy_log_is_visible_to_its_projects_members_only(people):
    response = as_user(people, "bob").post(
        "/upload", data={"slug": "alpha"}, files={"file": ("x.otreezip", b"x", "application/gzip")})
    job_page = response.headers["location"]
    assert as_user(people, "bob").get(job_page).status_code == 200
    assert "expired" in as_user(people, "eve").get(job_page).headers.get("location", "") + \
        "expired"  # redirected away, not shown
    assert as_user(people, "eve").get(job_page).status_code == 303


def test_forcing_past_another_groups_live_study_needs_membership_of_it(people):
    systemd._fake.enabled.add("alpha")
    systemd._fake.active.add("alpha")
    response = as_user(people, "eve").post("/p/beta/activate", data={"force": "true"})
    location = response.headers["location"]
    assert "another%20group" in location and "bob" in location
    assert "alpha" not in location  # whom to ask, without naming their project


def test_the_live_project_is_named_only_to_those_who_may_see_it(people):
    systemd._fake.enabled.add("alpha")
    systemd._fake.active.add("alpha")
    assert "alpha" not in as_user(people, "eve").get("/").text
    assert "alpha" in as_user(people, "bob").get("/").text


def test_members_can_give_and_take_access(people):
    bob = as_user(people, "bob")
    bob.post("/p/alpha/members/add", data={"name": "eve"})
    assert accounts.can_access(accounts.get_user("eve"), "alpha")
    bob.post("/p/alpha/members/remove", data={"name": "eve"})
    assert not accounts.can_access(accounts.get_user("eve"), "alpha")


# ------------------------------------------------------------------- audit trail


def test_downloading_a_backup_is_recorded_with_who_and_what(people):
    directory = config.get_settings().backup_dir("alpha")
    directory.mkdir(parents=True)
    (directory / "20260101-000000-before-reset.sql.gz").write_bytes(gzip.compress(b"x"))
    as_user(people, "bob").get("/p/alpha/backups/20260101-000000-before-reset.sql.gz")

    entries = audit.read(project="alpha")
    assert any(e.action == "download_backup" and e.user == "bob"
               and e.detail == "20260101-000000-before-reset.sql.gz" for e in entries)


def test_sign_ins_and_failures_are_recorded(people):
    sign_in(people, "bob", "nope")
    sign_in(people, "bob")
    actions = [(e.user, e.action) for e in audit.read()]
    assert ("bob", "sign_in_failed") in actions and ("bob", "sign_in") in actions


def test_the_history_records_who_acted(people):
    as_user(people, "bob").post("/p/alpha/members/add", data={"name": "eve"})
    assert any(e.user == "bob" and e.action == "access_granted" for e in audit.read())


# ------------------------------------------------------------ retention and delete


def _aged(path, days):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"x")
    then = time.time() - days * 86400
    os.utime(path, (then, then))
    return path


def test_nothing_is_deleted_until_a_retention_period_is_set(root):
    db.init()
    old = _aged(config.get_settings().backup_dir("gone") / "a.sql.gz", 1000)
    assert retention.sweep() == []
    assert old.exists()


def test_the_sweep_deletes_what_has_expired_but_keeps_each_projects_newest_backup(root):
    db.init()
    db.create_project("alpha", "alpha")
    settings = config.get_settings()
    oldest = _aged(settings.backup_dir("alpha") / "1.sql.gz", 400)
    newest = _aged(settings.backup_dir("alpha") / "2.sql.gz", 300)
    upload = _aged(settings.upload_dir("alpha") / "x.otreezip", 50)
    orphan = _aged(settings.backup_dir("gone") / "3.sql.gz", 400)
    retention.set_periods(backups=30, uploads=None)

    removed = set(retention.sweep())

    assert removed == {oldest, orphan}
    assert newest.exists() and upload.exists()
    assert sum(e.action == "retention_delete" for e in audit.read()) == 2


def test_deleting_keeps_the_data_aside_and_a_reused_name_starts_clean(root):
    db.init()
    db.create_project("alpha", "alpha")
    settings = config.get_settings()
    _aged(settings.backup_dir("alpha") / "1.sql.gz", 1)
    deploy.delete_project("alpha")

    assert not settings.backup_dir("alpha").exists()
    archives = retention.deleted_archives()
    assert len(archives) == 1 and archives[0]["name"].startswith("alpha-")

    db.create_project("alpha", "alpha")  # a new project with the same name
    from otreedeploy import app as app_module

    assert app_module._backups("alpha") == []


def test_deleting_with_purge_leaves_no_data_behind(root):
    db.init()
    db.create_project("alpha", "alpha")
    settings = config.get_settings()
    _aged(settings.backup_dir("alpha") / "1.sql.gz", 1)
    _aged(settings.upload_dir("alpha") / "x.otreezip", 1)

    assert deploy.delete_project("alpha", purge=True) is None

    assert not settings.backup_dir("alpha").exists()
    assert not settings.upload_dir("alpha").exists()
    assert retention.deleted_archives() == []


def test_purging_needs_its_own_typed_confirmation(people):
    response = as_user(people, "bob").post(
        "/p/alpha/delete", data={"confirm": "alpha", "purge": "true", "confirm_purge": ""})
    assert "delete%20all%20data" in response.headers["location"]
    assert db.get_project("alpha") is not None


# ------------------------------------------------------------ forged writes (m1)


def _post_restart(client, **headers):
    client.headers.pop("Origin", None)
    return client.post("/p/alpha/restart", data={"force": "true"}, headers=headers)


def test_a_form_posted_from_the_experiment_port_is_refused(people):
    client = as_user(people, "bob")
    assert _post_restart(client, Origin="http://testserver:80").status_code == 403
    assert _post_restart(client, Origin="http://testserver:8080").status_code == 403


def test_a_write_with_neither_origin_nor_referer_is_refused(people):
    assert _post_restart(as_user(people, "bob")).status_code == 403


def test_a_referer_from_another_port_is_refused_and_from_this_app_accepted(people):
    client = as_user(people, "bob")
    assert _post_restart(client, Referer="http://testserver:80/p/x/Page/1").status_code == 403
    assert _post_restart(client, Referer="http://testserver/p/alpha").status_code != 403


def test_the_sign_in_form_is_protected_too(people):
    people.headers.pop("Origin", None)
    response = people.post("/login", data={"username": "bob", "password": PASSWORD},
                           headers={"Origin": "http://evil.example"})
    assert response.status_code == 403
