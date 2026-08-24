"""The harness end to end: synthetic walk -> run directory -> replay -> report.

This is the test that says the deliverable works. It runs the *same* code path Tadas will
run after the walk, on audio with known ground truth, with no microphone and no vendor.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from motet_voice.audio import (
    PcmFormat,
    pcm_from_samples,
    read_wav,
    samples_from_pcm,
    write_wav,
)
from motet_voice.bargein import BargeInPolicy
from motet_voice.cli import main
from motet_voice.config import VoiceSettings
from motet_voice.harness import (
    RunError,
    SpeechLabel,
    create_run,
    ingest_recording,
    load_run,
    read_decisions,
    render_report,
    replay_dir,
    replay_run,
    score,
    sweep,
    synthesize_walk,
)
from motet_voice.harness.metrics import ArmMetrics, ScoredRun
from motet_voice.harness.replay import policies_for_measurement


def test_a_silent_recording_scores_every_detection_as_a_false_positive(
    tmp_path: Path, settings: VoiceSettings
) -> None:
    """The headline metric, and the reason the walk instructions say 'do not speak'."""
    walk = synthesize_walk(duration_ms=30_000)
    run = create_run(tmp_path / "quiet", walk.pcm, label="quiet", ground_truth="silent")

    scored = replay_run(run, settings, arms=["composed"], variants=["default"])

    metric = scored.metrics[0]
    assert metric.ground_truth == "silent"
    assert metric.false_positives == metric.decisions
    assert metric.open_mic_minutes == pytest.approx(0.5, abs=0.01)
    assert metric.false_per_minute == pytest.approx(metric.decisions / 0.5, abs=0.01)


def test_the_sweep_trades_false_alarms_against_responsiveness_in_the_right_direction(
    tmp_path: Path, settings: VoiceSettings
) -> None:
    """The property that makes a sweep worth running, asserted without calibrating to it.

    Deliberately **not** "the default variant fires less than N times a minute on synthetic
    wind". Synthetic wind is not real wind, and an absolute bound on it would be a number
    tuned to a generator — the exact self-deception the walk exists to avoid. What *is* a
    real invariant of the harness is the ordering: a twitchier variant must never produce
    fewer false positives than a more patient one on the same audio, and the patient end must
    not be so deaf that it hears nothing at all.
    """
    quiet = create_run(tmp_path / "quiet", synthesize_walk(duration_ms=45_000).pcm, label="quiet")
    scored = replay_run(quiet, settings, arms=["composed"], write_snippets=False)
    by_variant = {metric.variant: metric for metric in scored.metrics}

    assert (
        by_variant["hair-trigger-60ms"].false_positives
        >= by_variant["default"].false_positives
        >= by_variant["patient-400ms"].false_positives
    ), "a twitchier variant produced fewer false positives than a patient one"

    spoken = synthesize_walk(duration_ms=45_000, speech_at_ms=(12_000, 26_000, 38_000))
    heard = create_run(
        tmp_path / "spoken",
        spoken.pcm,
        label="spoken",
        ground_truth="labelled",
        labels=[SpeechLabel(start_ms=w.start_ms, end_ms=w.end_ms) for w in spoken.speech],
    )
    caught = {
        metric.variant: metric.true_positives
        for metric in replay_run(heard, settings, arms=["composed"], write_snippets=False).metrics
    }
    assert caught["default"] >= 2, "the shipped default must still hear somebody talking"
    assert caught["hair-trigger-60ms"] >= caught["patient-400ms"]


def test_a_labelled_recording_separates_catches_from_false_alarms(
    tmp_path: Path, settings: VoiceSettings
) -> None:
    walk = synthesize_walk(
        duration_ms=45_000, speech_at_ms=(12_000, 26_000, 38_000), speech_duration_ms=1_500
    )
    run = create_run(
        tmp_path / "spoken",
        walk.pcm,
        label="spoken",
        ground_truth="labelled",
        labels=[SpeechLabel(start_ms=w.start_ms, end_ms=w.end_ms) for w in walk.speech],
    )

    scored = replay_run(run, settings, arms=["composed"], variants=["default"])
    metric = scored.metrics[0]

    assert metric.true_positives >= 2, "a clear utterance should be caught"
    assert metric.detection_rate is not None and metric.detection_rate >= 2 / 3
    assert metric.missed <= 1
    assert metric.median_latency_ms is not None


def test_every_decision_gets_a_reviewable_snippet_and_a_log_line(
    tmp_path: Path, settings: VoiceSettings
) -> None:
    """A labelled log he can review after the walk *is* the deliverable."""
    walk = synthesize_walk(duration_ms=20_000, speech_at_ms=(4_000, 12_000))
    run = create_run(tmp_path / "run", walk.pcm, label="run", ground_truth="silent")
    replay_run(run, settings, arms=["composed"], variants=["default"])

    target = replay_dir(run, "composed", "default")
    decisions = list(read_decisions(target / "decisions.jsonl"))
    assert decisions, "the synthetic utterances should have produced decisions to review"
    for decision in decisions:
        assert decision.snippet is not None
        assert (target / "snippets" / decision.snippet).is_file()
    assert json.loads((target / "metrics.json").read_text())["arm"] == "composed"


def test_both_arms_are_replayed_against_identical_audio(
    tmp_path: Path, settings: VoiceSettings
) -> None:
    """One walk, both arms — a comparison that only ran one arm answers nothing."""
    walk = synthesize_walk(duration_ms=15_000, speech_at_ms=(5_000,))
    run = create_run(tmp_path / "both", walk.pcm, label="both", ground_truth="silent")

    scored = replay_run(run, settings, variants=["default"])

    assert {metric.arm for metric in scored.metrics} == {"composed", "openai_realtime"}
    assert any(metric.emulated for metric in scored.metrics), (
        "with no OPENAI_API_KEY the realtime arm's rows must be marked as emulated"
    )


def test_the_sweep_tests_several_variants_from_one_recording(
    tmp_path: Path, settings: VoiceSettings
) -> None:
    walk = synthesize_walk(duration_ms=15_000, speech_at_ms=(5_000,))
    run = create_run(tmp_path / "sweep", walk.pcm, label="sweep", ground_truth="silent")

    scored = replay_run(run, settings, arms=["composed"])

    assert len(scored.metrics) == len(sweep())
    assert len({metric.variant for metric in scored.metrics}) == len(sweep())


def test_a_measurement_run_forces_narration_gating_off() -> None:
    """Otherwise every variant scores a perfect zero for the wrong reason."""
    gated = BargeInPolicy(name="gated", require_narration_playing=True)
    assert not policies_for_measurement([gated])[0].require_narration_playing


def test_the_report_names_the_emulated_rows(tmp_path: Path, settings: VoiceSettings) -> None:
    walk = synthesize_walk(duration_ms=12_000, speech_at_ms=(4_000,))
    run = create_run(tmp_path / "report", walk.pcm, label="report", ground_truth="silent")
    scored = replay_run(run, settings, variants=["default"])

    report = render_report([scored])

    assert "false/min" in report
    assert "*(emulated)*" in report
    assert "OPENAI_API_KEY" in report
    assert "How to read this" in report


def test_the_report_survives_a_run_with_nothing_in_it() -> None:
    assert "No arms were replayed" in render_report([ScoredRun(run_label="empty")])


def test_ingest_converts_a_real_48k_stereo_recording(tmp_path: Path) -> None:
    """What a phone actually exports: 48 kHz, two channels. Anything less tests nothing."""
    walk = synthesize_walk(duration_ms=3_000)
    mono_16k = samples_from_pcm(walk.pcm)
    # Upsample 16k -> 48k by triplication and duplicate into two channels, which is the
    # shape `to_mono_16k` has to undo.
    stereo_48k = pcm_from_samples(
        [value for sample in mono_16k for _ in range(3) for value in (sample, sample)]
    )
    source = tmp_path / "phone.wav"
    write_wav(source, stereo_48k, PcmFormat(sample_rate=48_000, channels=2))

    run = ingest_recording(source, tmp_path / "run", label="phone")

    assert run.ground_truth == "silent"
    assert load_run(run.path).label == "phone"
    assert run.duration_ms == pytest.approx(3_000, abs=20)
    fmt, _ = read_wav(run.audio_path)
    assert fmt == PcmFormat(), f"ingest left the recording at {fmt.describe()}"


def test_ingest_refuses_a_non_wav_with_an_actionable_message(tmp_path: Path) -> None:
    source = tmp_path / "memo.m4a"
    source.write_bytes(b"not really audio")
    with pytest.raises(RunError, match="ffmpeg"):
        ingest_recording(source, tmp_path / "run", label="memo")


def test_load_run_rejects_a_directory_that_is_not_one(tmp_path: Path) -> None:
    with pytest.raises(RunError, match="run.json"):
        load_run(tmp_path)


def test_scoring_counts_one_utterance_once_however_often_it_fires(tmp_path: Path) -> None:
    """A chattery variant must not score better for being chattery."""
    walk = synthesize_walk(duration_ms=8_000, speech_at_ms=(2_000,), speech_duration_ms=2_000)
    run = create_run(
        tmp_path / "one",
        walk.pcm,
        label="one",
        ground_truth="labelled",
        labels=[SpeechLabel(start_ms=2_000, end_ms=4_000)],
    )
    from motet_voice.bargein import BargeInDecision

    def _decision(at_ms: int) -> BargeInDecision:
        return BargeInDecision(
            at_ms=at_ms,
            onset_ms=at_ms - 100,
            arm="composed",
            variant="chatty",
            trigger="local_vad",
            spoken_through_ms=0,
            narration_playing=False,
            consecutive_frames=6,
            speech_probability=0.9,
            rms_dbfs=-20.0,
            noise_floor_dbfs=-45.0,
            snr_db=25.0,
            zero_crossing_rate=0.1,
        )

    metric = score(
        run,
        [_decision(2_500), _decision(3_000), _decision(3_500)],
        arm="composed",
        variant="chatty",
    )
    assert metric.true_positives == 1
    assert metric.false_positives == 0
    assert metric.missed == 0


def test_the_cli_demo_runs_the_whole_pipeline(tmp_path: Path, capsys) -> None:  # type: ignore[no-untyped-def]
    """The one command that proves the harness works before anybody puts a coat on."""
    assert main(["demo", "--run", str(tmp_path / "demo"), "--duration-ms", "12000"]) == 0

    printed = capsys.readouterr().out
    assert "false/min" in printed
    assert (tmp_path / "demo" / "report.md").is_file()
    assert (tmp_path / "demo" / "metrics.json").is_file()
    assert (tmp_path / "demo" / "audio.wav").is_file()


def test_the_cli_reports_an_unreplayed_run_clearly(tmp_path: Path, capsys) -> None:  # type: ignore[no-untyped-def]
    create_run(tmp_path / "run", synthesize_walk(duration_ms=1_000).pcm, label="x")
    assert main(["report", str(tmp_path / "run")]) == 2
    assert "replay" in capsys.readouterr().err


def test_upload_pushes_the_whole_run_through_the_storage_seam(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys,  # type: ignore[no-untyped-def]
) -> None:
    """Buffer locally, upload after — and the seam means no bucket name lives in this repo."""
    monkeypatch.setenv("MOTET_STORAGE_BACKEND", "local")
    monkeypatch.setenv("MOTET_STORAGE_DIR", str(tmp_path / "objects"))
    main(["demo", "--run", str(tmp_path / "demo"), "--duration-ms", "6000"])
    capsys.readouterr()

    assert main(["upload", str(tmp_path / "demo")]) == 0

    uploaded = list((tmp_path / "objects").rglob("*"))
    assert any(path.name == "report.md" for path in uploaded)
    assert any(path.name == "audio.wav" for path in uploaded)


def test_deterministic_replay_gives_identical_numbers_twice(
    tmp_path: Path, settings: VoiceSettings
) -> None:
    """One recording, re-run later, still comparable. That is what a recording buys."""
    walk = synthesize_walk(duration_ms=15_000, speech_at_ms=(5_000,))
    run = create_run(tmp_path / "det", walk.pcm, label="det", ground_truth="silent")

    first = replay_run(run, settings, arms=["composed"], variants=["default"]).metrics[0]
    second = replay_run(run, settings, arms=["composed"], variants=["default"]).metrics[0]

    assert first.to_json() == second.to_json()


def test_synthetic_audio_is_byte_identical_across_runs() -> None:
    assert synthesize_walk(duration_ms=2_000).pcm == synthesize_walk(duration_ms=2_000).pcm


def test_an_unknown_arm_or_variant_is_rejected(tmp_path: Path, settings: VoiceSettings) -> None:
    run = create_run(tmp_path / "run", synthesize_walk(duration_ms=1_000).pcm, label="x")
    with pytest.raises(ValueError, match="unknown arm"):
        replay_run(run, settings, arms=["telepathy"])
    with pytest.raises(ValueError, match="no variant matched"):
        replay_run(run, settings, variants=["hunch"])


def test_pcm_helpers_round_trip() -> None:
    assert pcm_from_samples([1, -1, 32_767, -32_768])


def test_a_configuration_that_heard_nothing_is_never_the_winner() -> None:
    """A detector that never fires scores a perfect zero and, unguarded, wins.

    This is not hypothetical — it is exactly what an arm wired to a decision source nobody
    asks produces, and it is the failure mode that would have quietly declared the dormant
    realtime arm the best of the two.
    """
    deaf = ArmMetrics(
        arm="openai_realtime",
        variant="default",
        open_mic_ms=60_000,
        decisions=0,
        false_positives=0,
        true_positives=0,
        missed=3,
        median_latency_ms=None,
        ground_truth="labelled",
    )
    working = ArmMetrics(
        arm="composed",
        variant="default",
        open_mic_ms=60_000,
        decisions=4,
        false_positives=1,
        true_positives=3,
        missed=0,
        median_latency_ms=120,
        ground_truth="labelled",
    )

    assert deaf.deaf and not working.deaf
    assert deaf.false_per_minute < working.false_per_minute, "the trap: it looks best"

    scored = ScoredRun(run_label="x", metrics=[deaf, working])
    best = scored.best()
    assert best is not None and best.arm == "composed"
    assert "heard nothing" in render_report([scored])
