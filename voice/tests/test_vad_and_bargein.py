"""The detector: does it hear speech, and — the question that matters — does it ignore wind."""

from __future__ import annotations

from motet_voice.audio import iter_frames
from motet_voice.bargein import BargeInDecision, BargeInPolicy, VadTurnDetector
from motet_voice.harness import synthesize_walk
from motet_voice.vad import EnergyVad, ScriptedVad


def _run(pcm: bytes, policy: BargeInPolicy) -> list[BargeInDecision]:
    detector = VadTurnDetector(vad=EnergyVad(), policy=policy)
    detector.reset()
    return [
        decision
        for frame in iter_frames(pcm)
        if (decision := detector.observe(frame, narration_playing=True, spoken_through_ms=0))
        is not None
    ]


def test_speech_is_detected() -> None:
    walk = synthesize_walk(duration_ms=10_000, speech_at_ms=(3_000,), speech_duration_ms=1_500)
    decisions = _run(walk.pcm, BargeInPolicy(name="test"))

    assert decisions, "a clear 1.5s utterance must produce at least one barge-in"
    first = decisions[0]
    assert 3_000 <= first.at_ms <= 4_800, f"fired at {first.at_ms}ms, outside the utterance"


def test_the_refractory_window_collapses_one_utterance_into_one_decision() -> None:
    """Without this the headline metric counts syllables rather than interruptions."""
    walk = synthesize_walk(duration_ms=12_000, speech_at_ms=(4_000,), speech_duration_ms=4_000)
    decisions = _run(walk.pcm, BargeInPolicy(name="test", refractory_ms=5_000))
    assert len(decisions) == 1


def test_a_scripted_vad_needs_consecutive_frames_not_merely_enough_of_them() -> None:
    """Three isolated loud frames are a footstep. Three in a row are a syllable."""
    scattered = [0.9, 0.0, 0.9, 0.0, 0.9, 0.0] * 5
    detector = VadTurnDetector(
        vad=ScriptedVad(probabilities=scattered),
        policy=BargeInPolicy(name="test", consecutive_speech_frames=3, min_snr_db=0.0),
    )
    frames = list(iter_frames(synthesize_walk(duration_ms=2_000).pcm))
    assert not [
        decision
        for frame in frames
        if (decision := detector.observe(frame, narration_playing=True, spoken_through_ms=0))
    ]

    detector = VadTurnDetector(
        vad=ScriptedVad(probabilities=[0.9] * 10),
        policy=BargeInPolicy(name="test", consecutive_speech_frames=3, min_snr_db=0.0),
    )
    fired = [
        decision
        for frame in frames
        if (decision := detector.observe(frame, narration_playing=True, spoken_through_ms=0))
    ]
    assert fired


def test_require_narration_playing_suppresses_decisions_over_silence() -> None:
    walk = synthesize_walk(duration_ms=8_000, speech_at_ms=(4_000,))
    detector = VadTurnDetector(
        vad=EnergyVad(), policy=BargeInPolicy(name="test", require_narration_playing=True)
    )
    assert not [
        decision
        for frame in iter_frames(walk.pcm)
        if (decision := detector.observe(frame, narration_playing=False, spoken_through_ms=0))
    ]


def test_a_decision_carries_the_evidence_that_produced_it() -> None:
    """A barge-in nobody can explain afterwards is a barge-in nobody can debug."""
    walk = synthesize_walk(duration_ms=8_000, speech_at_ms=(4_000,))
    decision = _run(walk.pcm, BargeInPolicy(name="evidence"))[0]
    payload = decision.to_json()

    for field in (
        "at_ms",
        "onset_ms",
        "latency_ms",
        "arm",
        "variant",
        "trigger",
        "speech_probability",
        "rms_dbfs",
        "noise_floor_dbfs",
        "snr_db",
        "zero_crossing_rate",
        "consecutive_frames",
        "spoken_through_ms",
    ):
        assert field in payload, f"a decision must record {field}"
    assert payload["variant"] == "evidence"
    assert BargeInDecision.from_json(payload).at_ms == decision.at_ms


def test_a_long_utterance_does_not_talk_the_floor_up_to_itself() -> None:
    """The feedback loop this guards against made the detector *quieter* the stricter it got.

    With an exponential-average floor, a sentence drags the floor up to the speaker's own
    voice and the SNR collapses mid-word — the speaker gets cut off for talking too long. The
    quantile tracker bounds that: the floor can move at most ``up_step_db`` per 20 ms frame,
    so four seconds of speech can lift it by at most about 10 dB however loud it is, and what
    matters is that the *end* of a long utterance still reads as speech.
    """
    walk = synthesize_walk(duration_ms=12_000, speech_at_ms=(4_000,), speech_duration_ms=4_000)
    vad = EnergyVad()
    readings = {frame.start_ms: vad.observe(frame) for frame in iter_frames(walk.pcm)}

    climb = readings[7_800].noise_floor_dbfs - readings[3_800].noise_floor_dbfs
    assert climb <= 4_000 / 20 * vad.up_step_db + 0.01, "the floor moved faster than its bound"

    late = max(reading.snr_db for offset, reading in readings.items() if 7_000 <= offset < 8_000)
    assert late > 15.0, (
        f"by the last second of a four-second utterance the best SNR was only {late:.1f} dB; "
        "the floor has climbed to the speaker"
    )


def test_the_noise_floor_still_follows_the_environment() -> None:
    """The other half: a floor that never adapts is a floor calibrated to the wrong street."""
    quiet = synthesize_walk(duration_ms=6_000, wind=False, traffic=False, footsteps=False)
    windy = synthesize_walk(duration_ms=6_000)

    def settled(pcm: bytes) -> float:
        vad = EnergyVad()
        floor = 0.0
        for frame in iter_frames(pcm):
            floor = vad.observe(frame).noise_floor_dbfs
        return floor

    assert settled(windy.pcm) > settled(quiet.pcm) + 10.0
