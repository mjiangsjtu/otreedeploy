"""One project's code must not reach another project's data (R1 3.2, R2 P3)."""
from __future__ import annotations

import json

from . import projects
from .server import deploy_live, wait_for


def test_a_projects_code_cannot_reach_another_projects_data_in_any_phase(
    server, manager, workdir
):
    deploy_live(server, manager, projects.labdemo(workdir), sessions=2)
    server.sh(f"rm -f {projects.PROBE_FILE} && install -m 666 /dev/null {projects.PROBE_FILE}")

    # Build, resetdb and smoke test of the neighbour, then a data export (another boot).
    manager.upload(projects.with_neighbour_probe(workdir / "n")).assert_success()
    assert manager.get("/p/neighbour/data/wide.csv").status_code == 200

    records = [json.loads(line) for line in
               server.sh(f"cat {projects.PROBE_FILE}").out.splitlines() if line]
    argvs = " | ".join(r["argv"] for r in records)
    assert "resetdb" in argvs and "prodserver" in argvs, argvs
    for record in records:
        assert record["user"] == "otree_neighbour", record
        reached = [key for key in ("database", "env", "code", "state_db")
                   if record[key] == "READ"]
        assert not reached, json.dumps(record, indent=1)

    # And the neighbour's database belongs to the neighbour alone.
    assert server.foreign_owned("neighbour") == []
    assert server.psql("postgres", "SELECT has_database_privilege("
                       "'otree_neighbour', 'otree_labdemo', 'CONNECT')") == "f"


def test_projects_files_are_closed_to_other_projects(server, manager, workdir):
    deploy_live(server, manager, projects.labdemo(workdir))
    manager.upload(projects.renamed(projects.labdemo(workdir / "o"), workdir / "o2",
                                    "other.otreezip")).assert_success()
    for path in ("/srv/otree/labdemo/env", "/srv/otree/labdemo/current/settings.py"):
        assert not server.sh(f"test -r {path}", user="otree_other", check=False).ok, path
    # Its own project's files are its own.
    assert server.sh("test -r /srv/otree/labdemo/current/settings.py",
                     user="otree_labdemo", check=False).ok


def test_no_private_boot_outlives_the_app(server, manager, workdir):
    deploy_live(server, manager, projects.labdemo(workdir), sessions=1)
    manager.post("/p/labdemo/stop").assert_ok()
    assert manager.get("/p/labdemo/data/wide.csv").status_code == 200
    assert "otree-boot-labdemo" in server.sh(
        "systemctl list-units 'otree-boot-*' --no-legend").out

    # A crash or restart of the management app must not strand the boot.
    server.sh("systemctl restart otreedeploy")
    wait_for(lambda: "otree-boot" not in server.sh(
        "systemctl list-units 'otree-boot-*' --all --no-legend").out,
        timeout=60, what="leftover boots to be stopped")
    assert server.sh("pgrep -u otree_labdemo", check=False).out.strip() == ""
