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


@pytest.mark.xfail(strict=True, reason="M4: the documented restore loses data, reports success")
def test_the_documented_restore_brings_back_the_data_after_a_reset(server, manager, workdir):
    experiment, original = deploy_live(server, manager, projects.labdemo(workdir), sessions=2)
    manager.post("/p/labdemo/reset-db", confirm="labdemo", force="true").assert_ok()
    wait_for(lambda: experiment.create_session(label="after-reset-1").status_code == 200,
             timeout=90, what="the experiment after the reset")
    experiment.new_session(label="after-reset-2")
    backup = server.sh("ls -1 /var/lib/otreedeploy/backups/labdemo/*before-reset*").out.strip()

    # The README's restore instructions, verbatim (README.md, "Backups").
    result = server.sh(
        f"cd /tmp && cp {backup} backup.sql.gz && chmod 644 backup.sql.gz && "
        "gunzip -c backup.sql.gz | sudo -u otreedeploy psql otree_labdemo",
        check=False,
    )

    restored = _sessions(server)
    if result.ok:
        assert set(original) <= restored, "restore reported success but the data is not back"
    assert server.foreign_owned("labdemo") == []
    assert experiment.create_session().status_code == 200
