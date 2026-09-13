"""The interruption is decided by the session's own detector, on every arm.

Invariant 4 says the clock is ours; this module pins the half of it that the live channel
made easy to lose: **who decides that the listener has interrupted the briefing.** A vendor
that only hears audio once the floor has been taken cannot be the one to decide when to
take it, so ``interrupted_at`` has to come from the session's detector before a byte reaches
a vendor — and it has to come from the same detector when there is no vendor at all.

Three things are pinned, each against a fake clock and a fake vendor socket:

* live channel open — the local detector fires, the clock freezes and ``interrupted_at`` is
  emitted; only *then* does the vendor get the position and the pre-roll;
* live channel unavailable — the identical interruption fires, and the typed question is
  answered by the composed fallback;
* narration resumed while the channel is still engaged — the detector keeps deciding, the
  vendor gets the new position, and nothing is flushed twice.

The fourth half is the detector itself: a browser microphone with echo cancellation and
noise suppression on delivers *quiet* frames between utterances — below the absolute floor,
never exact zeros — and the noise floor has to seed and recover from those, or the first
frame above the floor is the listener's own voice and nothing ever fires.
"""

from __future__ import annotations

import asyncio
import random
import struct
from collections.abc import AsyncIterator, Callable, Mapping
from dataclasses import dataclass
from typing import Any

from motet_voice.audio import iter_frames
from motet_voice.bargein import BargeInPolicy, VadTurnDetector
from motet_voice.clock import PlaybackClock
from motet_voice.config import VoiceSettings
from motet_voice.contract import StartSessionRequest
from motet_voice.harness import synthesize_walk
from motet_voice.realtime import ComposedArm, LiveArm, TurnRequest, build_openai_arm
from motet_voice.realtime.interfaces import ConversationModel
from motet_voice.session import VoiceSession
from motet_voice.tools import ToolRegistry
from motet_voice.vad import EnergyVad

TRANSCRIPT = [
    {
        "title": "Acme raises a Series B",
        "start_ms": 0,
        "end_ms": 20_000,
        "claims": [
            {"start_ms": 0, "end_ms": 10_000, "spoken_text": "Acme raised forty million."},
            {
                "start_ms": 10_000,
                "end_ms": 20_000,
                "spoken_text": "The company is valued at roughly 300 million dollars.",
            },
        ],
    },
]
CONFIG = StartSessionRequest.model_validate(
    {
        "persona": {"name": "Motet", "instructions": "Answer briefly.", "voice": "narrator"},
        "context": {
            "notes": "Acme raised forty million.",
            "transcript": TRANSCRIPT,
            "spoken_through_ms": 12_000,
        },
        "turn_policy": {"mode": "open_mic"},
    }
)

PACKET_MS = 200
PACKET_BYTES = PACKET_MS * 32


class QuietTransport:
    """A vendor socket that opens and then says nothing on its own.

    Every event the session ever gets from it is one this test put there, so a decision
    made while the vendor is silent is a decision the session made by itself.
    """

    def __init__(self) -> None:
        self.sent: list[dict[str, Any]] = []
        #: What the session's clock said at the moment of each send, in the same order as
        #: :attr:`sent` — so a test can ask "was the clock already frozen when the vendor
        #: first heard anything" rather than inferring it from what came back.
        self.clock_at_send: list[tuple[bool, int | None]] = []
        self.witness: Callable[[], tuple[bool, int | None]] | None = None
        self._queue: asyncio.Queue[dict[str, Any] | None] = asyncio.Queue()

    async def send(self, event: Mapping[str, Any]) -> None:
        self.sent.append(dict(event))
        self.clock_at_send.append(self.witness() if self.witness else (False, None))
        if event["type"] == "session.update":
            self._queue.put_nowait({"type": "session.created"})

    async def events(self) -> AsyncIterator[dict[str, Any]]:
        while True:
            event = await self._queue.get()
            if event is None:
                return
            yield event

    async def aclose(self) -> None:
        self._queue.put_nowait(None)

    def kinds(self) -> list[str]:
        return [s["type"] for s in self.sent]

    def appended_bytes(self) -> int:
        import base64  # noqa: PLC0415

        return sum(
            len(base64.b64decode(s["audio"]))
            for s in self.sent
            if s["type"] == "input_audio_buffer.append"
        )


