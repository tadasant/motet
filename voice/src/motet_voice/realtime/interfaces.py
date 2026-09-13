"""The provider seam: two arms, one interface, a fake for each — invariants 1 and 7.

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

from collections.abc import AsyncIterator, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, Protocol, runtime_checkable

from motet_inference.types import Audio

from ..bargein import BargeInPolicy, TurnDetector


class ArmDormant(RuntimeError):
    """This arm is implemented but cannot run: a credential it needs is not provisioned."""


@dataclass(frozen=True)
class ArmCapabilities:
    """What an arm can actually do in this process, and what it cannot.

    Surfaced on ``/internal/health`` and echoed in the ``StartSession`` response. The point is that
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
    #: **Is the replayable detector an emulation rather than the real thing?** Reported by
    #: the arm rather than inferred by the harness from some other flag, because getting it
    #: wrong is the worst failure this system has: a table that silently presents emulated
    #: numbers as a measurement of a vendor.
    turn_detection_emulated: bool = False
    dormant_reason: str = ""
    notes: str = ""

    def to_json(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "turn_detection": self.turn_detection,
            "conversational": self.conversational,
            "replayable": self.replayable,
            "turn_detection_emulated": self.turn_detection_emulated,
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
    #: Everything the session knows — passed in, never looked up. Invariant 2.
    context_notes: str = ""
    #: Where the listener interrupted, rendered by :mod:`motet_voice.position` from the
    #: session's own clock and the timed transcript the caller sent. Fresh per turn where
    #: ``context_notes`` is fixed for the session; empty when no transcript was given, so
    #: the harness and every existing test are prompted exactly as before.
    position_notes: str = ""
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


# -- the live, speech-to-speech path ------------------------------------------------------
#
# `respond` is a *turn*: text or audio in, one answer out, and the arm holds the floor for
# the duration. A realtime provider does not work that way — the listener's audio streams
# in, the provider decides when the utterance ended, and the reply streams back — so an arm
# that can do that exposes a second, optional shape: a `LiveConversation` per session. The
# events it yields are ours, not the vendor's, so `session.py` never sees a provider frame
# and the composed arm could one day yield the same stream from its own four legs.


@dataclass(frozen=True)
class SpeechStarted:
    """The provider heard the listener start talking. Milliseconds are the provider's own
    audio-buffer offset — evidence for the decision record, never a position (invariant 4)."""

    audio_start_ms: int


@dataclass(frozen=True)
class SpeechStopped:
    audio_end_ms: int


@dataclass(frozen=True)
class UserTranscript:
    """What the provider heard the listener say."""

    text: str


@dataclass(frozen=True)
class AssistantAudio:
    """One chunk of the reply, raw little-endian 16-bit mono PCM at ``sample_rate``.

    ``item_id`` is the provider's handle on the reply, opaque to us and never sent to the
    client. It is what :meth:`LiveConversation.truncate` names when the listener talks over
    the reply, so the provider's own history holds only what was actually heard.
    """

    pcm: bytes
    sample_rate: int
    item_id: str = ""


@dataclass(frozen=True)
class AssistantTranscript:
    """The reply's text, complete, as the provider *generated* it — which is not always what
    the listener heard. See :class:`~motet_voice.live.LiveBridge` for the cut-off case."""

    text: str
    item_id: str = ""


@dataclass(frozen=True)
class ToolCallRequested:
    call: PendingToolCall


@dataclass(frozen=True)
class TurnDone:
    """The provider finished a response. ``pending_tools`` says whether it is waiting on us."""

    pending_tools: bool = False
    #: The listener talked over the reply and the provider cut it off. Not the end of a
    #: turn from the listener's point of view — they are mid-question — so narration must
    #: not resume on it.
    cancelled: bool = False
    usage: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class ProviderError:
    message: str
    code: str = "provider_error"


LiveEvent = (
    SpeechStarted
    | SpeechStopped
    | UserTranscript
    | AssistantAudio
    | AssistantTranscript
    | ToolCallRequested
    | TurnDone
    | ProviderError
)


@runtime_checkable
class LiveConversation(Protocol):
    """One session's live channel to a provider: audio and context in, our events out."""

    async def start(self) -> None:
        """Open the channel and apply the session's persona, context and tools."""
        ...

    async def append_audio(self, pcm: bytes) -> None:
        """Listener audio, mono 16 kHz PCM. The arm resamples if the provider wants else."""
        ...

    async def add_context(self, text: str) -> None:
        """A fresh block of context for the next reply — the interruption position."""
        ...

    async def add_user_text(self, text: str) -> None:
        """A typed question. The arm asks for a reply to it."""
        ...

    async def tool_output(self, call_id: str, output: Mapping[str, Any]) -> None:
        """What a requested tool returned. The arm asks the provider to continue."""
        ...

    async def truncate(self, item_id: str, audio_end_ms: int) -> None:
        """The listener heard only the first ``audio_end_ms`` of reply ``item_id``.

        Called when the listener talks over a reply, so that the provider's next answer is
        built on what was actually said aloud rather than on text nobody heard.
        """
        ...

    async def cancel_response(self) -> None:
        """Stop whatever reply is in flight and discard any listener audio not yet answered.

        Called when narration resumes with the channel still engaged — "never mind" — which
        is where audio would otherwise keep flowing to a provider nobody is talking to.
        """
        ...

    def events(self) -> AsyncIterator[LiveEvent]: ...

    async def aclose(self) -> None: ...


@runtime_checkable
class LiveArm(Protocol):
    """An arm that can hold a live, speech-to-speech conversation for a session.

    Optional: the composed arm does not implement it, and a session on an arm that lacks it
    runs the turn-shaped path (`respond`) with a typed question — which is what it did before
    this existed.
    """

    def open_live(self, request: TurnRequest) -> LiveConversation:
        """A fresh channel for one session. ``request.history`` is non-empty when a session
        is reopening a channel that died, and the conversation carries it forward."""
        ...


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
