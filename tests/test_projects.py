from __future__ import annotations

import pytest

from otreedeploy import projects
from otreedeploy.projects import InvalidSlug


@pytest.mark.parametrize(
    "filename,expected",
    [
        ("trust_game.otreezip", "trust_game"),
        ("Trust Game v2.otreezip", "trust_game_v2"),
        ("public-goods.otreezip", "public_goods"),
        ("Ultimatum!!.otreezip", "ultimatum"),
        ("2024_pilot.otreezip", "p_2024_pilot"),
        ("  spaced  .otreezip", "spaced"),
        ("study.tar.gz", "study"),
        ("---.otreezip", "project"),
    ],
)
def test_slugify(filename, expected):
    assert projects.slugify(filename) == expected


def test_slugify_strips_directory_components():
    assert projects.slugify("C:\\Users\\me\\My Study.otreezip") == "my_study"
    assert projects.slugify("/home/me/My Study.otreezip") == "my_study"


def test_slugify_truncates_long_names():
    slug = projects.slugify("a" * 200 + ".otreezip")
    assert len(slug) <= 39
    projects.validate(slug)


@pytest.mark.parametrize(
    "bad",
    [
        "",
        "Trust",          # uppercase
        "9lives",         # leading digit
        "has-hyphen",     # hyphen would split a systemd instance name confusingly
        "has space",
        "semi;colon",
        "quote'name",
        "dollar$sign",
        "../escape",
        "a" * 40,
        "otree",          # reserved
        "current",        # reserved
    ],
)
def test_validate_rejects(bad):
    with pytest.raises(InvalidSlug):
        projects.validate(bad)


def test_reserved_slug_gets_suffixed():
    assert projects.slugify("otree.otreezip") == "otree_1"


def test_unique_slug():
    assert projects.unique_slug("trust", set()) == "trust"
    assert projects.unique_slug("trust", {"trust"}) == "trust_2"
    assert projects.unique_slug("trust", {"trust", "trust_2"}) == "trust_3"


def test_derived_names():
    assert projects.db_name("trust_game") == "otree_trust_game"
    assert projects.unit_name("trust_game") == "otree@trust_game.service"


def test_derived_names_validate():
    with pytest.raises(InvalidSlug):
        projects.db_name("evil; DROP DATABASE postgres")
    with pytest.raises(InvalidSlug):
        projects.unit_name("evil boundary")
