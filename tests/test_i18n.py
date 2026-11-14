"""English and Simplified Chinese.

The catalogue is a plain dict keyed by English source text, so the risks are not the
usual gettext ones. They are: a translated string that drops a placeholder (silently
losing a port number), a template that raises only in one language, and a language
switcher that can be pointed off-site.
"""

from __future__ import annotations

import re

import pytest
from fastapi.testclient import TestClient

from otreedeploy import db, i18n, systemd

PLACEHOLDER = re.compile(r"\{(\w+)\}")


@pytest.fixture
def client(root):
    systemd.reset_fake_state()
    db.init()
    from otreedeploy.app import app

    with TestClient(app) as test_client:
        yield test_client


# ---------------------------------------------------------------- the catalogue


@pytest.mark.parametrize("english,chinese", sorted(i18n.ZH.items()))
def test_translation_keeps_every_placeholder(english, chinese):
    """A dropped {port} or {n} would render a sentence with a hole where a value goes."""
    assert set(PLACEHOLDER.findall(chinese)) == set(PLACEHOLDER.findall(english)), (
        f"placeholder mismatch for {english!r}"
    )


@pytest.mark.parametrize("english,chinese", sorted(i18n.ZH.items()))
def test_translation_is_present(english, chinese):
    assert chinese.strip(), f"empty translation for {english!r}"


def test_untranslated_text_falls_back_to_english():
    assert i18n.translate("zh", "a string nobody has translated") == (
        "a string nobody has translated"
    )


def test_placeholders_are_filled_in_both_languages():
    assert i18n.translate("en", "port {port}", port=80) == "port 80"
    assert "8080" in i18n.translate("zh", "port {port}", port=8080)


def test_text_containing_stray_braces_does_not_raise():
    """Error messages pass through translate; some contain braces of their own."""
    message = "unexpected {thing} in the archive"
    assert i18n.translate("zh", message, port=80) == message


@pytest.mark.parametrize(
    "given,expected",
    [("zh", "zh"), ("zh-CN", "zh"), ("zh_Hans_CN", "zh"), ("en-US", "en"),
     ("fr", "en"), ("", "en"), (None, "en"), ("  ZH  ", "zh")],
)
def test_language_codes_are_normalized(given, expected):
    assert i18n.normalize(given) == expected


def test_relative_times_use_the_right_unit():
    """The version this replaced divided seconds by 3600 and called them 'mins'."""
    import time

    now = time.time()
    assert i18n.ago("en", now - 90) == "1 min ago"
    assert i18n.ago("en", now - 3700) == "1 hour ago"
    assert i18n.ago("en", now - 90000) == "1 day ago"
    assert i18n.ago("zh", now - 3700) == "1 小时前"


# ------------------------------------------------------------------- the switcher


def test_english_is_the_default(client):
    body = client.get("/").text
    assert 'lang="en"' in body
    assert "Experiments" in body
    assert "实验项目" not in body


def test_accept_language_does_not_override_the_default(client):
    """Chinese is something you choose, not something a browser header decides."""
    body = client.get("/", headers={"Accept-Language": "zh-CN,zh;q=0.9"}).text
    assert 'lang="en"' in body


def test_switching_to_chinese_sticks(client):
    client.get("/lang/zh?next=/")
    body = client.get("/").text
    assert 'lang="zh-Hans"' in body
    assert "实验项目" in body
    assert "No experiments yet." not in body

    client.get("/lang/en?next=/")
    assert "No experiments yet." in client.get("/").text


def test_an_unknown_language_falls_back_to_english(client):
    client.get("/lang/fr?next=/")
    assert 'lang="en"' in client.get("/").text


@pytest.mark.parametrize("target", ["//evil.example.com", "https://evil.example.com", "javascript:alert(1)"])
def test_the_switcher_cannot_be_pointed_off_site(client, target):
    response = client.get(f"/lang/zh?next={target}", follow_redirects=False)
    assert response.headers["location"] == "/"


def test_the_switcher_returns_to_the_page_you_were_on(client):
    response = client.get("/lang/zh?next=/system", follow_redirects=False)
    assert response.headers["location"] == "/system"


@pytest.mark.parametrize("lang", ["en", "zh"])
@pytest.mark.parametrize("path", ["/", "/upload", "/system"])
def test_every_page_renders_in_both_languages(client, lang, path):
    client.get(f"/lang/{lang}?next=/")
    assert client.get(path).status_code == 200


@pytest.mark.slow
def test_the_project_and_data_pages_render_in_both_languages(client):
    """These two need a deployed project, so they are out of reach of the fast sweep --
    and they are the pages carrying the most translated text."""
    from otreedeploy import deploy

    from .conftest import REAL_OTREEZIP

    if not REAL_OTREEZIP.exists():
        pytest.skip("real otreezip fixture not present")

    slug = deploy.deploy(deploy.new_job("i18n"), REAL_OTREEZIP, "study.otreezip")

    client.get("/lang/zh?next=/")
    project = client.get(f"/p/{slug}")
    assert project.status_code == 200
    assert "危险操作" in project.text, "the project page should be in Chinese"
    assert "Danger zone" not in project.text
    assert client.get(f"/p/{slug}/data").status_code == 200

    client.get("/lang/en?next=/")
    project = client.get(f"/p/{slug}")
    assert "Danger zone" in project.text
    assert "危险操作" not in project.text

    from otreedeploy import otreeapi

    otreeapi.pool.shutdown()


def test_a_notice_carried_through_a_redirect_is_translated_on_arrival(client):
    """Notices travel in the URL as English keys and are looked up when rendered."""
    from otreedeploy import app as app_module

    client.cookies.set(i18n.COOKIE, "zh")
    page = client.get(app_module._with_message("/", "now serving on port {port}")).text
    assert "已在端口 80 上提供服务" in page
    assert "now serving on port" not in page


def test_a_message_in_the_url_that_this_app_did_not_sign_is_not_shown(client):
    page = client.get("/?error=Your+password+has+expired.+Re-enter+it+at+evil.example").text
    assert "evil.example" not in page


def test_a_format_spec_in_untrusted_text_is_shown_literally_not_executed():
    spec = "{port:>999999999}"
    assert i18n.message("en", spec, port=80) == spec
    assert i18n.message("zh", f"failed: {spec}", port=80) == f"failed: {spec}"


def test_a_signed_message_carrying_a_format_spec_renders_at_its_own_size(client):
    from otreedeploy import app as app_module

    page = client.get(app_module._with_message("/", "{port:>20000000}", error=True))
    assert len(page.content) < 200_000
    assert "{port:&gt;20000000}" in page.text or "{port:>20000000}" in page.text
