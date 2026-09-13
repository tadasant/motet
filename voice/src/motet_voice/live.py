"""The session's half of a live, speech-to-speech conversation.

:class:`~motet_voice.session.VoiceSession` owns the clock, the tools and the outbox and
knows nothing about any provider. An arm that can hold a live conversation
(:class:`~motet_voice.realtime.interfaces.LiveArm`) hands the session a
:class:`~motet_voice.realtime.interfaces.LiveConversation`, and this module is what sits
between the two: it decides *when* listener audio is forwarded, turns the conversation's
events into the session's wire events, runs the tools the model asks for, and keeps the
numbers a walk is judged by — latency, replies, what it cost. Barge-ins are not among
them: the session's own detector decides those on every arm (motet#93), and a vendor's
``speech_started`` is the question that follows one.

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

from opentelemetry import metrics

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
from .tools import ToolRegistry

logger = logging.getLogger("motet.voice.live")

# Created at import against OpenTelemetry's proxy meter, the same shape as
# `motet_inference.accounting`: a no-op until `obs.configure` installs a provider.
_meter = metrics.get_meter("motet.voice")

_realtime_tokens = _meter.create_counter(
    "motet.voice.realtime.tokens",
    unit="{token}",
    description=(
        "Tokens a live realtime channel was billed, by arm and kind. Audio tokens are "
        "the line that makes this arm materially dearer than the composed one, and they "
        "are only spent between a barge-in and the end of the reply — so "
        "`input_audio` growing while `motet.voice.realtime.replies` does not is the "
        "forwarding gate leaking narration to the vendor."
    ),
)
_realtime_replies = _meter.create_counter(
    "motet.voice.realtime.replies",
    unit="{reply}",
    description=(
        "Responses a live realtime channel finished, by arm and outcome: `completed`, "
        "`cancelled` (the listener talked over it or resumed narration), or `tool` "
        "(the model is waiting on a tool result)."
    ),
)

#: The kinds `motet.voice.realtime.tokens` is split by. Every one is added to on every
#: response, even at zero, so a panel can aggregate them without a gap reading as a zero.
TOKEN_KINDS: Final = ("input_text", "input_audio", "input_cached", "output_text", "output_audio")

#: How much listener audio is kept back for the flush at barge-in. 16 kHz mono int16 is
#: 32 bytes a millisecond; 600 ms covers a local detector's commit time several times over.
PREROLL_MS: Final = 600
_PREROLL_BYTES: Final = PREROLL_MS * 32

#: How long the floor stays open to the vendor after a barge-in that nobody follows with
#: speech. A barge-in can be noise — a door, a passing voice — and without a bound the mic
#: would stream to a billed socket until the listener pressed something. Long enough to
#: compose a spoken question, or to start typing one (a typed question cancels the guard).
LISTEN_TIMEOUT_SECONDS: Final = 30.0


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
    #: Forwarding listener audio to the provider right now. **This flag is the spend
    #: gate**: true from a barge-in until the reply ends (or narration resumes, or nobody
    #: speaks for :data:`LISTEN_TIMEOUT_SECONDS`), and false otherwise — so narration coming
    #: out of the speaker is never billed as audio tokens. A *typed* question does not open
    #: it: the question arrives as text, and nothing needs the mic.
    active: bool = False
    #: A reply is expected on this channel — after a barge-in, a typed question, or a tool
    #: result the model has yet to answer. Separate from :attr:`active` because a typed
    #: question owes a reply without opening the mic.
    reply_owed: bool = False
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
    #: Replies the listener talked over, and how much of each they heard.
    replies_cut: int = 0
    _preroll: deque[bytes] = field(default_factory=deque, init=False)
    _preroll_bytes: int = field(default=0, init=False)
    _reader: asyncio.Task[None] | None = field(default=None, init=False)
    _listener_talking: bool = field(default=False, init=False)
    _reply_started: bool = field(default=False, init=False)
    _reply_bytes: int = field(default=0, init=False)
    _last_forwarded_at: float | None = field(default=None, init=False)
    _speech_stopped_at: float | None = field(default=None, init=False)
    _first_audio_at: float | None = field(default=None, init=False)
    _reply_item_id: str = field(default="", init=False)
    #: Replies cut off by the listener: item id -> the fraction of the generated audio
    #: they heard, so the reply's transcript enters history cut to match.
    _cut: dict[str, float] = field(default_factory=dict, init=False)
    #: Where each reply's transcript sits in :attr:`history`, for a cut that arrives after
    #: the transcript did.
    _history_at: dict[str, int] = field(default_factory=dict, init=False)
    #: A tool result has been sent and the model owes its follow-up response. The
    #: provider's own ``response.done`` for the calling response arrives *after* the tool
    #: has already run, so the conversation's pending-call count cannot say this.
    _awaiting_follow_up: bool = field(default=False, init=False)
    _listen_guard: asyncio.Task[None] | None = field(default=None, init=False)
    failed: str = field(default="", init=False)
    #: The short vendor-neutral code for why the channel died, for the session's reopen
    #: decision — ``insufficient_quota`` mid-session is as final as it is at open.
    failure_reason: str = field(default="", init=False)

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
    ) -> LiveBridge:
        return cls(
            session_id=session_id,
            arm_name=arm_name,
            conversation=conversation,
            tools=tools,
            clock=clock,
            outbox=outbox,
            history=history,
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
        """The listener has the floor: context in, pre-roll in, then live audio.

        Already engaged, only the position goes in: the listener is still mid-exchange, the
        audio is already flowing, and the pre-roll has already been sent once. The fresh
        position matters because narration may have moved since the last one.
        """
        if self.active:
            await self.conversation.add_context(position_notes)
            return []
        await self.conversation.add_context(position_notes)
        preroll = b"".join(self._preroll)
        self._preroll.clear()
        self._preroll_bytes = 0
        self.active = True
        self.reply_owed = True
        self._reply_started = False
        self._arm_listen_guard()
        if preroll:
            self._last_forwarded_at = time.monotonic()
            await self.conversation.append_audio(preroll)
        return [
            SessionStateEvent(
                at_ms=self.clock.spoken_through_ms,
                state="listening",
                detail="live — speak your question",
                live=True,
            )
        ]

    async def disengage(self) -> None:
        """Narration resumed with the channel still engaged — "never mind, resume".

        Without this the gate stays open: every packet of narration out of the speaker
        goes to the vendor as billed audio until some reply happens to end, and a reply
        in flight keeps talking over the briefing. So the reply is cancelled, unanswered
        audio is discarded, and forwarding stops.
        """
        if not (self.active or self.reply_owed):
            return
        self.active = False
        self.reply_owed = False
        self._awaiting_follow_up = False
        self._cancel_listen_guard()
        if self._reply_started:
            # Cut off by the resume rather than by speech, and just as unheard past here.
            self._reply_started = False
            await self._cut_reply(time.monotonic())
        await self.conversation.cancel_response()

    def adopt_preroll(self, other: LiveBridge) -> None:
        """Carry a dead channel's pre-roll into its replacement, so a barge-in that reopens
        the channel still hands the vendor the first word."""
        self._preroll = deque(other._preroll)
        self._preroll_bytes = other._preroll_bytes

    def _arm_listen_guard(self) -> None:
        self._cancel_listen_guard()
        self._listen_guard = asyncio.create_task(self._close_floor_if_silent(self.speech_starts))

    def _cancel_listen_guard(self) -> None:
        if self._listen_guard is not None and not self._listen_guard.done():
            self._listen_guard.cancel()
        self._listen_guard = None

    async def _close_floor_if_silent(self, starts_at_engage: int) -> None:
        """Close a floor nobody spoke into — see :data:`LISTEN_TIMEOUT_SECONDS`."""
        await asyncio.sleep(LISTEN_TIMEOUT_SECONDS)
        if not self.active or self.speech_starts != starts_at_engage or self._reply_started:
            return
        logger.info(
            "live floor closed for session %s: nothing was said within %.0f s of the barge-in",
            self.session_id,
            LISTEN_TIMEOUT_SECONDS,
        )
        self._listen_guard = None  # this task; do not cancel it from inside
        try:
            await self.disengage()
        except Exception as exc:  # noqa: BLE001 — the reader reports a dead channel
            logger.warning("closing a silent floor failed for session %s: %s", self.session_id, exc)
        self.outbox.put_nowait(
            SessionStateEvent(
                at_ms=self.clock.spoken_through_ms, state="ready", detail="nothing heard"
            )
        )

    async def ask(self, text: str, position_notes: str) -> list[SessionEvent]:
        """A typed question on the live channel — the fallback the client keeps."""
        await self.conversation.add_context(position_notes)
        await self.conversation.add_user_text(text)
        # A reply is owed; the mic is not opened for it. Typing also means the listener is
        # not silent, so the guard on a spoken floor stands down.
        self.reply_owed = True
        self._cancel_listen_guard()
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
            self._end(str(exc), failure_reason(exc))
        else:
            # The provider closed its side. Anything the client asks from here on falls
            # back to the turn-shaped path; say so once rather than per frame.
            if not self.failed:
                self._end("provider closed the conversation", "connection_closed")

    def _end(self, failed: str, reason: str) -> None:
        """The channel is gone: close the gate and tell the client once, by code.

        The vendor's own exception text stays in the log — it can name the provider's host,
        and the client wire speaks our contract, not the vendor's (invariant 1).
        """
        self.failed = failed
        self.failure_reason = reason
        self.active = False
        self.reply_owed = False
        self._cancel_listen_guard()
        self.outbox.put_nowait(
            ErrorEvent(
                at_ms=self.clock.spoken_through_ms,
                code="live_unavailable",
                message=f"the live conversation ended ({reason}). Typed questions still work.",
            )
        )

    async def _handle(self, event: object) -> None:
        now = time.monotonic()
        at_ms = self.clock.spoken_through_ms

        if isinstance(event, SpeechStarted):
            # Counted, and deliberately **not** a barge-in. The vendor's VAD governs the
            # reply turn — where the question ends, and cutting the reply off — and the
            # session's own detector decides the interruption of narration (motet#93).
            # A vendor speech start is almost always the question the local barge-in
            # already opened the floor for, and counting it would double every barge-in.
            self.speech_starts += 1
            self._listener_talking = True
            self._cancel_listen_guard()
            if self._reply_started:
                # Talking over the reply. The provider cuts the response off on its own
                # side; the client has to drop what it has queued, and `listening` is the
                # frame it does that on.
                self._reply_started = False
                await self._cut_reply(now)
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
            if not self.reply_owed:
                # A reply the session already cancelled (narration resumed) can still have
                # chunks in flight from the vendor. Nobody is listening for it.
                return
            if not self._reply_started:
                self._reply_started = True
                self._reply_bytes = 0
                self._reply_item_id = event.item_id
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
            if not event.text:
                return
            text = event.text
            if event.item_id in self._cut:
                text = cut_transcript(text, self._cut[event.item_id])
            if event.item_id:
                self._history_at[event.item_id] = len(self.history)
            self.history.append({"role": "assistant", "text": text})
            self.outbox.put_nowait(TranscriptEvent(at_ms=at_ms, speaker="assistant", text=text))
            return

        if isinstance(event, ToolCallRequested):
            call = event.call
            # The answer to this call is a *second* response, and the first one's
            # `response.done` is still to come — it must not end the turn (see TurnDone).
            self._awaiting_follow_up = True
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
            calling = event.pending_tools or self._awaiting_follow_up
            outcome = "tool" if calling else "cancelled" if event.cancelled else "completed"
            self._record_usage(event.usage, outcome=outcome)
            if calling and not event.cancelled:
                # The response that asked for a tool is done; the one that answers it has
                # been requested and is what ends the turn. Its audio must still play.
                self._awaiting_follow_up = False
                return
            self._awaiting_follow_up = False
            if event.cancelled or self._listener_talking or not self.reply_owed:
                # Cut off by the listener, or a new utterance is already under way: the
                # floor is still theirs and the reply that follows will end the turn.
                return
            self.replies += 1
            self.active = False
            self.reply_owed = False
            self._cancel_listen_guard()
            self._reply_started = False
            self.outbox.put_nowait(
                SessionStateEvent(at_ms=at_ms, state="ready", detail="reply complete")
            )
            return

        if isinstance(event, ProviderError):
            # The vendor's code and prose go to the log; the client gets ours (invariant 1).
            logger.warning(
                "live provider error for session %s: code=%s %s",
                self.session_id,
                event.code,
                event.message,
            )
            self.outbox.put_nowait(
                ErrorEvent(
                    at_ms=at_ms,
                    code="provider_error",
                    message="the voice provider reported an error; ask again or resume",
                )
            )
            return

    async def _cut_reply(self, now: float) -> None:
        """The listener talked over the reply: record how much of it they actually heard.

        Heard is bounded twice — by the audio generated so far, and by wall-clock time since
        the first chunk left, since a client plays in real time and cannot have heard audio
        that has not had time to play. Both the vendor's history (``truncate``) and ours
        then hold what was said aloud, not a sentence the listener cut short and never got.
        """
        generated_ms = round(self._reply_bytes / 48)
        elapsed_ms = (
            round((now - self._first_audio_at) * 1000) if self._first_audio_at is not None else 0
        )
        heard_ms = max(0, min(generated_ms, elapsed_ms))
        self.replies_cut += 1
        item_id = self._reply_item_id
        if not item_id:
            return
        fraction = heard_ms / generated_ms if generated_ms else 0.0
        self._cut[item_id] = fraction
        if (index := self._history_at.get(item_id)) is not None and index < len(self.history):
            # The transcript arrived before the listener cut in; cut it where it stands.
            self.history[index]["text"] = cut_transcript(self.history[index]["text"], fraction)
        await self.conversation.truncate(item_id, heard_ms)

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

    def _record_usage(self, usage: Mapping[str, Any], *, outcome: str) -> None:
        """Fold one response's bill into the session total, the metric and the log.

        The metric is the fleet-wide number (invariant 11) and carries no session id, for
        `motet.llm.tokens`' reason; the log line is the one with the id in it.
        """
        _realtime_replies.add(1, {"arm": self.arm_name, "outcome": outcome})
        input_details = usage.get("input_token_details") or {}
        output_details = usage.get("output_token_details") or {}
        turn = {
            "input_tokens": _int(usage.get("input_tokens")),
            "input_audio_tokens": _int(input_details.get("audio_tokens")),
            "input_cached_tokens": _int(input_details.get("cached_tokens")),
            "output_tokens": _int(usage.get("output_tokens")),
            "output_audio_tokens": _int(output_details.get("audio_tokens")),
        }
        by_kind = {
            "input_text": max(0, turn["input_tokens"] - turn["input_audio_tokens"]),
            "input_audio": turn["input_audio_tokens"],
            "input_cached": turn["input_cached_tokens"],
            "output_text": max(0, turn["output_tokens"] - turn["output_audio_tokens"]),
            "output_audio": turn["output_audio_tokens"],
        }
        for kind in TOKEN_KINDS:
            _realtime_tokens.add(by_kind[kind], {"arm": self.arm_name, "kind": kind})
        if not usage:
            return
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
            "live_replies_cut": self.replies_cut,
            "live_usage": dict(self.usage),
            "live_failed": self.failed,
        }

    async def aclose(self) -> None:
        self._cancel_listen_guard()
        if self._reader is not None and not self._reader.done():
            self._reader.cancel()
            await asyncio.gather(self._reader, return_exceptions=True)
        await self.conversation.aclose()


def cut_transcript(text: str, fraction: float) -> str:
    """The part of a reply the listener heard, apportioned by length like claim timings.

    Approximate by construction — speech is not uniform in characters per second — and
    marked as cut so neither the model nor a reader takes it for the whole answer.
    """
    if fraction >= 1.0:
        return text
    kept = text[: max(0, round(len(text) * fraction))].rstrip()
    return f"{kept}… [cut off by the listener]" if kept else "[cut off by the listener]"


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