class RefusingTransport:
    """A vendor socket that will not open."""

    def __init__(self) -> None:
        self.sent: list[dict[str, Any]] = []

    async def send(self, event: Mapping[str, Any]) -> None:
        self.sent.append(dict(event))
        raise ConnectionError("vendor refused")

    async def events(self) -> AsyncIterator[dict[str, Any]]:
        raise ConnectionError("vendor refused")
        yield  # pragma: no cover

    async def aclose(self) -> None:
        return None


@dataclass
class RecordingModel:
    requests: list[TurnRequest]

    @property
    def name(self) -> str:
        return "recording"

    def reply(self, request: TurnRequest, user_text: str) -> str:
        self.requests.append(request)
        return f"roughly 300 million dollars (you asked: {user_text})"


def _text_arm(model: ConversationModel) -> ComposedArm:
    from motet_inference.fakes import FakeSpeechSynthesizer  # noqa: PLC0415

    return ComposedArm(model=model, synthesizer=FakeSpeechSynthesizer(), conversational=True)


class FakeTime:
    """A monotonic clock the test advances by hand, in step with the audio it feeds."""

    def __init__(self) -> None:
        self.seconds = 100.0

    def __call__(self) -> float:
        return self.seconds


def _session(
    settings: VoiceSettings, transport: Any, *, text_arm: ComposedArm | None = None
) -> tuple[VoiceSession, FakeTime]:
    arm = build_openai_arm(settings, transport=transport)
    assert isinstance(arm, LiveArm)
    now = FakeTime()
    session = VoiceSession.create(
        session_id="vs_local",
        config=CONFIG,
        arm=arm,
        tools=ToolRegistry({}),
        clock=PlaybackClock(now=now),
        text_arm=text_arm,
    )
    return session, now


def _quiet(duration_ms: int, *, level_dbfs: float = -65.0, seed: int = 7) -> bytes:
    """A gated microphone between utterances: faint noise, well under the absolute floor."""
    rng = random.Random(seed)
    amplitude = int(32767 * 10 ** (level_dbfs / 20))
    count = duration_ms * 16
    return struct.pack(f"<{count}h", *(rng.randint(-amplitude, amplitude) for _ in range(count)))


def _speech(duration_ms: int = 1_500) -> bytes:
    """An utterance with the harness's ordinary mic noise under it (~-48 dBFS)."""
    walk = synthesize_walk(
        duration_ms=duration_ms,
        speech_at_ms=(0,),
        speech_duration_ms=duration_ms,
        wind=False,
        traffic=False,
        footsteps=False,
    )
    return walk.pcm


def _packets(pcm: bytes) -> list[bytes]:
    return [pcm[i : i + PACKET_BYTES] for i in range(0, len(pcm), PACKET_BYTES)]


async def _feed(
    session: VoiceSession, now: FakeTime, pcm: bytes, transport: QuietTransport | None = None
) -> list[tuple[Any, list[str]]]:
    """Feed ``pcm`` as 200 ms packets, advancing the clock, and pair every event with what
    the vendor had been sent *by the time it was produced*."""
    out: list[tuple[Any, list[str]]] = []
    for packet in _packets(pcm):
        now.seconds += PACKET_MS / 1000
        for event in await session.receive_audio(packet):
            out.append((event, transport.kinds() if transport is not None else []))
    return out


# ----------------------------------------------------------------------- live channel open


