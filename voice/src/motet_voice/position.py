"""Where in the briefing the listener interrupted — derived from *our* clock, for the model.

The model used to be told everything about the episode and nothing about the moment. The
whole transcript arrived as static ``context_notes`` at session start, and the one number
that says what "that" refers to — ``spoken_through_ms``, frozen at the barge-in — went to
the *client* as ``interrupted_at`` and never reached the prompt. "What was that number?"
was therefore a question about the whole episode, and the model picked one.

This module closes that gap without touching either invariant that fences it:

* **Invariant 2** — the voice service looks nothing up. The caller sends the episode as a
  *timed* transcript in the session config (:class:`~motet_voice.contract.TimedSegment`),
  and this module only reads it.
* **Invariant 4** — the position is ours. The offset comes from
  :class:`~motet_voice.clock.PlaybackClock`, frozen at the interruption, and from nothing a
  provider said.

**The timings are apportioned, not measured** (AGENTS.md, "Claim timings are apportioned").
Segment boundaries are exact; a claim's place inside its segment is proportioned by
length. So "the last twenty seconds" is approximate at the edges, and a claim named as
"being said" may in truth have been the one just before or just after. That is fine for
context — a listener's "that" is rarely about a boundary — and the upgrade path, Cartesia's
own timestamps, is already recorded there.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any, Final

from .contract import TimedClaim, TimedSegment

#: How much narration before the offset is quoted back as "what was just said". Long enough
#: for a two-sentence claim and its predecessor; short enough not to crowd the persona.
RECENT_WINDOW_MS: Final = 25_000

#: A hard cap on the quoted window, in characters, because a claim can be long and the
#: block rides on every turn of a realtime session.
RECENT_MAX_CHARS: Final = 1_200


@dataclass(frozen=True)
class Position:
    """What was playing at one offset into the episode."""

    offset_ms: int
    segment: TimedSegment | None = None
    claim: TimedClaim | None = None
    #: Every claim whose end is at or before the offset, in narration order.
    heard: tuple[TimedClaim, ...] = ()
    #: Spoken text of the claims that overlap ``[offset - RECENT_WINDOW_MS, offset]``,
    #: oldest first — the material a "that" most likely refers to.
    recent: tuple[str, ...] = ()
    earlier_titles: tuple[str, ...] = ()
    later_titles: tuple[str, ...] = ()
    _all_titles: tuple[str, ...] = field(default=(), repr=False)

    @property
    def clock(self) -> str:
        return format_clock(self.offset_ms)

    def to_json(self) -> dict[str, Any]:
        """The client-facing summary carried on ``interrupted_at``."""
        if self.segment is None:
            return {}
        return {
            "clock": self.clock,
            "segment_title": self.segment.title,
            "claim_text": self.claim.spoken_text if self.claim is not None else "",
        }


def format_clock(offset_ms: int) -> str:
    seconds = max(0, offset_ms) // 1000
    return f"{seconds // 60}:{seconds % 60:02d}"


def locate(transcript: Sequence[TimedSegment], offset_ms: int) -> Position:
    """Find the segment and claim playing at ``offset_ms``.

    A segment is "playing" while ``start_ms <= offset < end_ms``; an offset exactly at a
    boundary belongs to the segment that is *starting*, because the clock is monotonic and
    the next word out of the speaker is that segment's. An offset past the last segment
    reports the last segment as current, since a listener who interrupts in the closing
    silence is still talking about the story that just ended.
    """
    if not transcript:
        return Position(offset_ms=offset_ms)

    ordered = sorted(transcript, key=lambda seg: seg.start_ms)
    current: TimedSegment | None = None
    for segment in ordered:
        if segment.start_ms <= offset_ms < max(segment.end_ms, segment.start_ms + 1):
            current = segment
            break
    if current is None:
        # Before the first segment, or after the last: attach to whichever is nearest in
        # narration order. Before the first is "nothing heard yet", which the heard/later
        # lists say on their own.
        current = ordered[-1] if offset_ms >= ordered[-1].end_ms else ordered[0]

    claim: TimedClaim | None = None
    for candidate in current.claims:
        if candidate.start_ms <= offset_ms < max(candidate.end_ms, candidate.start_ms + 1):
            claim = candidate
            break
    if claim is None and current.claims and offset_ms >= current.end_ms:
        claim = current.claims[-1]

    heard = tuple(
        c for segment in ordered for c in segment.claims if c.end_ms <= offset_ms and c.spoken_text
    )
    window_start = offset_ms - RECENT_WINDOW_MS
    recent = tuple(
        c.spoken_text
        for segment in ordered
        for c in segment.claims
        if c.spoken_text and c.start_ms < offset_ms and c.end_ms > window_start
    )
    if claim is not None and claim.spoken_text and claim.spoken_text not in recent:
        recent = (*recent, claim.spoken_text)

    index = ordered.index(current)
    earlier = tuple(seg.title for seg in ordered[:index] if seg.title)
    later = tuple(seg.title for seg in ordered[index + 1 :] if seg.title)
    return Position(
        offset_ms=offset_ms,
        segment=current,
        claim=claim,
        heard=heard,
        recent=recent,
        earlier_titles=earlier,
        later_titles=later,
        _all_titles=tuple(seg.title for seg in ordered),
    )


def position_notes(transcript: Sequence[TimedSegment], offset_ms: int) -> str:
    """The block a turn is handed so that "that" means what was just said.

    Empty when the caller sent no transcript, so a session that never had one is prompted
    exactly as before — the harness and the tests see no change.
    """
    position = locate(transcript, offset_ms)
    if position.segment is None:
        return ""

    parts = [
        f"The listener interrupted the briefing at {position.clock}, during the story "
        f"'{position.segment.title}'"
    ]
    if position.claim is not None and position.claim.spoken_text:
        parts[0] += f", while this was being said: '{position.claim.spoken_text}'."
    else:
        parts[0] += "."

    recent = _bounded(position.recent, RECENT_MAX_CHARS)
    if recent:
        parts.append(f"Narration in the last ~{RECENT_WINDOW_MS // 1000} seconds: '{recent}'")
    parts.append(
        "Earlier stories already heard: "
        + (", ".join(position.earlier_titles) if position.earlier_titles else "none yet")
        + "."
    )
    parts.append(
        "Stories not yet reached: "
        + (", ".join(position.later_titles) if position.later_titles else "none — this is the last")
        + "."
    )
    parts.append(
        "When they say 'that', 'this', 'they', or 'what was that number', assume they mean "
        "what was just said."
    )
    return " ".join(parts)


def _bounded(pieces: Sequence[str], max_chars: int) -> str:
    """Join oldest-first, keeping the *newest* text when the cap bites."""
    kept: list[str] = []
    total = 0
    for piece in reversed(pieces):
        if kept and total + len(piece) + 1 > max_chars:
            break
        kept.append(piece)
        total += len(piece) + 1
    return " ".join(reversed(kept))
