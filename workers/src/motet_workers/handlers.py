"""What each pipeline stage actually does.

``Paste-in → Integrate → Assemble → Script → TTS → object storage``.

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
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

import psycopg
from motet_db import EpisodeKind, EpisodeState, RuleError, SmartRule, SourceItemState, phase2, repo
from motet_db.models import StoredNewsItem, StoredSegment, StoredSourceItem
from motet_inference import (
    MPEG_MEDIA_TYPE,
    WAV_MEDIA_TYPE,
    Audio,
    IntegrationResult,
    NewsItem,
    SourceItem,
    Stages,
    collect_usage,
    estimate_duration_ms,
    join_audio,
    record_tts_characters,
)
from motet_sources.extract import find_links
from motet_storage import ObjectStore, episode_audio_key

from . import enrich, labels
from .ingest import handle_extract, handle_poll, record_poll_failure
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

    ``after_commit`` is the one addition, and it exists for one caller: a step that must
    happen only once the handler's work has durably landed, and must never be able to undo
    it — motet#96's label write-back, which moves a Gmail message after its item is
    integrated. ``loop.drain`` runs what a handler appends here after the work *and* the
    job's completion have committed and the job's serialization lock is released, once,
    swallowing anything it raises. It is discarded unrun when the handler fails. It is not
    a retry mechanism, not persisted, and not a queue: a worker that dies before it runs
    simply does not run it, and a caller that needs more than that needs a job instead.
    """

    conn: psycopg.Connection[Any]
    stages: Stages
    store: ObjectStore
    after_commit: list[Callable[[], object]] = field(default_factory=list)
    #: The seam to ``motet-enrich``, when this deployment has one (motet#102). Optional for
    #: the same reason ``stages`` and ``store`` are optional arguments to ``drain``: a
    #: one-shot drain and every test that builds a Context need to know none of it, and a
    #: worker with no client queues integrate directly — which is what every deployment did
    #: before enrichment shipped. Built once per process rather than per job so that a
    #: missing ``google-auth`` is a startup line rather than a swallowed exception inside
    #: the first enrichment, which is the ``motet-vault[kms]`` lesson AGENTS.md draws.
    enrich_client: object | None = None


# --- integrate -----------------------------------------------------------------------


