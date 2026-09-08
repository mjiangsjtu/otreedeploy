"""The password that keeps participants out of the management app.

The threat these describe is mundane and specific: a subject sitting at a lab machine
finds port 8000, and stops the study or downloads everybody's data. So the tests care
most about the ways in which a page might be reachable without signing in.
"""

from __future__ import annotations

import json
import time

import pytest
from fastapi.testclient import TestClient

from otreedeploy import auth, db, systemd

PASSWORD = "correct horse battery"


@pytest.fixture(autouse=True)
def clean_rate_limits():
    auth.reset_rate_limits()
    yield
    auth.reset_rate_limits()


@pytest.fixture
def client(root):
    systemd.reset_fake_state()
    db.init()
    from otreedeploy.app import app

    with TestClient(app, follow_redirects=False) as test_client:
        yield test_client


@pytest.fixture
def guarded(client):
    """A client whose server has a password set, and which has not signed in."""
    auth.set_password(PASSWORD)
    return client


def sign_in(client, password=PASSWORD):
    return client.post("/login", data={"password": password})


# ------------------------------------------------------------------ the stored secret


def test_a_fresh_install_has_no_password(root):
    assert not auth.is_configured()
    assert not auth.verify_password("anything")


def test_the_stored_password_is_readable_only_by_the_app(root):
    path = auth.set_password(PASSWORD)
    assert path.stat().st_mode & 0o777 == 0o600


def test_the_password_itself_is_not_written_to_disk(root):
    path = auth.set_password(PASSWORD)
    assert PASSWORD not in path.read_text()


def test_the_right_password_verifies_and_a_near_miss_does_not(root):
    auth.set_password(PASSWORD)
    assert auth.verify_password(PASSWORD)
    assert not auth.verify_password(PASSWORD + " ")
    assert not auth.verify_password("")


def test_a_password_below_the_minimum_length_is_refused(root):
    with pytest.raises(ValueError):
        auth.set_password("short")
    assert not auth.is_configured()


def test_setting_a_new_password_replaces_the_old_one(root):
    auth.set_password(PASSWORD)
    auth.set_password("a different one entirely")
    assert not auth.verify_password(PASSWORD)
    assert auth.verify_password("a different one entirely")


# ------------------------------------------------------------------- session tokens


def test_a_freshly_issued_token_is_accepted(root):
    auth.set_password(PASSWORD)
    assert auth.check_token(auth.issue_token())


def test_a_tampered_token_is_refused(root):
    auth.set_password(PASSWORD)
    expires, _, signature = auth.issue_token().partition(".")
    # Push the expiry out by a year while keeping the signature that covered the old one.
    assert not auth.check_token(f"{int(expires) + 31_536_000}.{signature}")
    assert not auth.check_token(f"{expires}.{signature[:-1]}x")
    assert not auth.check_token("nonsense")
    assert not auth.check_token("")


def test_a_token_stops_working_once_it_expires(root):
    auth.set_password(PASSWORD)
    stale = auth.issue_token(now=time.time() - auth.SESSION_MAX_AGE - 60)
    assert not auth.check_token(stale)


def test_changing_the_password_invalidates_existing_tokens(root):
    auth.set_password(PASSWORD)
    token = auth.issue_token()
    auth.set_password("something else that is long")
    assert not auth.check_token(token)


# --------------------------------------------------------------------- what is guarded


def test_without_a_password_the_app_serves_as_it_always_did(client):
    assert client.get("/").status_code == 200
    assert client.get("/system").status_code == 200


def test_a_page_with_no_password_says_so(client, monkeypatch):
    # The banner is for a real server; a dry run on a laptop is not what it warns about.
    monkeypatch.delenv("OTREEDEPLOY_DRY_RUN")
    assert "No password is set." in client.get("/").text
    monkeypatch.setenv("OTREEDEPLOY_DRY_RUN", "1")
    assert "No password is set." not in client.get("/").text


@pytest.mark.parametrize(
    "path",
    ["/", "/upload", "/system", "/p/study", "/p/study/data", "/p/study/logs", "/jobs/x"],
)
def test_every_page_sends_a_stranger_to_the_login_form(guarded, path):
    response = guarded.get(path)
    assert response.status_code == 303
    assert response.headers["location"].startswith("/login?next=")


def test_an_action_cannot_be_posted_without_signing_in(guarded):
    response = guarded.post("/p/study/stop", data={})
    assert response.status_code == 303
    assert response.headers["location"].startswith("/login")


def test_a_download_cannot_be_fetched_without_signing_in(guarded):
    assert guarded.get("/p/study/data/all_apps_wide.csv").status_code == 303


def test_the_login_page_and_its_stylesheet_are_reachable(guarded):
    assert guarded.get("/login").status_code == 200
    assert guarded.get("/static/style.css").status_code == 200


def test_the_login_page_can_be_switched_to_chinese_before_signing_in(guarded):
    switch = guarded.get("/lang/zh?next=/login")
    assert switch.status_code == 303
    guarded.cookies.set("otreedeploy_lang", "zh")
    page = guarded.get("/login")
    assert "登录" in page.text
    assert "Sign in" not in page.text


