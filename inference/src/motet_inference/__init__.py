"""Inference stage interfaces, deterministic fakes, and vendor adapters.

Import stages through :func:`get_stages` rather than constructing an implementation
directly — that is what keeps invariant 7 true.
"""

from .audio import (
    MPEG_MEDIA_TYPE,
    WAV_MEDIA_TYPE,
    AudioError,
    estimate_duration_ms,
    join_audio,
)
from .cartesia import validate_tts_startup
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
    "Audio",
    "AudioError",
    "Briefing",
    "Claim",
    "GroundingFailure",
    "GroundingReport",
    "GroundingValidator",
    "IntegrationResult",
    "Integrator",
    "MODE_ENV_VAR",
    "MPEG_MEDIA_TYPE",
    "NewsItem",
    "Script",
    "ScriptGenerator",
    "ScriptSegment",
    "SourceItem",
    "SourceSpan",
    "SpeechSynthesizer",
    "Stages",
    "WAV_MEDIA_TYPE",
    "build_briefing",
    "current_mode",
    "estimate_duration_ms",
    "fake_stages",
    "first_sentence",
    "get_stages",
    "join_audio",
    "real_stages",
    "validate_tts_startup",
]
