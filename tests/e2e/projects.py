"""Variants of the bundled labdemo project, each shaped to provoke one failure mode.

Every variant starts from the real `otree zip` output in tests/fixtures and changes only
what the scenario needs, so a failure means the scenario, not a malformed archive.
"""
from __future__ import annotations

import io
import tarfile
import textwrap
from pathlib import Path

LABDEMO = Path(__file__).resolve().parents[1] / "fixtures" / "labdemo.otreezip"


def _read(source: Path = LABDEMO) -> dict[str, bytes | None]:
    """Member name -> content (None for directories), in archive order."""
    members: dict[str, bytes | None] = {}
    with tarfile.open(source, "r:gz") as tar:
        for member in tar.getmembers():
            if member.isdir():
                members[member.name] = None
            elif member.isfile():
                members[member.name] = tar.extractfile(member).read()  # type: ignore[union-attr]
    return members


def _write(members: dict[str, bytes | None], destination: Path) -> Path:
    destination.parent.mkdir(parents=True, exist_ok=True)
    with tarfile.open(destination, "w:gz") as tar:
        for name, content in members.items():
            info = tarfile.TarInfo(name)
            info.mtime = 1_700_000_000
            if content is None:
                info.type = tarfile.DIRTYPE
                info.mode = 0o755
                tar.addfile(info)
            else:
                info.size = len(content)
                info.mode = 0o644
                tar.addfile(info, io.BytesIO(content))
    return destination


def _edit(members: dict, name: str, old: str, new: str) -> None:
    text = members[name].decode()
    assert old in text, f"{old!r} not found in {name}"
    members[name] = text.replace(old, new, 1).encode()


def labdemo(directory: Path) -> Path:
    return _write(_read(), directory / "labdemo.otreezip")


def with_extra_field(directory: Path) -> Path:
    """The same study with one more Player field -- the commonest mid-study change."""
    members = _read()
    _edit(
        members,
        "simple_task/__init__.py",
        "    answer = models.IntegerField(label='Pick a number', min=0, max=10)\n",
        "    answer = models.IntegerField(label='Pick a number', min=0, max=10)\n"
        "    extra = models.StringField(blank=True)\n",
    )
    return _write(members, directory / "labdemo.otreezip")


def with_new_app(directory: Path) -> Path:
    """The same study with a questionnaire app appended to the session."""
    members = _read()
    members["questionnaire"] = None
    members["questionnaire/__init__.py"] = textwrap.dedent(
        """\
        from otree.api import *


        class C(BaseConstants):
            NAME_IN_URL = 'questionnaire'
            PLAYERS_PER_GROUP = None
            NUM_ROUNDS = 1


        class Subsession(BaseSubsession):
            pass


        class Group(BaseGroup):
            pass


        class Player(BasePlayer):
            age = models.IntegerField(label='Your age', min=18, max=99)


        class Survey(Page):
            form_model = 'player'
            form_fields = ['age']


        page_sequence = [Survey]
        """
    ).encode()
    members["questionnaire/Survey.html"] = (
        b"{{ block title }}Survey{{ endblock }}\n"
        b"{{ block content }}{{ formfields }}{{ next_button }}{{ endblock }}\n"
    )
    _edit(
        members,
        "settings.py",
        "app_sequence=['simple_task']",
        "app_sequence=['simple_task', 'questionnaire']",
    )
    return _write(members, directory / "labdemo.otreezip")


def with_unbuildable_requirement(directory: Path) -> Path:
    """A typo in requirements.txt: the build cannot succeed."""
    members = _read()
    members["requirements.txt"] = (
        members["requirements.txt"] + b"this-package-does-not-exist-otreedeploy-e2e==9.9.9\n"
    )
    return _write(members, directory / "labdemo.otreezip")


# Records who is running experimenter-supplied code, and what that identity can reach.
# Written to /dev/shm, which every phase can reach -- the live unit cannot write to its
# project directory, and builds may run from a temporary copy. It never raises: the
# point is to observe, not to break the deploy.
PROBE_FILE = "/dev/shm/otreedeploy-probe.jsonl"

_PROBE = '''
PROBE_FILE = "%s"

def _otreedeploy_e2e_probe(phase):
    import json, os, pwd, sys
    from pathlib import Path
    try:
        record = {
            "phase": phase,
            "argv": " ".join(sys.argv[:3]),
            "user": pwd.getpwuid(os.geteuid()).pw_name,
            "read_auth_json": os.access("/var/lib/otreedeploy/auth.json", os.R_OK),
            "read_state_db": os.access("/var/lib/otreedeploy/state.db", os.R_OK),
            "read_backups": os.access("/var/lib/otreedeploy/backups", os.R_OK | os.X_OK),
            "write_app_code": os.access("/opt/otreedeploy/otreedeploy/app.py", os.W_OK),
        }
        # Opened without O_CREAT: the test pre-creates it world-writable, and creating
        # another user's file in a sticky directory is refused (fs.protected_regular).
        fd = os.open(PROBE_FILE, os.O_WRONLY | os.O_APPEND)
        os.write(fd, (json.dumps(record) + "\\n").encode())
        os.close(fd)
    except Exception:
        pass
''' % PROBE_FILE


def with_identity_probe(directory: Path) -> Path:
    """Experimenter code that reports which user it runs as, in every phase it runs in:
    the build (a local package's setup.py), and every boot (the app's __init__.py)."""
    members = _read()
    members["probe_pkg"] = None
    members["probe_pkg/setup.py"] = (
        _PROBE + '\n_otreedeploy_e2e_probe("build")\n\n'
        "from setuptools import setup\n"
        "setup(name='otreedeploy-e2e-probe', version='0.0.1', py_modules=[])\n"
    ).encode()
    members["requirements.txt"] = members["requirements.txt"] + b"./probe_pkg\n"
    members["simple_task/__init__.py"] = (
        _PROBE.encode() + b'\n_otreedeploy_e2e_probe("import")\n\n'
        + members["simple_task/__init__.py"]
    )
    return _write(members, directory / "labdemo.otreezip")


def renamed(source: Path, directory: Path, name: str) -> Path:
    """Upload the same content under another name, i.e. as another project."""
    target = directory / name
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(source.read_bytes())
    return target
