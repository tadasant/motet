"""One database per run — the property that stops two test runs destroying each other.

This is motet#15, and the diagnosis is worth restating because the symptom pointed
somewhere else. The ``db`` fixture truncates twelve tables before every test, which takes
``AccessExclusiveLock`` on each of them. ``bin/ci`` defaults ``DATABASE_URL`` to one fixed
name, so two runs on one machine — two agent sessions, two terminals, a local run beside a
CI job — were truncating each other's tables while the other was mid-INSERT. Postgres
detects the cycle, kills one, and the survivor carries on against tables that were emptied
underneath it. Nothing in either run is at fault, which is why it read as a leaked
connection: within one process the suite is serial and there is no second writer.

Two of the tests below are the guard and one is the explanation.
:func:`test_the_run_has_a_database_of_its_own` is what goes red if the isolation is ever
undone. :func:`test_a_second_writer_in_one_database_blocks_the_truncate` demonstrates,
deterministically and inside this run's own database, *why* that would matter — it is the
deadlock's precondition reproduced on demand rather than waited for.
"""

from __future__ import annotations

import threading
from typing import Any
from urllib.parse import urlsplit

import psycopg
import pytest
from motet_db import discover, migrate

#: The other writer's statement, held open. The one named in motet#15's deadlock report,
#: because it is the one that lost.
HOLD_JOBS_OPEN = "INSERT INTO jobs (queue) VALUES ('test_isolation') RETURNING id"

#: Long enough that a slow machine truncating twelve empty tables is never the reason a
#: test fails, short enough that a blocked truncate is a failure in seconds rather than a
#: hung suite.
LOCK_TIMEOUT = "5s"


def database_name(url: str) -> str:
    return urlsplit(url).path.lstrip("/")


def test_the_run_has_a_database_of_its_own(
    db: psycopg.Connection[Any], base_database_url: str
) -> None:
    """The fix in one assertion, and the test that goes red if it is undone."""
    row = db.execute("SELECT current_database() AS name").fetchone()
    assert row is not None
    assert row["name"] != database_name(base_database_url), (
        "this run is using the database DATABASE_URL names, so a second run would truncate"
        " these tables mid-test. See motet#15."
    )


def test_a_second_writer_in_one_database_blocks_the_truncate(
    db: psycopg.Connection[Any], database_url: str, truncate_statement: str
) -> None:
    """Why sharing a database is fatal, shown rather than asserted about.

    A second connection to *this run's own* database — standing in for the second run that
    used to be there — holds an uncommitted ``INSERT INTO jobs``. The fixture's own
    ``TRUNCATE`` then cannot proceed, because ``AccessExclusiveLock`` conflicts with the
    ``RowExclusiveLock`` that INSERT is holding. In motet#15 both sides were waiting, so
    Postgres called it a deadlock and killed one; here only one side waits, so it is a lock
    timeout — the same conflict, made observable without racing anything.

    Deliberately not run against a second *database*: databases do not share a lock table,
    so that version of this test would pass however broken the isolation was.
    """
    with psycopg.connect(database_url) as other:
        other.execute(HOLD_JOBS_OPEN)  # not committed: holding the lock is the point

        db.execute(f"SET lock_timeout = '{LOCK_TIMEOUT}'")
        with pytest.raises(psycopg.errors.LockNotAvailable):
            db.execute(truncate_statement)
        db.rollback()

        other.rollback()

    # And once nobody else is writing, the very same statement is instant.
    db.execute(truncate_statement)
    db.commit()


def test_two_migration_runs_on_one_database_do_not_collide(blank_database: str) -> None:
    """The one step of ``bin/ci`` that still touches the configured database.

    ``bin/ci`` applies migrations to ``DATABASE_URL`` before it runs pytest, so two
    concurrent ``bin/ci`` runs still meet there. On an already-migrated database that is a
    no-op; on a fresh one, both would apply the first pending migration and one would fail
    on a table the other had just created.
    """
    applied: list[list[str]] = []
    errors: list[BaseException] = []

    def run() -> None:
        try:
            applied.append(migrate(blank_database))
        except BaseException as exc:  # noqa: BLE001 — reported below rather than swallowed
            errors.append(exc)

    threads = [threading.Thread(target=run) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=60)

    assert not errors, errors
    # One run applies everything and the other finds nothing to do; which is which depends
    # on who took the lock first, and does not matter.
    assert sorted(len(versions) for versions in applied) == [0, len(discover())]