def handle_integrate(context: Context, payload: Mapping[str, Any]) -> None:
    """Fold one pasted source item into the user's news items.

    Runs under the user's serialization key (invariant 6), so this is the only ingestion
    touching this user's window right now — which is what makes "read the window, decide,
    write the result" safe without any further locking.

    A payload carrying ``labels.DELIBERATE_KEY`` is the owner's own ingest, and for a Gmail
    source with label sync set it also moves the message between labels — after this
    transaction commits, never inside it. See :mod:`motet_workers.labels`.
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
    if stored.state is SourceItemState.DISMISSED:
        # Only a held item — one with no integrate job — can be dismissed, and the dismiss
        # and the claim share a lock, so this should be unreachable. If it is reached, the
        # person said not to spend on this item, and that is the answer that stands.
        logger.warning("source item %s was dismissed; not integrating it", source_item_id)
        return

    window = repo.news_item_window(context.conn, stored.user_id)
    # Dedup is the volume stage — one completion per source item, with the whole window
    # in the prompt — so it is where per-item cost is worth attributing and where a
    # `cache_read=0` says the largest cost lever in the system is not engaging.
    with collect_usage() as spend:
        result = context.stages.integrator.integrate(
            _as_source_item(stored), [_as_news_item(item) for item in window]
        )
    if spend.requests:
        logger.info(
            "source item %s cost %d completion(s): %s",
            stored.id,
            spend.requests,
            spend.summary(),
        )

    merge_into, title, summary = _merge_target(result, window)
    if merge_into is not None:
        repo.merge_source_into_news_item(
            context.conn,
            news_item_id_=merge_into,
            source_item_id_=stored.id,
            title=title,
            summary=summary,
        )
        logger.info("merged source %s into news item %s", stored.id, merge_into)
    else:
        # The id the integrator proposed is discarded: primary keys are the database's to
        # assign, and a stage that could choose them could collide with an existing row.
        news_item_id = repo.insert_news_item(
            context.conn,
            user_id=stored.user_id,
            title=title,
            summary=summary,
            source_item_id_=stored.id,
        )
        logger.info("source %s became new news item %s", stored.id, news_item_id)

    repo.record_dedup_decision(
        context.conn,
        stored.id,
        _decision_record(
            result,
            backstop=merge_into is not None and not result.merged,
            title=title,
            summary=summary,
        ),
    )
    repo.mark_source_item(context.conn, stored.id, SourceItemState.INTEGRATED)
    # Registered, not run: the write-back moves a real message in a real mailbox, so it
    # waits for this transaction to commit, and it is only registered at all for a
    # deliberate ingest from a Gmail source with label sync set (motet#96).
    labels.schedule(context, stored, dict(payload))


def _decision_record(
    result: IntegrationResult, *, backstop: bool, title: str, summary: str
) -> repo.DedupDecisionRecord:
    """What to persist about this decision, beside the link row it produced (motet#91).

    ``basis`` is the step the outcome rests on, which is not always the integrator's: a
    "new story" that :func:`_merge_target`'s title backstop turned into a merge was never
    the model's call, and a stored ``relation`` of ``unrelated`` beside a merge would
    otherwise read as dedup contradicting itself. ``title`` and ``summary`` are what was
    written to the news item by this decision — not the integrator's proposal, which the
    second look and the backstop both deliberately discard.

    An integrator that returned no decision still gets a basis and the copy; the relation,
    reason, candidate and model are NULL, which the lifecycle view reports as not recorded.
    """
    decision = result.decision
    if backstop:
        basis = "title_backstop"
    elif decision is not None and decision.second_look is not None:
        basis = "second_look"
    else:
        basis = "first_pass"
    return repo.DedupDecisionRecord(
        relation=decision.relation if decision is not None else None,
        reason=(decision.reason or None) if decision is not None else None,
        candidate_id=decision.candidate_id if decision is not None else None,
        model=decision.model if decision is not None else None,
        basis=basis,
        title=title,
        summary=summary,
    )


def _merge_target(
    result: IntegrationResult, window: Sequence[StoredNewsItem]
) -> tuple[str | None, str, str]:
    """Which news item this source item joins, if any — and the title and summary to store.

    The model's decision, plus one deterministic backstop: **a "new story" whose title is
    already in the window is a merge.** That is motet#41, where three write-ups of one
    story were pasted, two merged, and the third came back as a separate news item under a
    byte-identical headline — so the backlog listed the same sentence twice and an episode
    would have read the story out twice under one heading.

    It is a backstop rather than the fix. What is *not* a judgement call is this: dedup
    writes the titles, so two items carrying the same one is dedup contradicting itself.
    Whether the *threshold* holds up on genuinely independent prose about one event is a
    different question, and it is answered a layer down rather than here — see
    ``ClaudeIntegrator._is_same_event``, where an answer the first pass is unsure about
    buys one focused pairwise re-ask. This rule stays because it is the one that needs no
    model at all: it holds for any ``Integrator``, including the fakes.

    **Unread items only, and that bound is what keeps the cost argument true.** The window
    also carries anything *read* within ``WINDOW_DAYS``, and a backstop merge into one of
    those would fold a fresh story into something the listener has already heard — where
    assembly, which selects unread items, will never speak it, leaving a log line as its
    only trace and a re-paste hitting the same rule rather than undoing it. The
    model-driven merge may still do that, and always could: it is what the window is *for*
    (see ``repo.news_item_window`` — a follow-up should absorb into this morning's story
    rather than reappear), and there it is a judgement about those two texts. A string
    match is not that judgement, so it does not get that reach. Against an *unread* twin
    the trade is the one motet#41 describes: two unrelated events producing byte-identical
    headlines inside one window, against the same story read aloud twice under one
    heading, which is the failure dedup exists to prevent.

    An empty proposed title matches nothing, deliberately. ``_normalize_title`` collapses
    whitespace, so a blank title and a whitespace-only one are the same string — and two
    items that both failed to get a title are not evidence of anything.

    Compared on a normalized title so that trailing whitespace or a capitalisation the
    model varied does not defeat it, and no further: fuzzy matching here would be a
    similarity threshold of its own, in the one place that is meant to have no opinion.
    Only the *stored* title and summary travel with the merge — the model was answering a
    different question and never wrote a combined summary for these two.
    """
    if result.merged:
        return result.news_item.id, result.news_item.title, result.news_item.summary

    proposed = _normalize_title(result.news_item.title)
    twin = (
        next(
            (
                item
                for item in window
                if item.read_at is None and _normalize_title(item.title) == proposed
            ),
            None,
        )
        if proposed
        else None
    )
    if twin is None:
        return None, result.news_item.title, result.news_item.summary

    logger.warning(
        "dedup returned a NEW story titled %r, which news item %s already carries; "
        "merging instead (motet#41)",
        result.news_item.title,
        twin.id,
    )
    return twin.id, twin.title, twin.summary


def _normalize_title(title: str) -> str:
    """Case, and runs of whitespace, and nothing else. See :func:`_merge_target`."""
    return " ".join(title.split()).casefold()


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


# --- script ---------------------------------------------------------------------------


#: The episode states that mean the script stage has already run to completion.
#:
#: ``handle_script`` writes ``rendering`` and enqueues the TTS job in the same
#: transaction, so an episode at ``rendering`` or beyond has already been scripted and
#: handed on — and the *only* thing a re-run can add is a second copy of everything the
#: module docstring promises there will never be a second copy of: another billed script
#: completion, a ``replace_segments`` racing whatever TTS is reading, and a second TTS job
#: for an episode that already has one. ``ready`` alone was not enough, because
#: ``rendering`` is precisely the state this handler itself writes: a job whose worker died
#: between the work commit and ``jobs.complete`` stays ``running`` with the work durably
#: applied, and ``jobs.STALE_LEASE_SECONDS`` makes it claimable again. That reclaim is the
#: intended recovery; re-executing a finished stage is not. (motet#50)
#:
#: **``pending`` and ``failed`` are deliberately absent, because neither is past this
#: stage.** ``pending`` means assembly never ran, which raises below rather than
#: returning quietly — a silent return there would hide a real bug. ``failed`` means a
#: stage gave up, and a re-scripted failed episode is a *re-script somebody asked for*:
#: short-circuiting it would strand the episode in ``failed`` with no TTS job and nothing
#: to say so, which is the quiet direction of this same bug.
#:
#: ``failed`` is not free of the problem above, and it is not this guard that closes it. A
#: *stale* script job can outlive its own episode's failure — its row sits ``running``
#: while the TTS job downstream exhausts its retries and marks the episode ``failed`` — so
#: a reclaim could put a failed episode back through the full stage, re-billing it and
#: clearing the ``last_error`` that said what went wrong. That is motet#55, and it is
#: closed one layer down, on the job row: the handler's own transaction records
#: ``jobs.work_committed_attempt``, and a claim that finds it set completes the job without
#: calling a handler at all. The fence is there rather than here precisely because *here*
#: the two are indistinguishable — a replay and a re-script somebody asked for both arrive
#: as a ``script`` job against a ``failed`` episode, and only the job row knows that one of
#: them is a different row.
SCRIPTED_STATES = frozenset({EpisodeState.RENDERING, EpisodeState.READY})


def handle_script(context: Context, payload: Mapping[str, Any]) -> None:
    """Write the briefing, store it as segments and claims, and hand it to TTS.

    **Every claim carries the source span its evidence was copied from** — invariant 3 —
    and the script adapter has already discarded any claim whose quote it could not locate
    verbatim in a source, so what is written here cites real text. Nothing validates the
    spoken sentence against that span: the grounding gate that used to sit between this
    stage and TTS was removed in motet#75, deliberately and with the risk stated there.

    An episode already in :data:`SCRIPTED_STATES` returns before any of that. This is a
    stage that has completed, not one that was skipped.
    """
    episode_id = _require(payload, "episode_id")
    episode = repo.get_episode(context.conn, episode_id)
    if episode is None:
        raise PermanentFailure(f"episode {episode_id} no longer exists")
    if episode.state in SCRIPTED_STATES:
        logger.info(
            "episode %s is already scripted (%s); nothing to do",
            episode_id,
            episode.state.value,
        )
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

    # Inside a block so that the question an operator asks — "what did this episode
    # cost" — has an answer with the episode id in it. The per-stage split is on the
    # metric, which is where a split belongs.
    with collect_usage() as spend:
        script = context.stages.script_generator.generate(stage_items, stage_sources)
    if spend.requests:
        # The episode id is the whole point of this line. It is what a metric must not
        # carry and what "what did that episode cost" cannot be answered without.
        logger.info(
            "episode %s scripting cost %d completion(s): %s",
            episode_id,
            spend.requests,
            spend.summary(),
        )

    if not script.segments:
        raise PermanentFailure("the script stage produced no usable segments")

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
            for segment in script.segments
        ],
        episode.max_duration_ms,
        episode_id,
    )
    repo.replace_segments(context.conn, episode_id, specs)
    repo.set_episode_state(context.conn, episode_id, EpisodeState.RENDERING)
    enqueue(context.conn, Queue.TTS, {"episode_id": episode_id})
    logger.info(
        "episode %s scripted: %d segments, %d claims",
        episode_id,
        len(specs),
        sum(len(spec.claims) for spec in specs),
    )


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

    What arrives is exactly the copy the script stage wrote and the duration cap kept:
    this stage reads segments out of the database rather than re-deriving anything, so
    nothing spoken here differs from what the episode screen shows.
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
    # Cartesia bills per character and the adapter sends the string it is handed
    # unchanged, so counting here counts what is billed. Without it an episode's audio
    # cost was recoverable only from a vendor dashboard, by timestamp.
    characters = 0
    for segment in episode.segments:
        if not segment.text.strip():
            raise PermanentFailure(f"segment {segment.id} has no text to speak")
        characters += len(segment.text)
        rendered.append(context.stages.speech_synthesizer.synthesize(segment.text))
    record_tts_characters(characters)

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
        "episode %s published: %d segments, %d ms, %d bytes, %d characters synthesized at %s",
        episode_id,
        len(rendered),
        total_ms,
        len(audio.data),
        characters,
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
    Queue.ENRICH: enrich.handle_enrich,
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

    **A paste goes straight to integrate, never to enrichment**, and that is the same line
    motet#91 drew: pasting *is* asking, so it queues its job here rather than waiting for a
    decision. Its links are still recorded, because the lifecycle view shows them and
    because a future "enrich this paste" would need them and cannot recover them later.
    """
    stored = repo.insert_source_item(
        conn, user_id=user_id, title=title, text=text, links=find_links(text)
    )
    enqueue(conn, Queue.INTEGRATE, {"source_item_id": stored.id}, serialize_key=user_id)
    return stored


def enqueue_integration(
    conn: psycopg.Connection[Any], *, user_id: str, source_item_ids: Sequence[str]
) -> list[str]:
    """Queue held source items for integration — the owner saying "ingest now".

    The API calls this. A connected source's ``handle_extract`` deliberately stops short
    of this stage (``motet_workers.ingest`` says why), so this is the *only* way a polled
    item reaches dedup. Each id is checked to be the caller's, ``pending`` and without an
    integrate job before a job is written for it; the rest are dropped without comment,
    and the ids actually queued come back so the caller can count both. Same queue and same
    serialization key as a paste — invariant 6 lives on this stage.

    **The payload also carries** :data:`labels.DELIBERATE_KEY`, and that flag is the whole
    trigger for label sync (motet#96): a Gmail source with labels set moves the message once
    the item is in. This is the only writer of it, so nothing automatic — a paste, a stale
    job queued before the ingest gate, a future "always ingest from this sender" — can reach
    a mailbox.

    **Some items go to the ``enrich`` queue instead** (motet#102): an item whose links reach
    a site the owner has added is fetched in full by an agent first, and the integrate job
    is written by :func:`motet_workers.enrich.handle_enrich` when that is finished. The rule
    is deterministic and costs nothing — see :func:`motet_workers.enrich.plan_enrichment` —
    and a deployment with ``MOTET_ENRICH`` unset never takes that branch at all.
    """
    claimed = repo.claim_held_source_items(conn, user_id, source_item_ids)
    config = enrich.load_config()
    # Once for the whole request: "select all" sends up to `repo.HELD_MAX_ITEMS` ids, and
    # the sites are the same answer for every one of them.
    sites = enrich.enrichment_sites(conn, user_id=user_id, config=config)
    for item_id in claimed:
        target = enrich.plan_enrichment(
            conn, user_id=user_id, item_id=item_id, config=config, sites=sites
        )
        if target is None:
            enqueue_integrate_job(conn, user_id=user_id, item_id=item_id, deliberate=True)
        else:
            enrich.enqueue_enrichment(
                conn,
                user_id=user_id,
                item_id=item_id,
                target=target,
                extra={labels.DELIBERATE_KEY: True},
            )
    return claimed


def enqueue_integrate_job(
    conn: psycopg.Connection[Any],
    *,
    user_id: str,
    item_id: str,
    payload: Mapping[str, Any] | None = None,
    deliberate: bool = False,
    enriched: bool = False,
) -> None:
    """Write one integrate job, carrying the two flags that survive the enrichment detour.

    The **only** writer of an integrate job for a held item, whether enrichment ran or not,
    so the payload a job carries is decided in one place. Two flags travel on it and both
    matter:

    * ``labels.DELIBERATE_KEY`` is motet#96's label write-back trigger. An item that went
      through enrichment was still the owner's own "ingest now", so the flag has to survive
      the detour — dropping it would silently stop the `Newsletters → Completed` move for
      exactly the items the owner cared most about.
    * ``enrich.ENRICHED_KEY`` says the agent has had its turn. Nothing downstream reads it
      today — ``handle_integrate`` does not branch on it, because by then the article is
      simply the item's text — and it is on the payload because a *replayed* job is the one
      case where "has this already been enriched" cannot be read off anything else.
    """
    body: dict[str, Any] = {"source_item_id": item_id}
    if deliberate or (payload is not None and payload.get(labels.DELIBERATE_KEY)):
        body[labels.DELIBERATE_KEY] = True
    if enriched:
        body[enrich.ENRICHED_KEY] = True
    enqueue(conn, Queue.INTEGRATE, body, serialize_key=user_id)


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
        # `extract` has no domain object to mark failed: a message that could not be
        # fetched has no row to mark — extraction is what writes one. `poll` does have
        # one, the source, and a poll that gave up puts its reason there: on
        # `sources.last_error` and on the source's last-sync result, which is what the
        # Sources screen reads (motet#94). The handler cannot write that itself, because
        # the transaction it would write it in is the one that rolled back.
        #
        # **That is not the same as leaving the failure unreported, and for a while it
        # was** (motet#35). The failed job row is the only record that the message was
        # ever seen, and the poll cursor has already moved past it, so nothing else will
        # ever look at it again. `repo.list_ingestion` reads those rows directly rather
        # than a recorder writing a stand-in row here: a `source_items` row invented at
        # this point would have no text, would sit in the table that anchors every
        # highlight and every claim, and would be indistinguishable from a message that
        # arrived empty.
        Queue.POLL: record_poll_failure,
        # `enrich` does NOT mark the item failed, and that is the difference worth reading
        # here: an agent that could not be reached costs the article, never the item. The
        # recorder writes the enrichment outcome and queues integrate, so the newsletter's
        # preview reaches the briefing exactly as it would have without enrichment.
        Queue.ENRICH: enrich.record_enrich_failure,
        Queue.INTEGRATE: source_item_failed,
        Queue.ASSEMBLE: episode_failed,
        Queue.SCRIPT: episode_failed,
        Queue.TTS: episode_failed,
    }
