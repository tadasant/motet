"""The pipeline, against a real Postgres and the deterministic fakes.

A real database rather than a mock, because everything interesting here *is* the database:
``SELECT ... FOR UPDATE SKIP LOCKED``, an advisory lock, a partial index, and a ``CHECK``
constraint that refuses a claim with no span. A fake database would verify none of it.

Skips without ``DATABASE_URL`` so a quick local run needs no Postgres; CI always has one.
"""

from __future__ import annotations

import json
import os
import re
import signal
import threading
import time
from collections.abc import Mapping, Sequence
from dataclasses import replace
from typing import Any

import psycopg
import pytest
from motet_db import EpisodeState, SourceItemState, phase2, repo
from motet_inference import (
    Audio,
    GroundingReport,
    GroundingValidator,
    NewsItem,
    Script,
    ScriptGenerator,
    SourceItem,
    Stages,
)
from motet_inference.llm import FakeLlmClient
from motet_inference.registry import fake_stages
from motet_storage import LocalObjectStore
from motet_workers import Queue, drain, enqueue_episode, enqueue_paste, jobs, loop, runner
from motet_workers.queues import PIPELINE

MORNING = (
    "Acme raises $20M Series A",
    "Acme raises $20M Series A. Acme announced the round on Tuesday, led by Northwind "
    "Ventures, bringing total funding to $31M.",
)
EVENING = (
    "Acme raises $20M Series A",
    "ACME SERIES A raises $20M. Acme's Series A closed this week with Northwind leading.",
)
INQUIRY = (
    "Regulator opens inquiry",
    "Regulator opens inquiry. The agency confirmed an inquiry into data retention.",
)

USER = repo.OWNER_USER_ID


def paste(conn: psycopg.Connection[Any], entry: tuple[str, str]) -> str:
    stored = enqueue_paste(conn, user_id=USER, title=entry[0], text=entry[1])
    conn.commit()
    return stored.id


def run_all(url: str) -> None:
    for queue in (Queue.INTEGRATE, Queue.ASSEMBLE, Queue.SCRIPT, Queue.TTS):
        drain(queue, url)


class TestQueue:
    def test_claiming_marks_running_and_counts_the_attempt(
        self, db: psycopg.Connection[Any]
    ) -> None:
        """Counted on claim, not on failure.

        A job that kills its worker outright never reaches the failure path, so counting
        there would let a poison job retry forever.
        """
        jobs.enqueue(db, Queue.INTEGRATE, {"source_item_id": "si_x"})
        db.commit()

        job = jobs.claim(db, Queue.INTEGRATE)
        assert job is not None
        assert job.attempts == 1
        assert job.payload == {"source_item_id": "si_x"}

    def test_a_second_claim_finds_nothing(self, db: psycopg.Connection[Any]) -> None:
        jobs.enqueue(db, Queue.INTEGRATE, {"source_item_id": "si_x"})
        db.commit()
        assert jobs.claim(db, Queue.INTEGRATE) is not None
        assert jobs.claim(db, Queue.INTEGRATE) is None

    def test_queues_do_not_steal_from_each_other(self, db: psycopg.Connection[Any]) -> None:
        """Separate queues on one table is the whole design: a Cartesia 429 must not
        stall dedup."""
        jobs.enqueue(db, Queue.TTS, {"episode_id": "ep_x"})
        db.commit()
        assert jobs.claim(db, Queue.INTEGRATE) is None
        assert jobs.claim(db, Queue.TTS) is not None

    def test_a_failure_reschedules_with_backoff_until_the_ceiling(
        self, db: psycopg.Connection[Any]
    ) -> None:
        jobs.enqueue(db, Queue.INTEGRATE, {"source_item_id": "si_x"})
        db.commit()

        for _ in range(jobs.DEFAULT_MAX_ATTEMPTS - 1):
            job = jobs.claim(db, Queue.INTEGRATE)
            assert job is not None
            assert jobs.fail(db, job, "boom") is True
            db.execute("UPDATE jobs SET run_at = now() WHERE id = %s", (job.id,))

        job = jobs.claim(db, Queue.INTEGRATE)
        assert job is not None
        assert jobs.fail(db, job, "boom") is False
        state = db.execute("SELECT state, last_error FROM jobs WHERE id = %s", (job.id,)).fetchone()
        assert state is not None
        assert state["state"] == "failed"
        assert state["last_error"] == "boom"

    def test_the_retry_ceiling_has_one_definition(self, db: psycopg.Connection[Any]) -> None:
        """`will_retry` is asked before `fail` applies it, so the two must not drift.

        The runner needs the answer *before* the call, because it records the domain object
        first — a lock order the work fence made load-bearing. Two copies of the ceiling is
        the obvious way that goes wrong quietly.
        """
        jobs.enqueue(db, Queue.INTEGRATE, {"source_item_id": "si_x"})
        db.commit()
        for _ in range(jobs.DEFAULT_MAX_ATTEMPTS):
            job = jobs.claim(db, Queue.INTEGRATE)
            assert job is not None
            predicted = jobs.will_retry(job)
            assert jobs.fail(db, job, "boom") is predicted
            db.execute("UPDATE jobs SET run_at = now() WHERE id = %s", (job.id,))

        assert self._job_row_by_queue(db, Queue.INTEGRATE)["state"] == "failed"

    def _job_row_by_queue(self, conn: psycopg.Connection[Any], queue: Queue) -> Mapping[str, Any]:
        row = conn.execute("SELECT state FROM jobs WHERE queue = %s", (queue.value,)).fetchone()
        assert row is not None
        return dict(row)

    def test_a_reclaimed_job_says_whether_its_work_already_landed(
        self, db: psycopg.Connection[Any]
    ) -> None:
        """The fence rides on the claim, so the runner never has to ask a second question.

        `attempts` rather than a flag: the claim that ran is not the claim that recovers
        the row, and a log line that cannot say which one applied the work is not worth
        reading.
        """
        jobs.enqueue(db, Queue.INTEGRATE, {"source_item_id": "si_x"})
        db.commit()
        first = jobs.claim(db, Queue.INTEGRATE)
        assert first is not None and first.work_committed_attempt is None

        jobs.mark_work_committed(db, first.id, attempts=first.attempts)
        db.execute(
            "UPDATE jobs SET locked_at = now() - make_interval(secs => %s) WHERE id = %s",
            (jobs.STALE_LEASE_SECONDS + 60, first.id),
        )

        second = jobs.claim(db, Queue.INTEGRATE)
        assert second is not None and second.id == first.id
        assert second.attempts == first.attempts + 1
        assert second.work_committed_attempt == first.attempts

    def test_deferring_does_not_burn_an_attempt(self, db: psycopg.Connection[Any]) -> None:
        """A busy serialization key is not a failure.

        Charging an attempt for it would let a busy user's ingestion exhaust its retries
        without anything having gone wrong.
        """
        jobs.enqueue(db, Queue.INTEGRATE, {"source_item_id": "si_x"}, serialize_key=USER)
        db.commit()
        job = jobs.claim(db, Queue.INTEGRATE)
        assert job is not None
        jobs.defer(db, job)

        row = db.execute("SELECT attempts, state FROM jobs WHERE id = %s", (job.id,)).fetchone()
        assert row is not None
        assert row["attempts"] == 0
        assert row["state"] == "ready"


class TestTheClaimQueryUsesItsIndexes:
    """motet#49: the claim query had an index for one of its two arms.

    `jobs_ready_idx` is partial on `state = 'ready'`, so it holds no `running` rows and
    cannot answer the lease-reclaim arm. A `BitmapOr` needs an index path for *every* arm,
    so with only one indexed the planner fell back to a sequential scan of every job ever
    run — on the hottest query in the system, over a table that at the time nothing pruned
    and that `jobs.prune` now bounds to a retention window of days rather than to the
    deployment's age.

    A comment on the index is what failed to catch that (it said "covers the claim query"
    for as long as the reclaim arm existed), so the plan is asserted rather than described.
    `EXPLAIN` on :data:`jobs.CLAIM_SQL` plans without executing, and it is the statement
    `claim` actually runs rather than a copy of it — a copy would keep its index while the
    query drifted off it, which is this bug one level down.

    The assertions are on the plan's `Index Cond` lines rather than on index names, for the
    same reason: a name is satisfied by an index that no longer answers the arm it is named
    for.
    """

    #: Enough `done` rows that a sequential scan is not simply the cheapest thing available:
    #: a plan taken over an empty table proves nothing. At this size Postgres 16 costs the
    #: seq scan at 87 against 22 for the `BitmapOr`, so the margin is not marginal.
    BACKLOG = 2000

    def _seed(self, conn: psycopg.Connection[Any]) -> None:
        """A queue that has been running a while: mostly `done`, a few ready, a few stale."""
        queues = [queue.value for queue in Queue]
        conn.execute(
            """
            INSERT INTO jobs (queue, payload, state, run_at, locked_at, attempts)
            SELECT (%s::text[])[1 + (i %% cardinality(%s::text[]))], '{}'::jsonb, 'done',
                   now() - make_interval(secs => i), now() - make_interval(secs => i), 1
            FROM generate_series(1, %s) AS i
            """,
            (queues, queues, self.BACKLOG),
        )
        conn.execute(
            """
            INSERT INTO jobs (queue, payload, state, run_at)
            SELECT q, '{}'::jsonb, 'ready', now() - make_interval(secs => i)
            FROM generate_series(1, 5) AS i, unnest(%s::text[]) AS q
            """,
            (queues,),
        )
        conn.execute(
            """
            INSERT INTO jobs (queue, payload, state, run_at, locked_at, attempts)
            SELECT q, '{}'::jsonb, 'running',
                   now() - make_interval(secs => %s), now() - make_interval(secs => %s), 1
            FROM generate_series(1, 2) AS i, unnest(%s::text[]) AS q
            """,
            (jobs.STALE_LEASE_SECONDS * 2, jobs.STALE_LEASE_SECONDS + 600, queues),
        )
        # Without fresh statistics the planner is costing a table it thinks is empty, and
        # the plan below would say nothing about the one production runs.
        conn.execute("ANALYZE jobs")
        conn.commit()

    def _plan(self, conn: psycopg.Connection[Any], queue: Queue) -> list[str]:
        return [
            row["QUERY PLAN"]
            for row in conn.execute(
                f"EXPLAIN {jobs.CLAIM_SQL}", (queue.value, jobs.STALE_LEASE_SECONDS)
            ).fetchall()
        ]

    def _index_cond(self, plan: list[str], index: str) -> str:
        """The `Index Cond` line belonging to `index`, which `EXPLAIN` puts directly under it.

        The *name* of an index says nothing about whether the planner could push the arm's
        predicate into it; the condition is where that shows up, and it is the difference
        between the index doing the work and the heap doing it.
        """
        for scan, cond in zip(plan, plan[1:], strict=False):
            if f"Bitmap Index Scan on {index}" in scan:
                assert "Index Cond" in cond, f"{index} has no condition:\n" + "\n".join(plan)
                return cond
        raise AssertionError(f"{index} is not in the plan:\n" + "\n".join(plan))

    def test_every_queue_claims_through_an_index_on_each_arm(
        self, db: psycopg.Connection[Any]
    ) -> None:
        """Asserted for every queue, and by condition rather than by index name.

        `integrate` is the queue that would hide the regression from the `Seq Scan`
        assertion alone: migration 0005's `jobs_source_item_idx` is partial on
        `queue = 'integrate'`, so the planner can walk *that* instead — no `Seq Scan` in the
        plan, and still every integrate job ever run read to find the handful that are
        claimable.

        And a name would hide the *shape*. `jobs_stale_idx` on `(queue, run_at, id)` — the
        shape migration 0007 rejects, which reads every `running` row on the queue and
        filters the lease in the heap — appears in the plan under exactly the same name.
        The `Index Cond` is the only place the difference surfaces.
        """
        self._seed(db)

        for queue in Queue:
            plan = self._plan(db, queue)
            printed = "\n".join(plan)
            assert not any("Seq Scan on jobs" in line for line in plan), (
                f"{queue.value}:\n{printed}"
            )

            # One index per arm, each carrying its own arm's predicate: `jobs_ready_idx`
            # (0001) answers "due", `jobs_stale_idx` (0007) answers "lease expired". An
            # index that stops matching its arm still appears by name; it stops appearing
            # here.
            ready = self._index_cond(plan, "jobs_ready_idx")
            assert "run_at <= now()" in ready, f"{queue.value}:\n{printed}"

            stale = self._index_cond(plan, "jobs_stale_idx")
            assert "locked_at <" in stale, f"{queue.value}:\n{printed}"

    def test_the_busy_key_filter_reads_pg_locks_once_and_only_when_it_can_matter(
        self, db: psycopg.Connection[Any]
    ) -> None:
        """motet#78's pre-filter, costed rather than described.

        Two properties, and each has a plan shape that would quietly lose it. A *hashed*
        subplan reads `pg_lock_status()` once per execution and probes a hash per candidate
        row; an unhashed correlated one would call it per row, and `pg_lock_status()` takes
        every lock-manager partition lock to answer. And on a queue whose rows carry no
        serialization key the `lock_key IS NULL` disjunct short-circuits, so the subplan is
        never evaluated at all — four of the six queues, and `EXPLAIN (ANALYZE)` says so in
        as many words.

        `EXPLAIN (ANALYZE)` really claims a job, so this runs inside a transaction it rolls
        back — unlike the plan-only assertions above, which is why it is a test of its own.
        """
        self._seed(db)
        # `_seed` writes no `serialize_key`, so give `integrate` the shape it has in
        # production — a key on every row — and leave `script` as the keyless queue it is.
        db.execute(
            "UPDATE jobs SET serialize_key = 'user-a', lock_key = %s WHERE queue = 'integrate'",
            (jobs.lock_key("user-a"),),
        )
        db.execute("ANALYZE jobs")
        db.commit()

        def plan(queue: Queue) -> list[str]:
            db.execute("BEGIN")
            try:
                return [
                    row["QUERY PLAN"]
                    for row in db.execute(
                        f"EXPLAIN (ANALYZE) {jobs.CLAIM_SQL}",
                        (queue.value, jobs.STALE_LEASE_SECONDS),
                    ).fetchall()
                ]
            finally:
                db.execute("ROLLBACK")

        keyed, keyless = plan(Queue.INTEGRATE), plan(Queue.SCRIPT)

        for printed in (keyed, keyless):
            assert any("hashed SubPlan" in line for line in printed), "\n".join(printed)

        # Keyed: read once for the whole claim, not once per candidate row.
        scanned = [line for line in keyed if "Function Scan on pg_lock_status" in line]
        assert scanned and all("loops=1" in line for line in scanned), "\n".join(keyed)

        # Keyless: not read at all.
        skipped = [line for line in keyless if "Function Scan on pg_lock_status" in line]
        assert skipped and all("never executed" in line for line in skipped), "\n".join(keyless)

    def test_explaining_the_claim_does_not_claim(self, db: psycopg.Connection[Any]) -> None:
        """`EXPLAIN` plans an `UPDATE ... RETURNING` without running it.

        The test above would otherwise be claiming jobs as a side effect of measuring how
        it would claim them, and a later assertion about `attempts` would be about this.
        """
        jobs.enqueue(db, Queue.INTEGRATE, {"source_item_id": "si_x"})
        db.commit()

        db.execute(f"EXPLAIN {jobs.CLAIM_SQL}", (Queue.INTEGRATE.value, jobs.STALE_LEASE_SECONDS))

        row = db.execute("SELECT state, attempts FROM jobs").fetchone()
        assert row is not None
        assert (row["state"], row["attempts"]) == ("ready", 0)