def test_the_local_detector_interrupts_before_the_vendor_hears_a_byte(
    settings: VoiceSettings,
) -> None:
    """Live open, narration playing, speech on the mic: ``interrupted_at`` comes from the
    session's own detector, the clock is frozen at it, and only then does the vendor get
    the position and the pre-roll."""
    transport = QuietTransport()
    session, now = _session(settings, transport)
    transport.witness = lambda: (session.clock.playing, session.clock.interrupted_at_ms)

    async def run() -> list[tuple[Any, list[str]]]:
        await session.start_live()
        assert session.live is not None
        session.narration_delivered(60_000)
        assert session.clock.playing
        events = await _feed(session, now, _quiet(1_000) + _speech(), transport)
        await session.aclose()
        return events

    events = asyncio.run(run())

    interruptions = [e for e, _ in events if e.type == "interrupted_at"]
    assert len(interruptions) == 1, [e.type for e, _ in events]
    interrupted = interruptions[0]
    assert interrupted.decision["trigger"] == "openai_server_vad_emulated", (
        "the interruption is the session's detector's, never the vendor's"
    )
    assert interrupted.decision["narration_playing"] is True
    # The clock froze at the interruption, and at the position narration had reached.
    assert not session.clock.playing
    assert interrupted.offset_ms == session.clock.spoken_through_ms
    assert 13_000 <= interrupted.offset_ms <= 14_600, interrupted.offset_ms
    assert interrupted.context["segment_title"] == "Acme raises a Series B"
    assert "300 million" in interrupted.context["claim_text"]
    # Nothing had reached the vendor when the decision was made — not the position, not a
    # byte of audio. The session decided alone: at the moment of the vendor's first send
    # after `session.update`, the clock was already frozen at the interruption.
    kinds = transport.kinds()
    assert transport.clock_at_send[0] == (False, None), "session.update went out before play"
    assert all(clock == (False, interrupted.offset_ms) for clock in transport.clock_at_send[1:]), (
        transport.clock_at_send
    )
    # And then the floor is handed over: position item, pre-roll, live audio.
    assert kinds[:3] == ["session.update", "conversation.item.create", "input_audio_buffer.append"]
    assert transport.sent[1]["item"]["role"] == "system"
    assert "interrupted the briefing at 0:1" in transport.sent[1]["item"]["content"][0]["text"]
    assert kinds.count("input_audio_buffer.append") >= 2, "live audio follows the pre-roll"
    # The `listening` frame rides in the same batch, after the interruption.
    index = next(i for i, (e, _) in enumerate(events) if e.type == "interrupted_at")
    following = events[index + 1][0]
    assert following.type == "session_state" and following.state == "listening"
    assert session.summary()["barge_ins"] == 1
    assert session.summary()["live_speech_starts"] == 0, "the vendor never said a word"


def test_narration_resumed_while_engaged_is_still_the_detectors_call(
    settings: VoiceSettings,
) -> None:
    """The listener took the floor, said nothing, pressed resume. The channel is still
    forwarding; the next utterance must still freeze the clock here — and the vendor gets
    the new position without the pre-roll being flushed a second time."""
    transport = QuietTransport()
    session, now = _session(settings, transport)

    async def run() -> tuple[list[tuple[Any, list[str]]], int, list[tuple[Any, list[str]]]]:
        await session.start_live()
        session.narration_delivered(60_000)
        first = await _feed(session, now, _quiet(1_000) + _speech(), transport)
        assert session.live is not None and session.live.active
        appended_after_first = transport.appended_bytes()
        # The client resumed narration; the vendor was never told the floor changed hands.
        session.narration_delivered(0)
        assert session.clock.playing
        # Past the refractory window, then speech again.
        second = await _feed(session, now, _quiet(2_600) + _speech(), transport)
        await session.aclose()
        return first, appended_after_first, second

    first, appended_after_first, second = asyncio.run(run())

    assert [e.type for e, _ in first if e.type == "interrupted_at"] == ["interrupted_at"]
    again = [e for e, _ in second if e.type == "interrupted_at"]
    assert len(again) == 1, [e.type for e, _ in second]
    assert again[0].decision["trigger"] == "openai_server_vad_emulated"
    assert not session.clock.playing
    assert again[0].offset_ms > first[0][0].offset_ms, "narration had moved on before it froze"
    assert session.summary()["barge_ins"] == 2
    # Two position items went in — one per interruption — and the second one carried
    # neither a second pre-roll nor a `listening` frame: the channel was already open.
    positions = [
        s
        for s in transport.sent
        if s["type"] == "conversation.item.create"
        if s["item"].get("role") == "system"
    ]
    assert len(positions) == 2
    assert [e.type for e, _ in second] == ["interrupted_at"]
    # Every byte of the second stretch was forwarded exactly once: 16 kHz in, 24 kHz to
    # the vendor, so half again as many bytes.
    second_bytes = len(_quiet(2_600) + _speech())
    forwarded = transport.appended_bytes() - appended_after_first
    assert forwarded == second_bytes * 3 // 2, (forwarded, second_bytes)


