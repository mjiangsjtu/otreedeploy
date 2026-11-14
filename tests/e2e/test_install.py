"""Installing, upgrading and rebooting: the server-administration the tool claims to spare."""
from __future__ import annotations

import time

import pytest

from .projects import labdemo
from .server import (
    PASSWORD,
    SRC_IN_CONTAINER,
    deploy_live,
    http_status,
    wait_for,
)


def test_a_fresh_install_passes_its_own_checks_and_asks_for_a_password(fresh_install, server):
    assert "otreedeploy is ready" in fresh_install
    assert "✗" not in fresh_install
    assert http_status(f"http://{server.ip}:8000/") == 303  # to the sign-in page
    mode = server.sh("stat -c '%a %U' /var/lib/otreedeploy/auth.json").out.split()
    assert mode == ["600", "otreedeploy"]


def test_no_service_runs_as_root(server, manager, workdir):
    deploy_live(server, manager, labdemo(workdir))
    users = server.sh(
        "for u in otreedeploy otree@labdemo; do systemctl show -p User --value $u; done"
    ).out.split()
    assert users == ["otreedeploy", "otree"]
    listeners = server.sh("ss -Hltnp '( sport = :80 or sport = :8000 )'").out
    pids = {int(p) for p in __import__("re").findall(r"pid=(\d+)", listeners)}
    owners = {server.sh(f"ps -o user= -p {pid}").out.strip() for pid in pids}
    assert "root" not in owners


def test_rerunning_bootstrap_upgrades_in_place_and_keeps_everything(server, manager, workdir):
    experiment, codes = deploy_live(server, manager, labdemo(workdir), sessions=1)
    started = time.time()
    result = server.sh(f"cd {SRC_IN_CONTAINER} && ./bootstrap.sh", check=False)
    assert result.ok, f"re-run exited {result.code}:\n{result.out[-3000:]}"
    assert time.time() - started < 300
    assert "you just chose" not in result.out, "the banner talks as if this were a new install"
    # Password, project and data survive; the live study kept serving.
    server.manager().login(PASSWORD)
    assert server.unit_active("labdemo")
    assert experiment.participant_codes(codes[0])


def test_the_live_experiment_comes_back_after_a_reboot(server, manager, workdir, metrics):
    experiment, codes = deploy_live(server, manager, labdemo(workdir), sessions=1)
    started = time.time()
    server.reboot()
    wait_for(lambda: http_status(f"http://{server.ip}/") is not None,
             timeout=120, what="the experiment after reboot")
    metrics.time("reboot_to_serving_seconds", time.time() - started)
    assert experiment.participant_codes(codes[0])
    assert http_status(f"http://{server.ip}:8000/login") == 200


def test_the_live_experiment_comes_back_after_postgresql_is_stopped_and_started(
    server, manager, workdir
):
    experiment, codes = deploy_live(server, manager, labdemo(workdir), sessions=1)
    # What a package upgrade does (deb-systemd-invoke stop, then start). A plain
    # `systemctl restart` would not reproduce it: a restart propagates through Requires=.
    server.sh("systemctl stop postgresql && sleep 2 && systemctl start postgresql")
    wait_for(lambda: server.unit_active("labdemo"), timeout=60, what="otree@labdemo active")
    wait_for(lambda: experiment.participant_codes(codes[0]), timeout=60,
             what="the experiment answering again")
    # And a plain restart, which also used to leave oTree with a dead connection.
    server.sh("systemctl restart postgresql")
    wait_for(lambda: experiment.participant_codes(codes[0]), timeout=60,
             what="the experiment answering after a restart")
    assert server.unit_enabled("labdemo")
