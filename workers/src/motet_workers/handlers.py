"""What each pipeline stage actually does.

``Paste-in → Integrate → Assemble → Script + grounding → TTS → object storage``.

Each handler is a function of ``(context, payload)`` that either returns — the job is
done — or raises, in which case the runner retries it with backoff. They are written to be
**idempotent**, because "retried once" is the normal case rather than the exception: a
handler re-run after a partial failure must converge on the same state rather than
producing a second copy of anything.

The stages are separate queues for a reason worth restating: they have different rate
limits and different failure modes. A Cartesia 429 must not stall dedup, and retrying a
dedup call must never re-synthesize twenty minutes of audio that was already paid for.
"""

from __future__ import annotations

import logging
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

import psycopg
from motet_db import EpisodeState, SourceItemState, repo
from motet_db.models import StoredNewsItem, StoredSourceItem
from motet_inference import (
    MPEG_MEDIA_TYPE,
    WAV_MEDIA_TYPE,
    Audio,
    GroundingReport,
    NewsItem,
    Script,
    ScriptSegment,
    SourceItem,
    Stages,
    estimate_duration_ms,
    join_audio,
)
from motet_storage import ObjectStore, episode_audio_key

from .jobs import enqueue
from .queues import Queue

logger = logging.getLogger("motet.worker.handlers")

#: What an episode's audio file is called, by media type. A podcast client picks its
#: decoder from the enclosure's MIME type, but the extension is what a human sees when
#: they download the file, and a `.mp3` that is really a WAV is a support ticket.
_EXTENSIONS = {MPEG_MEDIA_TYPE: "mp3", WAV_MEDIA_TYPE: "wav"}


class HandlerError(RuntimeError):
    """A stage failed in a way that is worth retrying."""


class PermanentFailure(RuntimeError):
    """A stage failed in a way that retrying cannot fix.

    Distinct from :class:`HandlerError` because the two want opposite treatment: an
    episode with nothing unread to say will still have nothing to say in ten minutes, and
    burning five attempts to discover that just delays the error a user needs to see.
    """


@dataclass(frozen=True)
class Context:
    """Everything a handler is allowed to reach.

    Deliberately small. A handler gets a connection, the inference stages, and object
    storage — it does not get the environment, an HTTP client, or a vendor SDK, because
    anything it could reach directly is something the fakes could not stand in for.
    """

    conn: psycopg.Connection[Any]
    stages: Stages
    store: ObjectStore


# --- integrate -----------------------------------------------------------------------


def handle_integrate(context: Context, payload: Mapping[str, Any]) -> None:
    """Fold one pasted source item into the user's news items.

    Runs under the user's serialization key (invariant 6), so this is the only ingestion
    touching this user's window right now — which is what makes "read the window, decide,
    write the result" safe without any further locking.
    """
    source_item_id = _require(payload, "source_item_id")
    stored = repo.get_source_item(context.conn, source_item_id)
    if stored is None:
        raise PermanentFailure(f"source item {source_item_id} no longer exists")
    if stored.state is SourceItemState.INTEGRATED:
        # A retry after the commit succeeded but the job update did not. Nothing to do —
        # and importantly, nothing to do *twice*.
        logger.info("source item %s is already integrated; nothing to do", source_item_id)
        return

    window = repo.news_item_window(context.conn, stored.user_id)
    result = context.stages.integrator.integrate(
        _as_source_item(stored), [_as_news_item(item) for item in window]
    )

    if result.merged:
        repo.merge_source_into_news_item(
            context.conn,
            news_item_id_=result.news_item.id,
            source_item_id_=stored.id,
            title=result.news_item.title,
            summary=result.news_item.summary,
        )
        logger.info("merged source %s into news item %s", stored.id, result.news_item.id)
    else:
        # The id the integrator proposed is discarded: primary keys are the database's to
        # assign, and a stage that could choose them could collide with an existing row.
        news_item_id = repo.insert_news_item(
            context.conn,
            user_id=stored.user_id,
            title=result.news_item.title,
            summary=result.news_item.summary,
            source_item_id_=stored.id,
        )
        logger.info("source %s became new news item %s", stored.id, news_item_id)

    repo.mark_source_item(context.conn, stored.id, SourceItemState.INTEGRATED)


