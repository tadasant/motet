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
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

import psycopg
from motet_db import EpisodeKind, EpisodeState, RuleError, SmartRule, SourceItemState, phase2, repo
from motet_db.models import StoredNewsItem, StoredSegment, StoredSourceItem
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

from .ingest import handle_extract, handle_poll
from .jobs import enqueue
from .queues import Queue

logger = logging.getLogger("motet.worker.handlers")

#: What an episode's audio file is called, by media type. A podcast client picks its
#: decoder from the enclosure's MIME type, but the extension is what a human sees when
#: they download the file, and a `.mp3` that is really a WAV is a support ticket.
_EXTENSIONS = {MPEG_MEDIA_TYPE: "mp3", WAV_MEDIA_TYPE: "wav"}

#: How much longer the spoken script is than the summary assembly estimates from.
#:
#: Assembly has to apply the duration cap before a script exists, so it estimates from
#: each story's one-or-two-sentence summary. The script then writes two to four narrated
#: claims for that story — several times longer. Estimating 1:1 made assembly pick far
#: more stories than could fit, and the script-stage trim then threw most of them away
#: after they had already been written.
#:
#: A blunt multiplier rather than anything cleverer: the honest answer is that nobody
#: knows the length until the script exists, and this only has to be close enough that
#: the trim downstream is a backstop rather than the normal path.
SCRIPT_EXPANSION = 3


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
    """Choose which stories fit inside the episode's duration cap.

    **One selector for both episode kinds.** A manual episode is
    :meth:`SmartRule.manual` — unread, no window, oldest first — and a smart episode is
    the same query with the four knobs turned. Two selection paths would eventually
    disagree about what "unread" means, and invariant 5 is precisely the rule that one
    fact must not have two definitions.

    The cap is applied against an *estimate*, because no audio exists yet and the
    alternative is synthesizing everything and discarding some of it — the largest cost
    line in the system. The estimate is scaled by :data:`SCRIPT_EXPANSION`, because what
    gets spoken is the script rather than the summary this is measuring. The script stage
    applies the cap again against the real copy; this pass is what keeps that one from
    having to discard stories on every run.
    """
    episode_id = _require(payload, "episode_id")
    episode = repo.get_episode(context.conn, episode_id)
    if episode is None:
        raise PermanentFailure(f"episode {episode_id} no longer exists")
    if episode.state is not EpisodeState.PENDING:
        logger.info("episode %s is already past assembly (%s)", episode_id, episode.state.value)
        return

    rule = _rule_for(context.conn, episode_id, episode.kind, episode.rule)
    candidates = phase2.select_for_rule(context.conn, episode.user_id, rule)
    if not candidates:
        raise PermanentFailure(
            f"no news items match this episode's rule ({rule.ranking.value}, "
            f"window {rule.window_days}d, unread_only={rule.unread_only})"
        )

    chosen: list[repo.SegmentSpec] = []
    budget_ms = episode.max_duration_ms
    for item in candidates:
        estimate = estimate_duration_ms(item.summary) * SCRIPT_EXPANSION
        if chosen and estimate > budget_ms:
            # `break` rather than `continue`: the candidates arrive in the rule's ranking
            # order, and skipping past a long story to fit a shorter one behind it would
            # silently override the ranking the user asked for.
            break
        # The first item always goes in, even if it alone exceeds the cap: an episode with
        # no segments is worse than an episode that runs slightly long.
        chosen.append(
            repo.SegmentSpec(news_item_id=item.id, text="", duration_ms=estimate, claims=())
        )
        budget_ms -= estimate

    repo.replace_segments(context.conn, episode_id, chosen)
    repo.set_episode_state(context.conn, episode_id, EpisodeState.SCRIPTING)
    enqueue(context.conn, Queue.SCRIPT, {"episode_id": episode_id})
    logger.info(
        "episode %s (%s, %s) assembled from %d of %d candidate news items",
        episode_id,
        episode.kind.value,
        rule.ranking.value,
        len(chosen),
        len(candidates),
    )


def _rule_for(
    conn: psycopg.Connection[Any],
    episode_id: str,
    kind: EpisodeKind,
    stored_rule: Mapping[str, Any] | None,
) -> SmartRule:
    """The rule this episode selects by.

    A smart episode carries a snapshot; a manual one uses the defaults. An unparsable
    snapshot is *permanent*: the rule was validated when the episode was created, so a
    rule that no longer parses means the schema changed underneath it, and retrying five
    times will not make it parse.
    """
    if kind is not EpisodeKind.SMART:
        return SmartRule.manual()
    try:
        return SmartRule.from_json(dict(stored_rule or {}))
    except RuleError as exc:
        raise PermanentFailure(f"episode {episode_id} has an unusable rule: {exc}") from exc


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

    specs = _within_cap(
        [
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
        ],
        episode.max_duration_ms,
        episode_id,
    )
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