def test_the_login_page_offers_no_way_into_the_app(guarded):
    page = guarded.get("/login").text
    for destination in ('href="/upload"', 'href="/system"'):
        assert destination not in page


def test_a_log_stream_gets_a_status_rather_than_a_login_page(guarded):
    """EventSource cannot act on a redirect to HTML; it needs to see the refusal."""
    response = guarded.get(
        "/p/study/logs/stream", headers={"accept": "text/event-stream"}
    )
    assert response.status_code == 401


# ------------------------------------------------------------------------- signing in


def test_signing_in_with_the_right_password_opens_the_app(guarded):
    response = sign_in(guarded)
    assert response.status_code == 303
    assert response.headers["location"] == "/"
    assert guarded.get("/").status_code == 200


def test_the_wrong_password_does_not_sign_you_in(guarded):
    response = sign_in(guarded, "not the password")
    assert response.status_code == 200
    assert "wrong password" in response.text
    assert auth.COOKIE not in response.cookies
    assert guarded.get("/").status_code == 303


def test_signing_in_returns_you_to_the_page_you_asked_for(guarded):
    response = guarded.post("/login", data={"password": PASSWORD, "next": "/system"})
    assert response.headers["location"] == "/system"


@pytest.mark.parametrize(
    "target", ["//evil.example.com", "https://evil.example.com", "javascript:alert(1)"]
)
def test_signing_in_never_redirects_off_this_app(guarded, target):
    response = guarded.post("/login", data={"password": PASSWORD, "next": target})
    assert response.headers["location"] == "/"


def test_signing_out_ends_the_session(guarded):
    sign_in(guarded)
    assert guarded.get("/").status_code == 200
    response = guarded.post("/logout")
    assert response.status_code == 303
    assert guarded.get("/").status_code == 303


def test_repeated_wrong_guesses_are_locked_out(guarded):
    for _ in range(auth.MAX_FAILURES):
        sign_in(guarded, "wrong")
    response = sign_in(guarded, PASSWORD)
    assert response.status_code == 200
    assert "too many attempts" in response.text
    # Even the right password waits: otherwise the lockout would leak which guess was
    # correct by letting exactly one of them through.
    assert guarded.get("/").status_code == 303


def test_a_successful_sign_in_forgets_earlier_failures(guarded):
    for _ in range(auth.MAX_FAILURES - 1):
        sign_in(guarded, "wrong")
    assert sign_in(guarded).status_code == 303
    for _ in range(auth.MAX_FAILURES - 1):
        sign_in(guarded, "wrong")
    assert "too many attempts" not in sign_in(guarded).text


# -------------------------------------------------------------- changing the password


def test_the_password_can_be_changed_from_the_system_page(guarded):
    sign_in(guarded)
    response = guarded.post(
        "/password",
        data={
            "current_password": PASSWORD,
            "new_password": "a whole new password",
            "confirm_password": "a whole new password",
        },
    )
    assert response.status_code == 303
    assert auth.verify_password("a whole new password")
    # The browser that made the change keeps working, on a cookie signed by the new
    # secret; every other one is out.
    assert guarded.get("/").status_code == 200


def test_changing_the_password_needs_the_current_one(guarded):
    sign_in(guarded)
    response = guarded.post(
        "/password",
        data={
            "current_password": "wrong",
            "new_password": "a whole new password",
            "confirm_password": "a whole new password",
        },
    )
    assert "error=" in response.headers["location"]
    assert auth.verify_password(PASSWORD)


def test_a_mistyped_new_password_changes_nothing(guarded):
    sign_in(guarded)
    guarded.post(
        "/password",
        data={
            "current_password": PASSWORD,
            "new_password": "a whole new password",
            "confirm_password": "a whole new pasword",
        },
    )
    assert auth.verify_password(PASSWORD)


def test_a_too_short_new_password_changes_nothing(guarded):
    sign_in(guarded)
    guarded.post(
        "/password",
        data={"current_password": PASSWORD, "new_password": "abc", "confirm_password": "abc"},
    )
    assert auth.verify_password(PASSWORD)


def test_the_first_password_cannot_be_claimed_through_the_web(client):
    """Until one is set the app is open, so anyone on the LAN could set it."""
    response = client.post(
        "/password",
        data={
            "current_password": "",
            "new_password": "claimed by a stranger",
            "confirm_password": "claimed by a stranger",
        },
    )
    assert response.status_code == 303
    assert not auth.is_configured()


# -------------------------------------------------------------------------- failure


def test_a_damaged_password_file_locks_the_app_rather_than_opening_it(guarded, root):
    (root / "var/lib/otreedeploy/auth.json").write_text("{ not json")
    response = guarded.get("/")
    assert response.status_code == 503


def test_an_unknown_hash_algorithm_locks_the_app(guarded, root):
    path = root / "var/lib/otreedeploy/auth.json"
    payload = json.loads(path.read_text())
    payload["algorithm"] = "rot13"
    path.write_text(json.dumps(payload))
    assert guarded.get("/").status_code == 503
