"""The four inference stages, as Protocols.

**Invariant 7:** every inference stage sits behind an interface with a fake for tests.
Nothing in this repo may call a vendor directly — it calls one of these, and the registry
decides whether the implementation behind it talks to a model or to a deterministic fake.

Each Protocol is deliberately narrow. If a stage needs more context, widen the value types
in ``types.py`` rather than handing an implementation a database session or an HTTP client:
a stage that can reach the database is a stage the fake cannot stand in for.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Protocol, runtime_checkable

from .types import Audio, GroundingReport, NewsItem, Script, SourceItem


@dataclass(frozen=True)
class IntegrationResult:
    """What integrating one source item did to the news-item window.

    ``news_item`` is the item the source was folded into — either a brand new one or an
    existing one that grew. ``merged`` says which of those happened, so a caller can tell
    "deduped" from "new story" without diffing the window.
    """

    news_item: NewsItem
    merged: bool


@runtime_checkable
class Integrator(Protocol):
    """Dedup/integrate: fold one source item into the current window of news items.

    One call per source item, against all news items in the window passed in-prompt — a
    day of news is roughly 4.5k tokens, which is why there is no vector store.
    """

    def integrate(self, item: SourceItem, window: Sequence[NewsItem]) -> IntegrationResult: ...


@runtime_checkable
class ScriptGenerator(Protocol):
    """Turn news items into spoken copy, with every claim carrying its source span."""

    def generate(
        self, news_items: Sequence[NewsItem], sources: Mapping[str, SourceItem]
    ) -> Script: ...


@runtime_checkable
class GroundingValidator(Protocol):
    """Gate TTS on whether every claim resolves to what its span actually says.

    **Invariant 3.** This runs before synthesis, never after. A report with failures means
    nothing gets spoken.
    """

    def validate(self, script: Script, sources: Mapping[str, SourceItem]) -> GroundingReport: ...


@runtime_checkable
class SpeechSynthesizer(Protocol):
    """Render validated copy to audio."""

    def synthesize(self, text: str) -> Audio: ...


@dataclass(frozen=True)
class Stages:
    """The full set of inference stages, resolved together.

    Callers take this rather than four separate arguments so that a test cannot
    accidentally mix a fake script generator with a real validator.
    """

    integrator: Integrator
    script_generator: ScriptGenerator
    grounding_validator: GroundingValidator
    speech_synthesizer: SpeechSynthesizer
