"""``repo.list_ingestion`` — the query behind "where did the thing I pasted go?".

Against a real Postgres, because the whole answer is a join between a domain table and the
job queue, and the interesting cases are the ones where the two rows disagree: a source
item that is still ``pending`` while its job has already lost three attempts, and a source
item that is ``integrated`` while its job row says ``done``.

The second half of this file is the case where there is no domain table row *at all* — a
polled mailbox message whose extraction failed, which is motet#35.

Skips without ``DATABASE_URL`` so a quick local run needs no Postgres; CI always has one.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from typing import Any

import psycopg
from motet_db import SourceItemState, SourceKind, phase2, repo

USER = repo.OWNER_USER_ID


def paste(conn: psycopg.Connection[Any], title: str = "Acme raises $20M") -> str:
    stored = repo.insert_source_item(conn, user_id=USER, title=title, text="Acme raised money.")
    return stored.id


def gmail_source(db: psycopg.Connection[Any], *, user_id: str = USER) -> str:
    return phase2.create_source(db, user_id=user_id, kind=SourceKind.GMAIL.value, name="Gmail").id


def polled_item(db: psycopg.Connection[Any], source_id: str, message_id: str) -> str:
    """A source item extraction did produce, keyed by the provider's message id."""
    item_id = phase2.insert_polled_source_item(
        db,
        user_id=USER,
        source_id_=source_id,
        external_id=message_id,
        title="Acme raises $20M",
        text="Acme raised money.",
    )
    assert item_id is not None
    return item_id


def enqueue_extract(
    db: psycopg.Connection[Any],
    source_id: str,
    message_id: str,
    *,
    attempts: int = 0,
    state: str = "ready",
    last_error: str | None = None,
    due_in_seconds: int = 0,
) -> None:
    """The job row a poll leaves behind, in whatever state the case under test needs.

    Written directly for the same reason ``enqueue_integrate`` is: ``db`` does not depend
    on ``workers``, and what is under test is the shape of the row the query reads.
    """
    db.execute(
        """
        INSERT INTO jobs (queue, payload, attempts, state, last_error, run_at)
        VALUES ('extract', %s::jsonb, %s, %s, %s, now() + make_interval(secs => %s))
        """,
        (
            json.dumps({"source_id": source_id, "message_id": message_id}),
            attempts,
            state,
            last_error,
            due_in_seconds,
        ),
    )


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


