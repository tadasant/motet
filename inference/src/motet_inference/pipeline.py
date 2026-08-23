"""Compose the stages into the narration path, up to (but not including) TTS.

`Dedup/Integrate → Assemble → Script + grounding`. Synthesis is deliberately *not* here:
invariant 3 says validation gates TTS, so the caller checks ``report.ok`` and only then
reaches for the synthesizer. Making that a separate step keeps the gate impossible to
skip by accident.

This is the composition the golden set exercises. It is intentionally thin — retries,
persistence, and queueing belong to the workers, not to the library.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass

from .interfaces import Stages
from .types import GroundingReport, NewsItem, Script, SourceItem


@dataclass(frozen=True)
class Briefing:
    """A scripted briefing plus the verdict on whether it may be spoken."""

    news_items: tuple[NewsItem, ...]
    script: Script
    grounding: GroundingReport

    @property
    def speakable(self) -> bool:
        return self.grounding.ok


def build_briefing(source_items: Iterable[SourceItem], stages: Stages) -> Briefing:
    """Run source items through dedup, scripting, and grounding validation.

    Source items are integrated one at a time against the growing window, which is what
    makes the result depend on ingestion order — and why invariant 6 serializes ingestion
    per user rather than fanning it out.
    """
    sources: dict[str, SourceItem] = {}
    window: list[NewsItem] = []

    for item in source_items:
        sources[item.id] = item
        result = stages.integrator.integrate(item, window)
        for index, existing in enumerate(window):
            if existing.id == result.news_item.id:
                window[index] = result.news_item
                break
        else:
            window.append(result.news_item)

    script = stages.script_generator.generate(window, sources)
    grounding = stages.grounding_validator.validate(script, sources)
    return Briefing(news_items=tuple(window), script=script, grounding=grounding)
