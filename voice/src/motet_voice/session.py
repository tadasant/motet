"""One live voice session: the clock, the turn detector, the tools, and the arm.

Everything a session *is* lives in this object, and nothing a session needs lives outside
the process. There is no session store, no database handle, and no lookup — invariant 2 —
so a session is entirely reconstructible from its config, which is what lets Cloud Run kill
an instance mid-walk without losing anything but the socket.

**The clock is the part to read carefully.** ``spoken_through_ms`` is ours (invariant 4):
this class advances it, freezes it on a barge-in, and hands that frozen offset to the client
as ``interrupted_at(offset)``. A provider that volunteers its own position gets it recorded
as drift and ignored.

**Nothing checks a reply against its material.** The advisory conversational grounding
check that used to run behind every reply was removed in motet#75, along with the hard
gate on the narration path; the risk that decision accepts is stated in the issue.
"""

from __future__ import annotations

import asyncio
import base64
import contextlib
import logging
from collections.abc import Awaitable
from dataclasses import dataclass, field
from typing import Any, Final

from motet_inference.accounting import Ledger, collect_usage

from .audio import DEFAULT_FRAME_MS, TARGET_SAMPLE_RATE, iter_frames
from .bargein import BargeInDecision, BargeInPolicy, TurnDetector
from .clock import PlaybackClock
from .contract import (
    AudioChunkEvent,
    ErrorEvent,
    InterruptedAtEvent,
    SessionEvent,
    SessionStateEvent,
    StartSessionRequest,
    ToolCallEvent,
    ToolResultEvent,
    TranscriptEvent,
    TurnPolicy,
)
from .live import LiveBridge, failure_reason
from .position import locate, position_notes
from .realtime import ArmDormant, LiveArm, RealtimeArm, TurnRequest
from .tools import ToolRegistry
from .vad import VadReading

logger = logging.getLogger("motet.voice.session")

#: How often, in listener audio, the session says what its detector is hearing. Ten
#: seconds: often enough that "I spoke and nothing fired" has a line within reach of the
#: moment, rare enough that an hour's walk is a few hundred lines rather than a flood.
LISTENER_AUDIO_LOG_MS: Final = 10_000

#: How many times one session reopens a live channel that opened and then died — a vendor
#: idle timeout, a deploy on their side, Cloud Run bounding the socket. Small and fixed:
#: each reopen is a vendor connection, and a channel that keeps dying is not going to be
#: fixed by opening it a fourth time.
MAX_LIVE_REOPENS: Final = 2

#: Failure reasons a reopen cannot fix, so it is not attempted: no key or fake mode, and
#: an account with no credit.
_NOT_REOPENABLE: Final = frozenset({"arm_dormant", "insufficient_quota"})


def policy_from(turn_policy: TurnPolicy, *, name: str = "session") -> BargeInPolicy:
    """Translate the wire turn policy into the internal barge-in policy.

    A translation rather than the same object, so that the internal policy can grow a dial
    without that dial becoming part of the client contract the moment it is added.
    """
    return BargeInPolicy(
        name=name,
        speech_probability_threshold=turn_policy.speech_probability_threshold,
        consecutive_speech_frames=turn_policy.consecutive_speech_frames,
        min_snr_db=turn_policy.min_snr_db,
        refractory_ms=turn_policy.refractory_ms,
        require_narration_playing=turn_policy.require_narration_playing,
    )


def _default_text_arm(arm: RealtimeArm) -> RealtimeArm | None:
    """Who answers a typed turn when there is no live channel.

    The arm itself, unless it is a :class:`LiveArm`. A realtime arm's ``respond`` opens the
    same vendor socket its live channel does, so it is no fallback for that channel failing
    — the app hands the session the composed arm instead (:class:`~motet_voice.app.VoiceApp`),
    and a session built without one reports that it cannot answer rather than retrying the
    vendor.
    """
    return None if isinstance(arm, LiveArm) else arm


