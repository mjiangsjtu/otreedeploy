"""Turning an extracted release directory into something runnable.

Two jobs: normalize the quirks of what `otree zip` produces, and build the project's own
virtualenv with uv from its own requirements.txt.
"""

from __future__ import annotations

import re
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

Logger = Callable[[str], None]

DEFAULT_REQUIREMENTS = "otree\npsycopg2-binary\n"

# A fresh `otree startproject` pins bare `psycopg2`, which is a source distribution: it
# needs libpq-dev and a C compiler at install time. The wheel-shipping `psycopg2-binary`
# is a drop-in replacement and is what makes uploads work on a container without a
# toolchain. Matches `psycopg2` but never `psycopg2-binary`.
_PSYCOPG2 = re.compile(r"^(\s*)psycopg2(?![-_.\w])(.*)$", re.MULTILINE)


@dataclass
class Normalization:
    notes: list[str]


def normalize_requirements(text: str) -> tuple[str, list[str]]:
    """Rewrite source-only pins that would need a compiler."""
    notes: list[str] = []
    new_text, count = _PSYCOPG2.subn(r"\1psycopg2-binary\2", text)
    if count:
        notes.append(
            f"rewrote psycopg2 -> psycopg2-binary ({count} line"
            f"{'s' if count > 1 else ''}) so no C compiler is needed"
        )
    return new_text, notes


def normalize_release(release_dir: Path) -> Normalization:
    """Fix up an extracted release in place. Returns human-readable notes for the log."""
    notes: list[str] = []

    # oTree ships an empty .python-version inside the archive; uv treats an empty or
    # malformed file as a request it cannot satisfy, so drop it rather than guess.
    pyversion = release_dir / ".python-version"
    if pyversion.exists() and not pyversion.read_text().strip():
        pyversion.unlink()
        notes.append("removed an empty .python-version")

    requirements = release_dir / "requirements.txt"
    if requirements.exists():
        text = requirements.read_text()
        new_text, req_notes = normalize_requirements(text)
        if new_text != text:
            requirements.write_text(new_text)
        notes.extend(req_notes)
    else:
        requirements.write_text(DEFAULT_REQUIREMENTS)
        notes.append("no requirements.txt in the archive; installed otree by default")

    return Normalization(notes=notes)


def _run(cmd: list[str], cwd: Path, log: Logger, what: str) -> None:
    log(f"$ {' '.join(cmd)}")
    process = subprocess.Popen(
        cmd,
        cwd=cwd,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        bufsize=1,
    )
    assert process.stdout is not None
    for line in process.stdout:
        log(line.rstrip("\n"))
    code = process.wait()
    if code != 0:
        raise BuildError(f"{what} failed (exit {code}); see the log above for details")


class BuildError(Exception):
    pass


def venv_python(release_dir: Path) -> Path:
    return release_dir / ".venv/bin/python"


def venv_otree(release_dir: Path) -> Path:
    return release_dir / ".venv/bin/otree"


def build_venv(release_dir: Path, log: Logger, uv: str = "uv") -> None:
    """Create .venv inside the release and install its requirements."""
    _run([uv, "venv", ".venv"], release_dir, log, "creating the virtualenv")
    _run(
        [
            uv,
            "pip",
            "install",
            "--python",
            str(venv_python(release_dir)),
            "-r",
            "requirements.txt",
        ],
        release_dir,
        log,
        "installing requirements",
    )


def otree_version(release_dir: Path) -> str | None:
    """Read the oTree version installed in the release's venv, from package metadata.

    Read, not imported: importing would execute the project's own environment, and
    this runs as the management app -- whose privileges uploaded code must never get.
    """
    for metadata in sorted(release_dir.glob(".venv/lib/python*/site-packages/otree-*.dist-info/METADATA")):
        try:
            with open(metadata, encoding="utf-8", errors="replace") as handle:
                for line in handle:
                    if line.startswith("Version:"):
                        return line.split(":", 1)[1].strip() or None
                    if not line.strip():
                        break
        except OSError:
            continue
    return None
