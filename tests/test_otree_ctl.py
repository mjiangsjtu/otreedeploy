"""Tests for deploy/otree-ctl, the one command the app is allowed to run as root.

The wrapper exists so the sudoers grant needs no argument wildcards; that only helps if
the wrapper itself is strict, so it gets tested directly. A stub stands in for systemctl
so we can see exactly what it would have been asked to do.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

SOURCE = Path(__file__).resolve().parent.parent / "deploy" / "otree-ctl"


@pytest.fixture
def ctl(tmp_path):
    """A copy of the wrapper wired to a stub that records its arguments."""
    stub = tmp_path / "systemctl-stub"
    log = tmp_path / "calls.log"
    stub.write_text(
        "#!/bin/bash\nprintf '%s\\n' \"$*\" >> " + str(log) + "\necho stub-ok\n"
    )
    stub.chmod(0o755)

    home = tmp_path / "srv-otree"
    (home / "study/releases/20261114-120000").mkdir(parents=True)

    script = tmp_path / "otree-ctl"
    script.write_text(
        SOURCE.read_text()
        .replace("SYSTEMCTL=/usr/bin/systemctl", f"SYSTEMCTL={stub}")
        .replace("SYSTEMD_RUN=/usr/bin/systemd-run", f"SYSTEMD_RUN={stub}")
        .replace("OTREE_HOME=/srv/otree", f"OTREE_HOME={home}")
    )
    script.chmod(0o755)

    def run(*args):
        return subprocess.run(
            [str(script), *args], capture_output=True, text=True, timeout=30
        )

    run.log = log  # type: ignore[attr-defined]
    run.home = home  # type: ignore[attr-defined]
    return run


def _calls(ctl):
    return ctl.log.read_text().splitlines() if ctl.log.exists() else []


@pytest.mark.parametrize(
    "verb", ["start", "stop", "restart", "enable", "disable", "is-active", "is-enabled"]
)
def test_permitted_verbs_reach_systemctl(ctl, verb):
    result = ctl(verb, "trust_game")
    assert result.returncode == 0, result.stderr
    assert _calls(ctl) == [f"{verb} otree@trust_game.service"]


def test_daemon_reload(ctl):
    assert ctl("daemon-reload").returncode == 0
    assert _calls(ctl) == ["daemon-reload"]


def test_show_with_properties(ctl):
    assert ctl("show", "trust_game", "ActiveState", "SubState").returncode == 0
    assert _calls(ctl) == [
        "show otree@trust_game.service -p ActiveState -p SubState"
    ]


@pytest.mark.parametrize(
    "verb",
    ["mask", "kill", "cat", "edit", "link", "set-property", "reboot", "poweroff", "--version"],
)
def test_rejects_other_verbs(ctl, verb):
    result = ctl(verb, "trust_game")
    assert result.returncode == 64
    assert "not permitted" in result.stderr
    assert _calls(ctl) == []


@pytest.mark.parametrize(
    "slug",
    [
        "../../etc/passwd",
        "trust game",
        "trust;reboot",
        "trust.service",
        "Trust",
        "9lives",
        "-",
        "",
        "a" * 60,
        "trust\nreboot",
        "$(reboot)",
        "trust@other",
    ],
)
def test_rejects_malformed_project_names(ctl, slug):
    result = ctl("stop", slug)
    assert result.returncode == 64, result.stdout
    assert "invalid project name" in result.stderr or "needs a project name" in result.stderr
    assert _calls(ctl) == []


def test_rejects_extra_arguments(ctl):
    result = ctl("stop", "trust_game", "--now", "other.service")
    assert result.returncode == 64
    assert _calls(ctl) == []


def test_rejects_bad_show_property(ctl):
    result = ctl("show", "trust_game", "ExecStart;reboot")
    assert result.returncode == 64
    assert "invalid property" in result.stderr
    assert _calls(ctl) == []


def test_rejects_no_verb(ctl):
    assert ctl().returncode == 64


def test_daemon_reload_takes_no_arguments(ctl):
    result = ctl("daemon-reload", "extra")
    assert result.returncode == 64
    assert _calls(ctl) == []


# ------------------------------------------------- verbs that run uploaded code

RELEASE = "20261114-120000"


@pytest.mark.parametrize("verb", ["build", "resetdb", "clean"])
def test_running_uploaded_code_is_confined_to_the_projects_own_user(ctl, verb):
    result = ctl(verb, "study", RELEASE)
    assert result.returncode == 0, result.stderr
    calls = _calls(ctl)
    assert calls, "nothing was run"
    for call in calls:
        assert "--uid=otree_study --gid=otree_study" in call
        for setting in ("NoNewPrivileges=yes", "ProtectSystem=strict", "PrivateTmp=yes",
                        f"ReadWritePaths={ctl.home}/study"):
            assert setting in call, (setting, call)
        assert "--wait" in call


def test_build_runs_venv_and_install_as_two_separate_confined_steps(ctl):
    assert ctl("build", "study", RELEASE).returncode == 0
    calls = _calls(ctl)
    assert len(calls) == 2
    assert calls[0].endswith("-- /usr/local/bin/uv venv .venv")
    assert "-- /usr/local/bin/uv pip install --python" in calls[1]
    assert calls[1].endswith("-r requirements.txt")


def test_only_resetdb_and_boot_see_the_projects_environment_file(ctl):
    for verb in ("build", "clean"):
        ctl(verb, "study", RELEASE)
    assert not any("EnvironmentFile" in c for c in _calls(ctl))
    ctl("resetdb", "study", RELEASE)
    assert f"EnvironmentFile={ctl.home}/study/env" in _calls(ctl)[-1]


def test_boot_is_a_named_confined_unit_on_a_loopback_port(ctl):
    assert ctl("boot", "study", RELEASE, "18042").returncode == 0
    (call,) = _calls(ctl)
    assert "--unit=otree-boot-study-18042" in call
    assert "--uid=otree_study" in call
    assert call.endswith("prodserver 127.0.0.1:18042")


@pytest.mark.parametrize(
    "release",
    ["../../other/releases/" + RELEASE, "latest", RELEASE + ";reboot", "20261114-12000",
     "", "20261114-999999", f"{RELEASE}/../x"],
)
@pytest.mark.parametrize("verb", ["build", "resetdb", "clean"])
def test_rejects_malformed_or_missing_releases(ctl, verb, release):
    result = ctl(verb, "study", release)
    assert result.returncode == 64, result.stdout
    assert _calls(ctl) == []


def test_rejects_a_release_reached_through_a_symlink(ctl):
    (ctl.home / "study/releases/20261114-130000").symlink_to(ctl.home / "study/releases" / RELEASE)
    result = ctl("build", "study", "20261114-130000")
    assert result.returncode == 64
    assert "not canonical" in result.stderr
    assert _calls(ctl) == []


@pytest.mark.parametrize("port", ["80", "8000", "17999", "18900", "65535", "18001;x", "", "1e4"])
def test_boot_and_unboot_reject_ports_outside_the_private_range(ctl, port):
    assert ctl("boot", "study", RELEASE, port).returncode == 64
    assert ctl("unboot", "study", port).returncode == 64
    assert _calls(ctl) == []


@pytest.mark.parametrize(
    "args",
    [
        ("build", "study"),
        ("build", "study", RELEASE, "extra"),
        ("boot", "study", RELEASE),
        ("provision",),
        ("provision", "study", "extra"),
        ("unprovision", "../etc"),
        ("unboot-all", "x"),
    ],
)
def test_rejects_wrong_argument_counts_and_names(ctl, args):
    assert ctl(*args).returncode == 64
    assert _calls(ctl) == []


@pytest.mark.parametrize("args", [("restore",), ("restore", "../x"), ("restore", "study", "x")])
def test_restore_rejects_bad_arguments(ctl, args):
    assert ctl(*args).returncode == 64
    assert _calls(ctl) == []


def test_schema_runs_the_probe_confined_with_the_projects_environment(ctl):
    assert ctl("schema", "study", RELEASE, "check").returncode == 0
    (call,) = _calls(ctl)
    assert "--uid=otree_study" in call and "ProtectSystem=strict" in call
    assert f"EnvironmentFile={ctl.home}/study/env" in call
    assert call.endswith("/.venv/bin/python /opt/otreedeploy/otreedeploy/schema_probe.py check")


@pytest.mark.parametrize("mode", ["", "drop", "apply;reboot", "--help"])
def test_schema_accepts_only_check_or_apply(ctl, mode):
    assert ctl("schema", "study", RELEASE, mode).returncode == 64
    assert _calls(ctl) == []
