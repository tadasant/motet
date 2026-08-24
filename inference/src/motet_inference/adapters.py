"""Vendor adapters — the real implementations of each stage.

Scaffold only: every method raises. They exist so the seam is visible and so wiring a
vendor is a matter of filling one class in, not of inventing where it goes.

**Nothing here may be imported into a test.** The registry refuses to hand these out
unless ``MOTET_INFERENCE_MODE=real`` is set explicitly, which never happens in CI.

Claude covers dedup/integrate, script generation, and grounding; Cartesia Sonic covers
TTS. Credentials arrive from the environment, resolved by infrastructure that lives in
the private repo — never read a key from a file in this tree.

The three text stages reach their model through ``motet_inference.llm``, which is built:
``build_client()`` plus ``build_request(cls.stage, ...)`` hands each class the model and
thinking depth configured for *its* stage. The ``stage`` attribute below is what ties a
class to that configuration, so filling one of these in never involves picking a model.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import ClassVar

from .interfaces import IntegrationResult
from .llm import LlmStage
from .types import Audio, GroundingReport, NewsItem, Script, SourceItem

_NOT_YET = "Real inference adapters are not part of the factory scaffold — see AGENTS.md."


class ClaudeIntegrator:
    """Dedup/integrate against the in-prompt window of news items."""

    stage: ClassVar[LlmStage] = LlmStage.DEDUP

    def integrate(self, item: SourceItem, window: Sequence[NewsItem]) -> IntegrationResult:
        raise NotImplementedError(_NOT_YET)


class ClaudeScriptGenerator:
    """Generate briefing copy in which every claim cites a span."""

    stage: ClassVar[LlmStage] = LlmStage.SCRIPT

    def generate(self, news_items: Sequence[NewsItem], sources: Mapping[str, SourceItem]) -> Script:
        raise NotImplementedError(_NOT_YET)


class ClaudeGroundingValidator:
    """Judge whether each claim is supported by the span it cites, paraphrase included."""

    stage: ClassVar[LlmStage] = LlmStage.GROUNDING

    def validate(self, script: Script, sources: Mapping[str, SourceItem]) -> GroundingReport:
        raise NotImplementedError(_NOT_YET)


class CartesiaSpeechSynthesizer:
    """Cartesia Sonic — the narration voice (invariant: Sonic narrates, realtime converses)."""

    def synthesize(self, text: str) -> Audio:
        raise NotImplementedError(_NOT_YET)
