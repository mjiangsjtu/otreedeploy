"""Compare a release's models with the project's existing database (review item M1).

oTree has no migrations. At start-up it only runs ``metadata.create_all()``, which adds
missing tables and never looks at columns. So a new field on an existing model is a
column the database lacks, and the study answers HTTP 500 on the next page -- while
the server itself starts and responds normally. This script finds such differences
before a release is published, and can apply the ones that are safe.

It runs with the *release's own* Python and as the *project's own* user (otree-ctl
`schema`), because building the metadata means importing the experimenter's models.
It is a standalone script: it may use only what the release's environment has (oTree
and SQLAlchemy), never the otreedeploy package.

Usage, from inside the release directory with DATABASE_URL set:
    python schema_probe.py check   # report only
    python schema_probe.py apply   # report, then add the missing nullable columns

The report is one line of JSON after MARKER on stdout; everything oTree prints while
loading is left above it.
"""

from __future__ import annotations

import json
import os
import sys

MARKER = "OTREEDEPLOY-SCHEMA:"


def _category(sa, column_type) -> str | None:
    """A coarse kind for a column type, or None when it cannot be compared reliably.

    Coarse on purpose: reflection never returns exactly the declared type (FLOAT comes
    back as DOUBLE PRECISION, a sized VARCHAR as plain VARCHAR). What matters is a
    change of kind -- a number becoming text -- which existing data cannot follow.
    """
    if isinstance(column_type, sa.types.TypeDecorator):
        # oTree's own column types wrap a standard one. `impl` is an instance in the
        # SQLAlchemy oTree ships with, a class in some other versions.
        impl = column_type.impl
        column_type = impl() if isinstance(impl, type) else impl
    for name, cls in (
        ("boolean", sa.Boolean),
        ("integer", sa.Integer),
        ("number", sa.Numeric),
        ("number", sa.Float),
        ("text", sa.String),
        ("time", sa.DateTime),
        ("time", sa.Date),
        ("binary", sa.LargeBinary),
        ("json", sa.JSON),
    ):
        if isinstance(column_type, cls):
            return name
    return None


def compare(sa, metadata, engine) -> dict:
    from sqlalchemy.schema import CreateColumn

    inspector = sa.inspect(engine)
    live_tables = set(inspector.get_table_names())
    report: dict = {
        "new_tables": [],
        "added": [],
        "removed": [],
        "blocking": [],
        "dropped_tables": sorted(live_tables - set(metadata.tables)),
    }
    for table in metadata.sorted_tables:
        if table.name not in live_tables:
            report["new_tables"].append(table.name)
            continue
        live = {c["name"]: c for c in inspector.get_columns(table.name)}
        for column in table.columns:
            where = f"{table.name}.{column.name}"
            if column.name not in live:
                has_default = column.server_default is not None
                if not column.nullable and not has_default:
                    report["blocking"].append(
                        f"{where} is a new required field without a default, which the "
                        "rows already collected cannot have"
                    )
                else:
                    ddl = str(CreateColumn(column).compile(dialect=engine.dialect)).strip()
                    report["added"].append({"table": table.name, "column": column.name,
                                            "ddl": ddl})
                continue
            new_kind = _category(sa, column.type)
            old_kind = _category(sa, live[column.name]["type"])
            if new_kind and old_kind and new_kind != old_kind:
                report["blocking"].append(
                    f"{where} changes from {old_kind} to {new_kind}, and the data "
                    "already collected cannot be converted automatically"
                )
        model_columns = {c.name for c in table.columns}
        for name, column in live.items():
            if name in model_columns:
                continue
            if not column.get("nullable", True) and column.get("default") is None:
                report["blocking"].append(
                    f"{table.name}.{name} is no longer in the code but is required in "
                    "the database, so new rows could not be saved"
                )
            else:
                report["removed"].append(f"{table.name}.{name}")
    return report


def apply(sa, engine, report: dict) -> None:
    """Add the missing nullable columns, all in one transaction."""
    preparer = engine.dialect.identifier_preparer
    with engine.begin() as connection:
        for change in report["added"]:
            connection.execute(sa.text(
                f"ALTER TABLE {preparer.quote(change['table'])} ADD COLUMN {change['ddl']}"
            ))


def main(argv: list[str]) -> int:
    if len(argv) != 2 or argv[1] not in {"check", "apply"}:
        print("usage: schema_probe.py check|apply", file=sys.stderr)
        return 64
    database_url = os.environ["DATABASE_URL"]
    # Build oTree's metadata against a throwaway in-memory database, so that loading
    # the models cannot create or change anything in the real one.
    os.environ["OTREE_IN_MEMORY"] = "1"
    os.environ["OTREE_EPHEMERAL"] = "1"
    sys.path.insert(0, os.getcwd())

    import sqlalchemy as sa

    import otree.main

    otree.main.setup()
    from otree.database import AnyModel

    engine = sa.create_engine(database_url)
    try:
        report = compare(sa, AnyModel.metadata, engine)
        if argv[1] == "apply" and not report["blocking"]:
            apply(sa, engine, report)
            report["applied"] = True
    finally:
        engine.dispose()
    print(MARKER + json.dumps(report), flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
