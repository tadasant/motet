"""Tests for the migration runner.

Discovery and naming are checked without a database. The apply path needs a real Postgres
and is skipped when ``DATABASE_URL`` is unset, so a laptop without Postgres still gets a
useful run — CI always has one, so the apply path is always covered there.
"""

from __future__ import annotations

import os
from pathlib import Path

import psycopg
import pytest
from motet_db import MIGRATIONS_DIR, MigrationError, discover, migrate

DATABASE_URL = os.environ.get("DATABASE_URL")
needs_postgres = pytest.mark.skipif(not DATABASE_URL, reason="DATABASE_URL is not set")


def test_every_migration_is_named_correctly_and_ordered() -> None:
    migrations = discover()
    assert migrations, "expected at least one migration"
    versions = [m.version for m in migrations]
    assert versions == sorted(versions)


def test_migrations_directory_is_where_the_runner_expects() -> None:
    assert MIGRATIONS_DIR.is_dir()
    assert MIGRATIONS_DIR.name == "migrations"


def test_a_badly_named_migration_is_rejected(tmp_path: Path) -> None:
    (tmp_path / "add-jobs.sql").write_text("SELECT 1;")
    with pytest.raises(MigrationError, match="does not match"):
        discover(tmp_path)


def test_duplicate_versions_are_rejected(tmp_path: Path) -> None:
    (tmp_path / "0001_a.sql").write_text("SELECT 1;")
    (tmp_path / "0001_b.sql").write_text("SELECT 1;")
    with pytest.raises(MigrationError, match="duplicate migration version"):
        discover(tmp_path)


@needs_postgres
def test_migrate_applies_everything_and_is_idempotent() -> None:
    assert DATABASE_URL is not None
    migrate(DATABASE_URL)

    # A second run must be a no-op — that is what makes it safe to run on every deploy.
    assert migrate(DATABASE_URL) == []

    with psycopg.connect(DATABASE_URL) as conn, conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM schema_migrations")
        row = cur.fetchone()
        assert row is not None
        assert row[0] == len(discover())

        cur.execute("SELECT to_regclass('public.jobs')")
        assert cur.fetchone() != (None,)


@needs_postgres
def test_the_queue_claim_skips_locked_rows() -> None:
    """The property the whole no-Redis decision rests on (AGENTS.md tripwire)."""
    assert DATABASE_URL is not None
    migrate(DATABASE_URL)

    claim = """
        SELECT id FROM jobs
        WHERE state = 'ready' AND queue = 'test_skip_locked' AND run_at <= now()
        ORDER BY run_at, id
        FOR UPDATE SKIP LOCKED
        LIMIT 1
    """

    with psycopg.connect(DATABASE_URL) as setup, setup.cursor() as cur:
        cur.execute("DELETE FROM jobs WHERE queue = 'test_skip_locked'")
        cur.execute("INSERT INTO jobs (queue) VALUES ('test_skip_locked') RETURNING id")
        row = cur.fetchone()
        assert row is not None
        job_id = row[0]
        setup.commit()

        try:
            with psycopg.connect(DATABASE_URL) as first, psycopg.connect(DATABASE_URL) as second:
                with first.cursor() as first_cur, second.cursor() as second_cur:
                    first_cur.execute(claim)
                    assert first_cur.fetchone() == (job_id,)

                    # The row is locked by `first`, so `second` must see nothing rather
                    # than block — that is the whole point of SKIP LOCKED.
                    second_cur.execute(claim)
                    assert second_cur.fetchone() is None
        finally:
            cur.execute("DELETE FROM jobs WHERE queue = 'test_skip_locked'")
            setup.commit()
