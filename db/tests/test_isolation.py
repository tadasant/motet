"""One database per run — the property that stops two test runs destroying each other.

This is motet#15, and the diagnosis is worth restating because the symptom pointed
somewhere else. The ``db`` fixture truncates twelve tables before every test, which takes
``AccessExclusiveLock`` on each of them. ``bin/ci`` defaults ``DATABASE_URL`` to one fixed
name, so two runs on one machine — two agent sessions, two terminals, a local run beside a
CI job — were truncating each other's tables while the other was mid-INSERT. Postgres
detects the cycle, kills one, and the survivor carries on against tables that were emptied
underneath it. Nothing in either run is at fault, which is why it read as a leaked
connection: within one process the suite is serial and there is no second writer.

So the tests below assert ownership rather than politeness. Both fail loudly if this suite
ever goes back to sharing a database, and the second one reproduces the deadlock's exact
precondition — an uncommitted ``INSERT INTO jobs`` in another run — deterministically,
rather than waiting for the timing to line up.
"""

from __future__ import annotations

import threading
from typing import Any
from urllib.parse import urlsplit

import psycopg
from motet_db import discover, migrate

#: The other run's write, held open. Matches the statement named in motet#15's deadlock
#: report, because that is the one that lost.
HOLD_JOBS_OPEN = "INSERT INTO jobs (queue) VALUES ('test_isolation') RETURNING id"

#: Long enough that a slow machine truncating twelve empty tables is never the reason this
#: fails, short enough that a regression is a failure in seconds rather than a hung suite.
LOCK_TIMEOUT = "5s"


def database_name(url: str) -> str:
    return urlsplit(url).path.lstrip("/")


def test_the_run_has_a_database_of_its_own(
    db: psycopg.Connection[Any], base_database_url: str
) -> None:
    """The whole fix in one assertion."""
    row = db.execute("SELECT current_database() AS name").fetchone()
    assert row is not None
    assert row["name"] != database_name(base_database_url), (
        "this run is using the database DATABASE_URL names, so a second run would truncate"
        " these tables mid-test. See motet#15."
    )


def test_another_runs_open_write_cannot_block_this_runs_truncate(
    db: psycopg.Connection[Any], another_run: str, truncate_statement: str
) -> None:
    """motet#15's deadlock, reconstructed: the other half is a *second run*, not a leak.

    ``another_run`` is a database exactly like the one this run got. Holding an uncommitted
    INSERT there and truncating here is precisely the pair of statements Postgres reported
    as a deadlock — and with a database each, neither can see the other's locks.
    """
    with psycopg.connect(another_run) as other:
        other.execute(HOLD_JOBS_OPEN)  # deliberately not committed: the lock is the point

        db.execute(f"SET lock_timeout = '{LOCK_TIMEOUT}'")
        db.execute(truncate_statement)
        db.commit()

        # And the other run's rows are still there — the deadlock was the loud half of
        # this bug, and rows vanishing from under a passing test was the quiet half.
        row = other.execute("SELECT count(*) AS n FROM jobs").fetchone()
        assert row is not None
        assert row[0] == 1
        other.rollback()


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