class TestSerialization:
    def test_a_second_worker_cannot_hold_the_same_user_s_key(self, _migrated: str) -> None:
        """Invariant 6, at the mechanism level.

        Two ingestion runs for one user must never overlap — dedup compares against the
        current window, so concurrent runs race into duplicate news items. Two *separate
        connections*, because an advisory lock is per session and a single connection would
        happily re-acquire its own.
        """
        with repo.connect(_migrated) as first, repo.connect(_migrated) as second:
            assert jobs.try_lock(first, USER) is True
            assert jobs.try_lock(second, USER) is False
            # A different user is unaffected: serialization is per user, not global.
            assert jobs.try_lock(second, "someone-else") is True

            jobs.unlock(first, USER)
            assert jobs.try_lock(second, USER) is True
            jobs.unlock(second, USER)
            jobs.unlock(second, "someone-else")

    def test_a_deferred_job_is_picked_up_once_the_key_frees(
        self, db: psycopg.Connection[Any], _migrated: str, object_store: LocalObjectStore
    ) -> None:
        source_id = paste(db, MORNING)

        with repo.connect(_migrated) as holder:
            assert jobs.try_lock(holder, USER) is True
            # The drain does not run it, and since motet#78 it does not claim it either:
            # the busy-key filter steps over the row, so it is left `ready` and untouched
            # rather than claimed and deferred. `TestTheClaimSkipsBusyKeys` is where that
            # distinction is the assertion; here it is still "nothing ran for this user".
            assert drain(Queue.INTEGRATE, _migrated) == 0
            row = db.execute(
                "SELECT state, attempts FROM jobs WHERE payload->>'source_item_id' = %s",
                (source_id,),
            ).fetchone()
            assert row is not None
            assert (row["state"], row["attempts"]) == ("ready", 0)
            jobs.unlock(holder, USER)

        db.execute("UPDATE jobs SET run_at = now()")
        db.commit()
        assert drain(Queue.INTEGRATE, _migrated) == 1


class TestIntegrate:
    def test_two_newsletters_about_one_story_become_one_news_item(
        self, db: psycopg.Connection[Any], _migrated: str
    ) -> None:
        paste(db, MORNING)
        paste(db, EVENING)
        paste(db, INQUIRY)

        assert drain(Queue.INTEGRATE, _migrated) == 3

        items = repo.list_news_items(db, USER)
        assert len(items) == 2
        acme = next(item for item in items if "Acme" in item.title)
        assert len(acme.source_item_ids) == 2
        assert all(item.read is False for item in items)

    def test_an_integrated_source_item_is_not_reprocessed(
        self, db: psycopg.Connection[Any], _migrated: str
    ) -> None:
        """Idempotence, which is the normal case rather than the exception.

        A retry after the work committed but the job update did not must not file the same
        source item against a second news item.
        """
        source_id = paste(db, MORNING)
        drain(Queue.INTEGRATE, _migrated)

        jobs.enqueue(db, Queue.INTEGRATE, {"source_item_id": source_id}, serialize_key=USER)
        db.commit()
        drain(Queue.INTEGRATE, _migrated)

        assert len(repo.list_news_items(db, USER)) == 1

    def test_a_vanished_source_item_fails_permanently_rather_than_retrying(
        self, db: psycopg.Connection[Any], _migrated: str
    ) -> None:
        jobs.enqueue(db, Queue.INTEGRATE, {"source_item_id": "si_ghost"}, serialize_key=USER)
        db.commit()
        drain(Queue.INTEGRATE, _migrated)

        row = db.execute("SELECT state, attempts FROM jobs").fetchone()
        assert row is not None
        # One attempt, not five: retrying cannot conjure a deleted row.
        assert (row["state"], row["attempts"]) == ("failed", 1)


class TestFullPipeline:
    def test_paste_to_published_audio(
        self, db: psycopg.Connection[Any], _migrated: str, object_store: LocalObjectStore
    ) -> None:
        """The whole Phase 1 path, end to end, on fakes."""
        for entry in (MORNING, EVENING, INQUIRY):
            paste(db, entry)
        drain(Queue.INTEGRATE, _migrated)

        episode_id = enqueue_episode(
            db, user_id=USER, title="Morning briefing", max_duration_ms=20 * 60_000
        )
        db.commit()

        assert drain(Queue.ASSEMBLE, _migrated) == 1
        assert repo.get_episode(db, episode_id).state is EpisodeState.SCRIPTING
        assert drain(Queue.SCRIPT, _migrated) == 1
        assert repo.get_episode(db, episode_id).state is EpisodeState.RENDERING
        assert drain(Queue.TTS, _migrated) == 1

        episode = repo.get_episode(db, episode_id)
        assert episode is not None
        assert episode.state is EpisodeState.READY
        assert episode.duration_ms > 0
        assert episode.audio_bytes and episode.audio_bytes > 0
        assert episode.audio_key is not None
        assert object_store.exists(episode.audio_key)
        assert len(object_store.get(episode.audio_key)) == episode.audio_bytes

        # Every claim carries a span into a real source item (invariant 3), and offsets
        # accumulate across segments (invariant 4 — playback position is ours).
        sources = repo.load_source_items(
            db, [claim.source_item_id for seg in episode.segments for claim in seg.claims]
        )
        assert episode.segments
        offset = 0
        for segment in episode.segments:
            assert segment.start_ms == offset
            offset += segment.duration_ms
            assert segment.claims
            for claim in segment.claims:
                source = sources[claim.source_item_id]
                assert source.text[claim.span_start : claim.span_end]

        # And the source items are marked done rather than left pending forever.
        for item in repo.load_source_items(db, [s for s in sources]).values():
            assert item.state is SourceItemState.INTEGRATED

    def test_the_duration_cap_limits_how_many_stories_get_in(
        self, db: psycopg.Connection[Any], _migrated: str
    ) -> None:
        """ "All unread" is capped, and the cap is applied before anything is synthesized."""
        for index in range(6):
            enqueue_paste(
                db,
                user_id=USER,
                title=f"Story number {index}",
                text=f"Story number {index}. " + " ".join(["word"] * 200),
            )
        db.commit()
        drain(Queue.INTEGRATE, _migrated)
        assert len(repo.unread_news_items(db, USER)) == 6

        # Roughly one story's worth of speech, so the cap has to bite.
        episode_id = enqueue_episode(db, user_id=USER, title="Short", max_duration_ms=30_000)
        db.commit()
        drain(Queue.ASSEMBLE, _migrated)

        episode = repo.get_episode(db, episode_id)
        assert episode is not None
        assert 0 < len(episode.segments) < 6

    def test_an_episode_with_nothing_unread_fails_visibly_and_immediately(
        self, db: psycopg.Connection[Any], _migrated: str
    ) -> None:
        episode_id = enqueue_episode(db, user_id=USER, title="Empty", max_duration_ms=60_000)
        db.commit()
        drain(Queue.ASSEMBLE, _migrated)

        episode = repo.get_episode(db, episode_id)
        assert episode is not None
        assert episode.state is EpisodeState.FAILED
        assert episode.last_error is not None
        # Phase 2 assembles both episode kinds through one rule-driven selector, so the
        # message names the rule that selected nothing rather than saying "unread".
        assert "no news items match this episode's rule" in episode.last_error
        # And it did not burn five attempts discovering that.
        row = db.execute("SELECT attempts FROM jobs WHERE queue = 'assemble'").fetchone()
        assert row is not None
        assert row["attempts"] == 1

    def test_rerunning_the_script_stage_replaces_segments_rather_than_appending(
        self, db: psycopg.Connection[Any], _migrated: str
    ) -> None:
        """A retry from `scripting` writes one set of segments, not two.

        The episode is put back into `scripting` before the second run because that is the
        state a *genuine* retry happens from — a handler that raised, or a worker that
        died, before `handle_script` committed anything. Left in `rendering` the second run
        would short-circuit (see `TestAFinishedScriptStageIsNotRerun`) and this would pass
        without `replace_segments` ever being called.
        """
        paste(db, MORNING)
        drain(Queue.INTEGRATE, _migrated)
        episode_id = enqueue_episode(db, user_id=USER, title="E", max_duration_ms=600_000)
        db.commit()
        drain(Queue.ASSEMBLE, _migrated)
        drain(Queue.SCRIPT, _migrated)
        before = len(repo.get_episode(db, episode_id).segments)

        repo.set_episode_state(db, episode_id, EpisodeState.SCRIPTING)
        jobs.enqueue(db, Queue.SCRIPT, {"episode_id": episode_id})
        db.commit()
        drain(Queue.SCRIPT, _migrated)

        assert len(repo.get_episode(db, episode_id).segments) == before


class CountingScriptGenerator:
    """A script generator that records being asked, and otherwise is the fake."""

    def __init__(self, inner: ScriptGenerator) -> None:
        self._inner = inner
        self.calls = 0

    def generate(self, news_items: Sequence[NewsItem], sources: Mapping[str, SourceItem]) -> Script:
        self.calls += 1
        return self._inner.generate(news_items, sources)


class CountingGroundingValidator:
    """The same, for the gate — the most expensive call in the pipeline."""

    def __init__(self, inner: GroundingValidator) -> None:
        self._inner = inner
        self.calls = 0

    def validate(self, script: Script, sources: Mapping[str, SourceItem]) -> GroundingReport:
        self.calls += 1
        return self._inner.validate(script, sources)


