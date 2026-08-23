"""Inference stage interfaces, deterministic fakes, and vendor adapters.

Import stages through :func:`get_stages` rather than constructing an implementation
directly — that is what keeps invariant 7 true.
"""

from .fakes import first_sentence
from .interfaces import (
    GroundingValidator,
    IntegrationResult,
    Integrator,
    ScriptGenerator,
    SpeechSynthesizer,
    Stages,
)
from .pipeline import Briefing, build_briefing
from .registry import MODE_ENV_VAR, current_mode, fake_stages, get_stages, real_stages
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

__all__ = [
    "MODE_ENV_VAR",
    "Audio",
    "Briefing",
    "Claim",
    "GroundingFailure",
    "GroundingReport",
    "GroundingValidator",
    "IntegrationResult",
    "Integrator",
    "NewsItem",
    "Script",
    "ScriptGenerator",
    "ScriptSegment",
    "SourceItem",
    "SourceSpan",
    "SpeechSynthesizer",
    "Stages",
    "build_briefing",
    "current_mode",
    "first_sentence",
    "fake_stages",
    "get_stages",
    "real_stages",
]
