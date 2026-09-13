"""The session's half of a live, speech-to-speech conversation.

:class:`~motet_voice.session.VoiceSession` owns the clock, the tools and the outbox and
knows nothing about any provider. An arm that can hold a live conversation
(:class:`~motet_voice.realtime.interfaces.LiveArm`) hands the session a
:class:`~motet_voice.realtime.interfaces.LiveConversation`, and this module is what sits
between the two: it decides *when* listener audio is forwarded, turns the conversation's
events into the session's wire events, runs the tools the model asks for, and keeps the
numbers a walk is judged by — barge-ins, latency, what it cost.

**Audio is forwarded only while the listener has the floor.** Every frame is remembered in
a short ring buffer, but nothing goes to the provider until a barge-in — the client's, or
the local detector's — and forwarding stops again when the reply is done. Two reasons, and
the second is the one that matters: the narration playing out of the speaker is not
something to pay audio tokens to transcribe, and a provider that heard everything would
answer questions nobody asked it.

**The ring buffer is what keeps the first word.** A local barge-in commits ~120 ms after
speech starts, and the provider's own VAD then needs the *beginning* of the utterance to
transcribe it. So the last ~600 ms of listener audio are flushed to the provider the moment
the floor is taken, before live forwarding begins.

**The position goes in before the audio.** The provider creates the response on its own
when its VAD hears the utterance end, so "the listener interrupted at 0:47, during …" has
to be in the conversation *before* the question arrives — see
:meth:`LiveBridge.engage`. It is a system item, not a rewrite of the instructions, so the
persona and the episode stay a stable prefix.
"""

from __future__ import annotations

import asyncio
import base64
import logging
import re
import time
from collections import deque
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any, Final

from .bargein import BargeInDecision, BargeInPolicy
from .clock import PlaybackClock
from .contract import (
    AudioChunkEvent,
    ErrorEvent,
    SessionEvent,
    SessionStateEvent,
    ToolCallEvent,
    ToolResultEvent,
    TranscriptEvent,
)
from .realtime.interfaces import (
    AssistantAudio,
    AssistantTranscript,
    LiveConversation,
    ProviderError,
    SpeechStarted,
    SpeechStopped,
    ToolCallRequested,
    TurnDone,
    UserTranscript,
)
from .realtime.openai_realtime import ServerVadRelay
from .tools import ToolRegistry

logger = logging.getLogger("motet.voice.live")

#: How much listener audio is kept back for the flush at barge-in. 16 kHz mono int16 is
#: 32 bytes a millisecond; 600 ms covers a local detector's commit time several times over.
PREROLL_MS: Final = 600
_PREROLL_BYTES: Final = PREROLL_MS * 32


