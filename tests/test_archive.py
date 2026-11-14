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


# ---------------------------------------------------------------- edge cases (N5)


def _tar_gz(path, members, end_marker=True):
    """A gzipped tar built by hand, so its end can be cut off on purpose."""
    import gzip
    import io
    import tarfile

    raw = io.BytesIO()
    with tarfile.open(fileobj=raw, mode="w") as tar:
        for name, data in members:
            info = tarfile.TarInfo(name)
            info.size = len(data)
            info.mode = 0o644
            tar.addfile(info, io.BytesIO(data))
    data = raw.getvalue()
    if not end_marker:
        # Keep the members, drop everything from the end-of-archive blocks on.
        size = sum(512 + -(-len(d) // 512) * 512 for _, d in members)
        data = data[:size]
    path.write_bytes(gzip.compress(data))
    return path


PROJECT = [("settings.py", b"SESSION_CONFIGS = []\n"), ("app/__init__.py", b"")]


def test_an_archive_cut_off_between_two_files_is_refused(tmp_path):
    """gzip's checksum cannot catch a tar that was cut short and then compressed."""
    whole = _tar_gz(tmp_path / "whole.otreezip", PROJECT)
    assert archive.inspect(whole).file_count == 2
    cut = _tar_gz(tmp_path / "cut.otreezip", PROJECT, end_marker=False)
    with pytest.raises(ArchiveError, match="incomplete"):
        archive.inspect(cut)


def test_a_path_that_is_both_a_file_and_a_folder_is_refused_in_words(tmp_path):
    path = _tar_gz(tmp_path / "x.otreezip", PROJECT + [("app", b"x"), ("app/models.py", b"")])
    with pytest.raises(ArchiveError, match="both a file and a folder"):
        archive.inspect(path)


def test_too_many_members_are_refused(tmp_path, monkeypatch):
    monkeypatch.setattr(archive, "MAX_MEMBERS", 3)
    path = _tar_gz(tmp_path / "x.otreezip", PROJECT + [(f"f{i}", b"") for i in range(5)])
    with pytest.raises(ArchiveError, match="more than 3 entries"):
        archive.inspect(path)


def test_too_much_uncompressed_data_is_refused(tmp_path, monkeypatch):
    monkeypatch.setattr(archive, "MAX_UNCOMPRESSED_BYTES", 1000)
    path = _tar_gz(tmp_path / "x.otreezip", PROJECT + [("big.bin", b"\0" * 5000)])
    with pytest.raises(ArchiveError):
        archive.inspect(path)


def test_setuid_and_setgid_bits_never_survive_extraction(make_archive, tmp_path):
    import io
    import tarfile

    path = tmp_path / "suid.otreezip"
    with tarfile.open(path, "w:gz") as tar:
        for name, mode in (("settings.py", 0o644), ("tool", 0o4755), ("other", 0o2755)):
            info = tarfile.TarInfo(name)
            info.size = 1
            info.mode = mode
            tar.addfile(info, io.BytesIO(b"x"))
    archive.extract(path, tmp_path / "out")
    for name in ("tool", "other"):
        mode = (tmp_path / "out" / name).stat().st_mode
        assert not mode & 0o6000
        assert mode & 0o777 == 0o755
