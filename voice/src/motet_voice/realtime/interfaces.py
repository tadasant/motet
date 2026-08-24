"""The provider seam: two arms, one interface, a fake for each — invariants 2 and 9.

**The whole barge-in spike reduces to this file.** The question Tadas walks outside to
settle is whether a hosted realtime model's server-side turn detection beats a composed
pipeline that runs VAD, STT, an LLM and TTS as separate legs. That is a question about two
implementations of one interface, and it is only answerable with data if swapping between
them is a config change rather than a rewrite.

The interface is split in two on purpose, because the two halves have very different
testability:

* :meth:`RealtimeArm.build_turn_detector` — **the measurable half.** Frames in, barge-in
  decisions out, no network, no credential, fully deterministic. This is what the offline
  harness replays a captured walk through, and it is why one recording can settle several
  config variants.
* :meth:`RealtimeArm.respond` — **the conversational half.** Needs a model. Present so the
  service is a service and not just a measuring stick, and faked end to end so nothing in
  CI reaches a vendor.

A live session is emphatically *not* required to produce the number the spike is about.
That separation is the single most useful decision in this module.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, Protocol, runtime_checkable

from motet_inference.types import Audio

from ..bargein import BargeInPolicy, TurnDetector


class ArmDormant(RuntimeError):
    """This arm is implemented but cannot run: a credential it needs is not provisioned."""


@dataclass(frozen=True)
class ArmCapabilities:
    """What an arm can actually do in this process, and what it cannot.

    Surfaced on ``/healthz`` and echoed in the ``StartSession`` response. The point is that
    "dormant" is something the system *says*, at boot, rather than something discovered
    when a listener asks a question into a silence.
    """

    name: str
    #: ``server`` — the provider decides turns inside its own socket. ``local`` — we do,
    #: with a VAD we can inspect. The axis the spike measures.
    turn_detection: str
    #: Can this arm hold a conversation right now?
    conversational: bool
    #: Can its turn detection be replayed offline, deterministically?
    replayable: bool
    dormant_reason: str = ""
    notes: str = ""

    def to_json(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "turn_detection": self.turn_detection,
            "conversational": self.conversational,
            "replayable": self.replayable,
            "dormant_reason": self.dormant_reason,
            "notes": self.notes,
        }


@dataclass(frozen=True)
class PendingToolCall:
    """A tool the model wants called, before anything has called it."""

    call_id: str
    name: str
    arguments: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class TurnRequest:
    """Everything an arm needs to answer one conversational turn.

    Carried as a value rather than as arguments so that adding something an arm needs does
    not change every arm's signature — and so that a turn can be logged verbatim, which is
    how a bad answer gets debugged after a walk.
    """

    persona_instructions: str
    voice: str
    #: The listener's audio for this turn, mono 16 kHz PCM. ``None`` when the client sent
    #: text, which is the path the harness and the tests use.
    user_pcm: bytes | None = None
    user_text: str | None = None
    #: Everything the session knows — passed in, never looked up. Invariant 3.
    context_notes: str = ""
    history: Sequence[Mapping[str, str]] = ()
    tools: Sequence[Mapping[str, Any]] = ()


@dataclass(frozen=True)
class AssistantTurn:
    """What the arm produced: what the listener said, what to say back, what to call."""

    text: str
    user_transcript: str = ""
    audio: Audio | None = None
    tool_calls: tuple[PendingToolCall, ...] = ()


@runtime_checkable
class RealtimeArm(Protocol):
    """One way of running a voice conversation, end to end."""

    @property
    def name(self) -> str: ...

    def capabilities(self) -> ArmCapabilities: ...

    def build_turn_detector(self, policy: BargeInPolicy) -> TurnDetector:
        """The measurable half. Must work with no credential and no network."""
        ...

    async def respond(self, request: TurnRequest) -> AssistantTurn: ...

    async def aclose(self) -> None: ...


@runtime_checkable
class SpeechRecognizer(Protocol):
    """The composed arm's STT leg."""

    @property
    def name(self) -> str: ...

    def transcribe(self, pcm: bytes) -> str: ...


@runtime_checkable
class ConversationModel(Protocol):
    """The composed arm's LLM leg — a text turn in, a text turn out."""

    @property
    def name(self) -> str: ...

    def reply(self, request: TurnRequest, user_text: str) -> str: ...
