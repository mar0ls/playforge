"""Schema migration contract: fresh installs, upgrades from 0.0.x, replays, downgrades.

The upgrade path is the part a released 1.0 can't get wrong, so the pre-0.1.0
database here is built with raw SQL rather than by importing the models — that
way it keeps describing the *old* shape even after the models move on.
"""
from __future__ import annotations

import pytest
from sqlalchemy import create_engine

from app.models import migrations


@pytest.fixture
def conn(tmp_path):
    """A connection to an empty on-disk SQLite database."""
    engine = create_engine(f"sqlite:///{tmp_path / 'test.db'}")
    with engine.begin() as c:
        yield c
    engine.dispose()


def _legacy_0_0_x_schema(c) -> None:
    """The `runs`/`schedules` shape shipped before the columns 001 backfills."""
    c.exec_driver_sql(
        "CREATE TABLE runs ("
        "  id INTEGER PRIMARY KEY AUTOINCREMENT,"
        "  project_id VARCHAR(64) NOT NULL,"
        "  playbook VARCHAR(512) NOT NULL,"
        "  inventory VARCHAR(512) DEFAULT '',"
        "  tags VARCHAR(512) DEFAULT '',"
        "  status VARCHAR(32) DEFAULT 'pending',"
        "  started_at DATETIME,"
        "  ended_at DATETIME,"
        "  stats_json TEXT DEFAULT '',"
        "  failures_json TEXT DEFAULT ''"
        ")"
    )
    c.exec_driver_sql(
        "CREATE TABLE schedules ("
        "  id INTEGER PRIMARY KEY AUTOINCREMENT,"
        "  project_id VARCHAR(64) NOT NULL,"
        "  cron VARCHAR(128) NOT NULL"
        ")"
    )


# --- fresh installs ----------------------------------------------------------

def test_empty_database_is_fresh(conn):
    assert migrations.is_fresh_database(conn) is True


def test_fresh_install_is_stamped_current_and_runs_no_steps(conn):
    applied = migrations.run(conn, fresh=True)

    # `create_all` already produced the current schema, so replaying steps over it
    # would be wasted work at best.
    assert applied == []
    assert migrations.read_schema_version(conn) == migrations.SCHEMA_VERSION


def test_database_with_tables_is_not_fresh(conn):
    _legacy_0_0_x_schema(conn)
    assert migrations.is_fresh_database(conn) is False


# --- upgrade from 0.0.x ------------------------------------------------------

def test_unstamped_legacy_database_gets_every_column(conn):
    _legacy_0_0_x_schema(conn)
    assert migrations.read_schema_version(conn) is None

    applied = migrations.run(conn, fresh=False)

    assert "run_and_schedule_columns" in applied
    runs = migrations.columns(conn, "runs")
    assert {"template_id", "environment_id", "schedule_id", "artifacts_json"} <= runs
    assert "timezone" in migrations.columns(conn, "schedules")
    assert migrations.read_schema_version(conn) == migrations.SCHEMA_VERSION


def test_upgrade_preserves_existing_rows(conn):
    """An upgrade must not lose run history."""
    _legacy_0_0_x_schema(conn)
    conn.exec_driver_sql(
        "INSERT INTO runs (project_id, playbook, status) VALUES ('proj-1', 'site.yml', 'successful')"
    )

    migrations.run(conn, fresh=False)

    row = conn.exec_driver_sql(
        "SELECT project_id, playbook, status, artifacts_json FROM runs"
    ).fetchone()
    assert row[0] == "proj-1"
    assert row[1] == "site.yml"
    assert row[2] == "successful"
    assert row[3] in ("", None)  # new column, no value for a pre-existing row


def test_partially_migrated_database_only_adds_what_is_missing(conn):
    """A 0.0.4-era database: some columns already added by the old _soft_migrate."""
    _legacy_0_0_x_schema(conn)
    conn.exec_driver_sql("ALTER TABLE runs ADD COLUMN template_id INTEGER")
    conn.exec_driver_sql("ALTER TABLE runs ADD COLUMN environment_id INTEGER")

    migrations.run(conn, fresh=False)

    runs = migrations.columns(conn, "runs")
    assert {"template_id", "environment_id", "schedule_id", "artifacts_json"} <= runs
    # No duplicate columns from the re-add attempt.
    assert len([c for c in runs if c == "template_id"]) == 1


# --- replay / idempotency ----------------------------------------------------

def test_second_run_applies_nothing(conn):
    _legacy_0_0_x_schema(conn)
    migrations.run(conn, fresh=False)

    assert migrations.run(conn, fresh=False) == []
    assert migrations.read_schema_version(conn) == migrations.SCHEMA_VERSION


def test_steps_are_individually_idempotent(conn):
    """Every step is replayed on unstamped databases, so re-running must be safe."""
    _legacy_0_0_x_schema(conn)
    for _, _, fn in migrations.MIGRATIONS:
        fn(conn)
        fn(conn)  # must not raise "duplicate column name"


def test_migration_versions_are_contiguous_and_match_schema_version():
    versions = [v for v, _, _ in migrations.MIGRATIONS]
    assert versions == list(range(1, len(versions) + 1)), "renumbered or gapped migration"
    assert migrations.SCHEMA_VERSION == versions[-1], "SCHEMA_VERSION out of sync with MIGRATIONS"


# --- downgrade guard ---------------------------------------------------------

def test_newer_database_does_not_raise_and_applies_nothing(conn, caplog):
    """Old image against a newer database: warn loudly, keep serving."""
    _legacy_0_0_x_schema(conn)
    migrations.stamp(conn, 99)

    with caplog.at_level("WARNING"):
        applied = migrations.run(conn, fresh=False)

    assert applied == []
    assert migrations.read_schema_version(conn) == 99  # not clobbered
    assert "older image against a newer database" in caplog.text


def test_unreadable_version_is_treated_as_unstamped(conn):
    _legacy_0_0_x_schema(conn)
    migrations._ensure_meta_table(conn)
    conn.exec_driver_sql(
        "INSERT INTO schema_meta (key, value) VALUES ('schema_version', 'banana')"
    )

    assert migrations.read_schema_version(conn) is None
    assert migrations.run(conn, fresh=False) != []


# --- helpers -----------------------------------------------------------------

def test_add_column_skips_missing_table(conn):
    assert migrations.add_column(conn, "nope", "col", "INTEGER") is False


def test_add_column_reports_whether_it_acted(conn):
    _legacy_0_0_x_schema(conn)
    assert migrations.add_column(conn, "runs", "brand_new", "INTEGER") is True
    assert migrations.add_column(conn, "runs", "brand_new", "INTEGER") is False
