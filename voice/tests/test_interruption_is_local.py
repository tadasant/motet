"""Interruption is decided locally, on every arm, from every packet (motet#93).

Two halves, and both were needed before a person's microphone could interrupt a briefing:

* **The detector's noise floor.** A browser microphone with echo cancellation and noise
  suppression on delivers -60 to -70 dBFS of residual between utterances, never exact zeros.
  Those frames were ignored as silence, so the first frame the floor seeded from was the
  listener's own voice — every utterance then read as ~0 dB SNR, and the owner's sessions
  closed with ``barge_ins: 0`` while the mic meter moved.
* **The order inside the session.** The detector observes every packet first, always —
  including while the live channel is forwarding — and a decision emits ``interrupted_at``
  before the vendor is handed the position and the pre-roll.
"""

from __future__ import annotations

import asyncio
import logging
import math
from collections.abc import AsyncIterator, Mapping
from typing import Any

import pytest
from motet_voice.audio import DEFAULT_FRAME_MS, TARGET_SAMPLE_RATE, iter_frames
from motet_voice.bargein import BargeInPolicy, VadTurnDetector
from motet_voice.clock import PlaybackClock
from motet_voice.config import VoiceSettings
from motet_voice.contract import InterruptedAtEvent, StartSessionRequest
from motet_voice.realtime import OpenAiRealtimeArm, TurnRequest
from motet_voice.session import VoiceSession
from motet_voice.tools import ToolRegistry
from motet_voice.vad import EnergyVad

FRAME_SAMPLES = TARGET_SAMPLE_RATE * DEFAULT_FRAME_MS // 1000


def _tone(*, dbfs: float, ms: int, hz: float = 300.0) -> bytes:
    """A sine at ``dbfs`` RMS. 300 Hz sits inside the detector's speech ZCR band."""
    amplitude = math.sqrt(2) * 32768 * 10 ** (dbfs / 20)
    count = TARGET_SAMPLE_RATE * ms // 1000
    out = bytearray()
    for index in range(count):
        value = round(amplitude * math.sin(2 * math.pi * hz * index / TARGET_SAMPLE_RATE))
        out += int.to_bytes(value & 0xFFFF, 2, "little")
    return bytes(out)


#: The browser's shape: a second of -65 dBFS residual, then somebody talking at -25 dBFS.
QUIET_ROOM = _tone(dbfs=-65.0, ms=1_000, hz=1_000.0)
SPEECH = _tone(dbfs=-25.0, ms=600)

TRANSCRIPT = [
    {
        "title": "Acme raises a Series B",
        "start_ms": 0,
        "end_ms": 60_000,
        "claims": [{"start_ms": 0, "end_ms": 60_000, "spoken_text": "Acme raised forty million."}],
    }
]


def _decisions(pcm: bytes, policy: BargeInPolicy) -> int:
    detector = VadTurnDetector(vad=EnergyVad(), policy=policy)
    fired = 0
    for frame in iter_frames(pcm, frame_ms=DEFAULT_FRAME_MS):
        if detector.observe(frame, narration_playing=True, spoken_through_ms=0) is not None:
            fired += 1
    return fired


# ---------------------------------------------------------------------- the noise floor


def test_a_quiet_residual_lead_in_no_longer_blinds_the_detector() -> None:
    """The owner's browser: quiet residual, then a question. It has to fire."""
    assert _decisions(QUIET_ROOM + SPEECH, BargeInPolicy(name="browser")) == 1


def test_a_quiet_frame_seeds_the_floor_at_the_absolute_floor(
    caplog: pytest.LogCaptureFixture,
) -> None:
    vad = EnergyVad()
    frame = next(iter(iter_frames(QUIET_ROOM, frame_ms=DEFAULT_FRAME_MS)))
    with caplog.at_level(logging.INFO, logger="motet.voice.vad"):
        reading = vad.observe(frame)
    assert reading.speech_probability == 0.0
    assert reading.noise_floor_dbfs == vad.absolute_floor_dbfs
    assert "noise floor seeded at -55.0 dBFS from a quiet frame" in caplog.text