@dataclass
class VoiceSession:
    """A single conversation, from ``StartSession`` to socket close."""

    session_id: str
    config: StartSessionRequest
    arm: RealtimeArm
    tools: ToolRegistry
    detector: TurnDetector
    #: The arm that answers a *typed* turn when there is no live channel to send it down —
    #: the composed arm behind a realtime arm, and the arm itself otherwise. ``None`` means
    #: nothing in this process can answer a typed question without the live channel, and
    #: :meth:`respond_to_text` says so instead of trying the vendor again. See
    #: :meth:`respond_to_text` for why this is never the realtime arm's own turn path.
    text_arm: RealtimeArm | None = None
    clock: PlaybackClock = field(default_factory=PlaybackClock)
    history: list[dict[str, str]] = field(default_factory=list)
    decisions: list[BargeInDecision] = field(default_factory=list)
    #: What this session's turns spent, accumulated across turns — the voice answer to
    #: "what did *that one* cost". See :meth:`respond_to_text` for why it is filled a turn
    #: at a time rather than by one block around the session.
    spend: Ledger = field(default_factory=Ledger)
    #: Every outbound event, so that one task writes to the socket and nothing else does.
    #: Two coroutines writing to one WebSocket is a protocol violation waiting for a busy
    #: walk. The socket drains this; see :mod:`motet_voice.app`.
    outbox: asyncio.Queue[SessionEvent] = field(default_factory=asyncio.Queue)
    #: The live, speech-to-speech channel, when the arm offers one (:class:`LiveArm`) and
    #: it opened. ``None`` on the composed arm and on a realtime arm whose vendor socket
    #: failed — both then run the turn-shaped path with a typed question, on
    #: :attr:`text_arm`. See :mod:`motet_voice.live`.
    live: LiveBridge | None = field(default=None, init=False)
    #: Why the live channel is not there, when it is not: a short vendor-neutral code
    #: (``insufficient_quota``, ``arm_dormant``, ``connection_closed``) and the message it
    #: came from. Carried on ``ready`` so a client can tell "no credits" from "no key" from
    #: "no arm" without reading a log.
    live_failure_reason: str = field(default="", init=False)
    live_failure_message: str = field(default="", init=False)
    _residue: bytes = field(default=b"", init=False)
    #: Frames consumed so far in this session. Audio arrives in packets that do not respect
    #: frame boundaries *or* start at zero, and every offset downstream — the refractory
    #: window, a decision's ``at_ms``, the snippet a reviewer listens to — is an offset into
    #: the session rather than into whichever packet happened to carry the frame.
    _frames_seen: int = field(default=0, init=False)
    #: Turns handed to the arm in this session, so a cost line can say which one it is.
    #: Counted rather than derived from :attr:`history`: a turn that produces no assistant
    #: text appends one entry instead of two, and arithmetic over that would drift.
    _turns_seen: int = field(default=0, init=False)
    _live_reopens: int = field(default=0, init=False)
    _last_listener_log_frame: int = field(default=0, init=False)

    @classmethod
    def create(
        cls,
        *,
        session_id: str,
        config: StartSessionRequest,
        arm: RealtimeArm,
        tools: ToolRegistry,
        clock: PlaybackClock | None = None,
        text_arm: RealtimeArm | None = None,
    ) -> VoiceSession:
        """Build a session. ``text_arm`` defaults to ``arm`` unless ``arm`` is a
        :class:`LiveArm`, whose turn path opens the same vendor socket the live channel
        does — see :meth:`respond_to_text`."""
        session_clock = clock or PlaybackClock()
        if config.context.spoken_through_ms:
            # The caller told us where the listener had got to. That is the one external
            # party allowed to set this clock, and it is not a provider — see clock.py.
            session_clock.seek(config.context.spoken_through_ms)
        return cls(
            session_id=session_id,
            config=config,
            arm=arm,
            tools=tools,
            text_arm=text_arm if text_arm is not None else _default_text_arm(arm),
            detector=arm.build_turn_detector(policy_from(config.turn_policy)),
            clock=session_clock,
        )

    # -- the live channel ---------------------------------------------------------------

    async def start_live(self) -> None:
        """Open a live conversation if the arm can hold one. Never raises.

        A vendor socket that will not open is a warning and a fallback, not a refused
        session: barge-in detection is local and a typed question still goes through
        :meth:`respond_to_text`. ``ready().detail`` says which of the two the client got.
        """
        if not isinstance(self.arm, LiveArm):
            return
        conversation = None
        try:
            conversation = self.arm.open_live(self._turn_request())
            bridge = LiveBridge.open(
                session_id=self.session_id,
                arm_name=self.arm.name,
                conversation=conversation,
                tools=self.tools,
                clock=self.clock,
                outbox=self.outbox,
                history=self.history,
            )
            await bridge.start()
        except Exception as exc:  # noqa: BLE001 — see the docstring
            self.live_failure_reason = failure_reason(exc)
            self.live_failure_message = str(exc)
            logger.warning(
                "live conversation did not open for session %s on arm %s (reason=%s): %s",
                self.session_id,
                self.arm.name,
                self.live_failure_reason,
                exc,
            )
            if conversation is not None:
                with contextlib.suppress(Exception):
                    await conversation.aclose()
            return
        self.live = bridge

    async def receive_audio(self, pcm: bytes) -> list[SessionEvent]:
        """Listener audio from the socket. **The local detector sees every packet, first.**

        On every arm, whether or not a live channel is open, and including while that
        channel is forwarding. The order is the property (motet#93): a decision freezes the
        clock and emits ``interrupted_at`` *before* the channel is handed the position and
        the pre-roll, so the vendor is only ever engaged behind a decision this process
        made. The session's detector decides the interruption of narration; the vendor's
        server VAD governs the reply turn.

        The old order sent audio straight to the vendor while the channel was engaged and
        consulted the detector only otherwise — so after "never mind, resume" with the
        channel still engaged, every later utterance went to a vendor with no reply to
        interrupt, and the walk was uninterruptible with no line anywhere.
        """
        live = self._live_channel()
        if live is not None:
            # Kept whether or not it is forwarded: it is the pre-roll a barge-in flushes.
            live.remember(pcm)
        events = self.observe_audio(pcm)
        interrupted = any(isinstance(event, InterruptedAtEvent) for event in events)
        if interrupted:
            live = await self._reopen_if_dropped() or self._live_channel()
            if live is not None:
                was_forwarding = live.active
                events.extend(
                    await self._on_live(live.engage(self._position_notes()), what="engaging")
                )
                if was_forwarding and (current := self._live_channel()) is not None:
                    # Engaged already: the pre-roll went out at the first barge-in, so this
                    # packet travels the ordinary way.
                    events.extend(await self._on_live(current.forward(pcm), what="forwarding"))
            return events
        if live is not None and live.active:
            events.extend(await self._on_live(live.forward(pcm), what="forwarding audio"))
        return events

    async def client_barge_in(self, *, trigger: str = "client") -> list[SessionEvent]:
        """An explicit interruption from the client, and the live channel engaged behind it."""
        events: list[SessionEvent] = [self.barge_in(trigger=trigger)]
        live = await self._reopen_if_dropped() or self._live_channel()
        if live is not None:
            events.extend(await self._on_live(live.engage(self._position_notes()), what="engaging"))
        return events

    def _live_channel(self) -> LiveBridge | None:
        """The live channel, if there is one and it is still up."""
        if self.live is not None and not self.live.failed:
            return self.live
        return None

    async def _reopen_if_dropped(self) -> LiveBridge | None:
        """Reopen a live channel that opened and then died, at the moment it is needed.

        Only at a barge-in or a question — never on a timer and never per packet, so a
        dead vendor costs at most one connection attempt per time the listener takes the
        floor, and at most :data:`MAX_LIVE_REOPENS` per session. A channel that never
        opened is not retried (the reason it failed is on ``ready`` and nothing has
        changed), and neither is one refused for a reason a reconnect cannot fix. The new
        channel is handed the conversation so far, so the listener does not start over.
        """
        dead = self.live
        if dead is None or not dead.failed or not isinstance(self.arm, LiveArm):
            return None
        reason = dead.failure_reason or self.live_failure_reason
        if reason:
            self.live_failure_reason = reason
        if reason in _NOT_REOPENABLE or self._live_reopens >= MAX_LIVE_REOPENS:
            return None
        self._live_reopens += 1
        with contextlib.suppress(Exception):
            await dead.aclose()
        self.live = None
        logger.info(
            "reopening the live conversation for session %s on arm %s (attempt %d of %d; "
            "it ended: %s)",
            self.session_id,
            self.arm.name,
            self._live_reopens,
            MAX_LIVE_REOPENS,
            dead.failed,
        )
        await self.start_live()
        if (reopened := self._live_channel()) is not None:
            reopened.adopt_preroll(dead)
        return reopened

    async def _on_live(
        self, call: Awaitable[list[SessionEvent] | None], *, what: str
    ) -> list[SessionEvent]:
        """Run one call on the live channel at the session boundary.

        **A vendor socket dying must never close the client's.** Every send on the live
        channel can raise the provider's own closed-connection error — quota, an idle
        timeout, a deploy on their side — and before this existed that exception walked
        straight out of the WebSocket handler as an ASGI failure, taking the narration down
        with it (the client saw a bare 1006). So the channel is marked failed, the reason is
        kept for the next ``ready``, and the client gets an ``error`` event: ``turn_failed``
        for a question it asked, ``live_unavailable`` for anything else. The next typed
        question then goes to :attr:`text_arm`, which is what the fallback is for.
        """
        try:
            return await call or []
        except Exception as exc:  # noqa: BLE001 — see the docstring
            live = self.live
            reason = failure_reason(exc)
            logger.warning(
                "live conversation failed while %s for session %s on arm %s (reason=%s): %s",
                what,
                self.session_id,
                self.arm.name,
                reason,
                exc,
            )
            if live is not None:
                live.failed = live.failed or str(exc)
                live.failure_reason = live.failure_reason or reason
                live.active = False
                live.reply_owed = False
            self.live_failure_reason = reason
            self.live_failure_message = str(exc)
            code = "turn_failed" if what == "asking" else "live_unavailable"
            fallback = (
                f" Typed questions are answered by the {self.text_arm.name} arm; ask again."
                if self.text_arm is not None
                else " No arm in this process can answer a typed question without it."
            )
            # The reason code, never the exception text: a vendor's error can name its own
            # host, and the client speaks our contract (invariant 1). The log has the rest.
            return [
                ErrorEvent(
                    at_ms=self.clock.spoken_through_ms,
                    code=code,
                    message=f"the live conversation ended ({reason}).{fallback}",
                )
            ]

    def _position_notes(self) -> str:
        return position_notes(self.config.context.transcript, self.clock.spoken_through_ms)

    def _interruption_context(self) -> dict[str, Any]:
        return locate(self.config.context.transcript, self.clock.spoken_through_ms).to_json()

    def _turn_request(self, text: str | None = None) -> TurnRequest:
        return TurnRequest(
            persona_instructions=self.config.persona.instructions,
            voice=self.config.persona.voice,
            user_text=text,
            context_notes=self.config.context.notes,
            position_notes=self._position_notes(),
            history=list(self.history),
            tools=self.tools.describe(),
        )

    # -- inbound audio ------------------------------------------------------------------

    def observe_audio(self, pcm: bytes) -> list[SessionEvent]:
        """Feed listener audio to the turn detector and emit any barge-in.

        Bytes arriving from a socket do not respect frame boundaries, so a partial frame is
        held over to the next call rather than padded. Padding would inject a slice of
        digital silence into the middle of an utterance every time a packet split awkwardly,
        which reads to an energy VAD as the end of speech.
        """
        if self.config.turn_policy.mode == "push_to_talk":
            return []

        buffer = self._residue + pcm
        frame_bytes = int(TARGET_SAMPLE_RATE * DEFAULT_FRAME_MS / 1000) * 2
        usable = len(buffer) - (len(buffer) % frame_bytes)
        self._residue = buffer[usable:]

        events: list[SessionEvent] = []
        for frame in iter_frames(
            buffer[:usable],
            frame_ms=DEFAULT_FRAME_MS,
            start_index=self._frames_seen,
            start_ms=self._frames_seen * DEFAULT_FRAME_MS,
        ):
            decision = self.detector.observe(
                frame,
                narration_playing=self.clock.playing,
                spoken_through_ms=self.clock.spoken_through_ms,
            )
            if decision is not None:
                events.append(self._interrupt(decision))
        self._frames_seen += usable // frame_bytes
        if (
            self._frames_seen - self._last_listener_log_frame
        ) * DEFAULT_FRAME_MS >= LISTENER_AUDIO_LOG_MS:
            self._log_listener_audio("periodic")
        return events

    def barge_in(self, *, trigger: str = "client") -> InterruptedAtEvent:
        """An explicit interruption from the client — push-to-talk, or a button.

        Recorded as a decision like the detector's, so a push-to-talk session does not close
        with ``barge_ins: 0`` — which it used to, because this built the event and never
        counted it. The measurements are the detector's latest reading, as evidence of what
        the mic was hearing when the button was pressed, not as the reason for it.
        """
        playing = self.clock.playing
        offset = self.clock.interrupt()
        reading = self._last_reading()
        at_ms = self._frames_seen * DEFAULT_FRAME_MS
        decision = BargeInDecision(
            at_ms=at_ms,
            onset_ms=at_ms,
            arm=self.arm.name,
            variant="client",
            trigger=trigger,
            spoken_through_ms=offset,
            narration_playing=playing,
            consecutive_frames=0,
            speech_probability=reading.speech_probability if reading else 0.0,
            rms_dbfs=reading.rms_dbfs if reading else 0.0,
            noise_floor_dbfs=reading.noise_floor_dbfs if reading else 0.0,
            snr_db=reading.snr_db if reading else 0.0,
            zero_crossing_rate=reading.zero_crossing_rate if reading else 0.0,
        )
        self.decisions.append(decision)
        self._log_barge_in(decision, offset)
        return InterruptedAtEvent(
            at_ms=offset,
            offset_ms=offset,
            decision=decision.to_json(),
            context=self._interruption_context(),
        )

    def _interrupt(self, decision: BargeInDecision) -> InterruptedAtEvent:
        offset = self.clock.interrupt()
        # The decision was built with the position *at the frame*; the authoritative offset
        # is the one the clock froze at, so the event carries that and the decision record
        # carries its own. They agree in practice and the event's is the one that binds.
        self.decisions.append(decision)
        self._log_barge_in(decision, offset)
        return InterruptedAtEvent(
            at_ms=offset,
            offset_ms=offset,
            decision=decision.to_json(),
            context=self._interruption_context(),
        )

    # -- saying where the audio went -----------------------------------------------------

    def _last_reading(self) -> VadReading | None:
        # Duck-typed: `TurnDetector` is a Protocol and a detector need not keep one.
        reading = getattr(self.detector, "last_reading", None)
        return reading if isinstance(reading, VadReading) else None

    def _route(self) -> str:
        live = self._live_channel()
        return "vendor+detector" if live is not None and live.active else "detector"

    def _log_listener_audio(self, why: str, *, heard_ms: int | None = None) -> None:
        """What the detector is hearing, and where the audio is going.

        A session where the listener spoke and nothing fired used to close with
        ``barge_ins: 0`` and not a line in between; the owner's first browser attempts were
        exactly that, and the cause — a noise floor seeded at speech level — would have been
        one of these lines away. ``snr_db`` near zero while somebody is talking is it.
        """
        self._last_listener_log_frame = self._frames_seen
        reading = self._last_reading()
        logger.info(
            "listener audio: session=%s why=%s route=%s playing=%s heard_ms=%d "
            "rms_dbfs=%s floor_dbfs=%s snr_db=%s barge_ins=%d",
            self.session_id,
            why,
            self._route(),
            self.clock.playing,
            heard_ms if heard_ms is not None else self._frames_seen * DEFAULT_FRAME_MS,
            f"{reading.rms_dbfs:.1f}" if reading else "-",
            f"{reading.noise_floor_dbfs:.1f}" if reading else "-",
            f"{reading.snr_db:.1f}" if reading else "-",
            len(self.decisions),
        )

    def _log_barge_in(self, decision: BargeInDecision, offset_ms: int) -> None:
        logger.info(
            "barge-in: session=%s arm=%s trigger=%s offset_ms=%d at_ms=%d latency_ms=%d "
            "rms_dbfs=%.1f floor_dbfs=%.1f snr_db=%.1f p=%.2f",
            self.session_id,
            decision.arm,
            decision.trigger,
            offset_ms,
            decision.at_ms,
            decision.latency_ms,
            decision.rms_dbfs,
            decision.noise_floor_dbfs,
            decision.snr_db,
            decision.speech_probability,
        )
        # The decision's own offset: mid-packet, the frame counter has not caught up yet.
        self._log_listener_audio("barge_in", heard_ms=decision.at_ms)

    # -- narration ----------------------------------------------------------------------

    def narration_delivered(self, duration_ms: int) -> None:
        self.clock.deliver(duration_ms)
        self.clock.start()

    def narration_paused(self, position_ms: int | None = None) -> None:
        """The listener paused the player — not a barge-in, and nothing is engaged.

        Before this frame existed a client had two bad choices: fake a barge-in (which
        opens the floor, and on a live channel starts paying for audio) or leave the clock
        running past a paused player.
        """
        if position_ms is not None:
            self.clock.seek(position_ms)
        self.clock.pause()

    async def narration_resumed(self, position_ms: int | None = None) -> list[SessionEvent]:
        """Narration is playing again — after a reply, after "never mind", after a pause.

        The clock restarts at the player's position, and **the live channel is disengaged**:
        whatever reply is in flight is cancelled and forwarding stops. That second half is
        the spend gate's other edge — without it a "never mind" left the vendor listening
        to the briefing, billed per audio token.
        """
        if position_ms is not None:
            self.clock.seek(position_ms)
        self.clock.start()
        if (live := self._live_channel()) is not None and live.active:
            return await self._on_live(live.disengage(), what="disengaging")
        return []

    def client_reported_position(self, position_ms: int) -> None:
        self.clock.seek(position_ms)

    def provider_reported_position(self, position_ms: int) -> int:
        """Record a provider's claim about position. Never acts on it. Invariant 4."""
        return self.clock.note_provider_position(position_ms)

    # -- turns --------------------------------------------------------------------------

    async def respond_to_text(self, text: str) -> list[SessionEvent]:
        """Run one conversational turn and execute whatever tools it asks for.

        The events this returns — the transcript and the audio — are what the client gets,
        in order, and nothing runs behind them: the advisory grounding check that used to
        follow a reply was removed in motet#75.
        """
        if (live := await self._reopen_if_dropped() or self._live_channel()) is not None:
            # The reply comes back through the live channel's reader, streamed, onto the
            # outbox; this returns only the echo of the question.
            return await self._on_live(live.ask(text, self._position_notes()), what="asking")
        # No live channel — the arm has none, it never opened, or it has since died. The
        # typed turn goes to `text_arm` and **never** to a realtime arm's own turn path:
        # that path opens the same vendor socket the live channel just failed on, so it
        # fails the same way, and its failure used to escape the WebSocket handler as an
        # ASGI exception — the client's socket closed with 1006 because a vendor's did.
        arm = self.text_arm
        if arm is None:
            return [
                ErrorEvent(
                    at_ms=self.clock.spoken_through_ms,
                    code="arm_dormant",
                    message=(
                        "the live conversation is unavailable"
                        f" ({self.live_failure_reason or 'not opened'}) and no arm in this"
                        " process can answer a typed question without it"
                    ),
                )
            ]
        request = self._turn_request(text)
        # One block per *turn*, not one per session, and the identifier is what forces that
        # (motet#58). `collect_usage` is a `ContextVar` ledger: it holds for the duration of
        # a `with` in one task, and a voice session is a socket's lifetime spanning many
        # tasks, so a block around the session would not reliably see a turn's completions.
        # A turn is also the unit a person waits on and the unit a conversational minute
        # would be priced by. The per-session total is these turns summed — see
        # :attr:`spend`.
        #
        # The block is around the arm and nothing else. Anything else this turn one day
        # grows — a second model call behind the reply, say — wants a scope of its own
        # rather than this one widened: it is not part of what the turn cost the listener
        # to wait for, and it is not free either.
        with collect_usage() as turn_spend:
            try:
                turn = await arm.respond(request)
            except ArmDormant as exc:
                return [
                    ErrorEvent(
                        at_ms=self.clock.spoken_through_ms, code="arm_dormant", message=str(exc)
                    )
                ]
            except Exception as exc:  # noqa: BLE001 — a failed turn must not end the walk
                # A vendor refusing the turn — quota, a dropped socket, a rejected request —
                # is an answer the listener can hear about; letting it propagate closes the
                # WebSocket with 1006 and takes the narration down with it.
                logger.exception("conversational turn failed for session %s", self.session_id)
                return [
                    ErrorEvent(
                        at_ms=self.clock.spoken_through_ms,
                        code="turn_failed",
                        message=f"the reply could not be produced: {exc}",
                    )
                ]
            finally:
                # In a `finally` because a turn that failed *after* its completion arrived
                # was still billed for it, and dropping that from the session total would
                # be motet#58's own defect one scope smaller. The metric is already safe —
                # it is written where the call is made — but this line is the only place
                # the session id meets the number.
                self._record_turn_spend(turn_spend, arm_name=arm.name)

        events: list[SessionEvent] = [
            TranscriptEvent(at_ms=self.clock.spoken_through_ms, speaker="user", text=text)
        ]
        self.history.append({"role": "user", "text": text})

        for call in turn.tool_calls:
            events.append(
                ToolCallEvent(
                    at_ms=self.clock.spoken_through_ms,
                    call_id=call.call_id,
                    name=call.name,
                    arguments=call.arguments,
                )
            )
            result = await self.tools.invoke(call.name, call.arguments)
            events.append(
                ToolResultEvent(
                    at_ms=self.clock.spoken_through_ms,
                    call_id=call.call_id,
                    name=call.name,
                    ok=result.ok,
                    result=result.result,
                    error=result.error,
                )
            )

        if turn.text:
            events.append(
                TranscriptEvent(
                    at_ms=self.clock.spoken_through_ms, speaker="assistant", text=turn.text
                )
            )
            self.history.append({"role": "assistant", "text": turn.text})

        if turn.audio is not None:
            events.append(
                AudioChunkEvent(
                    at_ms=self.clock.spoken_through_ms,
                    pcm_base64=base64.b64encode(turn.audio.data).decode("ascii"),
                    sample_rate=TARGET_SAMPLE_RATE,
                    duration_ms=turn.audio.duration_ms,
                    # What is actually in the field — the TTS leg hands back a container.
                    format=turn.audio.media_type or "pcm16",
                )
            )
            # Deliberately **not** `self.clock.deliver(...)`. This is the assistant *answering*,
            # not the briefing being narrated, and `spoken_through_ms` is a position in the
            # episode. Folding a reply into it would advance read state by however long the
            # assistant talked for, which is the sort of error nobody notices until a story
            # is marked read that the listener never heard.

        return events

    # -- cost, per turn and per session --------------------------------------------------

    def _record_turn_spend(self, turn_spend: Ledger, *, arm_name: str | None = None) -> None:
        """Fold one turn's completions into the session's running total, and log the turn.

        Silent for an arm with no LLM leg — the realtime arm speaks to a provider through
        its own socket and the fake arm calls no model at all — which is the right answer
        rather than a gap: a ledger with no entries means nothing went through the LLM seam,
        and that is exactly what happened. The turn is still counted, so the numbering in
        the lines that do get logged is the conversation's rather than the bill's.

        The entries are kept rather than only their totals, so ``stage`` survives into the
        session's own record. Today every one of them is ``LlmStage.VOICE``; a turn that one
        day reached a second stage would show up here instead of being silently added in.
        """
        self._turns_seen += 1
        if not turn_spend.requests:
            return
        self.spend.entries.extend(turn_spend.entries)
        logger.info(
            "voice turn cost %d completion(s): session=%s arm=%s turn=%d %s",
            turn_spend.requests,
            self.session_id,
            arm_name or self.arm.name,
            self._turns_seen,
            turn_spend.summary(),
        )

    # -- lifecycle ----------------------------------------------------------------------

    def ready(self) -> SessionStateEvent:
        """The first frame the client gets, and the one that says what kind of session it is.

        Three shapes on a :class:`LiveArm`: the live channel is open; it did not open and a
        typed question is answered by ``text_arm`` (``reason`` names why, in a code a client
        can branch on — ``insufficient_quota`` is not ``arm_dormant``); or it did not open
        and nothing can answer.

        **Whether anything can reply at all is its own field, and that is the fix for a
        session that looks alive and can never answer.** Production runs ``arm=composed``
        with no speech-to-text vendor provisioned: barge-in detection works, every frame is
        forwarded, and no reply is possible — which on a phone reads as "the VAD works but
        I get no audio". That was only ever in ``detail``, as prose, so the one place either
        client surfaced it was a line in the transcript log.

        It is **not** enough to put ``arm_dormant`` in ``reason`` and let the clients branch
        on that, which is what the first draft of this did: ``failure_reason`` already emits
        that same code for a :class:`LiveArm` whose channel would not open, and there a
        typed question *is* answered by ``text_arm``. One code, two opposite consequences.
        So ``can_answer`` says the thing directly — and it covers the third case neither
        code could, a ``LiveArm`` with no ``text_arm``, which has always been told it could
        type a question it cannot.
        """
        capabilities = self.arm.capabilities()
        detail = capabilities.dormant_reason or capabilities.notes
        reason: str | None = None
        if self.live is not None:
            detail = f"live conversation open · {detail}"
        elif not isinstance(self.arm, LiveArm) and capabilities.dormant_reason:
            reason = "arm_dormant"
        elif isinstance(self.arm, LiveArm):
            reason = self.live_failure_reason or "not_opened"
            if self.text_arm is not None:
                answered_by = (
                    f"answering typed questions with the {self.text_arm.name} arm"
                    if self.text_arm is not self.arm
                    else "typed questions only"
                )
            else:
                answered_by = "no arm can answer a typed question"
            detail = f"live conversation unavailable ({reason}); {answered_by} · {detail}"
        return SessionStateEvent(
            at_ms=self.clock.spoken_through_ms,
            state="ready",
            detail=detail,
            reason=reason,
            live=self.live is not None,
            # An open live channel answers by voice; otherwise it comes down to whether
            # the arm behind a typed question can hold a conversation. `text_arm is not
            # None` is **not** that test — a composed arm is its own `text_arm` whether or
            # not it has a speech-to-text vendor, which is exactly production's case.
            can_answer=(
                self.live is not None
                or (self.text_arm is not None and self.text_arm.capabilities().conversational)
            ),
        )

    def summary(self) -> dict[str, Any]:
        """What the session did — logged on close, and the shape a metric is read from."""
        return {
            "session_id": self.session_id,
            "arm": self.arm.name,
            "text_arm": self.text_arm.name if self.text_arm is not None else None,
            "live_failure_reason": self.live_failure_reason,
            # Every interruption this session decided — its detector's and the client's
            # explicit `barge_in` frames alike. A vendor's `speech_started` is not one:
            # that is `live_speech_starts`, the question that follows a barge-in.
            "barge_ins": len(self.decisions),
            "live_reopens": self._live_reopens,
            "spoken_through_ms": self.clock.spoken_through_ms,
            "max_provider_drift_ms": self.clock.max_provider_drift_ms,
            # The per-session cost line (motet#58). `motet.llm.tokens{stage="voice"}`
            # is the fleet-wide number and carries no session id, because a time series per
            # session is a time series per session forever; this is the line that has the
            # id in it. Rendered through `Ledger.summary` so a voice session's totals read
            # in the same field order as an episode's, and every field is present even at
            # zero for the reason `describe_usage` gives.
            #
            # **These two count the LLM *seam*, and a zero here is not a claim that the
            # session was free.** Two other vendor bills a session can run up are outside
            # it: the realtime arm bills through its own provider socket and never touches
            # `motet_inference.llm` (recording it means reading the `usage` off the
            # provider's `response.done`), and the composed arm's Cartesia synthesis is
            # billed per character, which `record_tts_characters` counts on the narration
            # path and nothing counts here. So read `llm_completions: 0` as "nothing went
            # through the seam this measures", never as "nothing was spent".
            "llm_completions": self.spend.requests,
            "llm_tokens": self.spend.summary(),
            **(self.live.summary() if self.live is not None else {}),
        }

    async def aclose(self) -> None:
        """End the session. Deliberately does **not** close the arm.

        The arm is process-wide — one per :class:`~motet_voice.app.VoiceApp`, shared by every
        concurrent session on the instance — and Cloud Run serves many requests per instance.
        A session closing it would take the vendor socket out from under whoever else is
        mid-conversation. The app owns the arm's lifetime; a session owns only its own state.
        """
        if self.live is not None:
            await self.live.aclose()
        logger.info("voice session closed: %s", self.summary())
