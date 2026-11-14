"""Uploaded code is arbitrary Python written by somebody else. Where does it run?"""
from __future__ import annotations

import json

import pytest

from . import projects
from .server import deploy_live, wait_for

FORBIDDEN = ("read_auth_json", "read_state_db", "read_backups", "write_app_code")


def _arm_probe(server) -> None:
    server.sh(f"rm -f {projects.PROBE_FILE} && install -m 666 /dev/null {projects.PROBE_FILE}")


def _probe_records(server) -> list[dict]:
    out = server.sh(f"cat {projects.PROBE_FILE}", check=False).out
    return [json.loads(line) for line in out.splitlines() if line.startswith("{")]


def test_the_live_experiment_runs_confined(server, manager, workdir):
    deploy_live(server, manager, projects.with_identity_probe(workdir))
    _arm_probe(server)
    server.sh("systemctl restart otree@labdemo")
    experiment = server.experiment("labdemo")
    wait_for(lambda: experiment.create_session().status_code == 200,
             timeout=90, what="the restarted experiment")

    live = [r for r in _probe_records(server) if "prodserver" in r["argv"]]
    assert live, "the probe did not run in the live service"
    for record in live:
        assert record["user"] == "otree"
        assert not any(record[key] for key in FORBIDDEN), record


@pytest.mark.xfail(strict=True, reason="M2: build, resetdb, smoke test and exports run unconfined")
def test_uploaded_code_never_runs_with_the_management_apps_privileges(server, manager, workdir):
    _arm_probe(server)
    deploy_live(server, manager, projects.with_identity_probe(workdir), sessions=1)
    manager.post("/p/labdemo/stop").assert_ok()
    assert manager.get("/p/labdemo/data/wide.csv").status_code == 200  # an export boot

    records = _probe_records(server)
    phases = {r["phase"] for r in records}
    assert {"build", "import"} <= phases, f"probe did not run in every phase: {phases}"
    escaped = [r for r in records
               if r["user"] != "otree" or any(r[key] for key in FORBIDDEN)]
    assert not escaped, json.dumps(escaped, indent=1)
