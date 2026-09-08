from __future__ import annotations

import io
import tarfile
import time
from pathlib import Path

import pytest

# A real archive produced by `otree zip` (oTree 6.0.15), containing a working app with
# one field and a session config. Checked in so the suite is self-contained -- and so the
# archive-safety tests are exercised against the genuine article, not a reconstruction.
REAL_OTREEZIP = Path(__file__).parent / "fixtures" / "labdemo.otreezip"

SESSION_CONFIG_NAME = "simple_task"


@pytest.fixture
def root(tmp_path, monkeypatch):
    """Repoint the whole filesystem layout at a temp dir."""
    monkeypatch.setenv("OTREEDEPLOY_ROOT", str(tmp_path))
    monkeypatch.setenv("OTREEDEPLOY_DRY_RUN", "1")
    return tmp_path


@pytest.fixture
def make_archive(tmp_path):
    """Build a .otreezip-shaped gzipped tar from a spec, including hostile shapes."""

    def _make(name: str, members: list[tuple], mtime: float | None = None) -> Path:
        path = tmp_path / name
        mtime = mtime if mtime is not None else time.time()
        with tarfile.open(path, "w:gz") as tar:
            for spec in members:
                kind = spec[0]
                if kind == "file":
                    _, member_name, content = spec
                    data = content.encode() if isinstance(content, str) else content
                    info = tarfile.TarInfo(member_name)
                    info.size = len(data)
                    info.mtime = mtime
                    info.mode = 0o644
                    tar.addfile(info, io.BytesIO(data))
                elif kind == "dir":
                    info = tarfile.TarInfo(spec[1])
                    info.type = tarfile.DIRTYPE
                    info.mtime = mtime
                    info.mode = 0o755
                    tar.addfile(info)
                elif kind == "symlink":
                    info = tarfile.TarInfo(spec[1])
                    info.type = tarfile.SYMTYPE
                    info.linkname = spec[2]
                    info.mtime = mtime
                    tar.addfile(info)
                elif kind == "hardlink":
                    info = tarfile.TarInfo(spec[1])
                    info.type = tarfile.LNKTYPE
                    info.linkname = spec[2]
                    info.mtime = mtime
                    tar.addfile(info)
                elif kind == "fifo":
                    info = tarfile.TarInfo(spec[1])
                    info.type = tarfile.FIFOTYPE
                    info.mtime = mtime
                    tar.addfile(info)
                else:  # pragma: no cover
                    raise ValueError(kind)
        return path

    return _make


@pytest.fixture
def otree_like(make_archive):
    """The shape oTree actually produces: members rooted at '/'."""
    return make_archive(
        "study.otreezip",
        [
            ("dir", "/"),
            ("file", "/settings.py", "SESSION_CONFIGS = []\n"),
            ("file", "/requirements.txt", "otree>=5.0.0a21\npsycopg2>=2.8.4\n"),
            ("file", "/__init__.py", ""),
            ("dir", "/_static/"),
            ("file", "/_static/global/empty.css", "/* */\n"),
        ],
    )


def create_session(slug, num_participants=2, label="pilot"):
    """Create a real session by asking the deployed oTree instance to make one.

    Earlier this fabricated a row directly in the database, which broke as soon as the
    export ran: oTree stores session config as a pickle, so a hand-written row makes the
    exporter throw. Going through oTree's own REST API keeps the fixture honest and means
    the test exercises the same path an experimenter would.
    """
    import httpx

    from otreedeploy import db as state, deploy, otreeapi

    project = state.get_project(slug)
    assert project is not None
    release_dir = deploy.current_release_dir(slug)
    assert release_dir is not None

    instance = otreeapi.pool.acquire(project, release_dir)
    response = httpx.post(
        f"http://127.0.0.1:{instance.port}/api/sessions",
        json={
            "session_config_name": SESSION_CONFIG_NAME,
            "num_participants": num_participants,
            "label": label,
        },
        headers={"otree-rest-key": project.rest_key},
        timeout=120.0,
    )
    assert response.status_code == 200, response.text[:1000]
    code = response.json()["code"]

    # Shut the helper instance down so that whatever the test does next has to stand on
    # its own -- in particular, so a later download genuinely exercises booting a
    # stopped project rather than quietly reusing this one.
    otreeapi.pool.release(slug)
    return code