def test_a_floor_seeded_too_high_walks_down_on_quiet_frames() -> None:
    """Speech first, then the quiet room: the floor recovers at the ordinary rate."""
    vad = EnergyVad()
    frames = list(iter_frames(SPEECH + QUIET_ROOM, frame_ms=DEFAULT_FRAME_MS))
    speech_frames = len(SPEECH) // (FRAME_SAMPLES * 2)
    for frame in frames[:speech_frames]:
        vad.observe(frame)
    high = vad.observe(frames[speech_frames]).noise_floor_dbfs
    after = high
    for frame in frames[speech_frames + 1 : speech_frames + 11]:
        after = vad.observe(frame).noise_floor_dbfs
    assert after == pytest.approx(max(vad.absolute_floor_dbfs, high - 10 * vad.down_step_db))
    assert after < high


def test_exact_zeros_still_neither_seed_nor_move_the_floor() -> None:
    """The voice-memo export's lead-in is still not evidence about the room."""
    vad = EnergyVad()
    zeros = bytes(FRAME_SAMPLES * 2 * 50)
    for frame in iter_frames(zeros, frame_ms=DEFAULT_FRAME_MS):
        vad.observe(frame)
    assert vad._floor_dbfs is None  # noqa: SLF001 — the property under test is "unset"


# ------------------------------------------------------------------ the session's order


class _Recorder:
    """A live conversation that records every call, in order, and says nothing back."""

    def __init__(self) -> None:
        self.calls: list[str] = []
        self.appended = 0

    async def start(self) -> None:
        self.calls.append("start")

    async def append_audio(self, pcm: bytes) -> None:
        self.calls.append("append")
        self.appended += len(pcm)

    async def add_context(self, text: str) -> None:
        self.calls.append("context")

    async def add_user_text(self, text: str) -> None:
        self.calls.append("user")

    async def tool_output(self, call_id: str, output: Mapping[str, Any]) -> None:
        self.calls.append("tool")

    async def truncate(self, item_id: str, audio_end_ms: int) -> None:
        self.calls.append("truncate")

    async def cancel_response(self) -> None:
        self.calls.append("cancel")

    async def events(self) -> AsyncIterator[Any]:
        await asyncio.Event().wait()
        yield  # pragma: no cover

    async def aclose(self) -> None:
        self.calls.append("close")


class _Clock:
    def __init__(self) -> None:
        self.now = 100.0

    def __call__(self) -> float:
        return self.now


def _session(recorder: _Recorder) -> tuple[VoiceSession, _Clock]:
    ticks = _Clock()
    arm = OpenAiRealtimeArm(model="test", conversation_factory=lambda _req: recorder)
    config = StartSessionRequest.model_validate(
        {
            "persona": {"name": "Motet", "instructions": "Answer briefly."},
            "context": {"transcript": TRANSCRIPT, "spoken_through_ms": 10_000},
        }
    )
    session = VoiceSession.create(
        session_id="vs_local",
        config=config,
        arm=arm,
        tools=ToolRegistry({}),
        clock=PlaybackClock(now=ticks),
    )
    return session, ticks


async def _feed(session: VoiceSession, pcm: bytes, *, packet_ms: int = 100) -> list[Any]:
    step = TARGET_SAMPLE_RATE * packet_ms // 1000 * 2
    events: list[Any] = []
    for offset in range(0, len(pcm), step):
        events.extend(await session.receive_audio(pcm[offset : offset + step]))
    return events


