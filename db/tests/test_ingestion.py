"""``repo.list_ingestion`` — the query behind "where did the thing I pasted go?".

Against a real Postgres, because the whole answer is a join between a domain table and the
job queue, and the interesting cases are the ones where the two rows disagree: a source
item that is still ``pending`` while its job has already lost three attempts, and a source
item that is ``integrated`` while its job row says ``done``.

Skips without ``DATABASE_URL`` so a quick local run needs no Postgres; CI always has one.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from typing import Any

import psycopg
from motet_db import SourceItemState, repo

USER = repo.OWNER_USER_ID


def paste(conn: psycopg.Connection[Any], title: str = "Acme raises $20M") -> str:
    stored = repo.insert_source_item(conn, user_id=USER, title=title, text="Acme raised money.")
    return stored.id


def enqueue_integrate(
    conn: psycopg.Connection[Any],
    item_id: str,
    *,
    attempts: int = 0,
    state: str = "ready",
    last_error: str | None = None,
    due_in_seconds: int = 0,
) -> None:
    """A job row for ``item_id``, in whatever state the case under test needs.

    Written here rather than through ``motet_workers.jobs`` on purpose: ``db`` does not
    depend on ``workers``, and what is under test is the shape of the row the query reads,
    not how it got there.
    """
    conn.execute(
        """
        INSERT INTO jobs (queue, payload, attempts, state, last_error, run_at)
        VALUES ('integrate', %s::jsonb, %s, %s, %s, now() + make_interval(secs => %s))
        """,
        (json.dumps({"source_item_id": item_id}), attempts, state, last_error, due_in_seconds),
    )


class TestListIngestion:
    def test_a_fresh_paste_is_queued_with_nothing_spent_on_it_yet(
        self, db: psycopg.Connection[Any]
    ) -> None:
        item_id = paste(db)
        enqueue_integrate(db, item_id)

        (status,) = repo.list_ingestion(db, USER)
        assert status.id == item_id
        assert status.state is SourceItemState.PENDING
        assert status.attempts == 0
        assert status.last_error is None
        # Due now rather than unscheduled: "queued" and "nothing will ever happen to this"
        # have to be tellable apart, and this is the field that tells them apart.
        assert status.next_attempt_at is not None

    def test_a_retrying_item_reports_the_attempt_and_the_reason(
        self, db: psycopg.Connection[Any]
    ) -> None:
        """The case the whole route exists for.

        The source item is still ``pending`` and still has no error of its own — that is
        only written when the retries run out — so everything a person needs in order to
        know it is *working* rather than *stuck* is on the job row.
        """
        item_id = paste(db)
        enqueue_integrate(
            db,
            item_id,
            attempts=3,
            state="ready",
            last_error="ReasoningNotAppliedError: no reasoning evidence in the response",
            due_in_seconds=120,
        )

        (status,) = repo.list_ingestion(db, USER)
        assert status.state is SourceItemState.PENDING
        assert status.attempts == 3
        assert status.last_error is not None
        assert "ReasoningNotAppliedError" in status.last_error
        assert status.next_attempt_at is not None

    def test_an_item_in_flight_has_no_next_attempt_scheduled(
        self, db: psycopg.Connection[Any]
    ) -> None:
        """``running`` means a worker holds it now, so there is nothing scheduled.

        Reported as null rather than as the stale ``run_at`` the claim left behind, which
        would read as an attempt due in the past and be indistinguishable from a stall.
        """
        item_id = paste(db)
        enqueue_integrate(db, item_id, attempts=1, state="running")

        (status,) = repo.list_ingestion(db, USER)
        assert status.attempts == 1
        assert status.next_attempt_at is None

    def test_a_failed_item_reports_the_error_written_when_the_retries_ran_out(
        self, db: psycopg.Connection[Any]
    ) -> None:
        item_id = paste(db)
        enqueue_integrate(db, item_id, attempts=5, state="failed", last_error="boom (job)")
        repo.mark_source_item(db, item_id, SourceItemState.FAILED, error="boom (source item)")

        (status,) = repo.list_ingestion(db, USER)
        assert status.state is SourceItemState.FAILED
        assert status.attempts == 5
        # The domain object's own note wins: it is the one written at the moment the
        # pipeline gave up, which is the moment being reported.
        assert status.last_error == "boom (source item)"
        assert status.next_attempt_at is None

    def test_a_failed_item_never_advertises_a_retry_its_job_row_still_claims(
        self, db: psycopg.Connection[Any]
    ) -> None:
        """The two rows disagreeing must not produce "failed, and trying again in 30s".

        Set up deliberately inconsistent — the source item has given up, the job row has
        not — because that is the only way to exercise the ``si.state = 'pending'`` half
        of the gate on its own. The queue never leaves them like this; a stray re-enqueue
        or a hand-written UPDATE could, and the answer has to stay coherent either way.
        """
        item_id = paste(db)
        enqueue_integrate(db, item_id, attempts=5, state="ready", due_in_seconds=30)
        repo.mark_source_item(db, item_id, SourceItemState.FAILED, error="gave up")

        (status,) = repo.list_ingestion(db, USER)
        assert status.state is SourceItemState.FAILED
        assert status.next_attempt_at is None

    def test_the_list_is_bounded(self, db: psycopg.Connection[Any]) -> None:
        """One poll can create hundreds of source items; the SPA polls this every 3s."""
        for index in range(repo.INGESTION_MAX_ITEMS + 5):
            paste(db, title=f"Item {index}")

        assert len(repo.list_ingestion(db, USER)) == repo.INGESTION_MAX_ITEMS

    def test_a_succeeded_item_lingers_briefly_and_then_drops_off(
        self, db: psycopg.Connection[Any]
    ) -> None:
        item_id = paste(db)
        enqueue_integrate(db, item_id, attempts=1, state="done")
        repo.mark_source_item(db, item_id, SourceItemState.INTEGRATED)

        (status,) = repo.list_ingestion(db, USER)
        assert status.state is SourceItemState.INTEGRATED

        later = datetime.now(UTC) + repo.INTEGRATED_GRACE + timedelta(minutes=1)
        assert repo.list_ingestion(db, USER, now=later) == []

    def test_an_item_with_no_job_row_at_all_is_still_reported(
        self, db: psycopg.Connection[Any]
    ) -> None:
        """The worst case, and the one an inner join would hide.

        A source item with nothing queued against it will never be processed by anything.
        It is exactly the row that must not be silently absent from a list whose job is to
        account for everything that was pasted.
        """
        item_id = paste(db)

        (status,) = repo.list_ingestion(db, USER)
        assert status.id == item_id
        assert status.attempts == 0
        assert status.next_attempt_at is None

    def test_it_reports_only_this_user(self, db: psycopg.Connection[Any]) -> None:
        db.execute("INSERT INTO users (id, email) VALUES ('other', NULL)")
        other = repo.insert_source_item(db, user_id="other", title="Theirs", text="Theirs.")
        enqueue_integrate(db, other.id)
        mine = paste(db, title="Mine")
        enqueue_integrate(db, mine)

        assert [status.id for status in repo.list_ingestion(db, USER)] == [mine]

    def test_newest_first(self, db: psycopg.Connection[Any]) -> None:
        first = paste(db, title="First")
        second = paste(db, title="Second")
        db.execute(
            "UPDATE source_items SET created_at = created_at - interval '1 hour' WHERE id = %s",
            (first,),
        )

        assert [status.id for status in repo.list_ingestion(db, USER)] == [second, first]


def test_the_expression_index_covers_the_job_lookup(db: psycopg.Connection[Any]) -> None:
    """Migration 0005's index is actually the one this query uses.

    An expression index is easy to write and easy to have quietly not match the expression
    in the query — which leaves a sequential scan of every job ever run behind a route the
    SPA polls while anything is pending. The plan is the only thing that can say.
    """
    item_id = paste(db)
    enqueue_integrate(db, item_id)
    db.execute("SET enable_seqscan = off")
    plan = db.execute(
        """
        EXPLAIN SELECT attempts FROM jobs
        WHERE queue = 'integrate' AND payload ->> 'source_item_id' = %s
        """,
        (item_id,),
    ).fetchall()
    assert any("jobs_source_item_idx" in str(row) for row in plan), plan
