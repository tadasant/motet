"""Measuring and joining audio.

Duration is not cosmetic: it is what a podcast client shows on a lockscreen, and every
segment's ``start_ms`` is accumulated from it. We own playback position (invariant 4), so a
duration that is quietly wrong desynchronizes a transcript from its audio for the whole
episode and nothing errors.
"""

from __future__ import annotations

import struct

import pytest
from motet_inference.audio import (
    MPEG_MEDIA_TYPE,
    WAV_MEDIA_TYPE,
    AudioError,
    duration_ms,
    estimate_duration_ms,
    join_audio,
    mpeg_duration_ms,
)
from motet_inference.fakes import FakeSpeechSynthesizer
from motet_inference.types import Audio

#: A 128 kbps, 44.1 kHz, MPEG-1 Layer III frame header. 1152 samples per frame, so each
#: frame is 1152/44100 s = 26.12 ms, and the frame is 417 bytes with no padding.
_FRAME_HEADER = bytes([0xFF, 0xFB, 0x90, 0x00])
_FRAME_BYTES = 417
_FRAME_MS = 1152 / 44100 * 1000


def mpeg(frames: int) -> bytes:
    return (_FRAME_HEADER + b"\x00" * (_FRAME_BYTES - 4)) * frames


class TestEstimate:
    def test_scales_with_word_count(self) -> None:
        assert estimate_duration_ms("one two three") == pytest.approx(1200, abs=1)

    def test_empty_text_is_zero_rather_than_a_minimum(self) -> None:
        # The assemble stage sums these against a duration cap; a non-zero floor for empty
        # text would let a handful of empty segments consume a real budget.
        assert estimate_duration_ms("   ") == 0


class TestMpegDuration:
    def test_sums_frames(self) -> None:
        assert mpeg_duration_ms(mpeg(100)) == pytest.approx(100 * _FRAME_MS, abs=1)

    def test_ignores_a_leading_id3v2_tag(self) -> None:
        """A tag is bytes with no audio in them, and it sits right where the frames start.

        Dividing file size by bit rate — the cheap way to get a duration — counts the tag
        as audio, which is how a five-second episode reports as six.
        """
        tag = b"ID3\x03\x00\x00" + bytes([0, 0, 2, 0]) + b"\x00" * 256
        assert mpeg_duration_ms(tag + mpeg(10)) == mpeg_duration_ms(mpeg(10))

    def test_ignores_a_trailing_id3v1_tag(self) -> None:
        tagged = mpeg(10) + b"TAG" + b"\x00" * 125
        assert mpeg_duration_ms(tagged) == mpeg_duration_ms(mpeg(10))

    def test_refuses_bytes_that_are_not_audio(self) -> None:
        with pytest.raises(AudioError):
            mpeg_duration_ms(b"this is a JSON error body, not an MP3")


class TestJoin:
    def test_mpeg_parts_concatenate_and_durations_sum(self) -> None:
        parts = [Audio(MPEG_MEDIA_TYPE, mpeg(10), 261), Audio(MPEG_MEDIA_TYPE, mpeg(5), 131)]
        joined = join_audio(parts)
        assert joined.data == mpeg(15)
        assert joined.duration_ms == 392
        # The joined stream is still a decodable MPEG stream, which is the whole reason
        # narration can be synthesized per segment.
        assert mpeg_duration_ms(joined.data) == pytest.approx(15 * _FRAME_MS, abs=1)

    def test_wav_parts_are_rewrapped_not_just_appended(self) -> None:
        """Concatenating WAV bytes produces a file whose header lies about its length.

        It plays the first segment and stops, which looks exactly like a synthesis bug in
        every segment after the first.
        """
        synth = FakeSpeechSynthesizer()
        parts = [synth.synthesize("one two three"), synth.synthesize("four five six seven")]
        joined = join_audio(parts)

        assert joined.data.count(b"RIFF") == 1
        (declared,) = struct.unpack("<I", joined.data[4:8])
        assert declared == len(joined.data) - 8
        assert duration_ms(joined.data, WAV_MEDIA_TYPE) == pytest.approx(joined.duration_ms, abs=2)

    def test_refuses_to_join_nothing(self) -> None:
        with pytest.raises(AudioError):
            join_audio([])

    def test_refuses_to_mix_formats(self) -> None:
        with pytest.raises(AudioError, match="mixed media types"):
            join_audio([Audio(MPEG_MEDIA_TYPE, mpeg(1), 26), Audio(WAV_MEDIA_TYPE, b"RIFF", 26)])

    def test_refuses_a_format_it_cannot_join(self) -> None:
        with pytest.raises(AudioError, match="audio/ogg"):
            join_audio([Audio("audio/ogg", b"OggS", 100)])