def _within_cap(
    specs: list[repo.SegmentSpec], max_duration_ms: int, episode_id: str
) -> list[repo.SegmentSpec]:
    """Enforce the episode's duration cap against the *script*, before any audio is paid for.

    The assemble stage already applied the cap, but it could only apply it to an estimate
    made from each story's one-or-two-sentence summary — and the script then writes two to
    four narrated claims per story. That is several times longer, so an episode capped at
    twenty minutes could comfortably publish forty. Nobody would see it until it was on a
    phone, because every stage in between succeeded.

    So the cap is applied a second time here, against the copy that will actually be
    spoken. This is the last point at which trimming is free: after this the segments go to
    TTS, and TTS is the largest cost line in the system.

    The first segment always survives, however long it is — the same rule assembly uses.
    An episode that runs over is a worse briefing; an episode with nothing in it is not a
    briefing at all.
    """
    kept: list[repo.SegmentSpec] = []
    total = 0
    for spec in specs:
        if kept and total + spec.duration_ms > max_duration_ms:
            break
        kept.append(spec)
        total += spec.duration_ms

    if len(kept) < len(specs):
        logger.warning(
            "episode %s scripted to ~%d ms against a %d ms cap; keeping %d of %d segments",
            episode_id,
            sum(spec.duration_ms for spec in specs),
            max_duration_ms,
            len(kept),
            len(specs),
        )
    return kept


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
    # Subtitles and chapters need per-claim timing, and the transcript already pairs every
    # spoken sentence with its source span — so this is the last piece that turns the
    # existing structure into captions. Re-read rather than reused: `set_segment_durations`
    # has just rewritten every segment's offsets, and apportioning against the pre-TTS
    # estimates would drift a little further out of sync with every segment.
    rendered_episode = repo.get_episode(context.conn, episode_id)
    assert rendered_episode is not None
    phase2.set_claim_timings(
        context.conn, episode_id, apportion_claim_timings(rendered_episode.segments)
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


def apportion_claim_timings(
    segments: Sequence[StoredSegment],
) -> list[tuple[str, int, int]]:
    """Spread each segment's measured duration across its claims, by character count.

    **An apportionment rather than a measurement, deliberately.** Narration is synthesized
    one *segment* at a time, so the only real numbers are the segment boundaries — which
    this is exact at. Within a segment, claims are proportioned by length.

    The alternative is synthesizing per claim, which would give exact per-claim timings.
    It was not chosen: it multiplies the request count by three or four for the same
    billed characters, it inserts a hard prosody break at every sentence, and the error it
    removes is small. Narration is a single voice at a near-constant pace, so
    proportion-by-length is accurate to a fraction of a second — well inside what a
    caption cue needs, since a client shows cues as blocks rather than word-by-word.

    If word-level timing is ever needed — karaoke highlighting, or seeking to a word — the
    upgrade is Cartesia's own timestamp output rather than more calls, and it would replace
    this function without touching anything that reads its result.
    """
    timings: list[tuple[str, int, int]] = []
    for segment in segments:
        if not segment.claims:
            continue
        weights = [max(1, len(claim.text)) for claim in segment.claims]
        total_weight = sum(weights)
        offset = 0
        for index, (claim, weight) in enumerate(zip(segment.claims, weights, strict=True)):
            if index == len(segment.claims) - 1:
                # The last claim absorbs the rounding, so the claims of a segment always
                # sum to exactly the segment's duration. Without this the drift is
                # invisible per segment and cumulative across an episode.
                duration = max(0, segment.duration_ms - offset)
            else:
                duration = round(segment.duration_ms * weight / total_weight)
            timings.append((claim.id, segment.start_ms + offset, duration))
            offset += duration
    return timings


def enqueue_smart_episode(
    conn: psycopg.Connection[Any],
    *,
    user_id: str,
    title: str,
    max_duration_ms: int,
    rule: SmartRule,
) -> str:
    """Create a rule-selected episode and queue its assembly.

    The rule is validated by the caller and stored as a snapshot here, so assembly reads
    a rule that was already known-good at creation time rather than discovering a bad one
    an hour later on a queue.
    """
    episode_id = repo.create_episode(
        conn,
        user_id=user_id,
        title=title,
        max_duration_ms=max_duration_ms,
        kind=EpisodeKind.SMART,
        rule=rule.to_json(),
    )
    enqueue(conn, Queue.ASSEMBLE, {"episode_id": episode_id})
    return episode_id


# --- shared --------------------------------------------------------------------------

#: Every queue the pipeline drains, one Cloud Run job each.
#:
#: ``poll`` and ``extract`` were named in :class:`Queue` from the start and had no handlers
#: in Phase 1; Gmail ingestion fills them in. X bookmarks would be a third source behind
#: the same two stages rather than a fourth queue.
HANDLERS = {
    Queue.POLL: handle_poll,
    Queue.EXTRACT: handle_extract,
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
        # `poll` and `extract` have no domain object to mark failed: a mailbox that could
        # not be reached has its error recorded on the source by the handler itself, and a
        # message that could not be fetched simply has no row yet.
        Queue.INTEGRATE: source_item_failed,
        Queue.ASSEMBLE: episode_failed,
        Queue.SCRIPT: episode_failed,
        Queue.TTS: episode_failed,
    }
