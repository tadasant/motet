"""Vendor adapters — the real implementations of each stage.

Scaffold only: every method raises. They exist so the seam is visible and so wiring a
vendor is a matter of filling one class in, not of inventing where it goes.

**Nothing here may be imported into a test.** The registry refuses to hand these out
unless ``MOTET_INFERENCE_MODE=real`` is set explicitly, which never happens in CI.

Claude covers dedup/integrate, script generation, and grounding; Cartesia Sonic covers
TTS. Credentials arrive from the environment, resolved by infrastructure that lives in
the private repo — never read a key from a file in this tree.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence

from .interfaces import IntegrationResult
from .types import Audio, GroundingReport, NewsItem, Script, SourceItem

_NOT_YET = "Real inference adapters are not part of the factory scaffold — see AGENTS.md."


class ClaudeIntegrator:
    """Dedup/integrate against the in-prompt window of news items."""

    def integrate(self, item: SourceItem, window: Sequence[NewsItem]) -> IntegrationResult:
        raise NotImplementedError(_NOT_YET)


class ClaudeScriptGenerator:
    """Generate briefing copy in which every claim cites a span."""

    def generate(self, news_items: Sequence[NewsItem], sources: Mapping[str, SourceItem]) -> Script:
        raise NotImplementedError(_NOT_YET)


class ClaudeGroundingValidator:
    """Judge whether each claim is supported by the span it cites, paraphrase included."""

    def validate(self, script: Script, sources: Mapping[str, SourceItem]) -> GroundingReport:
        raise NotImplementedError(_NOT_YET)


class CartesiaSpeechSynthesizer:
    """Cartesia Sonic — the narration voice (invariant: Sonic narrates, realtime converses)."""

    def synthesize(self, text: str) -> Audio:
        raise NotImplementedError(_NOT_YET)