@dataclass
class LiveBridge:
    """One session's live conversation, from the session's side."""

    session_id: str
    arm_name: str
    conversation: LiveConversation
    tools: ToolRegistry
    clock: PlaybackClock
    outbox: asyncio.Queue[SessionEvent]
    history: list[dict[str, str]]
    decisions: list[BargeInDecision]
    relay: ServerVadRelay = field(default_factory=ServerVadRelay)
    #: Forwarding listener audio to the provider right now.
    active: bool = False
    #: Vendor-detected utterances and completed replies, for the summary.
    speech_starts: int = 0
    replies: int = 0
    #: What the provider billed, summed across the session's turns. Audio tokens are the
    #: line that makes this arm materially dearer than the composed one; they are kept
    #: apart so the summary can say so.
    usage: dict[str, int] = field(
        default_factory=lambda: {
            "input_tokens": 0,
            "input_audio_tokens": 0,
            "input_cached_tokens": 0,
            "output_tokens": 0,
            "output_audio_tokens": 0,
        }
    )
    _preroll: deque[bytes] = field(default_factory=deque, init=False)
    _preroll_bytes: int = field(default=0, init=False)
    _reader: asyncio.Task[None] | None = field(default=None, init=False)
    _listener_talking: bool = field(default=False, init=False)
    _reply_started: bool = field(default=False, init=False)
    _reply_bytes: int = field(default=0, init=False)
    _last_forwarded_at: float | None = field(default=None, init=False)
    _speech_stopped_at: float | None = field(default=None, init=False)
    _first_audio_at: float | None = field(default=None, init=False)
    failed: str = field(default="", init=False)

    @classmethod
    def open(
        cls,
        *,
        session_id: str,
        arm_name: str,
        conversation: LiveConversation,
        tools: ToolRegistry,
        clock: PlaybackClock,
        outbox: asyncio.Queue[SessionEvent],
        history: list[dict[str, str]],
        decisions: list[BargeInDecision],
        policy: BargeInPolicy,
    ) -> LiveBridge:
        return cls(
            session_id=session_id,
            arm_name=arm_name,
            conversation=conversation,
            tools=tools,
            clock=clock,
            outbox=outbox,
            history=history,
            decisions=decisions,
            relay=ServerVadRelay(arm_name=arm_name, policy=policy),
        )

    async def start(self) -> None:
        """Apply the session to the provider and start reading from it."""
        await self.conversation.start()
        self._reader = asyncio.create_task(self._read())

    # -- listener audio -------------------------------------------------------------------

    def remember(self, pcm: bytes) -> None:
        """Keep the tail of the listener's audio, whether or not it is being forwarded."""
        if not pcm:
            return
        self._preroll.append(pcm)
        self._preroll_bytes += len(pcm)
        while self._preroll and self._preroll_bytes - len(self._preroll[0]) >= _PREROLL_BYTES:
            self._preroll_bytes -= len(self._preroll.popleft())

    async def forward(self, pcm: bytes) -> None:
        if not self.active or not pcm:
            return
        self._last_forwarded_at = time.monotonic()
        await self.conversation.append_audio(pcm)

    async def engage(self, position_notes: str) -> list[SessionEvent]:
        """The listener has the floor: context in, pre-roll in, then live audio."""
        if self.active:
            return []
        await self.conversation.add_context(position_notes)
        preroll = b"".join(self._preroll)
        self._preroll.clear()
        self._preroll_bytes = 0
        self.active = True
        self._reply_started = False
        if preroll:
            self._last_forwarded_at = time.monotonic()
            await self.conversation.append_audio(preroll)
        return [
            SessionStateEvent(
                at_ms=self.clock.spoken_through_ms,
                state="listening",
                detail="live — speak your question",
            )
        ]

    async def ask(self, text: str, position_notes: str) -> list[SessionEvent]:
        """A typed question on the live channel — the fallback the client keeps."""
        await self.conversation.add_context(position_notes)
        await self.conversation.add_user_text(text)
        self.active = True
        self._reply_started = False
        self._speech_stopped_at = time.monotonic()
        self.history.append({"role": "user", "text": text})
        return [TranscriptEvent(at_ms=self.clock.spoken_through_ms, speaker="user", text=text)]

    # -- provider events -> session events ------------------------------------------------

    async def _read(self) -> None:
        try:
            async for event in self.conversation.events():
                await self._handle(event)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 — a dead reader must say so on the wire
            logger.exception("live conversation reader failed for session %s", self.session_id)
            self.failed = str(exc)
            self.active = False
            self.outbox.put_nowait(
                ErrorEvent(
                    at_ms=self.clock.spoken_through_ms,
                    code="live_unavailable",
                    message=f"the live conversation ended: {exc}. Typed questions still work.",
                )
            )
        else:
            # The provider closed its side. Anything the client asks from here on falls
            # back to the turn-shaped path; say so once rather than per frame.
            if not self.failed:
                self.failed = "provider closed the conversation"
            self.active = False

    async def _handle(self, event: object) -> None:
        now = time.monotonic()
        at_ms = self.clock.spoken_through_ms

        if isinstance(event, SpeechStarted):
            self.speech_starts += 1
            self._listener_talking = True
            decision = self.relay.on_speech_started(
                audio_start_ms=event.audio_start_ms,
                narration_playing=self.clock.playing,
                spoken_through_ms=at_ms,
            )
            if decision is not None:
                self.decisions.append(decision)
            if self._reply_started:
                # Talking over the reply. The provider cuts the response off on its own
                # side; the client has to drop what it has queued, and `listening` is the
                # frame it does that on.
                self._reply_started = False
                self.outbox.put_nowait(
                    SessionStateEvent(at_ms=at_ms, state="listening", detail="interrupted reply")
                )
            return

        if isinstance(event, SpeechStopped):
            self._listener_talking = False
            self._speech_stopped_at = now
            return

        if isinstance(event, UserTranscript):
            if event.text:
                self.history.append({"role": "user", "text": event.text})
                self.outbox.put_nowait(
                    TranscriptEvent(at_ms=at_ms, speaker="user", text=event.text)
                )
            return

        if isinstance(event, AssistantAudio):
            if not self._reply_started:
                self._reply_started = True
                self._reply_bytes = 0
                self._first_audio_at = now
                self._log_latency(now)
                self.outbox.put_nowait(SessionStateEvent(at_ms=at_ms, state="speaking"))
            self._reply_bytes += len(event.pcm)
            self.outbox.put_nowait(
                AudioChunkEvent(
                    at_ms=at_ms,
                    pcm_base64=base64.b64encode(event.pcm).decode("ascii"),
                    sample_rate=event.sample_rate,
                    duration_ms=round(len(event.pcm) / 2 * 1000 / event.sample_rate),
                    format="pcm16",
                )
            )
            return

        if isinstance(event, AssistantTranscript):
            if event.text:
                self.history.append({"role": "assistant", "text": event.text})
                self.outbox.put_nowait(
                    TranscriptEvent(at_ms=at_ms, speaker="assistant", text=event.text)
                )
            return

        if isinstance(event, ToolCallRequested):
            call = event.call
            self.outbox.put_nowait(
                ToolCallEvent(
                    at_ms=at_ms, call_id=call.call_id, name=call.name, arguments=call.arguments
                )
            )
            result = await self.tools.invoke(call.name, call.arguments)
            self.outbox.put_nowait(
                ToolResultEvent(
                    at_ms=at_ms,
                    call_id=call.call_id,
                    name=call.name,
                    ok=result.ok,
                    result=result.result,
                    error=result.error,
                )
            )
            payload: dict[str, Any] = dict(result.result) if result.ok else {"error": result.error}
            await self.conversation.tool_output(call.call_id, payload)
            return

        if isinstance(event, TurnDone):
            self._record_usage(event.usage)
            if event.pending_tools:
                return
            if event.cancelled or self._listener_talking:
                # Cut off by the listener, or a new utterance is already under way: the
                # floor is still theirs and the reply that follows will end the turn.
                return
            self.replies += 1
            self.active = False
            self._reply_started = False
            self.outbox.put_nowait(
                SessionStateEvent(at_ms=at_ms, state="ready", detail="reply complete")
            )
            return

        if isinstance(event, ProviderError):
            logger.warning(
                "live provider error for session %s: code=%s %s",
                self.session_id,
                event.code,
                event.message,
            )
            self.outbox.put_nowait(ErrorEvent(at_ms=at_ms, code=event.code, message=event.message))
            return

    def _log_latency(self, first_audio_at: float) -> None:
        """The number the arm is being felt for: how long the listener waited."""
        since_stop = (
            round((first_audio_at - self._speech_stopped_at) * 1000)
            if self._speech_stopped_at is not None
            else None
        )
        since_last_frame = (
            round((first_audio_at - self._last_forwarded_at) * 1000)
            if self._last_forwarded_at is not None
            else None
        )
        logger.info(
            "live reply latency: session=%s arm=%s speech_stopped_to_first_audio_ms=%s "
            "last_mic_frame_to_first_audio_ms=%s",
            self.session_id,
            self.arm_name,
            since_stop,
            since_last_frame,
        )

    def _record_usage(self, usage: Mapping[str, Any]) -> None:
        if not usage:
            return
        input_details = usage.get("input_token_details") or {}
        output_details = usage.get("output_token_details") or {}
        turn = {
            "input_tokens": _int(usage.get("input_tokens")),
            "input_audio_tokens": _int(input_details.get("audio_tokens")),
            "input_cached_tokens": _int(input_details.get("cached_tokens")),
            "output_tokens": _int(usage.get("output_tokens")),
            "output_audio_tokens": _int(output_details.get("audio_tokens")),
        }
        for key, value in turn.items():
            self.usage[key] += value
        # Every field, even at zero, for the reason `describe_usage` gives: a field that
        # vanishes when it is zero is a field a log query cannot aggregate.
        logger.info(
            "live turn usage: session=%s arm=%s input=%d (audio=%d cached=%d) output=%d (audio=%d) "
            "reply_audio_ms=%d",
            self.session_id,
            self.arm_name,
            turn["input_tokens"],
            turn["input_audio_tokens"],
            turn["input_cached_tokens"],
            turn["output_tokens"],
            turn["output_audio_tokens"],
            round(self._reply_bytes / 48),
        )

    # -- summary and lifecycle ------------------------------------------------------------

    def summary(self) -> dict[str, Any]:
        return {
            "live": True,
            "live_speech_starts": self.speech_starts,
            "live_replies": self.replies,
            "live_usage": dict(self.usage),
            "live_failed": self.failed,
        }

    async def aclose(self) -> None:
        if self._reader is not None and not self._reader.done():
            self._reader.cancel()
            await asyncio.gather(self._reader, return_exceptions=True)
        await self.conversation.aclose()


