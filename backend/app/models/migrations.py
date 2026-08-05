"""Versioned SQLite schema migrations.

Replaces `_soft_migrate`, which replayed a hand-written list of ADD COLUMN guards
on every boot — fine for nullable columns, useless for backfills or renames, and
with no record of which schema a database was actually on.

Not Alembic: this app ships no infra beyond one SQLite file, and Alembic would add
a dependency, alembic.ini, env.py, a versions/ tree, plus the stamping problem for
databases existing installs already built with create_all.

Rules:
- SCHEMA_VERSION is the schema the running code expects.
- MIGRATIONS is ordered `(version, name, fn)`, contiguous from 1. Never edit or
  renumber a released step — append.
- The version a database is on lives in the `schema_meta` table.
- Fresh database: create_all already produces the current schema, so it's stamped
  at SCHEMA_VERSION and no step runs.
- Pre-0.1.0 database: has tables, no `schema_meta`. Treated as version 0, every
  step replayed — hence every step must be idempotent (add_column checks
  PRAGMA table_info first).

New migration: add a `_mNNN_*` function, append to MIGRATIONS, bump
SCHEMA_VERSION, cover it in tests/test_migrations.py.
"""
from __future__ import annotations

import logging

logger = logging.getLogger(__name__)

_META_TABLE = "schema_meta"


# --- introspection helpers ---------------------------------------------------

def table_exists(conn, table: str) -> bool:
    row = conn.exec_driver_sql(
        "SELECT name FROM sqlite_master WHERE type='table' AND name=?", (table,)
    ).fetchone()
    return row is not None


def columns(conn, table: str) -> set[str]:
    """Column names of `table`, or an empty set when the table doesn't exist."""
    return {row[1] for row in conn.exec_driver_sql(f"PRAGMA table_info({table})").fetchall()}


def add_column(conn, table: str, column: str, ddl: str) -> bool:
    """Add `column` to `table` unless it's already there. Returns True if added.

    No-op when the table itself is missing: `create_all` runs before migrations,
    so a missing table means this migration predates that model entirely.
    """
    existing = columns(conn, table)
    if not existing or column in existing:
        return False
    conn.exec_driver_sql(f"ALTER TABLE {table} ADD COLUMN {column} {ddl}")
    logger.info("migration: added %s.%s", table, column)
    return True


# --- migration steps ---------------------------------------------------------

def _m001_run_and_schedule_columns(conn) -> None:
    """Columns accreted across 0.0.1–0.0.7 by the old `_soft_migrate`.

    Replayed verbatim so a database from any 0.0.x lands on the same schema a
    fresh `create_all` produces.
    """
    add_column(conn, "runs", "template_id", "INTEGER")
    add_column(conn, "runs", "environment_id", "INTEGER")
    add_column(conn, "runs", "schedule_id", "INTEGER")
    add_column(conn, "runs", "artifacts_json", "TEXT DEFAULT ''")
    add_column(conn, "schedules", "timezone", "VARCHAR(64) DEFAULT ''")


def _m002_run_user_id(conn) -> None:
    """`runs.user_id` — who started a run, for the audit trail.

    The `users` table itself needs no step here: it's a model, so `create_all`
    builds it before migrations run. Only columns added to tables that already
    exist need a migration.
    """
    add_column(conn, "runs", "user_id", "INTEGER")


MIGRATIONS: list[tuple[int, str, object]] = [
    (1, "run_and_schedule_columns", _m001_run_and_schedule_columns),
    (2, "run_user_id", _m002_run_user_id),
]

SCHEMA_VERSION = 2


# --- version bookkeeping -----------------------------------------------------

def _ensure_meta_table(conn) -> None:
    conn.exec_driver_sql(
        f"CREATE TABLE IF NOT EXISTS {_META_TABLE} "
        "(key VARCHAR(64) PRIMARY KEY, value VARCHAR(64) NOT NULL)"
    )


def read_schema_version(conn) -> int | None:
    """Version this database is stamped at, or None if it was never stamped."""
    if not table_exists(conn, _META_TABLE):
        return None
    row = conn.exec_driver_sql(
        f"SELECT value FROM {_META_TABLE} WHERE key='schema_version'"
    ).fetchone()
    if row is None:
        return None
    try:
        return int(row[0])
    except (TypeError, ValueError):
        logger.warning("migration: unreadable schema_version %r, treating as unstamped", row[0])
        return None


def stamp(conn, version: int) -> None:
    _ensure_meta_table(conn)
    conn.exec_driver_sql(
        f"INSERT INTO {_META_TABLE} (key, value) VALUES ('schema_version', ?) "
        "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
        (str(version),),
    )


def is_fresh_database(conn) -> bool:
    """True when the database holds no application tables yet.

    Must be called *before* `create_all`, which is what makes a brand-new file
    distinguishable from a pre-0.1.0 one that predates version stamping.
    """
    rows = conn.exec_driver_sql(
        "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
    ).fetchall()
    return not rows


def run(conn, fresh: bool) -> list[str]:
    """Bring `conn`'s schema up to `SCHEMA_VERSION`. Returns the steps applied.

    Call after `create_all`, with the `fresh` verdict taken before it.
    """
    _ensure_meta_table(conn)

    if fresh:
        stamp(conn, SCHEMA_VERSION)
        logger.info("migration: fresh database stamped at schema v%d", SCHEMA_VERSION)
        return []

    current = read_schema_version(conn)
    if current is None:
        # Pre-0.1.0 database: unknown schema, replay every (idempotent) step.
        current = 0
        logger.info("migration: unstamped database, replaying from v0")

    if current > SCHEMA_VERSION:
        # An older image running against a newer database. We can't undo the
        # newer schema, and SQLite tolerates extra columns, so keep serving —
        # but make the mismatch impossible to miss in the logs.
        logger.warning(
            "migration: database is at schema v%d but this build expects v%d — "
            "you are running an older image against a newer database. Downgrades are "
            "not supported; restore a backup taken before the upgrade or run the newer image.",
            current, SCHEMA_VERSION,
        )
        return []

    applied: list[str] = []
    for version, name, fn in MIGRATIONS:
        if version <= current:
            continue
        logger.info("migration: applying v%d (%s)", version, name)
        fn(conn)  # type: ignore[operator]
        stamp(conn, version)
        applied.append(name)

    if applied:
        logger.info("migration: schema now at v%d (applied %s)", SCHEMA_VERSION, ", ".join(applied))
    return applied
