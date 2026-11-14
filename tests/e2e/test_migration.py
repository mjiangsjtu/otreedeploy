"""Upgrading an install of the submitted version (f7f62bc) to per-project users."""
from __future__ import annotations

import subprocess
import time

from . import projects
from .server import (
    PASSWORD,
    REPO,
    SRC_IN_CONTAINER,
    Server,
    deploy_live,
    http_status,
    wait_for,
)

SUBMITTED = "f7f62bc"


def test_upgrading_from_the_submitted_version_moves_projects_to_their_own_users(
    base_image, workdir, metrics
):
    server = Server(base_image)
    try:
        old = subprocess.run(["git", "-C", str(REPO), "archive", SUBMITTED],
                             capture_output=True, check=True).stdout
        server.copy_archive_in(old, "/root/otreedeploy-old")
        server.sh("cd /root/otreedeploy-old && ./bootstrap.sh",
                  env={"OTREEDEPLOY_PASSWORD": PASSWORD})
        wait_for(lambda: http_status(f"http://{server.ip}:8000/login") == 200, timeout=60,
                 what="the old management app")
        manager = server.manager().login()
        experiment, codes = deploy_live(server, manager, projects.labdemo(workdir),
                                        sessions=2)
        assert server.sh("systemctl show -p User --value otree@labdemo").out.strip() == "otree"

        server.copy_tree_in(REPO, SRC_IN_CONTAINER)
        started = time.time()
        upgrade = server.sh(f"cd {SRC_IN_CONTAINER} && ./bootstrap.sh", check=False)
        metrics.time("upgrade_from_submitted_seconds", time.time() - started)
        assert upgrade.ok, upgrade.out[-4000:]
        assert "labdemo runs as otree_labdemo" in upgrade.out

        # Same data, now served by the project's own user and role.
        assert server.sh("systemctl show -p User --value otree@labdemo").out.strip() \
            == "otree_labdemo"
        wait_for(lambda: experiment.participant_codes(codes[0]), timeout=60,
                 what="the migrated experiment")
        assert {c for c in server.psql("otree_labdemo",
                "SELECT code FROM otree_session").split()} == set(codes)
        assert server.foreign_owned("labdemo") == []
        assert server.sh("ls /var/lib/otreedeploy/backups/labdemo").out.count(
            "before-migration") == 1
        assert "otree" not in server.sh("id -nG otreedeploy").out.split()

        # The migrated project keeps working: a new app, then new sessions.
        manager = server.manager().login()
        manager.upload(projects.with_new_app(workdir / "v2"), slug="labdemo").assert_success()
        assert server.foreign_owned("labdemo") == []
        assert experiment.create_session().status_code == 200

        # Running the upgrade again changes nothing and backs up nothing new.
        again = server.sh(f"cd {SRC_IN_CONTAINER} && ./bootstrap.sh", check=False)
        assert again.ok, again.out[-3000:]
        assert server.sh("ls /var/lib/otreedeploy/backups/labdemo").out.count(
            "before-migration") == 1
    finally:
        server.remove()
