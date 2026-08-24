"""Request and response models — the shapes that become the OpenAPI contract.

This is the seam between the API and every client. Changing a model here changes
``openapi.yaml`` and the generated TypeScript client, and CI fails if either is stale.

**Invariant 1 is why this file matters more than it looks like it should.** No client ever
speaks a vendor protocol; it speaks this. Which means a provider swap is a change to the
adapters and to nothing a client can see — and that only stays true if the vendor-shaped
details never leak into these models.
"""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, Field


class HealthResponse(BaseModel):
    """Liveness plus enough wiring detail to tell 'quiet' from 'unmonitored'."""

    status: str = Field(description="'ok' when the process is serving")
    service: str = Field(description="OTel service name this process reports as")
    telemetry_configured: bool = Field(
        description="Whether OTLP export is configured. False means telemetry is a no-op."
    )
    errors_configured: bool = Field(
        description="Whether error reporting is configured. False means errors go nowhere."
    )
    authenticated: bool = Field(
        description=(
            "Whether /v1 requires a bearer token. False means this deployment is open to "
            "anyone who can reach it — legitimate on a laptop, a mistake anywhere else."
        )
    )
    inference_mode: str = Field(
        description="'fake' or 'real'. 'fake' means no vendor is ever called."
    )


#: Roughly 50k words — far more than any newsletter, and small enough that one paste
#: cannot blow up a dedup prompt. The text goes into that prompt verbatim, and then into
#: every script prompt for the news item it becomes, so an unbounded field here is an
#: unbounded bill retried five times over.
MAX_PASTE_CHARS = 200_000

#: Six hours. Not a plausible episode length; a guard against an integer that would
#: overflow the `integer` column and surface as a 500 instead of a 422.
MAX_EPISODE_DURATION_MS = 6 * 60 * 60 * 1000


class PasteRequest(BaseModel):
    """A blob of text pasted in by hand — Phase 1's only ingestion route."""

    title: str = Field(min_length=1, max_length=500)
    text: str = Field(min_length=1, max_length=MAX_PASTE_CHARS)


class SourceItemResponse(BaseModel):
    id: str
    title: str
    state: str = Field(
        description="'pending' until a worker integrates it, then 'integrated' or 'failed'."
    )


class SourceSpanModel(BaseModel):
    """A half-open character range in a source item — what makes a claim checkable."""

    source_item_id: str
    start: int
    end: int


class NewsItemResponse(BaseModel):
    """A deduped story. Read state lives here, per invariant 5 — not per episode."""

    id: str
    title: str
    summary: str
    source_item_ids: list[str]
    read: bool
    created_at: datetime


class ReadStateRequest(BaseModel):
    """Mark one news item read or unread.

    A body rather than two endpoints, because "unread" is a real thing a user wants: the
    backlog is the product's memory, and being unable to put something back is worse than
    never having marked it.
    """

    read: bool


class ClaimModel(BaseModel):
    """A reported assertion beside the span it came from (invariant 3).

    ``text`` is what gets spoken and may paraphrase; ``source_excerpt`` is the source text
    the span actually covers, resolved server-side. Both are sent because the episode
    screen shows them side by side — that display *is* the trust surface, and a client
    that had to fetch the source separately to render it would sometimes not bother.
    """

    text: str
    span: SourceSpanModel
    source_excerpt: str
    source_title: str


class SegmentResponse(BaseModel):
    news_item_id: str
    news_item_title: str
    text: str
    start_ms: int = Field(
        description=(
            "Where this segment starts in the episode audio. We own playback position "
            "(invariant 4); this never comes from a player."
        )
    )
    duration_ms: int
    claims: list[ClaimModel]


class EpisodeResponse(BaseModel):
    id: str
    title: str
    state: str = Field(description="pending -> scripting -> rendering -> ready, or failed.")
    duration_ms: int
    max_duration_ms: int
    audio_bytes: int | None
    audio_media_type: str | None
    last_error: str | None
    created_at: datetime
    published_at: datetime | None
    segments: list[SegmentResponse]


class CreateEpisodeRequest(BaseModel):
    """Phase 1 has manual episodes only: 'all unread', capped by duration."""

    title: str = Field(min_length=1, max_length=500)
    max_duration_ms: int = Field(gt=0, le=MAX_EPISODE_DURATION_MS)


class MarkListenedResponse(BaseModel):
    """The result of "I listened to this" — read state, synced (invariant 5)."""

    episode_id: str
    news_items_marked_read: int


class FeedInfoResponse(BaseModel):
    """The private feed URL, ready to paste into a podcast client.

    The token is returned in full rather than masked. It has to be: a feed URL is copied
    to a new device months after it was minted, and a secret the owner cannot read back is
    one that forces a rotation — which unsubscribes every client already using it.
    """

    url: str
    token: str
