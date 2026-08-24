"""Motet's voice service — the session contract, the provider arms, and the barge-in harness.

Three things live here, and the third is the reason the other two exist yet:

1. **The session contract** (:mod:`motet_voice.contract`, :mod:`motet_voice.app`) —
   ``StartSession(persona, tools, mcp_servers, context, turn_policy) -> session_token``,
   then a socket emitting transcripts, audio chunks, tool calls and ``interrupted_at``.
2. **Two provider arms behind one interface** (:mod:`motet_voice.realtime`) — a composed
   pipeline and OpenAI Realtime, swappable by configuration.
3. **The barge-in measurement harness** (:mod:`motet_voice.harness`) — the throwaway spike
   that settles the provider question with a number instead of an impression.

Two invariants govern the whole package and both are easy to violate:

* **It never touches the news DB.** No database credential, no schema knowledge, no
  ``motet-db`` dependency. It takes a session config and calls tools. If something here
  seems to need a query, the answer is a tool.
* **``spoken_through_ms`` is ours.** :class:`~motet_voice.clock.PlaybackClock` owns playback
  position; a provider's idea of where the listener is gets recorded as drift and ignored.
"""

from .bargein import BargeInDecision, BargeInPolicy, TurnDetector, VadTurnDetector
from .clock import PlaybackClock
from .config import ARMS, COMPOSED_ARM, OPENAI_REALTIME_ARM, VoiceSettings, load_settings
from .contract import (
    PLATFORM_TOOLS,
    InterruptedAtEvent,
    Persona,
    SessionContext,
    StartSessionRequest,
    StartSessionResponse,
    TurnPolicy,
)
from .session import VoiceSession
from .vad import EnergyVad, ScriptedVad, Vad, VadReading

__all__ = [
    "ARMS",
    "COMPOSED_ARM",
    "OPENAI_REALTIME_ARM",
    "PLATFORM_TOOLS",
    "BargeInDecision",
    "BargeInPolicy",
    "EnergyVad",
    "InterruptedAtEvent",
    "Persona",
    "PlaybackClock",
    "ScriptedVad",
    "SessionContext",
    "StartSessionRequest",
    "StartSessionResponse",
    "TurnDetector",
    "TurnPolicy",
    "Vad",
    "VadReading",
    "VadTurnDetector",
    "VoiceSession",
    "VoiceSettings",
    "load_settings",
]
