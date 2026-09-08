"""Safe inspection and extraction of oTree's ``.otreezip`` files.

Despite the name, an ``.otreezip`` is a **gzipped tar**, and oTree writes its members with
*absolute* paths::

    /
    /settings.py
    /_static/global/empty.css
    /requirements.txt

``otree unzip`` takes the destination folder from the *filename*, not from the archive.
We do not shell out to it: these files arrive over the network from experimenters, so we
vet every member ourselves and extract by hand rather than trusting ``extractall``.
"""

from __future__ import annotations

import hashlib
import posixpath
import tarfile
from dataclasses import dataclass
from pathlib import Path

# A settings.py at the project root is what makes a directory an oTree project.
MARKER = "settings.py"

# Guards against decompression bombs. An oTree project is source code and small static
# assets; anything approaching this is not a study.
MAX_UNCOMPRESSED_BYTES = 2 * 1024 * 1024 * 1024
MAX_MEMBERS = 20_000


class ArchiveError(Exception):
    """Raised when an upload is not a usable, safe oTree project."""


@dataclass(frozen=True)
class Entry:
    """One vetted archive member, with its path rewritten relative to the project root."""

    path: str  # relative, normalized, no leading slash, no '..'
    is_dir: bool
    size: int
    mode: int


@dataclass(frozen=True)
class Inspection:
    entries: list[Entry]
    prefix: str  # top-level directory stripped from member names, '' if none
    total_size: int
    has_requirements: bool

    @property
    def file_count(self) -> int:
        return sum(1 for e in self.entries if not e.is_dir)


def _sanitize(name: str) -> str | None:
    """Rewrite an archive member name to a safe relative path, or None to skip it.

    Raises ArchiveError if the name is actively hostile rather than merely uninteresting.
    """
    # oTree stores names rooted at '/'; strip that before anything else.
    cleaned = name.lstrip("/")
    # Windows-style separators would otherwise slip past the '..' check.
    cleaned = cleaned.replace("\\", "/")
    if not cleaned or cleaned in {".", "./"}:
        return None

    normalized = posixpath.normpath(cleaned)
    if normalized in {".", ""}:
        return None
    if posixpath.isabs(normalized):
        raise ArchiveError(f"archive contains an absolute path: {name!r}")
    if normalized == ".." or normalized.startswith("../"):
        raise ArchiveError(f"archive tries to escape its directory: {name!r}")
    if any(part == ".." for part in normalized.split("/")):
        raise ArchiveError(f"archive tries to escape its directory: {name!r}")
    return normalized


def _vet(tar: tarfile.TarFile) -> list[Entry]:
    entries: list[Entry] = []
    total = 0
    for count, member in enumerate(tar):
        if count >= MAX_MEMBERS:
            raise ArchiveError(f"archive has more than {MAX_MEMBERS} entries")

        if member.issym() or member.islnk():
            raise ArchiveError(
                f"archive contains a link ({member.name!r}); oTree projects should not "
                "contain symlinks or hard links"
            )
        if not (member.isfile() or member.isdir()):
            raise ArchiveError(
                f"archive contains a special file ({member.name!r}); only regular files "
                "and directories are allowed"
            )

        path = _sanitize(member.name)
        if path is None:
            continue

        if member.isfile():
            total += member.size
            if total > MAX_UNCOMPRESSED_BYTES:
                raise ArchiveError("archive expands to more than 2 GB")

        entries.append(
            Entry(
                path=path,
                is_dir=member.isdir(),
                size=member.size,
                # Drop setuid/setgid/sticky; keep only ordinary permission bits.
                mode=member.mode & 0o777,
            )
        )
    return entries


def _find_prefix(entries: list[Entry]) -> str:
    """Locate the directory holding settings.py.

    oTree writes members at the archive root, but projects re-packed by other tools are
    sometimes wrapped in a single top-level folder. Accept both shapes.
    """
    paths = {e.path for e in entries if not e.is_dir}
    if MARKER in paths:
        return ""

    tops = {e.path.split("/", 1)[0] for e in entries}
    if len(tops) == 1:
        only = tops.pop()
        if f"{only}/{MARKER}" in paths:
            return only

    raise ArchiveError(
        "this does not look like an oTree project: no settings.py found at the top level. "
        "Make sure you uploaded the .otreezip produced by 'otree zip'."
    )


def inspect(archive_path: Path) -> Inspection:
    """Vet an .otreezip without writing anything to disk."""
    try:
        with tarfile.open(archive_path, mode="r:gz") as tar:
            entries = _vet(tar)
    except tarfile.ReadError as exc:
        raise ArchiveError(
            "could not read this file as an .otreezip (expected a gzipped tar archive). "
            "Make sure you uploaded the file produced by 'otree zip'."
        ) from exc
    except EOFError as exc:
        raise ArchiveError("the uploaded file is truncated or incomplete") from exc

    if not entries:
        raise ArchiveError("the archive is empty")

    prefix = _find_prefix(entries)
    rebased: list[Entry] = []
    for entry in entries:
        path = entry.path
        if prefix:
            if path == prefix:
                continue
            if not path.startswith(prefix + "/"):
                continue
            path = path[len(prefix) + 1 :]
        if not path:
            continue
        rebased.append(
            Entry(path=path, is_dir=entry.is_dir, size=entry.size, mode=entry.mode)
        )

    return Inspection(
        entries=rebased,
        prefix=prefix,
        total_size=sum(e.size for e in rebased if not e.is_dir),
        has_requirements="requirements.txt" in {e.path for e in rebased},
    )


def extract(archive_path: Path, dest: Path) -> Inspection:
    """Vet, then extract into ``dest``, which must not already exist.

    Extraction is done member by member rather than via ``extractall`` so that no code
    path can ever write outside ``dest``.
    """
    inspection = inspect(archive_path)

    dest = dest.resolve()
    if dest.exists():
        raise ArchiveError(f"destination already exists: {dest}")
    dest.mkdir(parents=True)

    wanted = {e.path: e for e in inspection.entries}
    prefix = inspection.prefix

    with tarfile.open(archive_path, mode="r:gz") as tar:
        for member in tar:
            raw = _sanitize(member.name)
            if raw is None:
                continue
            path = raw
            if prefix:
                if not path.startswith(prefix + "/"):
                    continue
                path = path[len(prefix) + 1 :]
            entry = wanted.get(path)
            if entry is None:
                continue

            target = dest / path
            # Belt and braces: even after sanitizing, confirm we stayed inside dest.
            if not _within(dest, target):
                raise ArchiveError(f"refusing to write outside the project: {path!r}")

            if entry.is_dir:
                target.mkdir(parents=True, exist_ok=True)
                continue

            target.parent.mkdir(parents=True, exist_ok=True)
            source = tar.extractfile(member)
            if source is None:
                continue
            with open(target, "wb") as fh:
                while chunk := source.read(1024 * 256):
                    fh.write(chunk)
            # Preserve the executable bit, force-readable, never setuid.
            target.chmod(0o755 if entry.mode & 0o100 else 0o644)

    return inspection


def _within(root: Path, candidate: Path) -> bool:
    try:
        candidate.resolve().relative_to(root)
    except ValueError:
        return False
    return True


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as fh:
        while chunk := fh.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()
