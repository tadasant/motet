"""Fixtures for the voice tests.

Nothing here touches a network, a vendor, or a database. The root ``conftest.py`` already
pins ``MOTET_INFERENCE_MODE=fake`` for the whole session (invariant 7); this file adds the
voice-specific settings so a test never depends on what happens to be in the developer's
environment.
"""

from __future__ import annotations

import pytest
from motet_voice.config import VoiceSettings
from motet_voice.harness import synthesize_walk


@pytest.fixture
def settings() -> VoiceSettings:
    """Deterministic settings: fake mode, no vendor keys, a fixed session secret."""
    return VoiceSettings.from_env(
        {
            "MOTET_INFERENCE_MODE": "fake",
            "MOTET_VOICE_SESSION_SECRET": "test-secret-not-a-real-one",
        }
    )


@pytest.fixture
def quiet_walk() -> bytes:
    """Twelve seconds of wind, traffic and footsteps, and nobody talking."""
    return synthesize_walk(duration_ms=12_000).pcm


@pytest.fixture
def spoken_walk() -> bytes:
    """The same conditions, with three clear utterances in it."""
    return synthesize_walk(duration_ms=12_000, speech_at_ms=(2_000, 6_000, 9_500)).pcm