def _int(value: Any) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


_REASON_CHARS = re.compile(r"[^a-z0-9_]+")


def failure_reason(exc: BaseException) -> str:
    """A short, vendor-neutral code for why the live channel is not there.

    The point is that a client — and the owner reading its screen — can tell *no credits*
    from *no key* from *no arm* without opening a log. Three sources, in order of how much
    they say:

    * :class:`~motet_voice.realtime.interfaces.ArmDormant` — ``arm_dormant``: the arm has no
      transport, which is a missing key or fake mode.
    * A closed-connection error carrying the peer's close frame (``websockets`` puts it on
      ``exc.rcvd`` with ``.code`` and ``.reason``; duck-typed so this module imports no
      vendor library). The reason's first dotted segment is the code —
      ``insufficient_quota.credit_balance_exhausted`` becomes ``insufficient_quota`` — and a
      frame with no text becomes ``close_1013``.
    * Anything else — the exception's class name in snake case (``timeout_error``,
      ``os_error``), which at least says which layer refused.

    Never the message: that goes on the event as prose, and a client branching on prose is
    a client that breaks on a vendor's rewording.
    """
    from .realtime.interfaces import ArmDormant  # noqa: PLC0415 — avoid an import cycle

    if isinstance(exc, ArmDormant):
        return "arm_dormant"
    frame = getattr(exc, "rcvd", None)
    if frame is not None:
        text = str(getattr(frame, "reason", "") or "").strip().lower()
        head = text.split(".", 1)[0].split(" ", 1)[0]
        head = _REASON_CHARS.sub("_", head).strip("_")
        if head:
            return head
        code = getattr(frame, "code", None)
        if isinstance(code, int):
            return f"close_{code}"
        return "connection_closed"
    name = type(exc).__name__
    # `OSError` → `os_error`, `TimeoutError` → `timeout_error`: a run of capitals is one word.
    snake = re.sub(r"([A-Z]+)([A-Z][a-z])", r"\1_\2", name)
    snake = re.sub(r"([a-z0-9])([A-Z])", r"\1_\2", snake)
    return snake.lower() or "unknown"
