"""The audio layer everything else stands on."""

from __future__ import annotations

import math

import pytest
from motet_voice.audio import (
    SILENCE_DBFS,
    TARGET_SAMPLE_RATE,
    AudioError,
    PcmFormat,
    dbfs,
    duration_ms,
    iter_frames,
    pcm_from_samples,
    read_wav,
    samples_from_pcm,
    slice_ms,
    to_mono_16k,
    write_wav,
    zero_crossing_rate,
)


def _tone(
    frequency: float, ms: int, amplitude: float = 0.5, rate: int = TARGET_SAMPLE_RATE
) -> bytes:
    count = int(rate * ms / 1000)
    return pcm_from_samples(
        [
            int(amplitude * 32_767 * math.sin(2 * math.pi * frequency * i / rate))
            for i in range(count)
        ]
    )


def test_silence_is_floored_not_infinite() -> None:
    assert dbfs([0] * 320) == SILENCE_DBFS


def test_dbfs_tracks_amplitude() -> None:
    loud = samples_from_pcm(_tone(440, 100, amplitude=0.8))
    quiet = samples_from_pcm(_tone(440, 100, amplitude=0.05))
    assert dbfs(loud) > dbfs(quiet) + 20


def test_zero_crossing_rate_separates_low_from_high_frequency() -> None:
    low = samples_from_pcm(_tone(60, 100))
    high = samples_from_pcm(_tone(4_000, 100))
    assert zero_crossing_rate(low) < 0.02 < zero_crossing_rate(high)


def test_frames_drop_a_short_remainder_rather_than_padding_it() -> None:
    pcm = _tone(220, 105)  # 5 ms past a whole number of 20 ms frames
    got = list(iter_frames(pcm))
    assert len(got) == 5
    assert got[-1].end_ms == 100
    assert all(len(frame) == TARGET_SAMPLE_RATE * 20 // 1000 for frame in got)


def test_frames_reject_a_non_target_format() -> None:
    with pytest.raises(AudioError):
        list(iter_frames(b"\x00\x00", fmt=PcmFormat(sample_rate=48_000)))


def test_downmix_and_resample_reach_the_target_format() -> None:
    stereo_48k = PcmFormat(sample_rate=48_000, channels=2)
    samples = samples_from_pcm(_tone(300, 500, rate=48_000))
    interleaved = pcm_from_samples([value for sample in samples for value in (sample, sample)])

    converted = to_mono_16k(interleaved, stereo_48k)

    assert duration_ms(converted) == pytest.approx(500, abs=2)
    assert dbfs(samples_from_pcm(converted)) == pytest.approx(dbfs(samples), abs=1.5)


def test_eight_bit_audio_is_rejected_with_an_actionable_message() -> None:
    with pytest.raises(AudioError, match="16-bit"):
        to_mono_16k(b"\x01\x02", PcmFormat(sample_width=1))


def test_wav_round_trips(tmp_path) -> None:  # type: ignore[no-untyped-def]
    pcm = _tone(500, 250)
    path = tmp_path / "nested" / "tone.wav"
    write_wav(path, pcm)
    fmt, read_back = read_wav(path)
    assert fmt == PcmFormat()
    assert read_back == pcm


def test_slice_is_clamped_to_the_recording() -> None:
    pcm = _tone(500, 200)
    assert duration_ms(slice_ms(pcm, 50, 150)) == pytest.approx(100, abs=2)
    assert slice_ms(pcm, -5_000, 5_000) == pcm
    assert slice_ms(pcm, 500, 900) == b""
