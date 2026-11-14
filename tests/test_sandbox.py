"""Every step that runs uploaded code must go through otree-ctl in a real install.

Dry run has no per-project users, so the deploy tests cannot see *who* runs the code.
These tests pin down the contract instead: outside a dry run, building, resetdb,
booting and cleaning are requests to the root wrapper, which runs them as the project's
own user (tests/e2e checks that it really does).
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from otreedeploy import config, projects, sandbox

OTREE_CTL = Path(__file__).resolve().parent.parent / "deploy" / "otree-ctl"


@pytest.fixture
def real(root, monkeypatch):
    """Pretend to be a real install, recording what would be asked of otree-ctl."""
    monkeypatch.setattr(config, "dry_run", lambda: False)
    calls: list[list[str]] = []

    def ctl(*args, timeout=300):
        calls.append(list(args))
        return ""

    def ctl_stream(args, log):
        calls.append(list(args))
        return 0

    monkeypatch.setattr(sandbox, "_ctl", ctl)
    monkeypatch.setattr(sandbox, "_ctl_stream", ctl_stream)
    release = config.get_settings().release_dir("study", "20261114-120000")
    release.mkdir(parents=True)
    return calls, release


def test_building_resetdb_booting_and_cleaning_are_all_requests_to_otree_ctl(real):
    calls, release = real
    sandbox.provision("study")
    sandbox.build("study", release, lambda line: None)
    sandbox.resetdb("study", release, {}, lambda line: None)
    sandbox.boot("study", release, 18001)
    sandbox.clean("study", release)
    sandbox.unprovision("study")
    assert calls == [
        ["provision", "study"],
        ["build", "study", "20261114-120000"],
        ["resetdb", "study", "20261114-120000"],
        ["boot", "study", "20261114-120000", "18001"],
        ["clean", "study", "20261114-120000"],
        ["unprovision", "study"],
    ]


def test_a_release_of_another_project_is_refused_before_asking_otree_ctl(real):
    calls, _ = real
    other = config.get_settings().release_dir("other", "20261114-120000")
    other.mkdir(parents=True)
    with pytest.raises(sandbox.SandboxError, match="not a release of 'study'"):
        sandbox.build("study", other, lambda line: None)
    assert calls == []


def test_a_failed_build_is_reported_as_a_build_failure(real, monkeypatch):
    _, release = real
    monkeypatch.setattr(sandbox, "_ctl_stream", lambda args, log: 2)
    from otreedeploy import builder

    with pytest.raises(builder.BuildError, match="exit 2"):
        sandbox.build("study", release, lambda line: None)


def _bash_project_user(slug: str) -> str:
    function = subprocess.run(
        ["sed", "-n", "/^project_user() {/,/^}/p", str(OTREE_CTL)],
        capture_output=True, text=True, check=True,
    ).stdout
    assert function, "project_user() not found in otree-ctl"
    result = subprocess.run(
        ["bash", "-c", function + '\nproject_user "$1"', "_", slug],
        capture_output=True, text=True, check=True,
    )
    return result.stdout.strip()


@pytest.mark.parametrize(
    "slug",
    ["labdemo", "a", "x" * 26, "x" * 27, "trust_game_with_a_very_long_name_v2", "z" * 39],
)
def test_python_and_otree_ctl_agree_on_each_projects_user_name(slug):
    name = projects.project_user(slug)
    assert name == _bash_project_user(slug)
    assert len(name) <= 32, "Linux refuses longer user names"


def test_long_project_names_still_get_distinct_users():
    a = projects.project_user("a_long_study_name_number_one_x")
    b = projects.project_user("a_long_study_name_number_two_x")
    assert a != b
