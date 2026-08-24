"""Pick the arm for this process — and build both when the harness wants a comparison.

``MOTET_VOICE_ARM`` selects the deployed arm; the sweep ignores it and builds every arm,
because comparing them is the whole point of the exercise.
"""

from __future__ import annotations

from collections.abc import Mapping

from ..config import COMPOSED_ARM, OPENAI_REALTIME_ARM, VoiceSettings
from .composed import build_composed_arm
from .interfaces import RealtimeArm
from .openai_realtime import build_openai_arm


def build_arm(settings: VoiceSettings, *, env: Mapping[str, str] | None = None) -> RealtimeArm:
    """The arm this process serves sessions with."""
    if settings.arm == OPENAI_REALTIME_ARM:
        return build_openai_arm(settings)
    return build_composed_arm(settings, env=env)


def build_all_arms(
    settings: VoiceSettings, *, env: Mapping[str, str] | None = None
) -> dict[str, RealtimeArm]:
    """Every arm, for a sweep. One walk, both arms, same audio — that is the design.

    A sweep never asks which arm is configured: a comparison that only ran the configured
    arm would answer a question nobody asked.
    """
    return {
        COMPOSED_ARM: build_composed_arm(settings, env=env),
        OPENAI_REALTIME_ARM: build_openai_arm(settings),
    }