# --- assemble ------------------------------------------------------------------------


def handle_assemble(context: Context, payload: Mapping[str, Any]) -> None:
    """Choose which unread stories fit inside the episode's duration cap.

    Manual episodes only in Phase 1: "all unread", oldest first, until the cap is reached.
    No ranking and no scoring — that is Phase 2, and pretending otherwise here would be
    building a product instead of a factory.

    The cap is applied against an *estimate*, because no audio exists yet and the
    alternative is synthesizing everything and discarding some of it — which is the
    largest cost line in the system.
    """
    episode_id = _require(payload, "episode_id")
    episode = repo.get_episode(context.conn, episode_id)
    if episode is None:
        raise PermanentFailure(f"episode {episode_id} no longer exists")
    if episode.state is not EpisodeState.PENDING:
        logger.info("episode %s is already past assembly (%s)", episode_id, episode.state.value)
        return

    unread = repo.unread_news_items(context.conn, episode.user_id)
    if not unread:
        raise PermanentFailure("there are no unread news items to build an episode from")

    chosen: list[repo.SegmentSpec] = []
    budget_ms = episode.max_duration_ms
    for item in unread:
        estimate = estimate_duration_ms(item.summary)
        if chosen and estimate > budget_ms:
            break
        # The first item always goes in, even if it alone exceeds the cap: an episode with
        # no segments is worse than an episode that runs slightly long, and the caller
        # asked for "all unread" rather than "as much as fits exactly".
        chosen.append(
            repo.SegmentSpec(news_item_id=item.id, text="", duration_ms=estimate, claims=())
        )
        budget_ms -= estimate

    repo.replace_segments(context.conn, episode_id, chosen)
    repo.set_episode_state(context.conn, episode_id, EpisodeState.SCRIPTING)
    enqueue(context.conn, Queue.SCRIPT, {"episode_id": episode_id})
    logger.info(
        "episode %s assembled from %d of %d unread news items",
        episode_id,
        len(chosen),
        len(unread),
    )


# --- script + grounding --------------------------------------------------------------


def handle_script(context: Context, payload: Mapping[str, Any]) -> None:
    """Write the briefing, then refuse to pass on anything that is not grounded.

    **Invariant 3 lives in this function.** Validation runs here, before TTS is enqueued
    — never after — and a claim whose evidence does not support it is dropped rather than
    spoken. Dropping rather than failing the whole episode is deliberate: the remaining
    claims are individually grounded, so what ships is exactly the subset that passed.
    An episode where *nothing* passed is a failure, loudly, because that is a signal about
    the script stage rather than about one sentence.
    """
    episode_id = _require(payload, "episode_id")
    episode = repo.get_episode(context.conn, episode_id)
    if episode is None:
        raise PermanentFailure(f"episode {episode_id} no longer exists")
    if episode.state is EpisodeState.READY:
        logger.info("episode %s is already rendered; nothing to script", episode_id)
        return
    if not episode.segments:
        raise PermanentFailure("episode has no segments; assembly did not run")

    news_item_ids = [segment.news_item_id for segment in episode.segments]
    stored_items = repo.load_news_items(context.conn, news_item_ids)
    # Re-ordered to match the episode's segment order rather than the query's: the order
    # stories are spoken in is the assemble stage's decision, and a script written in a
    # different order would not line up with the segments it is written back into.
    ordered = [stored_items[item_id] for item_id in news_item_ids if item_id in stored_items]
    if not ordered:
        raise PermanentFailure("none of this episode's news items still exist")

    sources = repo.load_source_items(
        context.conn, [sid for item in ordered for sid in item.source_item_ids]
    )
    stage_items = [_as_news_item(item) for item in ordered]
    stage_sources = {sid: _as_source_item(item) for sid, item in sources.items()}

    script = context.stages.script_generator.generate(stage_items, stage_sources)
    report = context.stages.grounding_validator.validate(script, stage_sources)

    grounded = _drop_ungrounded(script, report)
    dropped = _claim_count(script) - _claim_count(grounded)
    if dropped:
        logger.warning(
            "grounding validation rejected %d of %d claims in episode %s",
            dropped,
            _claim_count(script),
            episode_id,
        )
    if not grounded.segments:
        raise PermanentFailure(
            "no claim in this episode survived grounding validation: "
            + "; ".join(f"{f.claim_text[:80]!r}: {f.reason}" for f in report.failures[:5])
        )

    specs = [
        repo.SegmentSpec(
            news_item_id=segment.news_item_id,
            text=segment.text,
            duration_ms=estimate_duration_ms(segment.text),
            claims=tuple(
                repo.ClaimSpec(
                    text=claim.text,
                    source_item_id=claim.span.source_item_id,
                    span_start=claim.span.start,
                    span_end=claim.span.end,
                )
                for claim in segment.claims
            ),
        )
        for segment in grounded.segments
    ]
    repo.replace_segments(context.conn, episode_id, specs)
    repo.set_episode_state(context.conn, episode_id, EpisodeState.RENDERING)
    enqueue(context.conn, Queue.TTS, {"episode_id": episode_id})
    logger.info(
        "episode %s scripted: %d segments, %d grounded claims",
        episode_id,
        len(specs),
        sum(len(spec.claims) for spec in specs),
    )


