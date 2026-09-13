"""The three inference stages, as Protocols.

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

from .types import Audio, NewsItem, Script, SourceItem


@dataclass(frozen=True)
class DedupDecision:
    """Why an integrator decided what it did — the part of its answer worth keeping.

    ``relation`` is ``same_event``, ``related`` or ``unrelated``; ``candidate_id`` is the
    window item the integrator judged closest, as it named it (possibly an id that is not
    in the window, which is a model error worth being able to see later); ``reason`` is its
    one sentence. ``model`` is what answered — an OpenRouter slug, or ``"fake"``.
    ``second_look`` is ``None`` when no second look was asked for, and otherwise its
    answer, with every failure of the second look counting as ``False`` exactly as the
    decision itself counts it.

    Descriptive only: the handler persists it beside the outcome (motet#91) and decides
    nothing from it. ``merged`` on :class:`IntegrationResult` stays the decision.
    """

    relation: str
    reason: str
    candidate_id: str | None
    model: str
    second_look: bool | None = None


@dataclass(frozen=True)
class IntegrationResult:
    """What integrating one source item did to the news-item window.

    ``news_item`` is the item the source was folded into — either a brand new one or an
    existing one that grew. ``merged`` says which of those happened, so a caller can tell
    "deduped" from "new story" without diffing the window. ``decision`` says why, and is
    optional so that an integrator with nothing to explain — a test double — is still an
    integrator; the handler records its absence as "not recorded".
    """

    news_item: NewsItem
    merged: bool
    decision: DedupDecision | None = None


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
class SpeechSynthesizer(Protocol):
    """Render spoken copy to audio."""

    def synthesize(self, text: str) -> Audio: ...


@dataclass(frozen=True)
class Stages:
    """The full set of inference stages, resolved together.

    Callers take this rather than three separate arguments so that a test cannot
    accidentally mix a fake script generator with a real synthesizer.
    """

    integrator: Integrator
    script_generator: ScriptGenerator
    speech_synthesizer: SpeechSynthesizer