# ---------------------------------------------------------------- live channel unavailable


def test_without_a_live_channel_the_same_interruption_fires_and_the_fallback_answers(
    settings: VoiceSettings,
) -> None:
    """Vendor refuses at open → the identical local interruption → the typed question is
    answered by the composed arm with the frozen position in its prompt."""
    requests: list[TurnRequest] = []
    transport = RefusingTransport()
    session, now = _session(settings, transport, text_arm=_text_arm(RecordingModel(requests)))

    async def run() -> tuple[list[tuple[Any, list[str]]], list[Any]]:
        await session.start_live()
        assert session.live is None
        session.narration_delivered(60_000)
        events = await _feed(session, now, _quiet(1_000) + _speech())
        answer = await session.respond_to_text("what number was that?")
        return events, answer

    events, answer = asyncio.run(run())

    interruptions = [e for e, _ in events if e.type == "interrupted_at"]
    assert len(interruptions) == 1, [e.type for e, _ in events]
    assert interruptions[0].decision["trigger"] == "openai_server_vad_emulated"
    assert not session.clock.playing
    assert [e.type for e, _ in events] == ["interrupted_at"], "no live channel, no `listening`"
    assert [s["type"] for s in transport.sent] == ["session.update"], "the vendor was retried"

    kinds = [(e.type, getattr(e, "speaker", None)) for e in answer]
    assert kinds == [("transcript", "user"), ("transcript", "assistant"), ("audio_chunk", None)]
    assert "roughly 300 million dollars" in answer[1].text
    assert len(requests) == 1
    assert "interrupted the briefing at 0:1" in requests[0].position_notes
    assert session.summary()["barge_ins"] == 1
    assert session.summary()["live_failure_reason"] == "connection_error"


# --------------------------------------------------------------- the detector on a gated mic


def _decisions(pcm: bytes, *, vad: EnergyVad | None = None) -> list[int]:
    detector = VadTurnDetector(vad=vad or EnergyVad(), policy=BargeInPolicy(name="test"))
    return [
        decision.at_ms
        for frame in iter_frames(pcm)
        if (decision := detector.observe(frame, narration_playing=True, spoken_through_ms=0))
        is not None
    ]


def test_a_gated_microphone_still_seeds_a_floor_the_listener_clears() -> None:
    """Quiet frames — under the absolute floor, not zeros — are what a browser mic with
    noise suppression delivers between utterances. They must seed the floor low, so that
    the first utterance is heard rather than becoming the floor."""
    fired = _decisions(_quiet(2_000) + _speech())
    assert fired, "an utterance after two seconds of a gated mic must interrupt"
    assert 2_000 <= fired[0] <= 2_800, f"fired at {fired[0]}ms, not at the utterance"


def test_a_floor_seeded_at_the_listeners_voice_recovers_through_quiet() -> None:
    """The failure this guards: speech is the first thing the mic delivers, the floor seeds
    at speech level, and — before — nothing that followed could bring it down, because
    frames under the floor did not move it. Now they walk it down at 7.5 dB a second."""
    vad = EnergyVad()
    first = _decisions(_speech(), vad=vad)
    assert not first, "seeded at the listener's own voice, the first utterance is missed"
    settled = vad.observe(next(iter_frames(_quiet(20)))).noise_floor_dbfs
    assert settled > vad.absolute_floor_dbfs + 10, "the floor really did seed at speech level"

    recovered = _decisions(_quiet(4_000) + _speech(), vad=vad)
    assert recovered, "four seconds of a quiet mic must bring the floor back down"
    assert recovered[0] >= 4_000


def test_exact_digital_silence_still_seeds_nothing() -> None:
    """Zeros are a dropout, not a quiet room: they neither seed nor move the floor, so the
    export-with-a-silent-head case keeps the behaviour the harness was tuned on."""
    vad = EnergyVad()
    for frame in iter_frames(bytes(16_000 * 2 * 3)):
        reading = vad.observe(frame)
    assert reading.noise_floor_dbfs == vad.absolute_floor_dbfs
    assert vad._floor_dbfs is None, "zeros must not have seeded the floor"