def _drop_ungrounded(script: Script, report: GroundingReport) -> Script:
    """Remove every claim the validator rejected, and any segment left empty.

    Matching on ``(news_item_id, claim text)`` rather than on identity because a
    :class:`GroundingFailure` carries exactly those two fields — it is a report, not a
    reference. Two identical claim texts under one news item would both be dropped
    together, which is the safe direction to be wrong in.
    """
    rejected = {(failure.news_item_id, failure.claim_text) for failure in report.failures}
    if not rejected:
        return script
    segments = []
    for segment in script.segments:
        kept = tuple(
            claim for claim in segment.claims if (segment.news_item_id, claim.text) not in rejected
        )
        if kept:
            segments.append(ScriptSegment(news_item_id=segment.news_item_id, claims=kept))
    return Script(segments=tuple(segments))


def _claim_count(script: Script) -> int:
    return sum(len(segment.claims) for segment in script.segments)


# --- TTS -----------------------------------------------------------------------------


def handle_tts(context: Context, payload: Mapping[str, Any]) -> None:
    """Synthesize each segment, join, upload, and publish the episode.

    Nothing here has to re-check grounding: an ungrounded claim was removed before this
    job was enqueued, so what arrives is exactly the copy that passed. That ordering is
    invariant 3, and it is enforced by the queue rather than by a flag.
    """
    episode_id = _require(payload, "episode_id")
    episode = repo.get_episode(context.conn, episode_id)
    if episode is None:
        raise PermanentFailure(f"episode {episode_id} no longer exists")
    if episode.state is EpisodeState.READY:
        logger.info("episode %s is already published", episode_id)
        return
    if not episode.segments:
        raise PermanentFailure("episode has no segments to synthesize")

    rendered: list[Audio] = []
    for segment in episode.segments:
        if not segment.text.strip():
            raise PermanentFailure(f"segment {segment.id} has no text to speak")
        rendered.append(context.stages.speech_synthesizer.synthesize(segment.text))

    audio = join_audio(rendered)
    extension = _EXTENSIONS.get(audio.media_type)
    if extension is None:
        raise PermanentFailure(f"synthesizer returned unsupported media type {audio.media_type!r}")

    key = episode_audio_key(episode.user_id, episode_id, extension)
    context.store.put(key, audio.data, content_type=audio.media_type)

    total_ms = repo.set_segment_durations(
        context.conn, episode_id, [part.duration_ms for part in rendered]
    )
    repo.publish_episode(
        context.conn,
        episode_id,
        audio_key=key,
        audio_bytes=len(audio.data),
        audio_media_type=audio.media_type,
        duration_ms=total_ms,
    )
    logger.info(
        "episode %s published: %d segments, %d ms, %d bytes at %s",
        episode_id,
        len(rendered),
        total_ms,
        len(audio.data),
        key,
    )


