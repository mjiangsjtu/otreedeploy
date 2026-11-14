"""Accounts on a real server: who sees what, and an audit trail nobody can rewrite."""
from __future__ import annotations

import subprocess

from . import projects
from .server import PASSWORD, REPO, SRC_IN_CONTAINER, Server, deploy_live, http_status, wait_for


def _add_user(server, name: str) -> None:
    server.sh(f"cd / && printf '%s\\n' '{PASSWORD}' | "
              f"/opt/otreedeploy/.venv/bin/otreedeploy-users add {name} --stdin",
              user="otreedeploy")


def test_one_groups_experiment_is_invisible_and_untouchable_for_another(server, manager, workdir):
    deploy_live(server, manager, projects.labdemo(workdir), sessions=1)  # admin's
    for name in ("bob", "eve"):
        _add_user(server, name)
    manager.post("/p/labdemo/members/add", name="bob").assert_ok()
    bob = server.manager().login(user="bob")
    eve = server.manager().login(user="eve")

    # eve: not listed, not reachable, cannot download, restore, delete or deploy over it.
    assert "labdemo" not in eve.get("/").text
    for path in ("/p/labdemo", "/p/labdemo/data/wide.csv", "/p/labdemo/logs"):
        assert eve.get(path).status_code == 404, path
    for path in ("/p/labdemo/delete", "/p/labdemo/stop", "/p/labdemo/reset-db"):
        assert eve.post(path, confirm="labdemo", force="true").status_code == 404, path
    assert server.unit_active("labdemo")
    job = eve.upload(projects.labdemo(workdir / "e"))  # same file name: a separate project
    assert server.sh("ls /srv/otree").out.split().count("labdemo") == 1
    assert "labdemo" in server.sh("ls /srv/otree").out

    # bob, a member: may download.
    assert bob.get("/p/labdemo/data/wide.csv").status_code == 200

    # eve's own project cannot take port 80 over labdemo's active participants.
    experiment = server.experiment("labdemo")
    code = experiment.participant_codes(server.psql(
        "otree_labdemo", "SELECT code FROM otree_session LIMIT 1"))[0]
    experiment.open_as_participant(code)
    outcome = eve.post(f"/p/{job.log.split('Project: ')[1].split()[0]}/activate", force="true")
    assert outcome.error and "another group" in outcome.message and "admin" in outcome.message
    assert "labdemo" not in outcome.message
    assert server.unit_active("labdemo")


def test_the_audit_trail_names_who_downloaded_what_and_ignores_forgeries(server, manager, workdir):
    deploy_live(server, manager, projects.labdemo(workdir), sessions=1)
    _add_user(server, "bob")
    manager.post("/p/labdemo/members/add", name="bob").assert_ok()
    server.manager().login(user="bob").get("/p/labdemo/data/wide.csv")

    # A project's own code (or anyone else on the box) writing a look-alike entry.
    server.sh("systemd-cat -t otreedeploy-audit echo forged", user="otree_labdemo")
    server.sh("logger --journald <<'EOF'\nSYSLOG_IDENTIFIER=otreedeploy-audit\n"
              "MESSAGE=forged\nAUDIT_USER=bob\nAUDIT_ACTION=forged_entry\n"
              "AUDIT_PROJECT=labdemo\nEOF", user="otree_labdemo")

    page = manager.get("/system?project=labdemo").text
    assert "download_data" in page and "bob" in page
    assert "forged" not in page
    # And it is in the journal itself, for whoever audits the server.
    uid = server.sh("id -u otreedeploy").out.strip()
    journal = server.sh(f"journalctl -t otreedeploy-audit _UID={uid} AUDIT_USER=bob -o cat").out
    assert "download_data labdemo" in journal


def test_deleting_with_purge_leaves_no_file_of_the_project(server, manager, workdir):
    deploy_live(server, manager, projects.labdemo(workdir), sessions=1)
    manager.post("/p/labdemo/reset-db", confirm="labdemo", force="true").assert_ok()
    manager.post("/p/labdemo/delete", confirm="labdemo", force="true", purge="true",
                 confirm_purge="delete all data").assert_ok()
    leftovers = server.sh("find /var/lib/otreedeploy /srv/otree -path '*labdemo*'",
                          check=False).out.strip()
    assert leftovers == ""
    assert server.psql("postgres", "SELECT count(*) FROM pg_database "
                       "WHERE datname = 'otree_labdemo'") == "0"


def test_upgrading_from_the_single_password_version_keeps_the_password_as_admin(
    base_image, workdir
):
    server = Server(base_image)
    try:
        old = subprocess.run(["git", "-C", str(REPO), "archive", "cf76b08"],
                             capture_output=True, check=True).stdout
        server.copy_archive_in(old, "/root/otreedeploy-old")
        server.sh("cd /root/otreedeploy-old && ./bootstrap.sh",
                  env={"OTREEDEPLOY_PASSWORD": "the old shared password"})
        wait_for(lambda: http_status(f"http://{server.ip}:8000/login") == 200, timeout=60,
                 what="the old app")
        old_client = server.manager()
        r = old_client.http.post("/login", data={"password": "the old shared password"})
        assert r.status_code == 303
        old_client.upload(projects.labdemo(workdir)).assert_success()

        server.copy_tree_in(REPO, SRC_IN_CONTAINER)
        upgrade = server.sh(f"cd {SRC_IN_CONTAINER} && ./bootstrap.sh", check=False)
        assert upgrade.ok, upgrade.out[-3000:]

        admin = server.manager().login("the old shared password", user="admin")
        assert "labdemo" in admin.get("/").text
        assert server.sh("test -e /var/lib/otreedeploy/auth.json.migrated").ok
    finally:
        server.remove()
