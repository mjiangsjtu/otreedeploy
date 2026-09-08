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

    script = tmp_path / "otree-ctl"
    script.write_text(
        SOURCE.read_text().replace("SYSTEMCTL=/usr/bin/systemctl", f"SYSTEMCTL={stub}")
    )
    script.chmod(0o755)

    def run(*args):
        return subprocess.run(
            [str(script), *args], capture_output=True, text=True, timeout=30
        )

    run.log = log  # type: ignore[attr-defined]
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
