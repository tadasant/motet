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
import logging
from dataclasses import dataclass, field
from typing import Any

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
from .realtime import ArmDormant, RealtimeArm, TurnRequest
from .tools import ToolRegistry

logger = logging.getLogger("motet.voice.session")


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


@dataclass
class VoiceSession:
    """A single conversation, from ``StartSession`` to socket close."""

    session_id: str
    config: StartSessionRequest
    arm: RealtimeArm
    tools: ToolRegistry
    detector: TurnDetector
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

    @classmethod
    def create(
        cls,
        *,
        session_id: str,
        config: StartSessionRequest,
        arm: RealtimeArm,
        tools: ToolRegistry,
        clock: PlaybackClock | None = None,
    ) -> VoiceSession:
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
            detector=arm.build_turn_detector(policy_from(config.turn_policy)),
            clock=session_clock,
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
        return events

    def barge_in(self, *, trigger: str = "client") -> InterruptedAtEvent:
        """An explicit interruption from the client — push-to-talk, or a button."""
        offset = self.clock.interrupt()
        return InterruptedAtEvent(
            at_ms=offset,
            offset_ms=offset,
            decision={"trigger": trigger, "arm": self.arm.name},
        )

    def _interrupt(self, decision: BargeInDecision) -> InterruptedAtEvent:
        offset = self.clock.interrupt()
        # The decision was built with the position *at the frame*; the authoritative offset
        # is the one the clock froze at, so the event carries that and the decision record
        # carries its own. They agree in practice and the event's is the one that binds.
        self.decisions.append(decision)
        return InterruptedAtEvent(at_ms=offset, offset_ms=offset, decision=decision.to_json())

    # -- narration ----------------------------------------------------------------------

    def narration_delivered(self, duration_ms: int) -> None:
        self.clock.deliver(duration_ms)
        self.clock.start()

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
        request = TurnRequest(
            persona_instructions=self.config.persona.instructions,
            voice=self.config.persona.voice,
            user_text=text,
            context_notes=self.config.context.notes,
            history=list(self.history),
            tools=self.tools.describe(),
        )
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
                turn = await self.arm.respond(request)
            except ArmDormant as exc:
                return [
                    ErrorEvent(
                        at_ms=self.clock.spoken_through_ms, code="arm_dormant", message=str(exc)
                    )
                ]
            finally:
                # In a `finally` because a turn that failed *after* its completion arrived
                # was still billed for it, and dropping that from the session total would
                # be motet#58's own defect one scope smaller. The metric is already safe —
                # it is written where the call is made — but this line is the only place
                # the session id meets the number.
                self._record_turn_spend(turn_spend)

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
                )
            )
            # Deliberately **not** `self.clock.deliver(...)`. This is the assistant *answering*,
            # not the briefing being narrated, and `spoken_through_ms` is a position in the
            # episode. Folding a reply into it would advance read state by however long the
            # assistant talked for, which is the sort of error nobody notices until a story
            # is marked read that the listener never heard.

        return events

    # -- cost, per turn and per session --------------------------------------------------

    def _record_turn_spend(self, turn_spend: Ledger) -> None:
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
            self.arm.name,
            self._turns_seen,
            turn_spend.summary(),
        )

    # -- lifecycle ----------------------------------------------------------------------

    def ready(self) -> SessionStateEvent:
        capabilities = self.arm.capabilities()
        return SessionStateEvent(
            at_ms=self.clock.spoken_through_ms,
            state="ready",
            detail=capabilities.dormant_reason or capabilities.notes,
        )

    def summary(self) -> dict[str, Any]:
        """What the session did — logged on close, and the shape a metric is read from."""
        return {
            "session_id": self.session_id,
            "arm": self.arm.name,
            "barge_ins": len(self.decisions),
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
        }

    async def aclose(self) -> None:
        """End the session. Deliberately does **not** close the arm.

        The arm is process-wide — one per :class:`~motet_voice.app.VoiceApp`, shared by every
        concurrent session on the instance — and Cloud Run serves many requests per instance.
        A session closing it would take the vendor socket out from under whoever else is
        mid-conversation. The app owns the arm's lifetime; a session owns only its own state.
        """
        logger.info("voice session closed: %s", self.summary())