# --- shared --------------------------------------------------------------------------

#: The queues Phase 1 drains. ``poll`` and ``extract`` exist in :class:`Queue` because the
#: pipeline shape is settled, but they belong to Gmail and X ingestion in Phase 2 — a
#: worker asked to drain one says so rather than silently succeeding on an empty queue.
HANDLERS = {
    Queue.INTEGRATE: handle_integrate,
    Queue.ASSEMBLE: handle_assemble,
    Queue.SCRIPT: handle_script,
    Queue.TTS: handle_tts,
}


def _require(payload: Mapping[str, Any], key: str) -> str:
    value = payload.get(key)
    if not isinstance(value, str) or not value:
        raise PermanentFailure(f"job payload is missing a usable {key!r}: {payload!r}")
    return value


def _as_source_item(stored: StoredSourceItem) -> SourceItem:
    """Persisted row to the value type stages see — without the user id.

    A stage that could tell users apart is a stage that could leak between them, and it
    would need to be trusted not to. Not handing it the identifier is cheaper than
    trusting it.
    """
    return SourceItem(id=stored.id, title=stored.title, text=stored.text)


def _as_news_item(stored: StoredNewsItem) -> NewsItem:
    return NewsItem(
        id=stored.id,
        title=stored.title,
        summary=stored.summary,
        source_item_ids=stored.source_item_ids,
    )


def enqueue_paste(
    conn: psycopg.Connection[Any], *, user_id: str, title: str, text: str
) -> StoredSourceItem:
    """Store pasted text and queue it for integration, in one transaction.

    The API calls this. Enqueueing in the same transaction that writes the row is the
    whole reason the queue lives in Postgres: with two systems there is always a window
    where the source item exists and nothing will ever pick it up.
    """
    stored = repo.insert_source_item(conn, user_id=user_id, title=title, text=text)
    enqueue(conn, Queue.INTEGRATE, {"source_item_id": stored.id}, serialize_key=user_id)
    return stored


def enqueue_episode(
    conn: psycopg.Connection[Any], *, user_id: str, title: str, max_duration_ms: int
) -> str:
    """Create a manual episode and queue its assembly."""
    episode_id = repo.create_episode(
        conn, user_id=user_id, title=title, max_duration_ms=max_duration_ms
    )
    enqueue(conn, Queue.ASSEMBLE, {"episode_id": episode_id})
    return episode_id


def episode_failed(conn: psycopg.Connection[Any], payload: Mapping[str, Any], error: str) -> None:
    """Mark an episode failed when one of its stages gives up.

    Called by the runner rather than by the handlers, so that "gave up" is decided in one
    place — a handler that marked its own episode failed on every raise would do it on
    retryable errors too, and the episode would flap between states while the job was
    still going to succeed.
    """
    episode_id = payload.get("episode_id")
    if isinstance(episode_id, str) and episode_id:
        repo.set_episode_state(conn, episode_id, EpisodeState.FAILED, error=error[:2000])


def source_item_failed(
    conn: psycopg.Connection[Any], payload: Mapping[str, Any], error: str
) -> None:
    source_item_id = payload.get("source_item_id")
    if isinstance(source_item_id, str) and source_item_id:
        repo.mark_source_item(conn, source_item_id, SourceItemState.FAILED, error=error[:2000])


def failure_recorders() -> Mapping[Queue, Any]:
    """Which "this stage gave up" note to write, per queue."""
    return {
        Queue.INTEGRATE: source_item_failed,
        Queue.ASSEMBLE: episode_failed,
        Queue.SCRIPT: episode_failed,
        Queue.TTS: episode_failed,
    }
