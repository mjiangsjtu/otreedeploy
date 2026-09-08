"""Project identity: slugs, and the derived names used by systemd and PostgreSQL.

A slug is the single identifier a project is known by everywhere: the directory under
/srv/otree, the systemd instance name, and the PostgreSQL database. It is therefore
interpolated into command lines and SQL identifiers, so the charset is deliberately
narrow and validated at every boundary.
"""

from __future__ import annotations

import re

# Lowercase, starts with a letter (unquoted PostgreSQL identifiers may not start with a
# digit), and no characters that mean anything to a shell, systemd, or SQL.
SLUG_RE = re.compile(r"^[a-z][a-z0-9_]{0,38}$")

RESERVED = {"current", "releases", "postgres", "template0", "template1", "otree"}


class InvalidSlug(ValueError):
    pass


def validate(slug: str) -> str:
    """Return the slug, or raise. Call this before any slug reaches a command or query."""
    if not SLUG_RE.match(slug):
        raise InvalidSlug(
            f"invalid project name {slug!r}: use 1-39 characters, starting with a letter, "
            "containing only lowercase letters, digits and underscores"
        )
    if slug in RESERVED:
        raise InvalidSlug(f"{slug!r} is a reserved name")
    return slug


def slugify(filename: str) -> str:
    """Derive a slug from an uploaded filename such as 'Trust Game v2.otreezip'."""
    stem = filename.rsplit("/", 1)[-1].rsplit("\\", 1)[-1]
    for suffix in (".otreezip", ".tar.gz", ".tgz", ".zip"):
        if stem.lower().endswith(suffix):
            stem = stem[: -len(suffix)]
            break

    slug = re.sub(r"[^a-z0-9]+", "_", stem.lower()).strip("_")
    slug = re.sub(r"_+", "_", slug)
    if not slug:
        slug = "project"
    if not slug[0].isalpha():
        slug = "p_" + slug
    slug = slug[:39].rstrip("_")
    if slug in RESERVED:
        slug = slug + "_1"
    return validate(slug)


def unique_slug(desired: str, taken: set[str]) -> str:
    """Append a numeric suffix until the slug is free."""
    if desired not in taken:
        return desired
    for n in range(2, 1000):
        candidate = f"{desired[:36]}_{n}"
        if candidate not in taken:
            return validate(candidate)
    raise InvalidSlug("could not find a free project name")


def db_name(slug: str) -> str:
    return f"otree_{validate(slug)}"


def unit_name(slug: str) -> str:
    return f"otree@{validate(slug)}.service"
