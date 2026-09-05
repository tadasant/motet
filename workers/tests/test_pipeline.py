"""The pipeline, against a real Postgres and the deterministic fakes.

A real database rather than a mock, because everything interesting here *is* the database:
``SELECT ... FOR UPDATE SKIP LOCKED``, an advisory lock, a partial index, and a ``CHECK``
constraint that refuses a claim with no span. A fake database would verify none of it.

Skips without ``DATABASE_URL`` so a quick local run needs no Postgres; CI always has one.
"""

from __future__ import annotations

import os
import signal
import threading
import time
from collections.abc import Mapping, Sequence
from typing import Any

import psycopg
import pytest
from motet_db import EpisodeState, SourceItemState, repo
from motet_inference import (
    GroundingReport,
    GroundingValidator,
    NewsItem,
    Script,
    ScriptGenerator,
    SourceItem,
    Stages,
)
from motet_inference.registry import fake_stages
from motet_storage import LocalObjectStore
from motet_workers import Queue, drain, enqueue_episode, enqueue_paste, jobs, loop, runner

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
    run — on the hottest query in the system, over a table nothing prunes.

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
            # The drain claims the job, finds the key busy, and hands it straight back.
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