class TestExtractJobsWithNoSourceItem:
    """motet#35 — a polled message before, or instead of, a ``source_items`` row.

    ``handle_extract`` writes the row when extraction *succeeds*, so between the poll and
    the parse the job row is the entire record that the message was ever seen. A message
    whose extraction fails five times leaves only that, and the poll cursor advanced in
    the same transaction that queued it — so it is never looked at again by anything.
    Reported from ``source_items`` alone, it was invisible on every surface the user has.
    """

    def test_a_message_that_never_extracted_is_reported_with_its_reason(
        self, db: psycopg.Connection[Any]
    ) -> None:
        """The defect, stated as an assertion. This list is the only place it can appear."""
        source_id = gmail_source(db)
        enqueue_extract(
            db,
            source_id,
            "18f2a3b4c5",
            attempts=5,
            state="failed",
            last_error="RuntimeError: gmail returned 503 for message 18f2a3b4c5",
        )

        (status,) = repo.list_ingestion(db, USER)
        assert status.state is SourceItemState.FAILED
        assert status.attempts == 5
        assert status.last_error is not None and "503" in status.last_error
        assert status.next_attempt_at is None
        # Named by the provider's id, because reading the subject line is the step that
        # failed — there is no title anywhere to show instead.
        assert status.title == "Gmail message 18f2a3b4c5"
        assert status.source_kind == SourceKind.GMAIL.value

    def test_an_auth_failure_and_a_fetch_failure_are_told_apart_by_their_reasons(
        self, db: psycopg.Connection[Any]
    ) -> None:
        """Two failure classes, two repairs: reconnect the mailbox, or wait for it.

        Both look identical from the queue's side — a failed job on the extract queue —
        which is exactly why the reason has to travel with the row.
        """
        source_id = gmail_source(db)
        enqueue_extract(
            db,
            source_id,
            "aaa",
            attempts=1,
            state="failed",
            last_error="PermanentFailure: source needs reconnecting: invalid_grant",
        )
        enqueue_extract(
            db,
            source_id,
            "bbb",
            attempts=5,
            state="failed",
            last_error="RuntimeError: gmail returned 503 for message bbb",
        )

        reasons = {status.title: status.last_error for status in repo.list_ingestion(db, USER)}
        assert "invalid_grant" in (reasons["Gmail message aaa"] or "")
        assert "503" in (reasons["Gmail message bbb"] or "")

    def test_a_message_still_being_retried_reports_the_attempt_and_the_next_one(
        self, db: psycopg.Connection[Any]
    ) -> None:
        """The same *working* / *stuck* distinction the integrate arm makes, one stage up."""
        source_id = gmail_source(db)
        enqueue_extract(
            db,
            source_id,
            "ccc",
            attempts=2,
            state="ready",
            last_error="timed out",
            due_in_seconds=30,
        )

        (status,) = repo.list_ingestion(db, USER)
        assert status.state is SourceItemState.PENDING
        assert status.attempts == 2
        assert status.next_attempt_at is not None

    def test_a_message_a_worker_holds_right_now_has_nothing_scheduled(
        self, db: psycopg.Connection[Any]
    ) -> None:
        source_id = gmail_source(db)
        enqueue_extract(db, source_id, "ddd", attempts=1, state="running")

        (status,) = repo.list_ingestion(db, USER)
        assert status.state is SourceItemState.PENDING
        assert status.next_attempt_at is None

    def test_a_message_that_made_it_is_reported_from_its_source_item_only(
        self, db: psycopg.Connection[Any]
    ) -> None:
        """One message is one line. The row is the better answer, so the job stands down."""
        source_id = gmail_source(db)
        enqueue_extract(db, source_id, "eee", attempts=1, state="done")
        item_id = polled_item(db, source_id, "eee")

        (status,) = repo.list_ingestion(db, USER)
        assert status.id == item_id
        assert status.title == "Acme raises $20M"
        assert status.source_kind == SourceKind.GMAIL.value

    def test_a_reclaimed_job_whose_row_already_landed_is_not_reported_twice(
        self, db: psycopg.Connection[Any]
    ) -> None:
        """The lease-reclaim case, which ``state <> 'done'`` alone does not cover.

        A worker that inserted the source item and died before ``jobs.complete`` leaves a
        ``running`` row that another worker will claim again. Both records then describe
        one message, and the ``NOT EXISTS`` on ``(source_id, external_id)`` — the key the
        unique index is built on — is what keeps the panel from showing it twice.
        """
        source_id = gmail_source(db)
        enqueue_extract(db, source_id, "fff", attempts=2, state="running")
        item_id = polled_item(db, source_id, "fff")

        statuses = repo.list_ingestion(db, USER)
        assert [status.id for status in statuses] == [item_id]

    def test_it_reports_only_this_user(self, db: psycopg.Connection[Any]) -> None:
        """Scoped through the source, because a job payload carries no user id."""
        # `users` is not truncated between tests — the seeded owner has to survive — so
        # this is idempotent rather than an insert.
        db.execute("INSERT INTO users (id, email) VALUES ('other', NULL) ON CONFLICT DO NOTHING")
        enqueue_extract(db, gmail_source(db, user_id="other"), "theirs")
        enqueue_extract(db, gmail_source(db), "mine")

        assert [status.title for status in repo.list_ingestion(db, USER)] == ["Gmail message mine"]

    def test_the_two_arms_are_ordered_and_bounded_together(
        self, db: psycopg.Connection[Any]
    ) -> None:
        """Newest first across both, and one bound over the union rather than one each.

        A per-arm limit that was also the answer's limit would let a full mailbox return
        twice what the route promises, on a list the SPA polls every few seconds.
        """
        source_id = gmail_source(db)
        for index in range(repo.INGESTION_MAX_ITEMS):
            paste(db, title=f"Paste {index}")
        # Aged, because everything a test writes shares one transaction timestamp and the
        # ordering under test is by time. In life the two arms are written minutes apart.
        db.execute("UPDATE source_items SET created_at = created_at - interval '1 hour'")
        for index in range(10):
            enqueue_extract(db, source_id, f"msg-{index}")

        statuses = repo.list_ingestion(db, USER)
        assert len(statuses) == repo.INGESTION_MAX_ITEMS
        # The extract jobs were written last, so newest-first puts them at the front.
        assert statuses[0].title.startswith("Gmail message")

    def test_a_message_the_extractor_deliberately_skipped_is_not_reported(
        self, db: psycopg.Connection[Any]
    ) -> None:
        """The line this arm draws, and the reason it is drawn at `done` rather than later.

        ``handle_extract`` catches ``ExtractionError`` — a receipt, a calendar invite, a
        message with no body — records it on the source, and returns, so the job completes
        with no source item behind it. A mailbox is mostly not newsletters, and reporting
        every one of them as content that failed to arrive would drown the one that did.
        """
        source_id = gmail_source(db)
        enqueue_extract(db, source_id, "a-receipt", attempts=1, state="done")

        assert repo.list_ingestion(db, USER) == []

    def test_two_open_jobs_for_one_message_report_it_once(
        self, db: psycopg.Connection[Any]
    ) -> None:
        """A message can genuinely be queued twice, and it is still one message.

        The provider's history window expires, ``handle_poll`` drops the cursor and
        re-lists — and a message whose earlier extraction *failed* has no source item, so
        the pre-check that makes a re-poll idempotent does not fire and a second job is
        queued. Two lines for one newsletter would be the accounting surface contradicting
        itself, which is motet#41's shape one stage up.
        """
        source_id = gmail_source(db)
        enqueue_extract(db, source_id, "dup", attempts=5, state="failed", last_error="boom")
        enqueue_extract(db, source_id, "dup", attempts=0, state="ready")

        (status,) = repo.list_ingestion(db, USER)
        # The newest, because it is the one something is still going to do.
        assert status.state is SourceItemState.PENDING
        assert status.attempts == 0

    def test_the_extract_arm_is_bounded_on_its_own(self, db: psycopg.Connection[Any]) -> None:
        """One Gmail poll queues a page of jobs; several polls queue several pages."""
        source_id = gmail_source(db)
        for index in range(repo.INGESTION_MAX_ITEMS + 10):
            enqueue_extract(db, source_id, f"msg-{index}")

        assert len(repo.list_ingestion(db, USER)) == repo.INGESTION_MAX_ITEMS

    def test_a_job_with_no_message_id_is_still_reportable(
        self, db: psycopg.Connection[Any]
    ) -> None:
        """A malformed payload fails permanently on its first attempt, and still shows.

        It is the one row whose reason nobody could guess from anywhere else, so falling
        over on the missing field — or reporting it with no title at all, when every
        caller renders one — would lose exactly the case worth keeping.
        """
        source_id = gmail_source(db)
        db.execute(
            """
            INSERT INTO jobs (queue, payload, attempts, state, last_error)
            VALUES ('extract', %s::jsonb, 1, 'failed', 'PermanentFailure: missing message_id')
            """,
            (json.dumps({"source_id": source_id}),),
        )

        (status,) = repo.list_ingestion(db, USER)
        assert status.title == "Gmail message (no id)"
        assert status.state is SourceItemState.FAILED

    def test_a_pasted_item_reports_the_kind_it_came_by(self, db: psycopg.Connection[Any]) -> None:
        """``source_kind`` is on both arms, because it is what says which repair applies."""
        item_id = paste(db)
        enqueue_integrate(db, item_id)

        (status,) = repo.list_ingestion(db, USER)
        assert status.source_kind == SourceKind.PASTE.value


