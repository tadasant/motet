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
    telemetry_exporting: bool = Field(
        description=(
            "Whether this process actually installed an exporter, which is a different "
            "question from whether the variables were set. False with "
            "telemetry_configured true means the wiring is right and the SDK did not "
            "start — check the startup log."
        )
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


class PasteRequest(BaseModel):
    """A blob of text pasted in by hand — Phase 1's only ingestion route."""

    title: str = Field(min_length=1, max_length=500)
    text: str = Field(min_length=1)


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
    max_duration_ms: int = Field(gt=0)


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


# --- Phase 2: connected sources ------------------------------------------------------


class SourceResponse(BaseModel):
    """A place source items come from — pasted text, or a connected mailbox."""

    id: str
    kind: str = Field(description="'paste' or 'gmail'.")
    name: str
    active: bool = Field(
        description="False means connected but paused: it is not polled, and nothing is lost."
    )
    connected: bool = Field(
        description=(
            "Whether a credential is stored for this source. Answered without decrypting "
            "anything — only workers can do that (invariant 8)."
        )
    )
    scopes: list[str] = Field(
        description="OAuth scopes actually granted, which may be more than were asked for."
    )
    last_polled_at: datetime | None
    last_error: str | None
    created_at: datetime


class ConnectSourceRequest(BaseModel):
    """Begin connecting a mailbox. Returns a URL for the user to visit."""

    provider: str = Field(
        default="gmail", description="Only 'gmail' in Phase 2. X bookmarks are not built."
    )
    name: str = Field(default="Gmail", min_length=1, max_length=200)
    query: str | None = Field(
        default=None,
        description=(
            "The provider's own search syntax, deciding which messages are newsletters. "
            "Defaults to Gmail's updates and promotions categories, which need no setup."
        ),
    )
    redirect_uri: str = Field(
        min_length=1,
        description=(
            "Where the provider sends the user back to. Supplied by the client rather "
            "than configured, because the SPA, a local dev server, and a future iOS app "
            "each have a different one."
        ),
    )


class ConnectSourceResponse(BaseModel):
    """Where to send the user, and the source the grant will attach to."""

    source_id: str
    authorization_url: str
    state: str = Field(
        description=(
            "The CSRF token for this authorization. Returned so a client can verify the "
            "callback it receives is the one it started."
        )
    )


class OAuthCallbackRequest(BaseModel):
    """What the provider redirected back with."""

    state: str = Field(min_length=1)
    code: str = Field(min_length=1)


# --- Phase 2: smart episodes ---------------------------------------------------------


class SmartRuleModel(BaseModel):
    """Filter, window, duration, ranking — how a smart episode chooses its stories.

    Duration is deliberately absent: it is ``max_duration_ms`` on the episode itself. Two
    copies of a cap is one too many, and the stale one is the one somebody would trust.
    """

    unread_only: bool = Field(
        default=True, description="Skip stories already read. Off for a 'catch me up' rule."
    )
    source_ids: list[str] = Field(
        default_factory=list,
        description="Only stories backed by these sources. Empty means every source.",
    )
    window_days: int = Field(
        default=2,
        ge=0,
        le=30,
        description="How far back to reach. 0 means no window, which is what manual does.",
    )
    ranking: str = Field(
        default="oldest_first",
        description=(
            "oldest_first (drains a backlog), newest_first (a morning briefing), or "
            "coverage (most independently reported first). All three are computed from "
            "the rows — ranking with a model is Phase 3."
        ),
    )
    max_items: int = Field(default=100, ge=1, le=100)


class CreateSmartEpisodeRequest(BaseModel):
    """An episode whose stories are selected by a rule rather than by 'all unread'."""

    title: str = Field(min_length=1, max_length=500)
    max_duration_ms: int = Field(gt=0)
    rule: SmartRuleModel = Field(default_factory=SmartRuleModel)


# --- Phase 2: read state from the audio side -----------------------------------------


class ListenProgressRequest(BaseModel):
    """How far into an episode the listener has got.

    Invariant 4: we own playback position, so this is a *report* from a client that we
    record, never a value read back out of a vendor SDK. Invariant 5 is what it does: a
    story whose segment has been passed is marked read, which is the same fact the backlog
    screen's toggle writes.
    """

    listened_through_ms: int = Field(
        ge=0,
        description=(
            "Monotonic on the server: seeking backwards is reviewing, not un-listening, "
            "so a smaller value never lowers the recorded position or un-marks a story."
        ),
    )


class ListenProgressResponse(BaseModel):
    episode_id: str
    listened_through_ms: int = Field(description="The position after applying monotonicity.")
    news_items_marked_read: int


# --- Phase 2: highlights -------------------------------------------------------------


class HighlightResponse(BaseModel):
    """A saved passage, anchored to the span of source text it quotes.

    The anchor is the source span and nothing else — claims are rewritten on every script
    retry and audio offsets move on every re-render, while a source item's text never
    changes. ``episode_id`` and ``anchor_ms`` say where the listener was when they saved
    it: provenance, not the anchor.
    """

    id: str
    news_item_id: str
    source_item_id: str
    span: SourceSpanModel
    quote: str = Field(
        description=(
            "What the source actually says at that span, read from the source item rather "
            "than taken from the caller — so a model calling save_highlight cannot write "
            "its paraphrase in and have it look verbatim."
        )
    )
    note: str | None
    episode_id: str | None
    anchor_ms: int | None
    created_at: datetime


class SaveHighlightRequest(BaseModel):
    """Save a passage. The platform tool `save_highlight` posts exactly this."""

    news_item_id: str = Field(min_length=1)
    source_item_id: str = Field(min_length=1)
    span_start: int = Field(ge=0)
    span_end: int = Field(gt=0)
    note: str | None = Field(default=None, max_length=2000)
    episode_id: str | None = Field(
        default=None, description="Where the user was listening. Provenance, not the anchor."
    )
    anchor_ms: int | None = Field(default=None, ge=0)
