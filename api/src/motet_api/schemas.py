"""Request and response models — the shapes that become the OpenAPI contract.

This is the seam between the API and every client. Changing a model here changes
``openapi.yaml`` and the generated TypeScript client, and CI fails if either is stale.

Deliberately thin: Phase 1's job is to establish the contract, not to fill it in.
"""

from __future__ import annotations

from pydantic import BaseModel, Field


class HealthResponse(BaseModel):
    """Liveness plus enough telemetry wiring to tell 'quiet' from 'unmonitored'."""

    status: str = Field(description="'ok' when the process is serving")
    service: str = Field(description="OTel service name this process reports as")
    telemetry_configured: bool = Field(
        description="Whether OTLP export is configured. False means telemetry is a no-op."
    )
    errors_configured: bool = Field(
        description="Whether error reporting is configured. False means errors go nowhere."
    )


class PasteRequest(BaseModel):
    """A blob of text pasted in by hand — Phase 1's only ingestion route."""

    title: str = Field(min_length=1, max_length=500)
    text: str = Field(min_length=1)


class SourceItemResponse(BaseModel):
    id: str
    title: str


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


class ClaimModel(BaseModel):
    """A reported assertion beside the span it came from (invariant 3)."""

    text: str
    span: SourceSpanModel


class SegmentResponse(BaseModel):
    news_item_id: str
    claims: list[ClaimModel]


class EpisodeResponse(BaseModel):
    id: str
    title: str
    duration_ms: int
    segments: list[SegmentResponse]


class CreateEpisodeRequest(BaseModel):
    """Phase 1 has manual episodes only: 'all unread', capped by duration."""

    title: str = Field(min_length=1, max_length=500)
    max_duration_ms: int = Field(gt=0)
