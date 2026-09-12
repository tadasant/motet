"""A whole morning's backlog through the narration path, end to end.

Nineteen news items is what a normal Tuesday looks like, and it is the size at which the
pipeline used to come apart: the grounding gate batched every claim into one call under a
fixed ceiling, spent all of it reasoning, returned no verdicts, and left the episode in
``scripting`` through every retry (motet#42). That gate was removed in motet#75, so what
this file now defends is the property that survived it — **a full backlog goes paste →
integrate → assemble → script → rendering → ready, at a realistic claim count, with no
stage in between**.

The script stage is the **real** adapter with a fake model underneath, because it is the
one stage whose parsing of a model answer decides how many claims reach the database. The
deterministic stage fakes write one claim per story and would hide a regression there.

Against a real Postgres, like the rest of ``workers/tests``.
"""

from __future__ import annotations

import json
from typing import Any

import psycopg
import pytest
from motet_db import EpisodeState, repo
from motet_inference.adapters import ClaudeScriptGenerator
from motet_inference.interfaces import Stages
from motet_inference.llm import FakeLlmClient
from motet_inference.registry import fake_stages
from motet_storage import LocalObjectStore
from motet_workers import Queue, drain, enqueue_episode, enqueue_paste

USER = repo.OWNER_USER_ID

#: The episode that died on staging. Sized to the failure rather than to the test.
BACKLOG = 19


def newsletter(index: int) -> tuple[str, str]:
    """One pasted item: a headline and four sentences, three of them quotable."""
    return (
        f"Chipmaker {index} posts quarterly results",
        f"Chipmaker {index} posts quarterly results. "
        f"The company reported revenue of ${index + 10} million for the quarter. "
        f"It said it would hire {index * 3} engineers over the next year. "
        f"The filing was published on Tuesday, according to the regulator.",
    )


def script_payload(db: psycopg.Connection[Any]) -> tuple[object, int]:
    """A script covering every news item, quoting its source verbatim.

    Written from what is actually in the database rather than from constants: the point of
    this test is the *number* of claims that reach the episode, and a quote the script
    stage could not locate would silently reduce it.
    """
    news_items = repo.list_news_items(db, USER)
    sources = repo.load_source_items(
        db, [sid for item in news_items for sid in item.source_item_ids]
    )
    segments = []
    claims_written = 0
    for item in news_items:
        source_id = item.source_item_ids[0]
        source = sources[source_id]
        quotes = [sentence.strip() for sentence in source.text.split(". ")][1:4]
        claims_written += len(quotes)
        segments.append(
            {
                "news_item_id": item.id,
                "claims": [
                    {
                        "text": f"Here is the news: {quote}",
                        "quote": quote,
                        "source_item_id": source_id,
                    }
                    for quote in quotes
                ],
            }
        )
    return {"segments": segments}, claims_written


def install(monkeypatch: pytest.MonkeyPatch, payload: object) -> None:
    """The real script adapter over a canned model; fakes for everything else."""
    import motet_workers.loop as loop

    base = fake_stages()
    monkeypatch.setattr(
        loop,
        "get_stages",
        lambda: Stages(
            integrator=base.integrator,
            script_generator=ClaudeScriptGenerator(
                FakeLlmClient(responses={"": json.dumps(payload)})
            ),
            speech_synthesizer=base.speech_synthesizer,
        ),
    )


def test_a_nineteen_item_backlog_scripts_and_renders(
    db: psycopg.Connection[Any],
    _migrated: str,
    object_store: LocalObjectStore,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Paste a morning's reading, get an episode — the journey that produced no audio.

    The assertion that matters after motet#75 is the *state transition*: ``handle_script``
    writes ``rendering`` directly, with every claim it parsed intact and nothing between
    the script and TTS to drop one.
    """
    for index in range(BACKLOG):
        title, text = newsletter(index)
        enqueue_paste(db, user_id=USER, title=title, text=text)
    db.commit()
    drain(Queue.INTEGRATE, _migrated)
    assert len(repo.list_news_items(db, USER)) == BACKLOG

    episode_id = enqueue_episode(
        db, user_id=USER, title="Morning briefing", max_duration_ms=90 * 60_000
    )
    db.commit()
    assert drain(Queue.ASSEMBLE, _migrated) == 1

    payload, claims_written = script_payload(db)
    assert claims_written > 50, "the point of this test is a realistic claim count"
    install(monkeypatch, payload)

    assert drain(Queue.SCRIPT, _migrated) == 1
    episode = repo.get_episode(db, episode_id)
    assert episode is not None
    assert episode.state is EpisodeState.RENDERING
    assert episode.last_error is None
    # Nothing was dropped between the script and the database.
    assert sum(len(segment.claims) for segment in episode.segments) == claims_written
    # And every claim still carries the span its quote was located at — the half of
    # invariant 3 motet#75 kept.
    sources = repo.load_source_items(
        db, [claim.source_item_id for segment in episode.segments for claim in segment.claims]
    )
    for segment in episode.segments:
        for claim in segment.claims:
            source = sources[claim.source_item_id]
            assert source.text[claim.span_start : claim.span_end] in claim.text

    assert drain(Queue.TTS, _migrated) == 1
    episode = repo.get_episode(db, episode_id)
    assert episode is not None
    assert episode.state is EpisodeState.READY
    assert episode.audio_bytes and episode.audio_bytes > 0
    assert episode.audio_key is not None
    assert object_store.exists(episode.audio_key)


def test_a_script_with_nothing_usable_in_it_fails_the_episode_permanently(
    db: psycopg.Connection[Any],
    _migrated: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The floor of the stage: an episode with nothing to say fails loudly, once.

    A model answer the script adapter can make no claim out of — every quote unlocatable,
    or no segments at all — used to be caught by the gate's "nothing survived" branch. That
    branch went with the gate (motet#75) and ``handle_script`` raises on an empty script
    instead, which has to stay a :class:`PermanentFailure`: the same request produces the
    same answer, so retrying it five times buys five billed failures and delays the error
    somebody needs to see.
    """
    title, text = newsletter(0)
    enqueue_paste(db, user_id=USER, title=title, text=text)
    db.commit()
    drain(Queue.INTEGRATE, _migrated)
    episode_id = enqueue_episode(
        db, user_id=USER, title="Morning briefing", max_duration_ms=90 * 60_000
    )
    db.commit()
    drain(Queue.ASSEMBLE, _migrated)

    install(monkeypatch, {"segments": []})
    assert drain(Queue.SCRIPT, _migrated) == 1

    episode = repo.get_episode(db, episode_id)
    assert episode is not None
    assert episode.state is EpisodeState.FAILED
    assert episode.last_error is not None
    assert "no usable segments" in episode.last_error
    # And it did not burn the whole retry ladder discovering that.
    row = db.execute("SELECT attempts FROM jobs WHERE queue = 'script'").fetchone()
    assert row is not None
    assert row["attempts"] == 1
