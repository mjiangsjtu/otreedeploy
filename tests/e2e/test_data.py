"""Data: exports, backups, reset and restore."""
from __future__ import annotations

import pytest

from . import projects
from .server import deploy_live, wait_for


def _sessions(server, slug="labdemo") -> set[str]:
    out = server.psql(f"otree_{slug}", "SELECT code FROM otree_session ORDER BY id")
    return {line for line in out.splitlines() if line}


def test_data_can_be_downloaded_from_a_stopped_project(server, manager, workdir):
    deploy_live(server, manager, projects.labdemo(workdir), sessions=2)
    manager.post("/p/labdemo/stop").assert_ok()
    assert not server.unit_active("labdemo")

    response = manager.get("/p/labdemo/data/wide.csv")

    assert response.status_code == 200, response.text[:1000]
    assert response.text.count("\n") >= 1 + 4  # header + 4 participants


def test_reset_takes_a_backup_first(server, manager, workdir):
    deploy_live(server, manager, projects.labdemo(workdir), sessions=2)
    manager.post("/p/labdemo/reset-db", confirm="labdemo", force="true").assert_ok()
    backups = server.sh("ls /var/lib/otreedeploy/backups/labdemo").out.split()
    assert any(name.endswith("-before-reset.sql.gz") for name in backups)
    assert _sessions(server) == set()


def test_restoring_a_backup_after_a_reset_brings_back_exactly_the_old_data(
    server, manager, workdir
):
    experiment, original = deploy_live(server, manager, projects.labdemo(workdir), sessions=2)
    manager.post("/p/labdemo/reset-db", confirm="labdemo", force="true").assert_ok()
    wait_for(lambda: experiment.create_session(label="after-reset-1").status_code == 200,
             timeout=90, what="the experiment after the reset")
    experiment.new_session(label="after-reset-2")
    backup = server.sh("ls -1 /var/lib/otreedeploy/backups/labdemo/ | grep before-reset"
                       ).out.strip()

    manager.post("/p/labdemo/restore", backup=backup, confirm="labdemo",
                 force="true").assert_ok()

    # Exactly the old sessions: none lost, no fragments of the new ones mixed in (M4).
    assert _sessions(server) == set(original)
    assert server.foreign_owned("labdemo") == []
    wait_for(lambda: experiment.create_session().status_code == 200, timeout=90,
             what="the experiment after the restore")
    assert any("before-restore" in name for name in
               server.sh("ls /var/lib/otreedeploy/backups/labdemo").out.split())


def test_a_backup_that_does_not_load_changes_nothing(server, manager, workdir):
    experiment, original = deploy_live(server, manager, projects.labdemo(workdir), sessions=1)
    server.sh("cd /var/lib/otreedeploy/backups && mkdir -p labdemo && "
              "printf 'CREATE TABLE half (x int);\nTHIS IS NOT SQL;\n' | gzip > "
              "labdemo/20260101-000000-broken.sql.gz && chown -R otreedeploy: labdemo")

    outcome = manager.post("/p/labdemo/restore", backup="20260101-000000-broken.sql.gz",
                           confirm="labdemo", force="true")

    assert outcome.error and "not restored" in outcome.message
    assert _sessions(server) == set(original)
    assert experiment.create_session().status_code == 200


def test_the_command_line_restore_works_as_documented(server, manager, workdir):
    experiment, original = deploy_live(server, manager, projects.labdemo(workdir), sessions=1)
    manager.post("/p/labdemo/reset-db", confirm="labdemo", force="true").assert_ok()
    backup = server.sh("ls -1 /var/lib/otreedeploy/backups/labdemo/*before-reset*").out.strip()

    # README.md, "Backups": the command for when the web page is not an option.
    server.sh(f"sudo /usr/local/sbin/otree-ctl restore labdemo < {backup}")

    assert _sessions(server) == set(original)
    wait_for(lambda: experiment.create_session().status_code == 200, timeout=90,
             what="the experiment after the restore")