def test_the_detector_decides_before_the_vendor_hears_anything(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """No client frame at all: the quiet room, then speech. ``interrupted_at`` comes first,
    then the vendor gets the position, then the pre-roll — and nothing reached it before."""
    recorder = _Recorder()
    session, ticks = _session(recorder)

    async def run() -> list[Any]:
        await session.start_live()
        session.narration_delivered(60_000)
        ticks.now += 2.0
        quiet = await _feed(session, QUIET_ROOM)
        assert quiet == []
        assert recorder.calls == ["start"], "narration was forwarded to the vendor"
        return await _feed(session, SPEECH)

    with caplog.at_level(logging.INFO, logger="motet.voice.session"):
        events = asyncio.run(run())

    assert isinstance(events[0], InterruptedAtEvent)
    assert events[0].offset_ms == 12_000
    # The realtime arm's local detector is the labelled emulation of the vendor's dials.
    assert events[0].decision["trigger"] == "openai_server_vad_emulated"
    assert events[1].type == "session_state" and events[1].state == "listening"
    assert recorder.calls[:3] == ["start", "context", "append"], (
        "the position has to be in the conversation before any audio"
    )
    assert session.summary()["barge_ins"] == 1
    assert "barge-in: session=vs_local" in caplog.text
    assert "listener audio: session=vs_local why=barge_in route=" in caplog.text


def test_never_mind_resume_closes_the_gate_and_the_walk_stays_interruptible() -> None:
    """After a barge-in, ``narration_resumed`` cancels the vendor's reply and stops
    forwarding; narration is not billed, and the next utterance interrupts again."""
    recorder = _Recorder()
    session, ticks = _session(recorder)

    async def run() -> list[Any]:
        await session.start_live()
        session.narration_delivered(60_000)
        ticks.now += 1.0
        await _feed(session, QUIET_ROOM + SPEECH)
        assert session.live is not None and session.live.active
        await session.narration_resumed(12_000)
        assert not session.live.active
        assert "cancel" in recorder.calls
        appended_before = recorder.appended
        # Two seconds of narration bleeding into the mic: none of it may reach the vendor.
        ticks.now += 3.0  # past the refractory window
        await _feed(session, QUIET_ROOM + QUIET_ROOM)
        assert recorder.appended == appended_before, "narration was billed as audio tokens"
        return await _feed(session, SPEECH)

    events = asyncio.run(run())
    assert any(isinstance(event, InterruptedAtEvent) for event in events), (
        "the walk became uninterruptible after never mind"
    )
    assert session.summary()["barge_ins"] == 2


def test_a_client_barge_in_is_counted(settings: VoiceSettings) -> None:
    """A push-to-talk session used to close with ``barge_ins: 0``."""
    recorder = _Recorder()
    session, _ = _session(recorder)

    async def run() -> list[Any]:
        await session.start_live()
        session.narration_delivered(60_000)
        return await session.client_barge_in()

    events = asyncio.run(run())
    assert isinstance(events[0], InterruptedAtEvent)
    assert events[0].decision["trigger"] == "client"
    assert session.summary()["barge_ins"] == 1


def test_a_paused_player_is_not_a_barge_in_and_engages_nothing() -> None:
    recorder = _Recorder()
    session, ticks = _session(recorder)

    async def run() -> None:
        await session.start_live()
        session.narration_delivered(60_000)
        ticks.now += 1.0
        session.narration_paused(11_000)
        # Talking while paused: `require_narration_playing` means nothing to interrupt.
        await _feed(session, QUIET_ROOM + SPEECH)

    asyncio.run(run())
    assert session.clock.spoken_through_ms == 11_000
    assert not session.clock.playing
    assert session.summary()["barge_ins"] == 0
    assert recorder.calls == ["start"]


def test_the_composed_arm_interrupts_from_the_same_detector(settings: VoiceSettings) -> None:
    """No live channel at all, and interruption still fires from listener audio."""
    from motet_voice.realtime import build_composed_arm  # noqa: PLC0415

    ticks = _Clock()
    session = VoiceSession.create(
        session_id="vs_composed",
        config=StartSessionRequest.model_validate(
            {
                "persona": {"name": "Motet", "instructions": "x"},
                "context": {"transcript": TRANSCRIPT},
            }
        ),
        arm=build_composed_arm(settings),
        tools=ToolRegistry({}),
        clock=PlaybackClock(now=ticks),
    )

    async def run() -> list[Any]:
        session.narration_delivered(60_000)
        ticks.now += 5.0
        return await _feed(session, QUIET_ROOM + SPEECH)

    events = asyncio.run(run())
    assert [type(event) for event in events] == [InterruptedAtEvent]
    assert events[0].context["segment_title"] == "Acme raises a Series B"


def test_turn_request_type_is_unchanged_for_the_harness() -> None:
    """Guard: the position field stays optional, so the harness is prompted as before."""
    assert TurnRequest(persona_instructions="p", voice="v").position_notes == ""
