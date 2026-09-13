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
class IntegrationResult:
    """What integrating one source item did to the news-item window.

    ``news_item`` is the item the source was folded into — either a brand new one or an
    existing one that grew. ``merged`` says which of those happened, so a caller can tell
    "deduped" from "new story" without diffing the window.
    """

    news_item: NewsItem
    merged: bool


@dataclass(frozen=True)
class TriageDecision:
    """PROTOTYPE — what triage decided about one source item.

    ``fetch`` means the item is only a preview and ``article_url`` is where the full
    article lives; ``raw`` means the text is the content and nothing more is needed.
    ``domain`` is the publisher's domain when the model could name it, normalized by the
    caller before it is matched against a connector.
    """

    decision: str
    article_url: str | None
    domain: str | None
    reason: str

    @property
    def fetch(self) -> bool:
        return self.decision == "fetch" and bool(self.article_url)


@runtime_checkable
class Triager(Protocol):
    """PROTOTYPE — decide whether a source item is content or a preview of an article."""

    def triage(self, item: SourceItem) -> TriageDecision: ...


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
    # PROTOTYPE — optional, and ``None`` means "no triage: every item is raw", so that
    # every existing ``Stages(...)`` construction and every test keeps its meaning.
    triager: Triager | None = None
