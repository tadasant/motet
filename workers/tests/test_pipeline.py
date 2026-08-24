"""The pipeline, against a real Postgres and the deterministic fakes.

A real database rather than a mock, because everything interesting here *is* the database:
``SELECT ... FOR UPDATE SKIP LOCKED``, an advisory lock, a partial index, and a ``CHECK``
constraint that refuses a claim with no span. A fake database would verify none of it.

Skips without ``DATABASE_URL`` so a quick local run needs no Postgres; CI always has one.
"""

from __future__ import annotations

from typing import Any

import psycopg
import pytest
from motet_db import EpisodeState, SourceItemState, repo
from motet_storage import LocalObjectStore
from motet_workers import Queue, drain, enqueue_episode, enqueue_paste, jobs

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
        assert "no unread news items" in episode.last_error
        # And it did not burn five attempts discovering that.
        row = db.execute("SELECT attempts FROM jobs WHERE queue = 'assemble'").fetchone()
        assert row is not None
        assert row["attempts"] == 1

    def test_rerunning_the_script_stage_replaces_segments_rather_than_appending(
        self, db: psycopg.Connection[Any], _migrated: str
    ) -> None:
        paste(db, MORNING)
        drain(Queue.INTEGRATE, _migrated)
        episode_id = enqueue_episode(db, user_id=USER, title="E", max_duration_ms=600_000)
        db.commit()
        drain(Queue.ASSEMBLE, _migrated)
        drain(Queue.SCRIPT, _migrated)
        before = len(repo.get_episode(db, episode_id).segments)

        jobs.enqueue(db, Queue.SCRIPT, {"episode_id": episode_id})
        db.commit()
        drain(Queue.SCRIPT, _migrated)

        assert len(repo.get_episode(db, episode_id).segments) == before


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
