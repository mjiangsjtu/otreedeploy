from __future__ import annotations

import pytest

from otreedeploy import archive
from otreedeploy.archive import ArchiveError

from .conftest import REAL_OTREEZIP


def test_reads_otree_absolute_path_layout(otree_like):
    """oTree writes members as '/settings.py'; those must rebase to 'settings.py'."""
    result = archive.inspect(otree_like)
    paths = {e.path for e in result.entries}
    assert "settings.py" in paths
    assert "_static/global/empty.css" in paths
    assert result.prefix == ""
    assert result.has_requirements
    assert not any(p.startswith("/") for p in paths)


def test_extracts_absolute_path_layout(otree_like, tmp_path):
    dest = tmp_path / "release"
    archive.extract(otree_like, dest)
    assert (dest / "settings.py").read_text() == "SESSION_CONFIGS = []\n"
    assert (dest / "_static/global/empty.css").exists()
    # Nothing landed at the filesystem root of the temp tree.
    assert not (tmp_path / "settings.py").exists()


@pytest.mark.skipif(
    not REAL_OTREEZIP.exists(), reason="real otreezip fixture not present"
)
def test_real_otree_archive(tmp_path):
    """Ground truth: an archive produced by `otree zip` at version 6.0.15."""
    dest = tmp_path / "real"
    result = archive.extract(REAL_OTREEZIP, dest)
    assert (dest / "settings.py").exists()
    assert (dest / "requirements.txt").exists()
    assert result.has_requirements


def test_wrapped_in_single_top_level_dir(make_archive, tmp_path):
    """Projects re-packed by other tools may be wrapped in one folder; strip it."""
    path = make_archive(
        "wrapped.otreezip",
        [
            ("dir", "myproj/"),
            ("file", "myproj/settings.py", "x = 1\n"),
            ("file", "myproj/requirements.txt", "otree\n"),
        ],
    )
    result = archive.inspect(path)
    assert result.prefix == "myproj"
    assert {e.path for e in result.entries if not e.is_dir} == {
        "settings.py",
        "requirements.txt",
    }
    dest = tmp_path / "out"
    archive.extract(path, dest)
    assert (dest / "settings.py").exists()
    assert not (dest / "myproj").exists()


def test_rejects_path_traversal(make_archive):
    path = make_archive(
        "evil.otreezip",
        [
            ("file", "/settings.py", "x = 1\n"),
            ("file", "/../../etc/passwd", "root::0:0::/:/bin/sh\n"),
        ],
    )
    with pytest.raises(ArchiveError, match="escape"):
        archive.inspect(path)


def test_rejects_deep_path_traversal(make_archive):
    path = make_archive(
        "evil2.otreezip",
        [
            ("file", "/settings.py", "x = 1\n"),
            ("file", "/a/b/../../../../etc/shadow", "nope\n"),
        ],
    )
    with pytest.raises(ArchiveError, match="escape"):
        archive.inspect(path)


def test_rejects_symlink(make_archive):
    path = make_archive(
        "link.otreezip",
        [
            ("file", "/settings.py", "x = 1\n"),
            ("symlink", "/shortcut", "/etc/passwd"),
        ],
    )
    with pytest.raises(ArchiveError, match="link"):
        archive.inspect(path)


def test_rejects_hardlink(make_archive):
    path = make_archive(
        "hard.otreezip",
        [
            ("file", "/settings.py", "x = 1\n"),
            ("hardlink", "/hard", "/etc/passwd"),
        ],
    )
    with pytest.raises(ArchiveError, match="link"):
        archive.inspect(path)


def test_rejects_special_file(make_archive):
    path = make_archive(
        "fifo.otreezip",
        [("file", "/settings.py", "x = 1\n"), ("fifo", "/pipe")],
    )
    with pytest.raises(ArchiveError, match="special file"):
        archive.inspect(path)


def test_rejects_windows_separator_traversal(make_archive):
    path = make_archive(
        "win.otreezip",
        [
            ("file", "/settings.py", "x = 1\n"),
            ("file", "/..\\..\\etc\\passwd", "nope\n"),
        ],
    )
    with pytest.raises(ArchiveError, match="escape"):
        archive.inspect(path)


def test_rejects_archive_without_settings(make_archive):
    path = make_archive(
        "nototree.otreezip",
        [("file", "/README.md", "hello\n"), ("file", "/main.py", "print(1)\n")],
    )
    with pytest.raises(ArchiveError, match="does not look like an oTree project"):
        archive.inspect(path)


def test_rejects_non_gzip(tmp_path):
    path = tmp_path / "bogus.otreezip"
    path.write_bytes(b"this is definitely not a tarball")
    with pytest.raises(ArchiveError, match="could not read this file"):
        archive.inspect(path)


def test_rejects_empty_archive(make_archive):
    path = make_archive("empty.otreezip", [])
    with pytest.raises(ArchiveError, match="empty"):
        archive.inspect(path)


def test_extract_refuses_existing_destination(otree_like, tmp_path):
    dest = tmp_path / "taken"
    dest.mkdir()
    with pytest.raises(ArchiveError, match="already exists"):
        archive.extract(otree_like, dest)


def test_strips_setuid_bits(make_archive, tmp_path):
    path = make_archive("suid.otreezip", [("file", "/settings.py", "x = 1\n")])
    dest = tmp_path / "out"
    archive.extract(path, dest)
    assert (dest / "settings.py").stat().st_mode & 0o7000 == 0
