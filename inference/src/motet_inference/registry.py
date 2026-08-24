"""Pick the inference implementations for the current process.

``fake`` is the default *everywhere*. Real adapters require an explicit
``MOTET_INFERENCE_MODE=real``, which only staging and production set — so a test or a
local script can never quietly start spending money, and a missing environment variable
fails toward the safe side rather than the expensive one.
"""

from __future__ import annotations

from .adapters import (
    CartesiaSpeechSynthesizer,
    ClaudeGroundingValidator,
    ClaudeIntegrator,
    ClaudeScriptGenerator,
)
from .fakes import (
    FakeGroundingValidator,
    FakeIntegrator,
    FakeScriptGenerator,
    FakeSpeechSynthesizer,
)
from .interfaces import Stages
from .mode import MODE_ENV_VAR, Mode, current_mode

__all__ = ["MODE_ENV_VAR", "Mode", "current_mode", "fake_stages", "get_stages", "real_stages"]


def fake_stages() -> Stages:
    return Stages(
        integrator=FakeIntegrator(),
        script_generator=FakeScriptGenerator(),
        grounding_validator=FakeGroundingValidator(),
        speech_synthesizer=FakeSpeechSynthesizer(),
    )


def real_stages() -> Stages:
    return Stages(
        integrator=ClaudeIntegrator(),
        script_generator=ClaudeScriptGenerator(),
        grounding_validator=ClaudeGroundingValidator(),
        speech_synthesizer=CartesiaSpeechSynthesizer(),
    )


def get_stages(mode: Mode | None = None) -> Stages:
    """Resolve all four stages together. Defaults to the environment, then to ``fake``."""
    return real_stages() if (mode or current_mode()) == "real" else fake_stages()
