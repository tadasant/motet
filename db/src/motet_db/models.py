"""Row shapes, as frozen dataclasses.

These are the *persisted* model, deliberately distinct from the value types the inference
stages pass around in ``motet_inference.types``. The two look similar today and will not
stay that way: a stored news item carries read state and timestamps that no inference
stage should ever see, and a stage's ``SourceItem`` carries no user id because a stage
that could tell users apart is a stage that could leak between them.

``motet_db.repo`` converts between them at the boundary, which is the only place the
translation lives.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import Any


class EpisodeState(StrEnum):
    """One state per pipeline stage that can fail and be retried on its own.

    ``pending`` — created, waiting to be assembled from the backlog.
    ``scripting`` — assembled; script generation and grounding validation are next.
    ``rendering`` — grounded; TTS and upload are next.
    ``ready`` — audio is in object storage and the episode is in the feed.
    ``failed`` — a stage gave up. ``last_error`` says which and why.
    """

    PENDING = "pending"
    SCRIPTING = "scripting"
    RENDERING = "rendering"
    READY = "ready"
    FAILED = "failed"


class SourceItemState(StrEnum):
    PENDING = "pending"
    INTEGRATED = "integrated"
    FAILED = "failed"


@dataclass(frozen=True)
class StoredSourceItem:
    id: str
    user_id: str
    source_id: str
    title: str
    text: str
    state: SourceItemState
    created_at: datetime


@dataclass(frozen=True)
class IngestionStatus:
    """A pasted or polled item on its way to becoming a news item — and why it is not one.

    Assembled from two rows, because the answer genuinely lives in two places. The *source
    item* knows whether the pipeline gave up on it; the *job* knows how many attempts have
    been spent, when the next one is due, and what the last one said. A source item that is
    still being retried has no error of its own — ``last_error`` is only written when the
    retries run out — so a view built from ``source_items`` alone cannot tell "working on
    it" apart from "sitting there", which is exactly the thing a user cannot see.

    ``attempts`` is zero and ``next_attempt_at`` is ``None`` when there is no job row at
    all: nothing to report rather than nothing happening.

    **Sometimes there is no source item at all, and that is motet#35.** A polled message
    only becomes a ``source_items`` row once extraction *succeeds*, so a Gmail message
    that fails to extract has just the job row — and the job row is then the whole record
    that the message was ever seen. Such an entry carries a synthesized ``id`` and
    ``title`` (see :func:`~motet_db.repo.list_ingestion`); every other field means exactly
    what it means for a source item.

    ``source_kind`` is the ingestion route it arrived by. It is here because it decides
    what a person can *do* about a failure: a failed paste can be pasted again, and a
    failed mailbox message cannot — the poll cursor has already moved past it.
    """

    id: str
    title: str
    state: SourceItemState
    attempts: int
    next_attempt_at: datetime | None
    last_error: str | None
    created_at: datetime
    source_kind: str


@dataclass(frozen=True)
class StoredNewsItem:
    """A deduped story, with the read state invariant 5 puts here and nowhere else."""

    id: str
    user_id: str
    title: str
    summary: str
    read_at: datetime | None
    created_at: datetime
    source_item_ids: tuple[str, ...]

    @property
    def read(self) -> bool:
        return self.read_at is not None


@dataclass(frozen=True)
class StoredClaim:
    """A reported assertion beside the span of source text that evidences it.

    ``text`` is what gets spoken and may be a paraphrase; ``span_start``/``span_end`` are
    a half-open range into the cited source item's text, and that range is *verbatim*.
    Keeping the two separate is what lets narration read like prose while every sentence
    of it still resolves to something a human wrote.
    """

    id: str
    position: int
    text: str
    source_item_id: str
    span_start: int
    span_end: int
    #: Where this claim sits in the episode audio. Apportioned from the segment's measured
    #: duration by the TTS stage — see `motet_workers.handlers.apportion_claim_timings`.
    #: Zero until TTS has run, which is what the subtitle route checks.
    start_ms: int = 0
    duration_ms: int = 0


@dataclass(frozen=True)
class StoredSegment:
    id: str
    news_item_id: str
    position: int
    text: str
    start_ms: int
    duration_ms: int
    claims: tuple[StoredClaim, ...]


@dataclass(frozen=True)
class StoredEpisode:
    id: str
    user_id: str
    title: str
    state: EpisodeState
    #: How this episode chose its stories. ``MANUAL`` for every Phase 1 episode, which is
    #: why the column defaults rather than being backfilled.
    kind: EpisodeKind
    #: The rule snapshot a smart episode was built from, or ``None`` for a manual one.
    #: A snapshot rather than a reference: "why does this episode contain these stories"
    #: has to stay answerable after the rule is edited.
    rule: dict[str, Any] | None
    max_duration_ms: int
    duration_ms: int
    audio_key: str | None
    audio_bytes: int | None
    audio_media_type: str | None
    last_error: str | None
    #: How far the listener has actually got, in milliseconds. We own playback position
    #: (invariant 4) and this is written by our own API from a client's report — never
    #: read back out of a vendor SDK. Its only job is deciding which news items are read.
    listened_through_ms: int
    created_at: datetime
    published_at: datetime | None
    segments: tuple[StoredSegment, ...]

    @property
    def has_audio(self) -> bool:
        return self.state is EpisodeState.READY and self.audio_key is not None


@dataclass(frozen=True)
class StoredSource:
    """A place source items come from: pasted text, or a connected mailbox.

    ``config`` is the user's intent (which Gmail query to poll) and ``sync_state`` is our
    bookmark (where the last poll got to). Conflating the two would mean "change your
    Gmail query" silently re-ingested the archive.
    """

    id: str
    user_id: str
    kind: str
    name: str
    config: dict[str, Any]
    sync_state: dict[str, Any]
    active: bool
    last_polled_at: datetime | None
    last_error: str | None
    created_at: datetime


@dataclass(frozen=True)
class Highlight:
    """A saved passage, anchored to the span of source text it quotes.

    The anchor is ``(source_item_id, span_start, span_end)`` and nothing else. Claims are
    rewritten on every script retry and audio offsets move on every re-render, so neither
    can hold a highlight; ``source_items.text`` never changes, so it can. ``episode_id``
    and ``anchor_ms`` record where the listener was when they saved it — provenance, not
    the anchor. See migration 0003 for the full argument.
    """

    id: str
    user_id: str
    news_item_id: str
    source_item_id: str
    span_start: int
    span_end: int
    quote: str
    note: str | None
    episode_id: str | None
    anchor_ms: int | None
    created_at: datetime


class EpisodeKind(StrEnum):
    """How an episode chose its stories.

    ``MANUAL`` is Phase 1's "everything unread, oldest first, until the cap".
    ``SMART`` selected by a rule, a snapshot of which is stored on the episode.
    """

    MANUAL = "manual"
    SMART = "smart"


class SourceKind(StrEnum):
    PASTE = "paste"
    GMAIL = "gmail"


class CredentialPurpose(StrEnum):
    """Which half of an OAuth grant a sealed credential holds.

    ``REFRESH`` is the long-lived grant, issued once at consent. ``ACCESS`` is the
    short-lived token derived from it. Both are sealed; the distinction matters because
    overwriting a refresh token with the ``None`` a refresh returns is how an integration
    silently disconnects an hour after being connected.
    """

    REFRESH = "refresh"
    ACCESS = "access"
