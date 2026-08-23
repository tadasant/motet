"""Value types passed between inference stages.

These are the *contract* between stages, not the persisted data model — the database
schema lives in ``motet_db``. Everything here is frozen so a stage cannot mutate its
input out from under the caller.
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class SourceItem:
    """One extracted document — a newsletter, a post, a pasted blob."""

    id: str
    title: str
    text: str


@dataclass(frozen=True)
class SourceSpan:
    """A half-open character range in a specific source item's ``text``.

    This is the unit that makes invariant 3 enforceable: a claim without a span that
    resolves to its source is a claim that never gets spoken.
    """

    source_item_id: str
    start: int
    end: int

    def resolve(self, sources: dict[str, SourceItem]) -> str | None:
        """Return the referenced text, or ``None`` if the span does not resolve."""
        item = sources.get(self.source_item_id)
        if item is None or self.start < 0 or self.end > len(item.text) or self.start >= self.end:
            return None
        return item.text[self.start : self.end]


@dataclass(frozen=True)
class NewsItem:
    """A deduped story, backed by one or more source items."""

    id: str
    title: str
    summary: str
    source_item_ids: tuple[str, ...]


@dataclass(frozen=True)
class Claim:
    """A single reported assertion, with the span it came from."""

    text: str
    span: SourceSpan


@dataclass(frozen=True)
class ScriptSegment:
    """The spoken copy for one news item."""

    news_item_id: str
    claims: tuple[Claim, ...]

    @property
    def text(self) -> str:
        return " ".join(claim.text for claim in self.claims)


@dataclass(frozen=True)
class Script:
    """A full briefing, ready for grounding validation and then TTS."""

    segments: tuple[ScriptSegment, ...]

    @property
    def text(self) -> str:
        return "\n\n".join(segment.text for segment in self.segments)


@dataclass(frozen=True)
class GroundingFailure:
    news_item_id: str
    claim_text: str
    reason: str


@dataclass(frozen=True)
class GroundingReport:
    """The verdict that gates TTS. ``ok`` false means nothing gets synthesized."""

    failures: tuple[GroundingFailure, ...] = field(default=())

    @property
    def ok(self) -> bool:
        return not self.failures


@dataclass(frozen=True)
class Audio:
    """Synthesized narration for one segment."""

    media_type: str
    data: bytes
    duration_ms: int