def test_the_extract_index_covers_the_open_job_lookup(db: psycopg.Connection[Any]) -> None:
    """Migration 0008's index is the one the extract arm uses.

    Same reasoning as the integrate one above: nothing prunes `jobs`, so without a
    matching partial expression index this arm is a sequential scan of every job ever run,
    behind a route the SPA polls while anything is pending.

    ``EXPLAIN`` runs against ``repo.INGESTION_SQL`` itself rather than a transcription of
    the arm. A copy would keep matching the index while the statement being run drifted
    off it, which is exactly the failure ``motet_workers.jobs.CLAIM_SQL`` was hoisted to
    avoid after motet#49.
    """
    source_id = gmail_source(db)
    enqueue_extract(db, source_id, "ggg")
    db.execute("SET enable_seqscan = off")
    plan = db.execute(
        f"EXPLAIN {repo.INGESTION_SQL}",
        {
            "user_id": USER,
            "now": None,
            "grace": repo.INTEGRATED_GRACE.total_seconds(),
            "limit": repo.INGESTION_MAX_ITEMS,
        },
    ).fetchall()
    assert any("jobs_extract_open_idx" in str(row) for row in plan), plan


class TestWorkerHeartbeats:
    """Is anything draining the queue? — the fact motet#38 turned on.

    Nothing in `jobs` distinguishes an idle queue with a worker on it from an idle queue
    with none, which is why a stalled paste and a busy one looked identical.
    """

    def test_a_heartbeat_is_written_once_per_queue_and_then_moves(
        self, db: psycopg.Connection[Any]
    ) -> None:
        now, beats = repo.worker_heartbeats(db)
        assert beats == []
        # The clock comes back even with no rows: "no worker has ever run" is the case
        # that most needs a `now` to age the answer against.
        assert now is not None

        repo.record_worker_heartbeat(db, "integrate")
        _, (first,) = repo.worker_heartbeats(db)

        repo.record_worker_heartbeat(db, "integrate")
        server_now, (second,) = repo.worker_heartbeats(db)

        assert second.queue == "integrate"
        assert second.last_seen_at >= first.last_seen_at
        # The clock is the database's, and it is at or after the heartbeat it wrote.
        assert server_now >= second.last_seen_at

    def test_queues_come_back_most_recently_seen_first(self, db: psycopg.Connection[Any]) -> None:
        """So the newest is `[0]`, which is what "when did any worker last run" reads."""
        repo.record_worker_heartbeat(db, "tts")
        db.commit()
        repo.record_worker_heartbeat(db, "integrate")
        db.commit()

        assert [beat.queue for beat in repo.worker_heartbeats(db)[1]] == ["integrate", "tts"]