class TestASlowJobKeepsItsLease:
    """motet#53: a job slower than the lease was reclaimed while its worker was alive.

    A script job ran 2580 seconds against a full backlog — longer than
    `STALE_LEASE_SECONDS`, which was set to be "longer than the slowest stage can
    legitimately take" against a stage whose size is the user's backlog. A second worker
    took the row and redid the whole thing: a 22k-token script completion, the entire
    grounding cascade, and a complete Cartesia synthesis, all billed twice for one episode.

    `TestAFinishedScriptStageIsNotRerun` is the neighbouring guard and does not cover this:
    there the first run had *finished*, so the episode's state could say so. Here the first
    worker has committed nothing and the episode is exactly where it should be, so no entry
    guard can tell the two workers apart. The lease is the only thing that can.

    These drive a handler that is slower than its own lease rather than sleeping for half an
    hour: the handler backdates its `locked_at` to what forty-odd minutes of work would have
    left, and then asks — from a second connection, as a second worker would — whether the
    row can be claimed. An assertion inside a handler would be swallowed by `_execute` and
    turn into a retry, so what each handler does is *record*, and the test asserts after.
    """

    def _backdate(self, url: str, seconds: int) -> None:
        """Age every running job's lease, as a handler slower than the lease would."""
        with repo.connect(url) as conn:
            conn.execute(
                "UPDATE jobs SET locked_at = now() - make_interval(secs => %s) "
                "WHERE state = 'running'",
                (seconds,),
            )
            conn.commit()

    def _lease_age_seconds(self, conn: psycopg.Connection[Any], job_id: int) -> float:
        row = conn.execute(
            "SELECT extract(epoch FROM now() - locked_at) AS age FROM jobs WHERE id = %s",
            (job_id,),
        ).fetchone()
        assert row is not None
        return float(row["age"])

    def _slow_job(
        self, db: psycopg.Connection[Any], url: str, body: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Enqueue one job on `integrate` whose handler is `body`, and drain it."""
        jobs.enqueue(db, Queue.INTEGRATE, {"source_item_id": "si_slow"})
        db.commit()

        def handler(_context: Any, _payload: Mapping[str, Any]) -> None:
            body()

        monkeypatch.setitem(loop.HANDLERS, Queue.INTEGRATE, handler)
        assert drain(Queue.INTEGRATE, url) == 1

    def test_a_handler_slower_than_the_lease_is_not_reclaimed(
        self, db: psycopg.Connection[Any], _migrated: str, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The fix. The keeper is touching, so a second worker finds nothing."""
        monkeypatch.setattr(jobs, "LEASE_TOUCH_SECONDS", 0.05)
        seen: dict[str, Any] = {}

        def body() -> None:
            self._backdate(_migrated, jobs.STALE_LEASE_SECONDS + 60)
            with repo.connect(_migrated) as other:
                other.autocommit = True
                job_id = other.execute("SELECT id FROM jobs").fetchone()["id"]  # type: ignore[index]
                # Wait for one touch to land, rather than for a wall-clock guess.
                deadline = time.monotonic() + 10
                while self._lease_age_seconds(other, job_id) > 60:
                    assert time.monotonic() < deadline, "the lease was never extended"
                    time.sleep(0.02)
                seen["age_after_touch"] = self._lease_age_seconds(other, job_id)
                seen["reclaimed"] = jobs.claim(other, Queue.INTEGRATE)

        self._slow_job(db, _migrated, body, monkeypatch)

        # Not "claim happened to find the row locked": the lease is demonstrably fresh.
        assert seen["age_after_touch"] < 60
        assert seen["reclaimed"] is None

    def test_without_the_lease_touch_a_second_worker_claims_it(
        self, db: psycopg.Connection[Any], _migrated: str, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The defect, reproduced: with nothing touching, the same scenario double-claims.

        The touch interval is pushed past the length of the test rather than the code
        being reverted, so what runs is the pre-fix behaviour — a `running` row that
        nobody refreshes — through the post-fix code path.
        """
        monkeypatch.setattr(jobs, "LEASE_TOUCH_SECONDS", 3600)
        seen: dict[str, Any] = {}

        def body() -> None:
            self._backdate(_migrated, jobs.STALE_LEASE_SECONDS + 60)
            with repo.connect(_migrated) as other:
                other.autocommit = True
                seen["reclaimed"] = jobs.claim(other, Queue.INTEGRATE)

        self._slow_job(db, _migrated, body, monkeypatch)

        # Two workers, one job, and the second one is about to run the whole stage again.
        assert seen["reclaimed"] is not None

    def test_a_wedged_worker_stops_extending_and_its_row_comes_back(
        self, db: psycopg.Connection[Any], _migrated: str, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The other failure direction, bounded on purpose.

        A heartbeat driven from inside the worker stops when the process does, so a crash
        is already covered. What is not is a process that is alive and wedged — and a
        keeper that touched forever would strand its row in `running` with hand-written
        SQL against production as the only recovery, which invariant 10 forbids. Past
        `MAX_LEASE_EXTENSION_SECONDS` the keeper gives up and the ordinary stale window
        takes over.
        """
        monkeypatch.setattr(jobs, "LEASE_TOUCH_SECONDS", 0.05)
        monkeypatch.setattr(jobs, "MAX_LEASE_EXTENSION_SECONDS", 0.1)
        seen: dict[str, Any] = {}

        def body() -> None:
            # Past the cap, after which the deadline is checked *before* every touch — so
            # no touch can land after the backdate however slow the machine is.
            time.sleep(0.5)
            self._backdate(_migrated, jobs.STALE_LEASE_SECONDS + 60)
            with repo.connect(_migrated) as other:
                other.autocommit = True
                job_id = other.execute("SELECT id FROM jobs").fetchone()["id"]  # type: ignore[index]
                seen["age"] = self._lease_age_seconds(other, job_id)
                seen["reclaimed"] = jobs.claim(other, Queue.INTEGRATE)

        self._slow_job(db, _migrated, body, monkeypatch)

        # Nothing put the lease back, so the row is claimable again.
        assert seen["age"] > jobs.STALE_LEASE_SECONDS
        assert seen["reclaimed"] is not None

    def test_a_worker_that_lost_its_lease_finds_out(self, db: psycopg.Connection[Any]) -> None:
        """`attempts` is the fence, and it costs no column.

        A worker whose lease did lapse must not stamp `locked_at` onto a row another worker
        is now running: that would extend the duplicate rather than prevent it, silently.
        """
        jobs.enqueue(db, Queue.INTEGRATE, {"source_item_id": "si_x"})
        db.commit()
        first = jobs.claim(db, Queue.INTEGRATE)
        assert first is not None

        assert jobs.touch(db, first.id, attempts=first.attempts) is jobs.LeaseTouch.HELD

        db.execute(
            "UPDATE jobs SET locked_at = now() - make_interval(secs => %s) WHERE id = %s",
            (jobs.STALE_LEASE_SECONDS + 60, first.id),
        )
        second = jobs.claim(db, Queue.INTEGRATE)
        assert second is not None and second.id == first.id

        # `LOST`, not `SETTLED`: the row is still running, under somebody else's claim.
        assert jobs.touch(db, first.id, attempts=first.attempts) is jobs.LeaseTouch.LOST
        assert jobs.touch(db, second.id, attempts=second.attempts) is jobs.LeaseTouch.HELD

    def test_a_finished_job_reports_settled_rather_than_lost(
        self, db: psycopg.Connection[Any]
    ) -> None:
        """A touch racing its own job's `complete` is not a duplicate run.

        The window is one connect wide and a fleet will meet it. Calling it `LOST` would
        put "the stage is running twice" into GlitchTip at ERROR about a job that ran once.
        """
        jobs.enqueue(db, Queue.INTEGRATE, {"source_item_id": "si_x"})
        db.commit()
        job = jobs.claim(db, Queue.INTEGRATE)
        assert job is not None
        jobs.complete(db, job.id)

        assert jobs.touch(db, job.id, attempts=job.attempts) is jobs.LeaseTouch.SETTLED
        row = db.execute("SELECT state FROM jobs WHERE id = %s", (job.id,)).fetchone()
        assert row is not None and row["state"] == "done"

        # A row that is gone entirely is settled too, not a lost lease.
        db.execute("DELETE FROM jobs WHERE id = %s", (job.id,))
        assert jobs.touch(db, job.id, attempts=job.attempts) is jobs.LeaseTouch.SETTLED

    def test_the_keeper_stops_when_the_job_does(
        self, db: psycopg.Connection[Any], _migrated: str, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """`stop.set()` and the join are what keep a keeper from outliving its job.

        Asserted on the thread rather than on the row, because the row cannot show it: a
        keeper that ignored the Event would find the job `done`, get `SETTLED`, and change
        nothing — while still holding a thread and reconnecting every interval, one per job
        for the life of an always-on worker. What is wrong there is the thread, so that is
        what this looks at.
        """
        monkeypatch.setattr(jobs, "LEASE_TOUCH_SECONDS", 0.02)
        live: list[list[str]] = []

        def body() -> None:
            live.append([t.name for t in threading.enumerate() if t.name.startswith("lease-")])
            time.sleep(0.2)

        self._slow_job(db, _migrated, body, monkeypatch)

        # One while the handler ran, and none once `drain` returned.
        assert len(live[0]) == 1
        assert [t.name for t in threading.enumerate() if t.name.startswith("lease-")] == []

    def test_a_touch_that_cannot_reach_postgres_keeps_trying(
        self, db: psycopg.Connection[Any], _migrated: str, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The `continue` in the keeper's exception arm, which is a decision.

        `STALE_LEASE_SECONDS` is thirty touch intervals wide precisely so one unreachable
        minute does not hand a healthy job to another worker. Returning there would.
        """
        monkeypatch.setattr(jobs, "LEASE_TOUCH_SECONDS", 0.05)
        connect = repo.connect
        attempts: list[int] = []

        def flaky(*args: Any, **kwargs: Any) -> Any:
            # Only the keeper's own connects fail: the test's helpers share this module
            # attribute, and breaking those would fail the handler instead of the touch.
            if not threading.current_thread().name.startswith("lease-"):
                return connect(*args, **kwargs)
            attempts.append(1)
            if len(attempts) == 1:
                raise psycopg.OperationalError("connection refused")
            return connect(*args, **kwargs)

        seen: dict[str, Any] = {}

        def body() -> None:
            monkeypatch.setattr(loop.repo, "connect", flaky)
            self._backdate(_migrated, jobs.STALE_LEASE_SECONDS + 60)
            with connect(_migrated) as other:
                other.autocommit = True
                job_id = other.execute("SELECT id FROM jobs").fetchone()["id"]  # type: ignore[index]
                deadline = time.monotonic() + 10
                while self._lease_age_seconds(other, job_id) > 60:
                    assert time.monotonic() < deadline, "the keeper gave up after one failure"
                    time.sleep(0.02)
                seen["recovered"] = True
            monkeypatch.setattr(loop.repo, "connect", connect)

        self._slow_job(db, _migrated, body, monkeypatch)

        assert seen["recovered"] is True
        assert len(attempts) >= 2


class TestAFinishedScriptStageIsNotRerun:
    """motet#50: a `script` job reclaimed after its stage finished must do nothing.

    `_execute` commits the handler's work and `jobs.complete` in two transactions, which
    is deliberate — squashing them would roll back the attempt counter with the work and a
    poison job would retry forever. The cost is a window: a worker that dies between them
    leaves the row `running` with the work durably applied, and `STALE_LEASE_SECONDS`
    makes it claimable again. That reclaim is the intended recovery for every other stage;
    for `script` it used to re-execute a stage that had already completed.

    None of that shows up in the database — the stage converges on the same segments — so
    what this watches is the seams a re-run would have crossed: the two model calls, the
    segment rewrite, and the TTS job.
    """

    def _reclaimable(self, conn: psycopg.Connection[Any], queue: Queue) -> None:
        """Put `queue`'s job back exactly as a killed worker would have left it."""
        conn.execute(
            """
            UPDATE jobs
            SET state = 'running', locked_at = now() - make_interval(secs => %s)
            WHERE queue = %s
            """,
            (jobs.STALE_LEASE_SECONDS + 60, queue.value),
        )
        conn.commit()

    def _tts_jobs(self, conn: psycopg.Connection[Any]) -> list[int]:
        return [
            row["id"]
            for row in conn.execute(
                "SELECT id FROM jobs WHERE queue = %s ORDER BY id", (Queue.TTS.value,)
            ).fetchall()
        ]

    def test_a_reclaimed_script_job_rescripts_nothing_and_queues_nothing(
        self,
        db: psycopg.Connection[Any],
        _migrated: str,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        base = fake_stages()
        script_generator = CountingScriptGenerator(base.script_generator)
        grounding_validator = CountingGroundingValidator(base.grounding_validator)
        stages = Stages(
            integrator=base.integrator,
            script_generator=script_generator,
            grounding_validator=grounding_validator,
            speech_synthesizer=base.speech_synthesizer,
        )

        paste(db, MORNING)
        drain(Queue.INTEGRATE, _migrated, stages=stages)
        episode_id = enqueue_episode(db, user_id=USER, title="E", max_duration_ms=600_000)
        db.commit()
        drain(Queue.ASSEMBLE, _migrated, stages=stages)
        assert drain(Queue.SCRIPT, _migrated, stages=stages) == 1

        # The state the stage itself wrote, and the work it handed on.
        assert repo.get_episode(db, episode_id).state is EpisodeState.RENDERING
        assert (script_generator.calls, grounding_validator.calls) == (1, 1)
        queued = self._tts_jobs(db)
        assert len(queued) == 1
        segments = [(s.id, s.text) for s in repo.get_episode(db, episode_id).segments]

        rewrites: list[str] = []
        replace_segments = repo.replace_segments

        def spy(
            conn: psycopg.Connection[Any],
            episode_id_: str,
            specs: Sequence[repo.SegmentSpec],
        ) -> None:
            rewrites.append(episode_id_)
            replace_segments(conn, episode_id_, specs)

        monkeypatch.setattr(repo, "replace_segments", spy)

        self._reclaimable(db, Queue.SCRIPT)
        # Reclaimed — the point is that it ran and found nothing to do, not that the
        # lease held it back.
        assert drain(Queue.SCRIPT, _migrated, stages=stages) == 1

        # Not a second billed script completion, and not a second grounding pass at
        # `effort='max'`.
        assert (script_generator.calls, grounding_validator.calls) == (1, 1)
        # Not a rewrite of segments a concurrent TTS job may be reading.
        assert rewrites == []
        # And not a second TTS job for an episode that already has one.
        assert self._tts_jobs(db) == queued

        episode = repo.get_episode(db, episode_id)
        assert episode.state is EpisodeState.RENDERING
        assert [(s.id, s.text) for s in episode.segments] == segments
        # The reclaimed row is settled rather than left running for the next sweep.
        row = db.execute(
            "SELECT state FROM jobs WHERE queue = %s", (Queue.SCRIPT.value,)
        ).fetchone()
        assert row is not None and row["state"] == "done"

    def test_a_published_episode_is_still_left_alone(
        self, db: psycopg.Connection[Any], _migrated: str, object_store: LocalObjectStore
    ) -> None:
        """The `ready` half of the guard, which is the case it always covered."""
        paste(db, MORNING)
        drain(Queue.INTEGRATE, _migrated)
        episode_id = enqueue_episode(db, user_id=USER, title="E", max_duration_ms=600_000)
        db.commit()
        run_all(_migrated)
        assert repo.get_episode(db, episode_id).state is EpisodeState.READY

        jobs.enqueue(db, Queue.SCRIPT, {"episode_id": episode_id})
        db.commit()
        drain(Queue.SCRIPT, _migrated)

        assert repo.get_episode(db, episode_id).state is EpisodeState.READY
        assert not [
            row
            for row in db.execute(
                "SELECT id FROM jobs WHERE queue = %s AND state = 'ready'", (Queue.TTS.value,)
            ).fetchall()
        ]


class BrokenSynthesizer:
    """A synthesizer that is always down — a Cartesia outage, from the queue's side."""

    def synthesize(self, text: str) -> Audio:
        raise RuntimeError("Cartesia is unreachable")


class TestAStaleJobDoesNotReplayWorkThatAlreadyLanded:
    """motet#55: a stale `script` row must not put a *failed* episode back through the stage.

    The sequence is entirely inside existing behaviour. `_execute` commits the handler's
    work and the job's outcome in two transactions — the failure arm has no choice, since
    `jobs.fail` is written on a connection whose work transaction has just aborted — so a
    worker that dies between them leaves the row `running` with the work durably applied.
    Meanwhile the TTS job that work enqueued exhausts its retries and `_record_failure`
    marks the episode `failed`. Half an hour later the lease expires, the `script` row is
    claimable, and `failed` is a state `handle_script` is *deliberately* allowed to run
    from — so the whole stage ran again: another billed script completion, another grounding
    pass at `effort='max'`, a second TTS job, and `last_error` overwritten with NULL, which
    is the answer to "why did this episode fail" gone.

    `TestAFinishedScriptStageIsNotRerun` (motet#50) is the neighbouring guard and cannot
    reach this: it reads the *episode*, and a replay and a re-script somebody asked for
    arrive identically there. `TestASlowJobKeepsItsLease` (motet#53) cannot either — its
    heartbeat is a thread inside the worker, so it dies with the worker and a genuinely
    dead one still goes stale on schedule, which is exactly the window here.

    So the fence is on the job row: the handler's own transaction writes
    `work_committed_attempt`, and a claim that finds it set completes the job without
    calling a handler. A deliberate re-script is a different row, with the column NULL, and
    still runs — the second test is that half, because the quiet failure direction (an
    episode stranded in `failed` with no TTS job and nothing alerting on it) is invisible
    in both directions and is the reason this was not fixed with a wider state check.
    """

    def _expire_lease(self, conn: psycopg.Connection[Any], queue: Queue) -> None:
        """Age the running job's lease, as thirty minutes of a dead worker would."""
        conn.execute(
            "UPDATE jobs SET locked_at = now() - make_interval(secs => %s) "
            "WHERE queue = %s AND state = 'running'",
            (jobs.STALE_LEASE_SECONDS + 60, queue.value),
        )
        conn.commit()

    def _job_row(self, conn: psycopg.Connection[Any], queue: Queue) -> Mapping[str, Any]:
        row = conn.execute(
            "SELECT id, state, attempts, work_committed_attempt FROM jobs WHERE queue = %s",
            (queue.value,),
        ).fetchone()
        assert row is not None
        return dict(row)

    def _tts_jobs(self, conn: psycopg.Connection[Any]) -> list[int]:
        return [
            row["id"]
            for row in conn.execute(
                "SELECT id FROM jobs WHERE queue = %s ORDER BY id", (Queue.TTS.value,)
            ).fetchall()
        ]

    def _counting_stages(
        self,
    ) -> tuple[Stages, CountingScriptGenerator, CountingGroundingValidator]:
        base = fake_stages()
        script_generator = CountingScriptGenerator(base.script_generator)
        grounding_validator = CountingGroundingValidator(base.grounding_validator)
        return (
            Stages(
                integrator=base.integrator,
                script_generator=script_generator,
                grounding_validator=grounding_validator,
                speech_synthesizer=base.speech_synthesizer,
            ),
            script_generator,
            grounding_validator,
        )

    def _scripted_episode(self, db: psycopg.Connection[Any], url: str, stages: Stages) -> str:
        paste(db, MORNING)
        drain(Queue.INTEGRATE, url, stages=stages)
        episode_id = enqueue_episode(db, user_id=USER, title="E", max_duration_ms=600_000)
        db.commit()
        drain(Queue.ASSEMBLE, url, stages=stages)
        return episode_id

    def _fail_the_tts_job(self, db: psycopg.Connection[Any], url: str, stages: Stages) -> None:
        """Let the TTS job exhaust its ladder, which is what marks the episode failed."""
        broken = Stages(
            integrator=stages.integrator,
            script_generator=stages.script_generator,
            grounding_validator=stages.grounding_validator,
            speech_synthesizer=BrokenSynthesizer(),
        )
        for _ in range(jobs.DEFAULT_MAX_ATTEMPTS):
            # The backoff is real and the point here is the ceiling, not the wait.
            db.execute(
                "UPDATE jobs SET run_at = now() WHERE queue = %s AND state = 'ready'",
                (Queue.TTS.value,),
            )
            db.commit()
            drain(Queue.TTS, url, stages=broken)

    def test_a_stale_script_job_does_not_resurrect_a_failed_episode(
        self,
        db: psycopg.Connection[Any],
        _migrated: str,
        object_store: LocalObjectStore,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """The whole sequence, played out: die, fail, expire, reclaim."""
        stages, script_generator, grounding_validator = self._counting_stages()
        episode_id = self._scripted_episode(db, _migrated, stages)

        # 1. The stage runs, and the worker dies between the two commits. `SystemExit`
        #    rather than an exception, because `_execute` catches `Exception` and would
        #    turn one into a retry — what is being reproduced is a process that stopped,
        #    with its work already committed and nothing left to record the outcome.
        def die(*_args: Any, **_kwargs: Any) -> None:
            raise SystemExit("the worker died before it could mark the job done")

        with pytest.MonkeyPatch.context() as killing:
            killing.setattr(jobs, "complete", die)
            with pytest.raises(SystemExit):
                drain(Queue.SCRIPT, _migrated, stages=stages)

        # The work landed — and the fence landed with it, in the same transaction.
        assert repo.get_episode(db, episode_id).state is EpisodeState.RENDERING
        assert (script_generator.calls, grounding_validator.calls) == (1, 1)
        queued = self._tts_jobs(db)
        assert len(queued) == 1
        script_row = self._job_row(db, Queue.SCRIPT)
        assert script_row["state"] == "running"
        assert script_row["work_committed_attempt"] == 1
        segments = [(s.id, s.text) for s in repo.get_episode(db, episode_id).segments]

        # 2. The TTS job downstream exhausts its retries, which is what marks the episode
        #    failed and puts the reason on it.
        self._fail_the_tts_job(db, _migrated, stages)
        failed = repo.get_episode(db, episode_id)
        assert failed.state is EpisodeState.FAILED
        assert failed.last_error is not None
        reason = failed.last_error

        rewrites: list[str] = []
        replace_segments = repo.replace_segments

        def spy(
            conn: psycopg.Connection[Any],
            episode_id_: str,
            specs: Sequence[repo.SegmentSpec],
        ) -> None:
            rewrites.append(episode_id_)
            replace_segments(conn, episode_id_, specs)

        monkeypatch.setattr(repo, "replace_segments", spy)

        # 3. Thirty minutes pass and the stale row is claimed. It ran — the point is that
        #    it ran and did nothing, not that the lease held it back.
        self._expire_lease(db, Queue.SCRIPT)
        assert drain(Queue.SCRIPT, _migrated, stages=stages) == 1

        # Not a second script completion, and not a second grounding pass at `effort='max'`.
        assert (script_generator.calls, grounding_validator.calls) == (1, 1)
        assert rewrites == []
        # Not a second TTS job for an episode that already has one.
        assert self._tts_jobs(db) == queued

        # And the quiet half: the episode is still failed, and still says why.
        episode = repo.get_episode(db, episode_id)
        assert episode.state is EpisodeState.FAILED
        assert episode.last_error == reason
        assert [(s.id, s.text) for s in episode.segments] == segments
        # The reclaimed row is settled rather than left running for the next sweep.
        assert self._job_row(db, Queue.SCRIPT)["state"] == "done"

    def test_the_fence_reports_itself_rather_than_returning_silently(
        self, db: psycopg.Connection[Any]
    ) -> None:
        """`already_applied` is an outcome on `motet.jobs.processed`, not an early return.

        "How often does a worker die with its work committed" is a question nothing could
        answer before, and a refactor that returned `completed` here would pass every other
        test in this class while deleting it.
        """
        jobs.enqueue(db, Queue.INTEGRATE, {"source_item_id": "si_x"})
        db.commit()
        job = jobs.claim(db, Queue.INTEGRATE)
        assert job is not None
        jobs.mark_work_committed(db, job.id, attempts=job.attempts)
        db.commit()

        claimed = replace(job, work_committed_attempt=job.attempts)

        def handler(_context: Any, _payload: Mapping[str, Any]) -> None:
            raise AssertionError("the stage must not run again")

        db.autocommit = True
        try:
            outcome = loop._execute(db, claimed, handler, None, None, {})
        finally:
            db.autocommit = False
        assert outcome == "already_applied"
        assert self._job_row(db, Queue.INTEGRATE)["state"] == "done"

    def test_a_deliberate_re_script_of_a_failed_episode_still_runs(
        self,
        db: psycopg.Connection[Any],
        _migrated: str,
        object_store: LocalObjectStore,
    ) -> None:
        """The other direction, and the one a wider state check would have broken.

        An episode that genuinely needs re-scripting must not be short-circuited into
        sitting in `failed` forever with no TTS job and nothing alerting on it. That
        failure is invisible, which is why it is asserted rather than assumed: a *new*
        `script` job carries no fence, so the stage runs in full.
        """
        stages, script_generator, grounding_validator = self._counting_stages()
        episode_id = self._scripted_episode(db, _migrated, stages)
        assert drain(Queue.SCRIPT, _migrated, stages=stages) == 1
        self._fail_the_tts_job(db, _migrated, stages)
        assert repo.get_episode(db, episode_id).state is EpisodeState.FAILED

        db.execute("DELETE FROM jobs WHERE queue = %s", (Queue.TTS.value,))
        db.commit()
        jobs.enqueue(db, Queue.SCRIPT, {"episode_id": episode_id})
        db.commit()
        assert drain(Queue.SCRIPT, _migrated, stages=stages) == 1

        # The stage ran, the episode moved on, and there is a TTS job to move it.
        assert (script_generator.calls, grounding_validator.calls) == (2, 2)
        episode = repo.get_episode(db, episode_id)
        assert episode.state is EpisodeState.RENDERING
        assert episode.last_error is None
        assert len(self._tts_jobs(db)) == 1

    def test_a_handler_that_raised_leaves_no_fence_and_the_retry_runs(
        self, db: psycopg.Connection[Any], _migrated: str, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The fence is written inside the work's transaction, so a rollback takes it too.

        A fence that survived a failed attempt would be the stranding bug in its purest
        form: the retry the ladder scheduled would arrive, be mistaken for a replay, and
        mark the job done having done nothing at all.
        """
        jobs.enqueue(db, Queue.INTEGRATE, {"source_item_id": "si_x"})
        db.commit()

        calls: list[str] = []

        def handler(_context: Any, payload: Mapping[str, Any]) -> None:
            calls.append(str(payload["source_item_id"]))
            if len(calls) == 1:
                raise RuntimeError("the first attempt fell over")

        monkeypatch.setitem(loop.HANDLERS, Queue.INTEGRATE, handler)
        assert drain(Queue.INTEGRATE, _migrated) == 1

        row = self._job_row(db, Queue.INTEGRATE)
        assert row["state"] == "ready"
        assert row["work_committed_attempt"] is None

        db.execute("UPDATE jobs SET run_at = now() WHERE id = %s", (row["id"],))
        db.commit()
        assert drain(Queue.INTEGRATE, _migrated) == 1

        # The retry actually ran, and only then was the job done.
        assert calls == ["si_x", "si_x"]
        assert self._job_row(db, Queue.INTEGRATE)["state"] == "done"


class TestReadState:
    def test_read_state_is_one_fact_reachable_two_ways(
        self, db: psycopg.Connection[Any], _migrated: str
    ) -> None:
        """Invariant 5. Marking read on the backlog and having listened write the same
        column, so the two can never disagree."""
        paste(db, MORNING)
        paste(db, INQUIRY)
        drain(Queue.INTEGRATE, _migrated)
        items = repo.list_news_items(db, USER)

        updated = repo.set_news_item_read(db, user_id=USER, item_id=items[0].id, read=True)
        assert updated is not None and updated.read

        marked = repo.mark_news_items_read(db, user_id=USER, item_ids=[i.id for i in items])
        # Only the one that was still unread is counted; marking read is idempotent.
        assert marked == 1
        assert all(item.read for item in repo.list_news_items(db, USER))

        repo.set_news_item_read(db, user_id=USER, item_id=items[0].id, read=False)
        assert repo.unread_news_items(db, USER)

    def test_another_user_s_item_cannot_be_marked(
        self, db: psycopg.Connection[Any], _migrated: str
    ) -> None:
        paste(db, MORNING)
        drain(Queue.INTEGRATE, _migrated)
        item = repo.list_news_items(db, USER)[0]
        assert (
            repo.set_news_item_read(db, user_id="someone-else", item_id=item.id, read=True) is None
        )


class TestConstraints:
    def test_a_claim_cannot_be_written_without_a_real_span(
        self, db: psycopg.Connection[Any], _migrated: str
    ) -> None:
        """Invariant 3 at the storage layer.

        Application code is the first line of defence and the constraint is the last: a bug
        that produced an empty span would otherwise reach TTS and be spoken.
        """
        paste(db, MORNING)
        drain(Queue.INTEGRATE, _migrated)
        item = repo.list_news_items(db, USER)[0]
        episode_id = repo.create_episode(db, user_id=USER, title="E", max_duration_ms=60_000)

        with pytest.raises(psycopg.errors.CheckViolation):
            repo.replace_segments(
                db,
                episode_id,
                [
                    repo.SegmentSpec(
                        news_item_id=item.id,
                        text="spoken",
                        duration_ms=1,
                        claims=(
                            repo.ClaimSpec(
                                text="spoken",
                                source_item_id=item.source_item_ids[0],
                                span_start=5,
                                span_end=5,
                            ),
                        ),
                    )
                ],
            )
        db.rollback()

    def test_a_source_item_cannot_belong_to_two_news_items(
        self, db: psycopg.Connection[Any], _migrated: str
    ) -> None:
        """Dedup that double-counted would speak one story twice."""
        paste(db, MORNING)
        drain(Queue.INTEGRATE, _migrated)
        item = repo.list_news_items(db, USER)[0]

        # Filing the same source item a second time — into the same story or another one —
        # is refused by the UNIQUE on `news_item_sources.source_item_id`.
        with pytest.raises(psycopg.errors.UniqueViolation):
            repo.merge_source_into_news_item(
                db,
                news_item_id_=item.id,
                source_item_id_=item.source_item_ids[0],
                title="t",
                summary="s",
            )
        db.rollback()


class TestFeedTokens:
    def test_minted_once_and_stable(self, db: psycopg.Connection[Any]) -> None:
        first = repo.ensure_feed_token(db, USER)
        assert repo.ensure_feed_token(db, USER) == first
        assert repo.user_for_feed_token(db, first) == USER

    def test_rotation_revokes_the_old_url(self, db: psycopg.Connection[Any]) -> None:
        """Which unsubscribes every client using it — that is the point of rotating."""
        old = repo.ensure_feed_token(db, USER)
        new = repo.rotate_feed_token(db, USER)

        assert new != old
        assert repo.user_for_feed_token(db, old) is None
        assert repo.user_for_feed_token(db, new) == USER

    def test_an_unknown_or_empty_token_resolves_to_nobody(
        self, db: psycopg.Connection[Any]
    ) -> None:
        assert repo.user_for_feed_token(db, "") is None
        assert repo.user_for_feed_token(db, "not-a-token") is None


class TestDurationCap:
    def test_the_cap_is_applied_again_to_the_script_not_just_the_summary(self) -> None:
        """The finding this test exists for: assembly capped an estimate, scripting did not.

        Assembly measures a story's one-or-two-sentence summary; the script then writes two
        to four narrated claims for it. Without a second pass an episode capped at twenty
        minutes could publish forty, and nothing between here and a phone would notice.
        """
        from motet_workers.handlers import _within_cap

        specs = [
            repo.SegmentSpec(news_item_id=f"ni_{i}", text="x", duration_ms=10_000, claims=())
            for i in range(6)
        ]
        # Two fit inside 25s; the third would take the total to 30s, so it is cut.
        kept = _within_cap(specs, 25_000, "ep_x")
        assert [spec.news_item_id for spec in kept] == ["ni_0", "ni_1"]

    def test_the_first_segment_survives_however_long_it_is(self) -> None:
        """An episode that runs over is a worse briefing; an empty one is not a briefing."""
        from motet_workers.handlers import _within_cap

        specs = [repo.SegmentSpec(news_item_id="ni_0", text="x", duration_ms=999_999, claims=())]
        assert _within_cap(specs, 1_000, "ep_x") == specs

    def test_assembly_estimates_the_script_rather_than_the_summary(
        self, db: psycopg.Connection[Any], _migrated: str
    ) -> None:
        """Assembly's estimate is scaled, so the script-stage trim is a backstop.

        Estimating from the summary alone made assembly pick far more stories than could
        fit, and the trim then discarded scripts that had already been written and paid for.
        """
        from motet_workers.handlers import SCRIPT_EXPANSION

        assert SCRIPT_EXPANSION > 1
        for index in range(6):
            enqueue_paste(
                db,
                user_id=USER,
                title=f"Story number {index}",
                text=f"Story number {index}. " + " ".join(["word"] * 60),
            )
        db.commit()
        drain(Queue.INTEGRATE, _migrated)

        # ~24s of summary per story unscaled, ~72s scaled: the cap has to bite sooner.
        episode_id = enqueue_episode(db, user_id=USER, title="Short", max_duration_ms=100_000)
        db.commit()
        drain(Queue.ASSEMBLE, _migrated)

        episode = repo.get_episode(db, episode_id)
        assert episode is not None
        assert 0 < len(episode.segments) <= 2

    def test_the_expansion_factor_is_applied_exactly_once(
        self, db: psycopg.Connection[Any], _migrated: str
    ) -> None:
        """Pins the estimate to `summary x SCRIPT_EXPANSION`, and to nothing else.

        The test above bounds the segment count loosely enough that applying the factor
        twice still passed it — which is how a duplicated constant and a squared estimate
        (~9x rather than 3x) survived. An episode capped at twenty minutes then assembled
        as though every story were three times its real length, so it silently held a
        third of the stories it should have. Asserting the stored estimate exactly is
        what makes that visible.
        """
        from motet_inference import estimate_duration_ms
        from motet_workers.handlers import SCRIPT_EXPANSION

        enqueue_paste(
            db,
            user_id=USER,
            title="Only story",
            text="Only story. " + " ".join(["word"] * 60),
        )
        db.commit()
        drain(Queue.INTEGRATE, _migrated)

        # Large enough that the cap cannot trim anything: this is about the estimate, not
        # about which stories fit.
        episode_id = enqueue_episode(db, user_id=USER, title="E", max_duration_ms=60 * 60_000)
        db.commit()
        drain(Queue.ASSEMBLE, _migrated)

        episode = repo.get_episode(db, episode_id)
        assert episode is not None
        assert len(episode.segments) == 1

        items = repo.load_news_items(db, [episode.segments[0].news_item_id])
        summary = next(iter(items.values())).summary
        assert episode.segments[0].duration_ms == estimate_duration_ms(summary) * SCRIPT_EXPANSION


class TestDedupWindow:
    def test_the_cap_keeps_the_most_recent_stories(
        self, db: psycopg.Connection[Any], _migrated: str
    ) -> None:
        """Capping an oldest-first ordering would keep only the stale end of the backlog.

        A follow-up about something from this morning would then find nothing to merge
        into — exactly the large-paste case the cap is supposed to bound.
        """
        for index in range(5):
            item_id = repo.insert_news_item(
                db,
                user_id=USER,
                title=f"Story {index}",
                summary="s",
                source_item_id_=repo.insert_source_item(
                    db, user_id=USER, title=f"Story {index}", text=f"Story {index}. Body."
                ).id,
            )
            # Distinct timestamps, explicitly. `now()` is *transaction* time in Postgres, so
            # rows written in one transaction share it and the ordering falls through to a
            # random id. Real ingestion writes one source item per job, and therefore per
            # transaction, so this is what production actually looks like.
            db.execute(
                "UPDATE news_items SET created_at = now() - make_interval(mins => %s) "
                "WHERE id = %s",
                (10 - index, item_id),
            )
        db.commit()

        window = repo.news_item_window(db, USER, max_items=2)
        titles = [item.title for item in window]

        assert titles == ["Story 3", "Story 4"]  # newest two, re-sorted oldest-first


class TestNothingDrainsTheQueue:
    """motet#38: the SPA promised a worker within seconds, and nothing was running.

    The application half of the fix is here — a worker that polls, over every queue, in
    one process — plus the heartbeat that lets the API say which of the two situations a
    queued item is actually in.
    """

    def test_runner_all_carries_a_paste_the_whole_way_to_audio(
        self, db: psycopg.Connection[Any], _migrated: str, object_store: LocalObjectStore
    ) -> None:
        """One invocation, no per-stage dispatch, no human.

        The old shape was one process per queue, started by hand: `integrate`, then
        `assemble`, then `script`, then `tts`, each a separate `workflow_dispatch` in a
        repository the product's user has never heard of. This is that whole sequence as
        one command, which is what makes an always-on worker a deployment rather than a
        rewrite.
        """
        paste(db, MORNING)
        episode_id = enqueue_episode(db, user_id=USER, title="Briefing", max_duration_ms=600_000)
        db.commit()

        assert runner.main(["all"]) == 0

        episode = repo.get_episode(db, episode_id)
        assert episode is not None
        assert episode.state is EpisodeState.READY
        assert episode.audio_key is not None

    def test_a_drain_says_it_ran_even_when_there_was_nothing_to_do(
        self, db: psycopg.Connection[Any], _migrated: str
    ) -> None:
        """The whole point: an idle queue with a worker on it, and one without, differ.

        Nothing in the `jobs` table distinguishes them, which is why a queued item looked
        identical whether it was about to move or never would.
        """
        assert repo.worker_heartbeats(db)[1] == []

        drain(Queue.INTEGRATE, _migrated)

        _, beats = repo.worker_heartbeats(db)
        assert [beat.queue for beat in beats] == ["integrate"]

    def test_polling_over_everything_heartbeats_every_queue(
        self, db: psycopg.Connection[Any], _migrated: str
    ) -> None:
        assert runner.main(["all"]) == 0
        assert {beat.queue for beat in repo.worker_heartbeats(db)[1]} == {q.value for q in Queue}

    def test_the_llm_client_is_built_once_for_the_process_not_once_per_drain(
        self, db: psycopg.Connection[Any], _migrated: str, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """`real_stages()` mints a fresh `LlmClient` every call.

        OpenRouter's sticky upstream routing is per client, and that routing is what keeps
        the dedup prompt cache warm — the largest LLM cost lever in the system. A poll loop
        resolving stages per drain would throw it away on every sweep, six times a pass.
        """
        from motet_inference import get_stages as real_get_stages

        calls = 0

        def counted() -> Any:
            nonlocal calls
            calls += 1
            return real_get_stages()

        monkeypatch.setattr(runner, "get_stages", counted)
        assert runner.main(["all"]) == 0
        assert calls == 1

    def test_sigterm_stops_the_poll_loop_rather_than_killing_the_process(
        self, monkeypatch: pytest.MonkeyPatch, _migrated: str
    ) -> None:
        """A long-lived worker is the thing Cloud Run signals on every deploy.

        Without a handler the default disposition kills it outright, skipping the obs
        flush in `main`'s `finally` — so the spans and metrics describing the shutdown are
        the ones that never leave.
        """
        drained: list[Queue] = []

        def fake_drain(queue: Queue, url: str, *, max_jobs: int, **_: Any) -> int:
            drained.append(queue)
            # A bound, so a flag that never gets set fails this test instead of hanging
            # the suite until GitHub's six-hour limit.
            assert len(drained) <= 4, "SIGTERM did not stop the poll loop"
            os.kill(os.getpid(), signal.SIGTERM)
            return 0

        monkeypatch.setattr(runner, "drain", fake_drain)
        previous = signal.getsignal(signal.SIGTERM)
        try:
            assert runner.main(["integrate", "--poll-seconds", "0.01"]) == 0
        finally:
            signal.signal(signal.SIGTERM, previous)
        assert drained[0] is Queue.INTEGRATE


class TestJobRetention:
    """motet#56: nothing ever deleted a job row, so `jobs` grew for the deployment's life.

    `complete()` flips a row to `done` and it stays. One row per pipeline stage per pasted
    item and per episode, forever — which after motet#49's index is no longer a latency
    problem and is still storage, autovacuum work, and the footprint of every other index
    on the table.

    **The risk this class is mostly about is the quiet one.** Deleting too little is
    visible in `pg_total_relation_size`; deleting too much destroys `last_error`, which for
    `poll` and `extract` is the only record anywhere that a mailbox message was seen and
    lost — and `list_ingestion`'s extract arm, the surface that reports it, has no time
    bound of its own, so such a row does not age off the user's screen, it vanishes from
    it. Both windows are therefore asserted against the readers that need them, not only
    against themselves.
    """

    def terminal(
        self,
        conn: psycopg.Connection[Any],
        *,
        state: str,
        age_seconds: float,
        queue: Queue = Queue.INTEGRATE,
        payload: Mapping[str, Any] | None = None,
        count: int = 1,
        attempts: int = 1,
    ) -> None:
        """`count` rows that reached `state` `age_seconds` ago.

        Written straight in rather than by running and failing jobs: what is under test is
        a `DELETE` keyed on `state` and `updated_at`, and driving a row to an age of eight
        days through the queue's own API is not available at any price.
        """
        conn.execute(
            """
            INSERT INTO jobs (queue, payload, state, attempts, run_at, created_at, updated_at)
            SELECT %s, %s::jsonb, %s, %s,
                   now() - make_interval(secs => %s),
                   now() - make_interval(secs => %s),
                   now() - make_interval(secs => %s)
            FROM generate_series(1, %s)
            """,
            (
                queue.value,
                json.dumps(dict(payload or {})),
                state,
                attempts,
                age_seconds,
                age_seconds,
                age_seconds,
                count,
            ),
        )
        conn.commit()

    def sweep(self, conn: psycopg.Connection[Any], **kwargs: Any) -> jobs.Pruned:
        """`jobs.prune` on an autocommit connection, which is its documented precondition.

        Seeding above commits, so flipping the connection here rather than taking a second
        one keeps each test reading as one story — and it means every test below exercises
        the mode the bound depends on, instead of the transactional one where the batching
        is not a bound at all.
        """
        conn.commit()
        conn.autocommit = True
        return jobs.prune(conn, **kwargs)

    def states(self, conn: psycopg.Connection[Any]) -> list[tuple[str, int]]:
        rows = conn.execute(
            "SELECT state, count(*) AS n FROM jobs GROUP BY state ORDER BY state"
        ).fetchall()
        return [(row["state"], row["n"]) for row in rows]

    def test_the_windows_outlive_the_readers_that_need_them(self) -> None:
        """The two orderings the whole design rests on, pinned as constants.

        `INTEGRATED_GRACE` is the bound on the only reader that wants a `done` row at all;
        a `done` window shorter than it would blank the ingestion panel's success line
        while somebody was looking at it. And `failed` must outlive `done` by enough to be
        a different decision rather than a rounding of the same one — a `failed` row is
        the only copy of `last_error`.
        """
        assert jobs.DONE_RETENTION_SECONDS > repo.INTEGRATED_GRACE.total_seconds()
        assert jobs.FAILED_RETENTION_SECONDS > jobs.DONE_RETENTION_SECONDS * 10

    def test_pruning_inside_a_transaction_is_refused(self, db: psycopg.Connection[Any]) -> None:
        """The bound is autocommit, and a violated precondition here is otherwise invisible.

        Inside one transaction the batches hold every row lock until the last of them
        commits, which is the unbounded `DELETE` the batching exists to avoid — against the
        claim query the pruning is meant to be helping. It deletes exactly the same rows
        either way, so every assertion about *which* rows still passes: the single property
        the design rests on could be dropped without a test going red. Hence a `ValueError`
        and this test, rather than a sentence in a docstring.
        """
        self.terminal(db, state="done", age_seconds=jobs.DONE_RETENTION_SECONDS + 3600)
        assert not db.autocommit

        with pytest.raises(ValueError, match="autocommit"):
            jobs.prune(db)

        assert self.states(db) == [("done", 1)], "the refusal must not have deleted anything"

    def test_a_done_row_past_the_window_goes_and_one_inside_it_stays(
        self, db: psycopg.Connection[Any]
    ) -> None:
        self.terminal(db, state="done", age_seconds=jobs.DONE_RETENTION_SECONDS + 3600)
        self.terminal(db, state="done", age_seconds=jobs.DONE_RETENTION_SECONDS - 3600)
        assert self.states(db) == [("done", 2)]

        pruned = self.sweep(db)

        assert pruned.deleted["done"] == 1
        assert not pruned.capped
        assert self.states(db) == [("done", 1)]

    def test_a_failed_row_is_kept_long_after_a_done_one_would_be_gone(
        self, db: psycopg.Connection[Any]
    ) -> None:
        """The asymmetry, at the age where it is the only thing deciding.

        Both rows are a fortnight old. The `done` one is redundant with `source_items` by
        then; the `failed` one still carries the sentence that says why the job stopped
        being retried, and for `poll` and `extract` there is no second copy of it anywhere.
        """
        fortnight = 14 * 24 * 3600
        self.terminal(db, state="done", age_seconds=fortnight)
        self.terminal(db, state="failed", age_seconds=fortnight, payload={"why": "keep me"})

        pruned = self.sweep(db)

        assert pruned.deleted == {"done": 1, "failed": 0}
        assert self.states(db) == [("failed", 1)]

    def test_a_failed_row_past_its_own_window_goes_too(self, db: psycopg.Connection[Any]) -> None:
        """Longer is not forever: the table has to be bounded on both states or on neither."""
        self.terminal(db, state="failed", age_seconds=jobs.FAILED_RETENTION_SECONDS + 86400)
        self.terminal(db, state="failed", age_seconds=jobs.FAILED_RETENTION_SECONDS - 86400)

        pruned = self.sweep(db)

        assert pruned.deleted["failed"] == 1
        assert self.states(db) == [("failed", 1)]

    def test_a_job_ages_from_when_it_finished_not_from_when_it_was_enqueued(
        self, db: psycopg.Connection[Any]
    ) -> None:
        """`updated_at`, which `complete` and `fail` write, and nothing writes afterwards.

        A job that went up the backoff ladder — five attempts, ten minutes of waiting, a
        vendor outage in between — was enqueued long before it settled, and a stage whose
        work is the user's whole backlog can run for the better part of an hour by itself.
        Keyed on `created_at` this row would be deleted the moment it completed, which is
        the version of this change that silently loses a job's record as it produces it.
        `id` is not a clock either, for the same reason.
        """
        db.execute(
            """
            INSERT INTO jobs (queue, payload, state, attempts, created_at, updated_at)
            VALUES ('script', '{}'::jsonb, 'done', 5,
                    now() - make_interval(secs => %s), now() - make_interval(secs => 60))
            """,
            (jobs.DONE_RETENTION_SECONDS + 86400,),
        )
        db.commit()

        assert self.sweep(db).total == 0
        assert self.states(db) == [("done", 1)]

    def test_a_live_job_is_never_touched_however_old_it_is(
        self, db: psycopg.Connection[Any]
    ) -> None:
        """Age is not the criterion — reaching a terminal state is, and age bounds it.

        A `ready` job a year old is a job somebody is still owed; a `running` one that old
        is a stranded row the lease reclaim exists to recover. Deleting either would turn
        a retention sweep into data loss, and `run_at`/`created_at` are the columns that
        would have done it, which is why the statement keys on `updated_at` and `state`.
        """
        year = 365 * 24 * 3600
        self.terminal(db, state="ready", age_seconds=year)
        self.terminal(db, state="running", age_seconds=year)

        pruned = self.sweep(db)

        assert pruned.total == 0
        assert self.states(db) == [("ready", 1), ("running", 1)]

    def test_the_delete_is_bounded_and_says_so(self, db: psycopg.Connection[Any]) -> None:
        """One sweep removes a fixed number of rows and leaves the rest for the next one.

        An unbounded `DELETE` on a queue table holds every row lock it takes until it
        commits, against the claim query the pruning is meant to be helping. The batch size
        is what caps the locks; the batch count is what caps the sweep.
        """
        budget = 8
        excess = 5
        self.terminal(
            db,
            state="done",
            age_seconds=jobs.DONE_RETENTION_SECONDS + 3600,
            count=budget + excess,
        )

        first = self.sweep(db, batch_size=2, max_batches=4)

        assert first.deleted["done"] == budget
        assert first.capped, "a sweep that stopped on its budget has to say so"
        assert self.states(db) == [("done", excess)]

        second = self.sweep(db, batch_size=2, max_batches=4)

        assert second.deleted["done"] == excess
        assert not second.capped
        assert self.states(db) == []

    def test_the_oldest_rows_go_first(self, db: psycopg.Connection[Any]) -> None:
        """Which rows a capped sweep takes, and it is the answer that makes it converge.

        A sweep that took an arbitrary batch would leave the oldest rows to be re-read on
        every pass — and on a table whose growth outran one sweep, never delete them.
        """
        for age_days in (30, 20, 10):
            # `attempts` is a spare integer column, used here only to label which row is
            # which: the rows are otherwise identical apart from an age this then reads back.
            self.terminal(db, state="done", age_seconds=age_days * 24 * 3600, attempts=age_days)

        self.sweep(db, batch_size=1, max_batches=1)

        remaining = [row["attempts"] for row in db.execute("SELECT attempts FROM jobs").fetchall()]
        assert sorted(remaining) == [10, 20]

    def test_the_ingestion_panel_keeps_a_just_landed_paste_intact(
        self, db: psycopg.Connection[Any], _migrated: str
    ) -> None:
        """The one reader that wants a `done` row, at the far edge of the grace it is given.

        `list_ingestion` joins the just-succeeded `integrate` job onto the line it keeps up
        for `INTEGRATED_GRACE` after a paste lands. Aged to nine minutes, the line is still
        up and the join still has to find its row — which is the boundary a `done` window
        under the grace would cross, and a fresh row proves nothing about.

        **What such a window would cost is the attempt count, not the line**: the arm is
        driven by `source_items` and joins the job *left*, so the row goes on being
        reported, emptied. That is why `attempts` is what this asserts.
        """
        paste(db, MORNING)
        drain(Queue.INTEGRATE, _migrated)
        nearly_expired = repo.INTEGRATED_GRACE.total_seconds() - 60
        db.execute(
            """
            UPDATE jobs SET updated_at = now() - make_interval(secs => %s)
            WHERE queue = 'integrate'
            """,
            (nearly_expired,),
        )
        db.execute(
            "UPDATE source_items SET integrated_at = now() - make_interval(secs => %s)",
            (nearly_expired,),
        )
        db.commit()

        (before,) = repo.list_ingestion(db, USER)
        assert before.state is SourceItemState.INTEGRATED
        assert before.attempts == 1

        pruned = self.sweep(db)

        assert pruned.total == 0
        (after,) = repo.list_ingestion(db, USER)
        assert (after.id, after.state, after.attempts) == (before.id, before.state, 1)

    def test_a_lost_mailbox_message_is_still_reported_after_a_sweep(
        self, db: psycopg.Connection[Any]
    ) -> None:
        """The quiet failure, guarded at the surface it would have failed at.

        `handle_extract` writes a `source_items` row only when extraction succeeds, and
        `handle_poll` has already moved the cursor past the message — so a `failed`
        extract job is the whole record that a newsletter arrived and was lost, and
        `list_ingestion`'s extract arm reports it with no time bound of its own. Delete the
        row and the message does not age off the panel; it disappears from it, with nothing
        anywhere saying it ever existed. A fortnight is past the `done` window and nowhere
        near the `failed` one.
        """
        source_id = phase2.create_source(db, user_id=USER, kind="gmail", name="Gmail").id
        db.commit()
        self.terminal(
            db,
            state="failed",
            queue=Queue.EXTRACT,
            age_seconds=14 * 24 * 3600,
            payload={"source_id": source_id, "message_id": "msg_lost"},
        )
        db.execute(
            "UPDATE jobs SET last_error = %s WHERE queue = 'extract'",
            ("HttpError: the mailbox would not answer",),
        )
        db.commit()

        self.sweep(db)

        (status,) = repo.list_ingestion(db, USER)
        assert status.state is SourceItemState.FAILED
        assert status.last_error == "HttpError: the mailbox would not answer"

    def test_the_prune_statement_reads_its_index_rather_than_the_table(
        self, db: psycopg.Connection[Any]
    ) -> None:
        """Migration 0010's index, asserted on the plan of the statement `prune` runs.

        Without it the sweep is a sequential scan of `jobs` — the very scan it exists to
        make unnecessary, run every hour, taking row locks. That is motet#49's mistake
        facing the other way, and a comment on the index is what failed to catch it the
        first time, so this `EXPLAIN`s `jobs.PRUNE_SQL` itself rather than a copy.
        """
        # Both shapes, because they plan differently and only one of them is the sweep
        # doing work: rows inside the window are the hourly no-op, rows past it are the
        # delete that matters. The index has to answer the *search* in both.
        self.terminal(db, state="done", age_seconds=3600, count=2000)
        self.terminal(db, state="done", age_seconds=jobs.DONE_RETENTION_SECONDS + 3600, count=2000)
        # Without fresh statistics the planner is costing a table it thinks is empty, and
        # would pick a sequential scan for any query at all.
        db.execute("ANALYZE jobs")
        db.commit()

        plan = "\n".join(
            row["QUERY PLAN"]
            for row in db.execute(
                f"EXPLAIN {jobs.PRUNE_SQL}",
                ("done", jobs.DONE_RETENTION_SECONDS, jobs.PRUNE_BATCH_SIZE),
            ).fetchall()
        )

        # The *inner* scan — how the rows to delete are found — is the claim. Not the outer
        # `DELETE ... WHERE id IN`, which Postgres plans as a hash semi-join over a seq scan
        # on a small table and as a nested loop on the primary key on a large one: both are
        # cost-justified, neither is what this index is for, and asserting no sequential scan
        # anywhere in the plan would fail on a realistic table for no defect.
        assert "jobs_terminal_idx" in plan, plan
        assert "Seq Scan on jobs jobs_1" not in plan, plan

    def test_a_one_shot_drain_sweeps_once(
        self, db: psycopg.Connection[Any], _migrated: str, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The Cloud Run job shape is the one production runs, so it cannot be left out.

        A sweep gated on a clock would never fire in a process that exits after one drain
        — and since motet#71 an execution starts whenever somebody pastes, so that process
        is most of what runs at all.
        """
        sweeps: list[str] = []
        monkeypatch.setattr(runner, "prune_jobs", lambda url: sweeps.append(url))

        assert runner.main(["integrate"]) == 0

        assert sweeps == [_migrated]

    def test_the_poll_loop_sweeps_on_its_first_pass_and_then_waits(
        self, _migrated: str, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Once per interval, not once per pass — the loop runs several times a second.

        The first pass sweeps rather than waiting out an interval, because a worker
        restarted oftener than the interval would otherwise never sweep at all.
        """
        sweeps: list[str] = []
        passes = 0

        def fake_drain(queue: Queue, url: str, *, max_jobs: int, **_: Any) -> int:
            nonlocal passes
            passes += 1
            assert passes <= 5, "the poll loop did not stop"
            if passes == 5:
                os.kill(os.getpid(), signal.SIGTERM)
            return 0

        monkeypatch.setattr(runner, "drain", fake_drain)
        monkeypatch.setattr(runner, "prune_jobs", lambda url: sweeps.append(url))
        previous = signal.getsignal(signal.SIGTERM)
        try:
            assert runner.main(["integrate", "--poll-seconds", "0.001"]) == 0
        finally:
            signal.signal(signal.SIGTERM, previous)

        assert sweeps == [_migrated], f"{passes} passes swept {len(sweeps)} times"

    def test_the_poll_loop_sweeps_again_once_the_interval_elapses(
        self, _migrated: str, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The other half, and the one a "swept once per process" bug would slip past.

        A long-lived worker is the shape the interval exists for, so "it swept, then stopped
        asking" has to be distinguishable from "it swept, then waited". With the interval at
        zero every pass is due, so the count of sweeps has to track the count of passes.
        """
        sweeps: list[str] = []
        passes = 0

        def fake_drain(queue: Queue, url: str, *, max_jobs: int, **_: Any) -> int:
            nonlocal passes
            passes += 1
            assert passes <= 5, "the poll loop did not stop"
            if passes == 4:
                os.kill(os.getpid(), signal.SIGTERM)
            return 0

        monkeypatch.setattr(runner, "drain", fake_drain)
        monkeypatch.setattr(runner, "PRUNE_INTERVAL_SECONDS", 0.0)
        monkeypatch.setattr(runner, "prune_jobs", lambda url: sweeps.append(url))
        previous = signal.getsignal(signal.SIGTERM)
        try:
            assert runner.main(["integrate", "--poll-seconds", "0.001"]) == 0
        finally:
            signal.signal(signal.SIGTERM, previous)

        assert len(sweeps) == passes == 4

    def test_a_sweep_that_cannot_reach_the_database_does_not_stop_the_worker(
        self, _migrated: str
    ) -> None:
        """Pruning is bookkeeping beside work somebody is waiting on.

        The cost of skipping an hour is an hour of rows; the cost of a worker that exits
        because its retention sweep could not connect is every job on every queue.
        """
        pruned = loop.prune_jobs("postgresql://nobody@127.0.0.1:1/nowhere")

        assert pruned.total == 0
        assert not pruned.capped

    def test_a_sweep_that_deleted_nothing_still_records_that_it_ran(
        self, db: psycopg.Connection[Any], _migrated: str
    ) -> None:
        """A sweep that found nothing and a sweep that never ran are different facts.

        The counter carries a zero for every swept state for exactly that reason, which is
        the never-infer-"no errors"-from-"no data" trap in AGENTS.md; this asserts the
        shape the metric is built from, since the counter itself is a no-op with no obs
        stack configured.
        """
        assert loop.prune_jobs(_migrated).deleted == {"done": 0, "failed": 0}


class TestIdenticalTitles:
    """motet#41: a "new" story whose headline the backlog already carries is a merge."""

    def test_a_new_story_with_a_title_already_in_the_window_merges_instead(
        self, db: psycopg.Connection[Any], _migrated: str, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Three write-ups of one story; the third came back as its own news item.

        Dedup writes the titles, so two items carrying the same one is dedup contradicting
        itself — and in audio it is the story read out twice, under one heading. The stub
        here answers "new" the way the real model did on staging.
        """
        from motet_inference import IntegrationResult, NewsItem
        from motet_workers import handlers

        existing = repo.insert_news_item(
            db,
            user_id=USER,
            title="Canada announces $20bn retaliatory tariffs",
            summary="Canada announced tariffs.",
            source_item_id_=paste(db, MORNING),
        )
        source_item_id = paste(db, EVENING)
        db.commit()

        class AlwaysNew:
            def integrate(self, item: SourceItem, window: Any) -> IntegrationResult:  # noqa: ARG002
                return IntegrationResult(
                    news_item=NewsItem(
                        id="ni_proposed",
                        # Byte-identical, capitalisation and spacing aside — which is
                        # exactly what normalizing the comparison is for.
                        title="  canada announces $20bn RETALIATORY tariffs ",
                        summary="A third outlet on the same tariffs.",
                        source_item_ids=(item.id,),
                    ),
                    merged=False,
                )

        context = handlers.Context(conn=db, stages=_stages_with(AlwaysNew()), store=None)
        handlers.handle_integrate(context, {"source_item_id": source_item_id})

        items = repo.list_news_items(db, USER)
        assert [item.id for item in items] == [existing]
        assert set(items[0].source_item_ids) == {items[0].source_item_ids[0], source_item_id}
        # The stored title is untouched: the merge is a backstop, and the model was
        # answering a different question when it wrote that title.
        assert items[0].title == "Canada announces $20bn retaliatory tariffs"

    def test_an_empty_title_matches_nothing(
        self, db: psycopg.Connection[Any], _migrated: str
    ) -> None:
        """Two items that both failed to get a title are not evidence of anything."""
        from motet_workers.handlers import _merge_target

        class Stored:
            id = "ni_blank"
            title = "   "
            read_at = None

        result = _stub_result(title="")
        assert _merge_target(result, [Stored()])[0] is None  # type: ignore[list-item]

    def test_an_already_read_story_is_left_alone(
        self, db: psycopg.Connection[Any], _migrated: str
    ) -> None:
        """The window also carries recently *read* items, and merging into one hides a story.

        Assembly selects unread items, so a fresh story folded into one already heard is
        never spoken, leaves a log line as its only trace, and a re-paste hits the same
        rule rather than undoing it. The model may still merge into a read item — that is
        a judgement about two texts, and what the window is for — but a string match is
        not that judgement, so it does not get that reach.
        """
        from motet_inference import IntegrationResult, NewsItem
        from motet_workers import handlers

        read_item = repo.insert_news_item(
            db,
            user_id=USER,
            title="Canada announces $20bn retaliatory tariffs",
            summary="Canada announced tariffs.",
            source_item_id_=paste(db, MORNING),
        )
        repo.set_news_item_read(db, user_id=USER, item_id=read_item, read=True)
        source_item_id = paste(db, EVENING)
        db.commit()

        class AlwaysNew:
            def integrate(self, item: SourceItem, window: Any) -> IntegrationResult:  # noqa: ARG002
                return IntegrationResult(
                    news_item=NewsItem(
                        id="ni_proposed",
                        title="Canada announces $20bn retaliatory tariffs",
                        summary="A second outlet on the same tariffs.",
                        source_item_ids=(item.id,),
                    ),
                    merged=False,
                )

        context = handlers.Context(conn=db, stages=_stages_with(AlwaysNew()), store=None)
        handlers.handle_integrate(context, {"source_item_id": source_item_id})

        assert len(repo.list_news_items(db, USER)) == 2
        # Said directly rather than inferred from the count: the read item was left
        # exactly as it was, and the new story is the one an episode will now speak.
        read_again = next(i for i in repo.list_news_items(db, USER) if i.id == read_item)
        assert source_item_id not in read_again.source_item_ids
        assert [i.read for i in repo.list_news_items(db, USER, unread_only=True)] == [False]

    def test_a_genuinely_new_story_is_still_new(
        self, db: psycopg.Connection[Any], _migrated: str
    ) -> None:
        paste(db, MORNING)
        paste(db, INQUIRY)
        db.commit()
        drain(Queue.INTEGRATE, _migrated)

        assert len(repo.list_news_items(db, USER)) == 2


#: Three genuinely independent write-ups of one event, in the arrival order they had on
#: staging. No two share a headline and no two share a sentence — which is the whole point:
#: the identical-title backstop cannot reach this case, and only a judgement about the two
#: texts can.
TARIFFS_NPR = (
    "Canada to impose $20 billion in retaliatory tariffs",
    "Canada to impose $20 billion in retaliatory tariffs. Prime Minister Mark Carney said "
    "Ottawa will place tariffs on $20 billion of American goods beginning Friday, "
    "responding to the levies Washington imposed this week.",
)
TARIFFS_FT = (
    "Ottawa hits back at Washington with sweeping duties",
    "Ottawa hits back at Washington with sweeping duties. Canada will apply duties worth "
    "C$27bn to US imports from Friday, Carney told reporters, in what officials described "
    "as a measured first response.",
)
TARIFFS_AP = (
    "Canadian officials detail the goods facing new charges",
    "Canadian officials detail the goods facing new charges. The Canadian government "
    "published a list on Thursday of American products that will face new charges from "
    "Friday, and said further measures would follow if Washington does not back down.",
)

#: A phrase that appears only in ``prompts.SECOND_LOOK_SYSTEM``. The first pass's prompt
#: carries the source item too, so keying the two calls apart has to be done on the part
#: that differs — the instructions.
_SECOND_LOOK = "second look of a news briefing"


class _ScriptedModel:
    """A model that answers each dedup prompt from a script, and counts its calls.

    Not a stub *integrator*: the real :class:`~motet_inference.adapters.ClaudeIntegrator`
    runs on top of this, so what the test exercises is the production decision procedure —
    prompt construction, the three-way relation, the second look and everything it does
    with a failure. Only the model is fake, which is the same arrangement
    ``inference/tests/test_adapters.py`` and ``workers/tests/test_accounting.py`` use.

    The window ids are not knowable in advance — Postgres assigns them — so a scripted
    answer names its candidate by reading the window back out of the rendered prompt.
    """

    def __init__(self, first_pass: Sequence[Mapping[str, Any]], second_look: Mapping[str, Any]):
        self._first_pass = list(first_pass)
        self._second_look = second_look
        self.calls: list[str] = []

    def complete(self, request: Any) -> Any:
        rendered = "\n".join(part.text for m in request.messages for part in m.parts)
        if _SECOND_LOOK in rendered:
            self.calls.append("second_look")
            answer: Mapping[str, Any] = self._second_look
        else:
            self.calls.append("first_pass")
            answer = dict(self._first_pass.pop(0))
            if answer.get("closest_news_item_id") == "@newest":
                window = re.findall(r"^- id: (\S+)", rendered, re.M)
                answer["closest_news_item_id"] = window[-1] if window else None
        return FakeLlmClient(responses={"": json.dumps(answer)}).complete(request)


def _integrate_with(
    db: psycopg.Connection[Any],
    model: _ScriptedModel,
    entries: Sequence[tuple[str, str]],
) -> None:
    """Paste each entry and integrate it, one at a time, through the real adapter."""
    from motet_inference.adapters import ClaudeIntegrator
    from motet_workers import handlers

    context = handlers.Context(conn=db, stages=_stages_with(ClaudeIntegrator(model)), store=None)
    for entry in entries:
        source_item_id = paste(db, entry)
        handlers.handle_integrate(context, {"source_item_id": source_item_id})
        db.commit()


def _first_pass(relation: str, title: str, *, closest: str | None = "@newest") -> dict[str, Any]:
    return {
        "closest_news_item_id": closest,
        "relation": relation,
        "reason": "scripted",
        "title": title,
        "summary": "Canada answered the US tariffs.",
    }


class TestThreeWriteUpsOfOneStory:
    """motet#41's remaining half, end to end.

    Three write-ups of the Canada tariff story were pasted on staging. Dedup merged two and
    returned the third as its own news item — so the backlog listed one event twice and an
    episode would have narrated it twice. The identical-title backstop (``TestIdenticalTitles``)
    catches that only when the model happens to write the same headline twice; here it
    writes a different one, which is the case nothing caught.

    **What these tests pin is the decision procedure, not a model's behaviour.** Whether a
    real Claude answers ``related`` rather than ``unrelated`` on the AP piece is not
    something an offline test can assert — invariant 7 keeps every vendor out of CI. What
    *is* asserted is that an unsure first pass is asked again, that a confirming second look
    collapses the story, and that a rejecting one leaves it split.
    """

    def test_an_unsure_third_write_up_is_asked_again_and_collapses(
        self, db: psycopg.Connection[Any], _migrated: str
    ) -> None:
        from motet_workers.handlers import _normalize_title

        # A different headline for the same event: the exact case the string backstop
        # cannot see.
        ap_headline = "Ottawa lists the American goods facing new charges"
        model = _ScriptedModel(
            first_pass=[
                _first_pass(
                    "unrelated", "Canada to impose $20bn in retaliatory tariffs", closest=None
                ),
                _first_pass("same_event", "Canada answers US tariffs with $20bn of duties"),
                _first_pass("related", ap_headline),
            ],
            second_look={"same_event": True, "reason": "One announcement, three write-ups."},
        )

        _integrate_with(db, model, [TARIFFS_NPR, TARIFFS_FT])
        before = repo.list_news_items(db, USER)
        assert len(before) == 1
        # Checked before the third arrives, because a merge writes the proposed headline
        # onto the row: after it, the two titles agree *because* of the merge.
        assert _normalize_title(before[0].title) != _normalize_title(ap_headline), (
            "the identical-title backstop must not be what saved this case"
        )

        _integrate_with(db, model, [TARIFFS_AP])

        items = repo.list_news_items(db, USER)
        assert len(items) == 1, "three accounts of one event are one story"
        assert len(items[0].source_item_ids) == 3
        assert model.calls == ["first_pass", "first_pass", "first_pass", "second_look"], (
            "the second look fires once, on the one answer that was unsure"
        )
        assert items[0].title == before[0].title, (
            "a second-look merge keeps the stored headline: the one the first pass wrote "
            "was written for this source item alone"
        )

    def test_the_same_three_split_when_the_second_look_says_no(
        self, db: psycopg.Connection[Any], _migrated: str
    ) -> None:
        """The failure as it was, and the false-split direction in one test.

        Identical input to the case above, with the second look answering "no" — which is
        both what the pipeline did before this change and what has to keep happening when
        two accounts really are of different events. A second look buys a *question*, never
        a merge: nothing here can fold two stories together without an affirmative answer.
        """
        model = _ScriptedModel(
            first_pass=[
                _first_pass(
                    "unrelated", "Canada to impose $20bn in retaliatory tariffs", closest=None
                ),
                _first_pass("same_event", "Canada answers US tariffs with $20bn of duties"),
                _first_pass("related", "Ottawa lists the American goods facing new charges"),
            ],
            second_look={"same_event": False, "reason": "A later step, not the announcement."},
        )
        _integrate_with(db, model, [TARIFFS_NPR, TARIFFS_FT, TARIFFS_AP])

        items = repo.list_news_items(db, USER)
        assert len(items) == 2
        assert sorted(len(item.source_item_ids) for item in items) == [1, 2]

    def test_a_confident_split_never_reaches_the_second_look(
        self, db: psycopg.Connection[Any], _migrated: str
    ) -> None:
        """The cost bound, measured rather than asserted in a comment.

        Dedup is the volume line. A design that re-asked about every source item would
        double the most-made call in the system; this one spends a second completion only
        on the band the first pass says it is unsure about.
        """
        model = _ScriptedModel(
            first_pass=[
                _first_pass(
                    "unrelated", "Canada to impose $20bn in retaliatory tariffs", closest=None
                ),
                _first_pass("unrelated", "Regulator opens inquiry"),
            ],
            second_look={"same_event": True, "reason": "never asked"},
        )
        _integrate_with(db, model, [TARIFFS_NPR, INQUIRY])

        assert len(repo.list_news_items(db, USER)) == 2
        assert model.calls == ["first_pass", "first_pass"]


def _stages_with(integrator: Any) -> Any:
    """The deterministic stage set with dedup swapped out for a stub."""
    from dataclasses import replace

    from motet_inference import get_stages

    return replace(get_stages(), integrator=integrator)


def _stub_result(*, title: str) -> Any:
    """An integrator answer that says "new", carrying whatever title the case needs."""
    from motet_inference import IntegrationResult, NewsItem

    return IntegrationResult(
        news_item=NewsItem(id="ni_proposed", title=title, summary="s", source_item_ids=("si_1",)),
        merged=False,
    )


class TestTheClaimSkipsBusyKeys:
    """motet#78: one user's burst must not be a tax every other worker pays.

    Invariant 6 already holds — an `integrate` job carries `serialize_key = user_id` and a
    worker takes an advisory lock on it after the claim, so at most one worker is ever
    integrating for a given user. What it did not hold is *cost*. The queue is ordered by
    `run_at`, so two thousand rows for one busy user sit at the head of it, and every other
    worker claimed and deferred each of them in turn — writing `state`, `attempts`,
    `locked_at`, `run_at` and `updated_at` twice per row — before it reached anybody else's
    work.

    These run on real connections because an advisory lock is a property of a *session*:
    one connection re-acquires its own lock happily, so a single-connection test would
    assert the opposite of the thing.
    """

    #: Enough of user A's rows at the head of the queue that the old claim-and-defer path
    #: is unmistakable in the assertions below. Small enough to stay a fast test.
    BURST = 50

    def _burst(self, conn: psycopg.Connection[Any], *, user: str, count: int, age: int) -> None:
        """`count` ready `integrate` jobs for `user`, the oldest `age` seconds back."""
        for i in range(count):
            job_id = jobs.enqueue(conn, Queue.INTEGRATE, {"user": user, "i": i}, serialize_key=user)
            conn.execute(
                "UPDATE jobs SET run_at = now() - make_interval(secs => %s) WHERE id = %s",
                (age - i, job_id),
            )
        conn.commit()

    def _rows(self, conn: psycopg.Connection[Any], user: str) -> list[tuple[Any, ...]]:
        return [
            (row["id"], row["state"], row["attempts"], row["run_at"], row["updated_at"])
            for row in conn.execute(
                """
                SELECT id, state, attempts, run_at, updated_at FROM jobs
                WHERE serialize_key = %s ORDER BY id
                """,
                (user,),
            ).fetchall()
        ]

    def test_a_busy_user_s_burst_is_stepped_over_untouched(
        self, db: psycopg.Connection[Any], _migrated: str
    ) -> None:
        """The fairness property itself, stated as the issue states it.

        User A's rows are older *and* first, and A's key is held by another session. The
        claim must return user B's row and leave every one of A's exactly as it found it —
        not merely leave them claimable, which the old defer path also did, but leave them
        unwritten.
        """
        self._burst(db, user="user-a", count=self.BURST, age=600)
        self._burst(db, user="user-b", count=1, age=1)

        with repo.connect(_migrated) as holder:
            assert jobs.try_lock(holder, "user-a") is True
            before = self._rows(db, "user-a")

            claimed = jobs.claim(db, Queue.INTEGRATE)
            db.commit()

            assert claimed is not None
            assert claimed.serialize_key == "user-b"
            # Not "still ready", which deferring also achieves: untouched. `updated_at`,
            # `run_at` and `attempts` are the three columns the claim-and-defer cycle wrote
            # on every row it stepped over.
            assert self._rows(db, "user-a") == before
            assert all(row[1:3] == ("ready", 0) for row in before)

            jobs.unlock(holder, "user-a")

    def test_a_whole_drain_pass_leaves_the_busy_user_alone(
        self, db: psycopg.Connection[Any], _migrated: str, object_store: LocalObjectStore
    ) -> None:
        """The same property through `drain`, which is where the busy loop actually ran.

        A single claim stepping over the burst proves the predicate; a drain pass proves
        the loop does not come back for it. The burst is skipped, B's one job runs, and the
        pass ends because nothing else is claimable — rather than because it exhausted
        `MAX_JOBS_PER_RUN` deferring.
        """
        self._burst(db, user="user-a", count=self.BURST, age=600)
        source_id = paste(db, MORNING)  # user B is the real owner; its key is `USER`.
        db.execute(
            "UPDATE jobs SET run_at = now() WHERE payload->>'source_item_id' = %s", (source_id,)
        )
        db.commit()

        with repo.connect(_migrated) as holder:
            assert jobs.try_lock(holder, "user-a") is True
            before = self._rows(db, "user-a")

            assert drain(Queue.INTEGRATE, _migrated) == 1

            assert self._rows(db, "user-a") == before
            jobs.unlock(holder, "user-a")

    def test_nothing_changes_when_no_key_is_held(self, db: psycopg.Connection[Any]) -> None:
        """The other direction, and the one that would starve a queue if the filter is wrong.

        With no advisory lock anywhere, the pre-filter must be invisible: the claim returns
        the oldest due row, exactly as it did before motet#78.
        """
        self._burst(db, user="user-a", count=3, age=600)
        self._burst(db, user="user-b", count=1, age=1)

        oldest = db.execute(
            "SELECT id FROM jobs WHERE state = 'ready' ORDER BY run_at, id LIMIT 1"
        ).fetchone()
        assert oldest is not None

        claimed = jobs.claim(db, Queue.INTEGRATE)
        assert claimed is not None
        assert claimed.id == oldest["id"]
        assert claimed.serialize_key == "user-a"

    def test_a_key_taken_after_the_filter_still_ends_in_a_defer(
        self, db: psycopg.Connection[Any], _migrated: str, object_store: LocalObjectStore
    ) -> None:
        """The pre-filter is an optimisation; `try_lock` is still the correctness fence.

        A row with no `lock_key` is exactly the shape of that race made deterministic: the
        filter cannot know the key and therefore offers the row, and the lock then refuses
        it. Same path a genuine race takes — the key is taken in the window between the two
        — and the same outcome, which is `defer`, not a duplicate run.

        It is also the pre-migration shape. A `ready` row written before migration 0011 and
        outside its backfill has a NULL key, and it must still be *offered*: a NULL read as
        "held" would strand it silently, which is why the predicate is `lock_key IS NULL OR
        NOT EXISTS (...)` and not a bare `NOT IN`.
        """
        source_id = paste(db, MORNING)
        db.execute("UPDATE jobs SET lock_key = NULL")
        db.commit()

        with repo.connect(_migrated) as holder:
            assert jobs.try_lock(holder, USER) is True

            # Claimed — the filter had nothing to match on — then handed straight back.
            assert drain(Queue.INTEGRATE, _migrated) == 0

            row = db.execute(
                "SELECT state, attempts, run_at > now() AS deferred FROM jobs "
                "WHERE payload->>'source_item_id' = %s",
                (source_id,),
            ).fetchone()
            assert row is not None
            assert (row["state"], row["attempts"], row["deferred"]) == ("ready", 0, True)
            jobs.unlock(holder, USER)

    def test_the_lease_reclaim_arm_still_reclaims(self, db: psycopg.Connection[Any]) -> None:
        """The second arm of the claim is untouched by the filter, with a key present.

        A `running` row whose worker died is the only thing standing between a killed
        worker and a job nobody ever runs. It carries a `serialize_key` and therefore a
        `lock_key`, so a filter written slightly wrong — matching on the row's own key
        without asking whether anybody holds it — would take exactly this recovery away.
        """
        job_id = jobs.enqueue(db, Queue.INTEGRATE, {"source_item_id": "si_x"}, serialize_key=USER)
        db.execute(
            """
            UPDATE jobs SET state = 'running', attempts = 1,
                   locked_at = now() - make_interval(secs => %s)
            WHERE id = %s
            """,
            (jobs.STALE_LEASE_SECONDS + 60, job_id),
        )
        db.commit()

        claimed = jobs.claim(db, Queue.INTEGRATE)
        assert claimed is not None
        assert (claimed.id, claimed.attempts) == (job_id, 2)

    def test_a_reclaimable_row_is_left_alone_while_its_key_is_held(
        self, db: psycopg.Connection[Any], _migrated: str
    ) -> None:
        """A wedged worker's row is now stepped over rather than claimed and deferred.

        `MAX_LEASE_EXTENSION_SECONDS` describes this case: past the cap the keeper stops
        touching, the row goes stale, and the reclaiming worker used to find the key still
        busy and defer — round and round until the wedged process died. The outcome is
        unchanged (the job does not run while another session holds its key); what is gone
        is the churn, and the ERROR line naming the wedged job still comes from the keeper.
        """
        job_id = jobs.enqueue(db, Queue.INTEGRATE, {"source_item_id": "si_x"}, serialize_key=USER)
        db.execute(
            """
            UPDATE jobs SET state = 'running', attempts = 1,
                   locked_at = now() - make_interval(secs => %s)
            WHERE id = %s
            """,
            (jobs.STALE_LEASE_SECONDS + 60, job_id),
        )
        db.commit()

        with repo.connect(_migrated) as holder:
            assert jobs.try_lock(holder, USER) is True
            assert jobs.claim(db, Queue.INTEGRATE) is None
            jobs.unlock(holder, USER)

        # And it is reclaimable the moment the key frees, rather than stranded.
        claimed = jobs.claim(db, Queue.INTEGRATE)
        assert claimed is not None
        assert claimed.id == job_id

    def test_only_this_database_s_advisory_locks_count(
        self, db: psycopg.Connection[Any], _migrated: str
    ) -> None:
        """`pg_locks` is cluster-wide; an advisory lock is not.

        Every pytest run creates a database of its own (motet#15), so a run beside a local
        one — or beside a second agent session — holds advisory locks under the very keys
        this one uses. Without the `database` predicate each would stop claiming work
        because the other was busy, which is a flake that looks exactly like an empty queue.
        """
        self._burst(db, user=USER, count=1, age=60)

        elsewhere = psycopg.conninfo.make_conninfo(_migrated, dbname="postgres")
        with psycopg.connect(elsewhere) as other:
            other.execute("SELECT pg_advisory_lock(%s)", (jobs.lock_key(USER),))
            try:
                claimed = jobs.claim(db, Queue.INTEGRATE)
                assert claimed is not None
                assert claimed.serialize_key == USER
            finally:
                other.execute("SELECT pg_advisory_unlock(%s)", (jobs.lock_key(USER),))

    def test_the_backfill_agrees_with_the_python_hash(self, db: psycopg.Connection[Any]) -> None:
        """Migration 0011's SQL and `jobs.lock_key` must produce the same bigint.

        Two implementations of one hash is the shape that drifts, so this runs the
        migration's *own* statement — read out of the file rather than transcribed — over
        rows this test inserts with a NULL key, and compares what it wrote against the
        Python function. A transcription here would keep agreeing while the migration did
        not.

        The keys deliberately include a non-ASCII one (the SQL says `convert_to(...,
        'UTF8')` and the encoding is where these part company) and one whose digest has its
        top bit set, which is where an arithmetic reassembly overflows `bigint` instead of
        producing a negative number.
        """
        from motet_db import MIGRATIONS_DIR

        sql = (MIGRATIONS_DIR / "0011_job_lock_key.sql").read_text()
        backfill = sql[sql.index("UPDATE jobs") :]

        keys = ["motet-owner", "user-42", "ünïcode", "", "src_abc"]
        assert any(jobs.lock_key(key) < 0 for key in keys), "no key exercises the sign bit"

        for key in keys:
            db.execute(
                "INSERT INTO jobs (queue, payload, serialize_key) VALUES (%s, '{}'::jsonb, %s)",
                (Queue.INTEGRATE.value, key),
            )
        # A terminal row is deliberately outside the backfill's WHERE clause: it can never
        # be claimed again, so rewriting the whole table to give it a key buys nothing.
        db.execute(
            "INSERT INTO jobs (queue, payload, serialize_key, state) "
            "VALUES (%s, '{}'::jsonb, %s, 'done')",
            (Queue.INTEGRATE.value, "already-finished"),
        )
        db.execute("UPDATE jobs SET lock_key = NULL")
        db.execute(backfill)
        db.commit()

        written = {
            row["serialize_key"]: row["lock_key"]
            for row in db.execute("SELECT serialize_key, lock_key FROM jobs").fetchall()
        }
        assert written == {key: jobs.lock_key(key) for key in keys} | {"already-finished": None}


class TestTheScalingSignal:
    """motet#78: depth is the wrong number for a queue that serializes.

    Two thousand ready `integrate` rows for one user can employ one worker, not two
    thousand — invariant 6 says so. A scaler reading depth would start a pool that spends
    its life deferring, which is the cost the claim filter above removes and not a reason
    to have started the workers.
    """

    def test_a_serialized_queue_counts_users_and_a_plain_one_counts_rows(
        self, db: psycopg.Connection[Any]
    ) -> None:
        for user, count in (("user-a", 5), ("user-b", 2), ("user-c", 1)):
            for _ in range(count):
                jobs.enqueue(db, Queue.INTEGRATE, {"user": user}, serialize_key=user)
        for _ in range(4):
            jobs.enqueue(db, Queue.SCRIPT, {"episode_id": "ep_x"})
        db.commit()

        readiness = {entry.queue: entry for entry in jobs.queue_readiness(db)}

        # Eight rows, three users, three workers' worth of work.
        assert (readiness["integrate"].ready, readiness["integrate"].ready_keys) == (8, 3)
        # No serialization key, so every row is its own unit and the two agree.
        assert (readiness["script"].ready, readiness["script"].ready_keys) == (4, 4)

    def test_every_queue_is_reported_even_with_nothing_on_it(
        self, db: psycopg.Connection[Any]
    ) -> None:
        """An absent series reads as "fine" on both surfaces that consume this."""
        readiness = jobs.queue_readiness(db)
        assert [entry.queue for entry in readiness] == [queue.value for queue in PIPELINE]
        assert all((entry.ready, entry.ready_keys) == (0, 0) for entry in readiness)

    def test_work_that_is_not_due_is_not_work_a_new_worker_could_take(
        self, db: psycopg.Connection[Any]
    ) -> None:
        """A queue full of rows backing off is a queue with nothing to scale up for.

        Three states that all look like depth and none of which a new worker can start on:
        a retry waiting on the backoff ladder, a row deferred because its key was busy, and
        a row another worker is already running.
        """
        jobs.enqueue(db, Queue.INTEGRATE, {"i": 1}, serialize_key="user-a", delay_seconds=600)
        jobs.enqueue(db, Queue.INTEGRATE, {"i": 2}, serialize_key="user-b")
        running = jobs.enqueue(db, Queue.INTEGRATE, {"i": 3}, serialize_key="user-c")
        db.execute("UPDATE jobs SET state = 'running', locked_at = now() WHERE id = %s", (running,))
        db.commit()

        readiness = {entry.queue: entry for entry in jobs.queue_readiness(db)}
        assert (readiness["integrate"].ready, readiness["integrate"].ready_keys) == (1, 1)

    def test_a_drain_pass_puts_the_numbers_on_the_gauges(
        self, db: psycopg.Connection[Any], _migrated: str, object_store: LocalObjectStore
    ) -> None:
        """Emitted beside the heartbeat, for *every* queue rather than the one being drained.

        A pool scaled to zero drains nothing and would emit nothing, so a gauge covering
        only the queue its own worker is on could never be the signal that scales that pool
        back up. One grouped query answers for all six either way.
        """
        recorded: list[tuple[str, int, Mapping[str, Any]]] = []

        class _Gauge:
            def __init__(self, name: str) -> None:
                self.name = name

            def set(self, value: int, attributes: Mapping[str, Any]) -> None:
                recorded.append((self.name, value, attributes))

        for user in ("user-a", "user-b"):
            jobs.enqueue(db, Queue.TTS, {"user": user}, serialize_key=user)
        jobs.enqueue(db, Queue.TTS, {"user": "user-a"}, serialize_key="user-a")
        db.commit()

        with pytest.MonkeyPatch.context() as patch:
            patch.setattr(loop, "_queue_ready", _Gauge("motet.jobs.ready"))
            patch.setattr(loop, "_queue_ready_keys", _Gauge("motet.jobs.ready_keys"))
            # A queue with nothing on it: the pass still reports every queue.
            drain(Queue.ASSEMBLE, _migrated)

        emitted = {(name, attributes["motet.queue"]): value for name, value, attributes in recorded}
        assert emitted[("motet.jobs.ready", "tts")] == 3
        assert emitted[("motet.jobs.ready_keys", "tts")] == 2
        assert emitted[("motet.jobs.ready", "assemble")] == 0
        assert {queue for _, queue in emitted} == {queue.value for queue in PIPELINE}

    def test_a_readiness_failure_does_not_stop_the_drain(
        self, db: psycopg.Connection[Any], _migrated: str, object_store: LocalObjectStore
    ) -> None:
        """Measuring the queue must never be able to stop draining it."""
        paste(db, MORNING)

        def _boom(_conn: psycopg.Connection[Any]) -> list[jobs.QueueReadiness]:
            raise RuntimeError("no")

        with pytest.MonkeyPatch.context() as patch:
            patch.setattr(loop.jobs, "queue_readiness", _boom)
            assert drain(Queue.INTEGRATE, _migrated) == 1
