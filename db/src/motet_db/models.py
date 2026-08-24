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
    max_duration_ms: int
    duration_ms: int
    audio_key: str | None
    audio_bytes: int | None
    audio_media_type: str | None
    last_error: str | None
    created_at: datetime
    published_at: datetime | None
    segments: tuple[StoredSegment, ...]

    @property
    def has_audio(self) -> bool:
        return self.state is EpisodeState.READY and self.audio_key is not None
