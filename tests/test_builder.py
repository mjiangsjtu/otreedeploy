from __future__ import annotations

from otreedeploy import builder


def test_rewrites_bare_psycopg2():
    text = "otree>=5.0.0a21\npsycopg2>=2.8.4\n"
    out, notes = builder.normalize_requirements(text)
    assert "psycopg2-binary>=2.8.4" in out
    assert "\npsycopg2>" not in out
    assert notes


def test_rewrites_pinned_psycopg2():
    out, _ = builder.normalize_requirements("psycopg2==2.9.9\n")
    assert out == "psycopg2-binary==2.9.9\n"


def test_rewrites_unversioned_psycopg2():
    out, _ = builder.normalize_requirements("psycopg2\n")
    assert out == "psycopg2-binary\n"


def test_leaves_psycopg2_binary_alone():
    text = "psycopg2-binary==2.9.9\n"
    out, notes = builder.normalize_requirements(text)
    assert out == text
    assert not notes


def test_leaves_psycopg3_alone():
    text = "psycopg[binary]>=3.2\n"
    out, notes = builder.normalize_requirements(text)
    assert out == text
    assert not notes


def test_leaves_unrelated_packages_alone():
    text = "numpy==1.26.0\notree\n"
    out, notes = builder.normalize_requirements(text)
    assert out == text
    assert not notes


def test_normalize_release_removes_empty_python_version(tmp_path):
    (tmp_path / ".python-version").write_text("\n")
    (tmp_path / "requirements.txt").write_text("otree\n")
    result = builder.normalize_release(tmp_path)
    assert not (tmp_path / ".python-version").exists()
    assert any("python-version" in n for n in result.notes)


def test_normalize_release_keeps_real_python_version(tmp_path):
    (tmp_path / ".python-version").write_text("3.12\n")
    (tmp_path / "requirements.txt").write_text("otree\n")
    builder.normalize_release(tmp_path)
    assert (tmp_path / ".python-version").read_text().strip() == "3.12"


def test_normalize_release_supplies_missing_requirements(tmp_path):
    result = builder.normalize_release(tmp_path)
    assert (tmp_path / "requirements.txt").exists()
    assert "otree" in (tmp_path / "requirements.txt").read_text()
    assert any("no requirements.txt" in n for n in result.notes)


def test_normalize_release_rewrites_requirements_on_disk(tmp_path):
    (tmp_path / "requirements.txt").write_text("otree\npsycopg2>=2.8.4\n")
    builder.normalize_release(tmp_path)
    assert "psycopg2-binary" in (tmp_path / "requirements.txt").read_text()
