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
    MPEG_SYNC_WINDOW,
    WAV_MEDIA_TYPE,
    AudioError,
    duration_ms,
    estimate_duration_ms,
    join_audio,
    mpeg_duration_ms,
    sniff_media_type,
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


class TestSniff:
    """What the bytes *are*, against what the object is about to be labelled as.

    A player identifies an episode by the content type it was uploaded under and by the
    extension on its key — on a phone, by the extension alone, because a local file has no
    HTTP response to carry a type. Both are derived from ``Audio.media_type``, so this is
    the one place that compares that field to the bytes it describes.
    """

    def test_an_mpeg_frame_header_is_mpeg(self) -> None:
        assert sniff_media_type(mpeg(1)) == MPEG_MEDIA_TYPE

    def test_an_id3_tag_ahead_of_the_frames_is_mpeg(self) -> None:
        tag = b"ID3\x04\x00\x00\x00\x00\x00\x0a" + b"\x00" * 10
        assert sniff_media_type(tag + mpeg(2)) == MPEG_MEDIA_TYPE

    def test_an_id3_tag_with_nothing_behind_it_is_not(self) -> None:
        """A tag is metadata about audio, and here there is no audio for it to be about."""
        assert sniff_media_type(b"ID3\x04\x00\x00\x00\x00\x00\x0a" + b"\x00" * 10) is None

    def test_leading_bytes_the_parser_steps_past_are_stepped_past_here_too(self) -> None:
        """The guard must be no stricter than the check that admitted the segment.

        `mpeg_duration_ms` resynchronises past bytes that are not a frame, so a vendor
        response opening with padding, or a tag it does not parse, passes synthesis — and a
        player resynchronises the same way. Refusing it at publish would be a permanent
        failure, after the whole episode was billed, of an object that would have played.
        """
        for junk in (b"\x00" * 7, b"APETAGEX" + b"\x00" * 24, b"\xff" * 3 + b"\x00"):
            assert sniff_media_type(junk + mpeg(3)) == MPEG_MEDIA_TYPE, junk

    def test_the_window_is_bounded(self) -> None:
        assert sniff_media_type(b"\x00" * (MPEG_SYNC_WINDOW + 1) + mpeg(3)) is None

    def test_a_sync_shaped_pair_not_followed_by_a_frame_is_chance(self) -> None:
        """One header is a byte pair; a header whose *length* lands on another is audio."""
        stray = _FRAME_HEADER + b"\x00" * 100  # says 417 bytes long, and is 104
        assert sniff_media_type(stray + b"\x00" * 400) is None
        assert sniff_media_type(_FRAME_HEADER + b"\x00" * 50) == MPEG_MEDIA_TYPE, "ends inside"

    def test_a_riff_wave_header_is_wav(self) -> None:
        wav = FakeSpeechSynthesizer().synthesize("one two").data
        assert sniff_media_type(wav) == WAV_MEDIA_TYPE

    def test_what_the_pipeline_actually_uploads_matches_what_it_labels_it(self) -> None:
        """The joined object, not a segment — the join is what the store receives."""
        synth = FakeSpeechSynthesizer()
        joined = join_audio([synth.synthesize("one two three"), synth.synthesize("four five")])
        assert sniff_media_type(joined.data) == joined.media_type

        mpeg_joined = join_audio([Audio(MPEG_MEDIA_TYPE, mpeg(4), 104)] * 2)
        assert sniff_media_type(mpeg_joined.data) == mpeg_joined.media_type

    @pytest.mark.parametrize(
        "data",
        [
            pytest.param(b"", id="empty"),
            pytest.param(b"\xff", id="one byte of a sync word"),
            pytest.param(b"<!DOCTYPE html><html>Sign in</html>", id="an html sign-in page"),
            pytest.param(b'{"detail":"Not authenticated"}', id="a json error body"),
            pytest.param(b"<?xml version='1.0'?><Error><Code>AccessDenied</Code>", id="gcs denial"),
            pytest.param(b"OggS\x00\x02" + b"\x00" * 20, id="opus in ogg, which iOS refuses"),
        ],
    )
    def test_what_is_not_audio_is_not_claimed_to_be(self, data: bytes) -> None:
        assert sniff_media_type(data) is None

    def test_a_reserved_mpeg_version_is_not_an_mpeg_frame(self) -> None:
        """Eleven set bits are not enough: a byte pair merely starting 0xFF is not audio."""
        assert sniff_media_type(bytes([0xFF, 0xEB, 0x90, 0x00])) is None

    def test_adts_aac_is_not_claimed_to_be_mpeg(self) -> None:
        """Unrecognised rather than mis-recognised, and that is the right answer here.

        ADTS AAC reuses MPEG audio's sync word and is separated from it by the reserved
        layer bits. This pipeline emits only MPEG and WAV — ``_EXTENSIONS`` has no key to
        name an AAC object with — so an AAC stream reaching the upload is a stage that has
        gone wrong, and failing the check is what should happen. (MotetKit's own sniffer
        *does* name it, because there the question is which extension a file on a phone
        needs and iOS plays AAC.)
        """
        for second in (0xF1, 0xF9):
            assert sniff_media_type(bytes([0xFF, second, 0x50, 0x80])) is None
