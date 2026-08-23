"""Deterministic fakes for every inference stage.

These are what tests, CI, and local development run against — see invariant 7. They must
stay **deterministic** (same input, same output, no clock, no randomness, no network) and
**cheap**, because the golden set runs them on every CI run.

A fake is not a mock: each one implements the stage's contract honestly, just with a
trivial rule standing in for a model. ``FakeGroundingValidator`` in particular does the
real work — span resolution is not something a model decides — which is exactly why
grounding can be enforced in CI before any vendor is wired up.
"""

from __future__ import annotations

import hashlib
import re
import struct
from collections.abc import Mapping, Sequence

from .interfaces import IntegrationResult
from .types import (
    Audio,
    Claim,
    GroundingFailure,
    GroundingReport,
    NewsItem,
    Script,
    ScriptSegment,
    SourceItem,
    SourceSpan,
)

_WORD_RE = re.compile(r"[a-z0-9]+")

# Roughly conversational narration pace. Fixed, so durations stay deterministic.
_WORDS_PER_MINUTE = 150


def _dedup_key(title: str) -> str:
    """Normalize a title to the key the fake dedups on.

    Case, punctuation, and word order are all ignored, so "Acme raises $20M" and
    "$20M — ACME Raises!" collapse to the same story. Nothing cleverer: a fake that
    guessed at synonyms would be a worse model, not a better fake. Judging that two
    differently *worded* headlines are one story is the real adapter's job.
    """
    return " ".join(sorted(_WORD_RE.findall(title.lower())))


def _stable_id(prefix: str, seed: str) -> str:
    return f"{prefix}_{hashlib.sha256(seed.encode()).hexdigest()[:12]}"


def first_sentence_bounds(text: str) -> tuple[int, int]:
    """Half-open bounds of the first sentence of ``text``, terminator included."""
    match = re.search(r"[.!?](\s|$)", text)
    end = match.start() + 1 if match else len(text)
    return 0, end


def first_sentence(text: str) -> str:
    """The first sentence, without its terminator or surrounding whitespace.

    Shared with the golden-set harness, which derives a source item's title this way, so
    the harness and the fakes cannot disagree about where a sentence ends.
    """
    start, end = first_sentence_bounds(text)
    return text[start:end].strip().rstrip(".!?").strip()


def first_sentence_span(item: SourceItem) -> SourceSpan:
    """The span covering the source's first sentence.

    Used by the fake script generator as the thing a claim quotes. It is a real span into
    real text, so the grounding validator has something genuine to check.
    """
    start, end = first_sentence_bounds(item.text)
    return SourceSpan(source_item_id=item.id, start=start, end=end)


class FakeIntegrator:
    """Dedup by normalized title; merge into the existing news item when it matches."""

    def integrate(self, item: SourceItem, window: Sequence[NewsItem]) -> IntegrationResult:
        key = _dedup_key(item.title)
        for existing in window:
            if _dedup_key(existing.title) == key:
                merged = NewsItem(
                    id=existing.id,
                    title=existing.title,
                    summary=existing.summary,
                    source_item_ids=(*existing.source_item_ids, item.id),
                )
                return IntegrationResult(news_item=merged, merged=True)

        created = NewsItem(
            id=_stable_id("ni", key),
            title=item.title,
            summary=item.text[: item.text.find("\n")] if "\n" in item.text else item.text,
            source_item_ids=(item.id,),
        )
        return IntegrationResult(news_item=created, merged=False)


class FakeScriptGenerator:
    """Emit one claim per news item, quoting the first source verbatim.

    Quoting verbatim is the point: the resulting script passes grounding validation by
    construction, which lets the golden set assert the *contract* — every claim resolves —
    without a model in the loop. A news item whose sources are all missing produces no
    segment rather than an ungrounded one.
    """

    def generate(self, news_items: Sequence[NewsItem], sources: Mapping[str, SourceItem]) -> Script:
        segments: list[ScriptSegment] = []
        for news_item in news_items:
            source = next(
                (sources[sid] for sid in news_item.source_item_ids if sid in sources), None
            )
            if source is None:
                continue
            span = first_sentence_span(source)
            quoted = source.text[span.start : span.end]
            segments.append(
                ScriptSegment(
                    news_item_id=news_item.id,
                    claims=(Claim(text=quoted, span=span),),
                )
            )
        return Script(segments=tuple(segments))


class FakeGroundingValidator:
    """Check that each claim is exactly what its span says.

    Not a stand-in for a model — this *is* the check invariant 3 describes, and the real
    adapter differs only by also judging paraphrase. Keeping the strict version here means
    an ungrounded claim fails in CI, on a laptop, with no API key.
    """

    def validate(self, script: Script, sources: Mapping[str, SourceItem]) -> GroundingReport:
        failures: list[GroundingFailure] = []
        for segment in script.segments:
            for claim in segment.claims:
                resolved = claim.span.resolve(dict(sources))
                if resolved is None:
                    reason = "span does not resolve to any source text"
                elif resolved != claim.text:
                    reason = "claim text does not match the span it cites"
                else:
                    continue
                failures.append(
                    GroundingFailure(
                        news_item_id=segment.news_item_id,
                        claim_text=claim.text,
                        reason=reason,
                    )
                )
        return GroundingReport(failures=tuple(failures))


class FakeSpeechSynthesizer:
    """Return a silent WAV whose length tracks the text, so durations are meaningful.

    Silent rather than absent audio: downstream code that measures duration, uploads
    bytes, or builds an RSS enclosure gets something structurally real to work with.
    """

    _SAMPLE_RATE = 8000

    def synthesize(self, text: str) -> Audio:
        words = len(text.split())
        duration_ms = max(1, round(words / _WORDS_PER_MINUTE * 60_000))
        frames = int(self._SAMPLE_RATE * duration_ms / 1000)
        data = self._silent_wav(frames)
        return Audio(media_type="audio/wav", data=data, duration_ms=duration_ms)

    def _silent_wav(self, frames: int) -> bytes:
        byte_rate = self._SAMPLE_RATE * 2  # 16-bit mono
        payload = b"\x00\x00" * frames
        header = b"RIFF" + struct.pack("<I", 36 + len(payload)) + b"WAVE"
        header += b"fmt " + struct.pack("<IHHIIHH", 16, 1, 1, self._SAMPLE_RATE, byte_rate, 2, 16)
        header += b"data" + struct.pack("<I", len(payload))
        return header + payload
