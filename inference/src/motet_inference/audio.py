"""Measuring and joining synthesized audio.

Narration is synthesized **one segment at a time** and then joined. That costs nothing
extra — the same words are spoken either way — and buys two things Phase 1 needs:

* a real per-segment duration, which is what ``start_ms`` on each segment is accumulated
  from. We own playback position (invariant 4), so those offsets have to come from the
  audio we actually produced, not from a player and not from a vendor's estimate.
* independent retry of a single failed segment, rather than re-synthesizing a whole
  twenty-minute briefing because one call 429'd.

Joining is format-aware and deliberately not a generic "concatenate bytes": that happens
to work for MPEG audio and produces a corrupt file for WAV, which is the sort of thing
that only shows up when a podcast client refuses to play the episode.
"""

from __future__ import annotations

import struct
from collections.abc import Sequence

from .types import Audio

MPEG_MEDIA_TYPE = "audio/mpeg"
WAV_MEDIA_TYPE = "audio/wav"

#: Roughly conversational narration pace, shared with the fake synthesizer so an
#: estimated duration and a fake-rendered one agree. Real audio replaces the estimate the
#: moment TTS runs; the estimate exists only to apply an episode's duration cap *before*
#: paying for synthesis.
WORDS_PER_MINUTE = 150


class AudioError(ValueError):
    """Synthesized audio was not the shape it claimed to be."""


def estimate_duration_ms(text: str) -> int:
    """How long ``text`` will take to say, near enough to cap an episode by.

    Used by the assemble stage, which has to decide *which* news items fit inside a
    duration cap before any audio exists. Deliberately crude: the alternative is
    synthesizing everything and throwing some away, which is the largest cost line in the
    system.
    """
    words = len(text.split())
    if words == 0:
        return 0
    return max(1, round(words / WORDS_PER_MINUTE * 60_000))


def join_audio(parts: Sequence[Audio]) -> Audio:
    """Concatenate synthesized segments into one playable file.

    Durations are summed from the parts rather than re-measured, because the parts are
    what the segment offsets were derived from — re-measuring could disagree with them by
    a frame and silently desynchronize a transcript from its audio.
    """
    if not parts:
        raise AudioError("cannot join zero audio parts")
    media_types = {part.media_type for part in parts}
    if len(media_types) != 1:
        raise AudioError(f"cannot join audio of mixed media types: {sorted(media_types)}")
    media_type = parts[0].media_type
    total_ms = sum(part.duration_ms for part in parts)

    if media_type == MPEG_MEDIA_TYPE:
        # MPEG frames are self-describing and self-synchronizing, so appending one
        # stream to another is a valid stream. This is how every podcast ad-stitcher
        # works, and it is why MP3 is the format this pipeline asks Cartesia for.
        joined = b"".join(part.data for part in parts)
        return Audio(media_type=media_type, data=joined, duration_ms=total_ms)
    if media_type == WAV_MEDIA_TYPE:
        return Audio(media_type=media_type, data=_join_wav(parts), duration_ms=total_ms)
    raise AudioError(f"do not know how to join {media_type!r} audio")


def duration_ms(audio_bytes: bytes, media_type: str) -> int:
    """Measure a rendered file, for checking a vendor's output against what we asked for."""
    if media_type == MPEG_MEDIA_TYPE:
        return mpeg_duration_ms(audio_bytes)
    if media_type == WAV_MEDIA_TYPE:
        return _wav_duration_ms(audio_bytes)
    raise AudioError(f"do not know how to measure {media_type!r} audio")


# --- MPEG ---------------------------------------------------------------------------

# Layer III bitrates in kbps, indexed by the header's 4-bit bitrate index. Index 0 is
# "free format" and index 15 is invalid; both are recorded as 0 and treated as a bad
# frame, because a stream containing either is not something we produced.
_BITRATES_V1: tuple[int, ...] = (
    0,
    32,
    40,
    48,
    56,
    64,
    80,
    96,
    112,
    128,
    160,
    192,
    224,
    256,
    320,
    0,
)
_BITRATES_V2: tuple[int, ...] = (
    0,
    8,
    16,
    24,
    32,
    40,
    48,
    56,
    64,
    80,
    96,
    112,
    128,
    144,
    160,
    0,
)
_SAMPLE_RATES: dict[int, tuple[int, int, int]] = {
    3: (44100, 48000, 32000),  # MPEG 1
    2: (22050, 24000, 16000),  # MPEG 2
    0: (11025, 12000, 8000),  # MPEG 2.5
}
_LAYER_III = 1


def mpeg_duration_ms(data: bytes) -> int:
    """Sum the durations of every MPEG audio frame in ``data``.

    Walking the frames rather than dividing bytes by a nominal bit rate, because the
    cheap version is wrong for anything variable-rate and wrong by a whole ID3 tag's
    worth for anything tagged. Duration is user-visible — it is the number a podcast
    client shows on the lockscreen — and it anchors every segment offset, so it is worth
    measuring rather than assuming.
    """
    offset = _skip_id3v2(data)
    end = _strip_id3v1(data)
    total_samples = 0
    sample_rate = 0
    while offset + 4 <= end:
        header = data[offset : offset + 4]
        if header[0] != 0xFF or (header[1] & 0xE0) != 0xE0:
            # Not a sync word. Either padding between frames or a tag we do not parse;
            # step one byte and keep looking rather than giving up on the file.
            offset += 1
            continue
        frame = _parse_frame(header)
        if frame is None:
            offset += 1
            continue
        frame_length, samples, sample_rate = frame
        total_samples += samples
        offset += frame_length

    if total_samples == 0 or sample_rate == 0:
        raise AudioError("no MPEG audio frames found; this is not audio we synthesized")
    return round(total_samples / sample_rate * 1000)


def _parse_frame(header: bytes) -> tuple[int, int, int] | None:
    """``(frame_length_bytes, samples_in_frame, sample_rate)`` for a valid Layer III frame."""
    version_bits = (header[1] >> 3) & 0b11
    layer_bits = (header[1] >> 1) & 0b11
    if version_bits == 1 or layer_bits != _LAYER_III:
        return None  # reserved version, or a layer Cartesia does not emit

    bitrate_index = (header[2] >> 4) & 0b1111
    sample_rate_index = (header[2] >> 2) & 0b11
    padding = (header[2] >> 1) & 0b1
    if sample_rate_index == 3:
        return None

    is_v1 = version_bits == 3
    bitrate_kbps = (_BITRATES_V1 if is_v1 else _BITRATES_V2)[bitrate_index]
    if bitrate_kbps == 0:
        return None
    sample_rate = _SAMPLE_RATES[version_bits][sample_rate_index]
    samples = 1152 if is_v1 else 576
    frame_length = (samples // 8) * bitrate_kbps * 1000 // sample_rate + padding
    if frame_length <= 4:
        return None
    return frame_length, samples, sample_rate


def _skip_id3v2(data: bytes) -> int:
    """Length of a leading ID3v2 tag, which carries no audio and no duration."""
    if len(data) < 10 or data[:3] != b"ID3":
        return 0
    # A syncsafe integer: seven bits per byte, so a size can never contain a sync word.
    size = 0
    for byte in data[6:10]:
        size = (size << 7) | (byte & 0x7F)
    return 10 + size


def _strip_id3v1(data: bytes) -> int:
    if len(data) >= 128 and data[-128:-125] == b"TAG":
        return len(data) - 128
    return len(data)


# --- WAV ----------------------------------------------------------------------------


def _chunks(data: bytes) -> dict[bytes, bytes]:
    """The RIFF chunks of a WAV file, by id.

    Chunk-walking rather than assuming the canonical 44-byte header: a synthesizer is
    entitled to emit a ``LIST``/``INFO`` chunk, and slicing at a fixed offset would then
    treat metadata as samples — audible as a burst of noise at every segment boundary.
    """
    if len(data) < 12 or data[:4] != b"RIFF" or data[8:12] != b"WAVE":
        raise AudioError("not a RIFF/WAVE file")
    found: dict[bytes, bytes] = {}
    offset = 12
    while offset + 8 <= len(data):
        chunk_id = data[offset : offset + 4]
        (size,) = struct.unpack("<I", data[offset + 4 : offset + 8])
        body = data[offset + 8 : offset + 8 + size]
        found.setdefault(chunk_id, body)
        offset += 8 + size + (size % 2)  # chunks are word-aligned
    if b"fmt " not in found or b"data" not in found:
        raise AudioError("WAV file has no fmt/data chunk")
    return found


def _join_wav(parts: Sequence[Audio]) -> bytes:
    fmt = _chunks(parts[0].data)[b"fmt "]
    payload = b""
    for part in parts:
        chunks = _chunks(part.data)
        if chunks[b"fmt "] != fmt:
            raise AudioError("cannot join WAV parts recorded with different formats")
        payload += chunks[b"data"]
    header = b"RIFF" + struct.pack("<I", 4 + 8 + len(fmt) + 8 + len(payload)) + b"WAVE"
    header += b"fmt " + struct.pack("<I", len(fmt)) + fmt
    header += b"data" + struct.pack("<I", len(payload))
    return header + payload


def _wav_duration_ms(data: bytes) -> int:
    chunks = _chunks(data)
    fields: tuple[int, ...] = struct.unpack("<HHIIHH", chunks[b"fmt "][:16])
    channels, sample_rate, bits = fields[1], fields[2], fields[5]
    frame_bytes = channels * (bits // 8)
    if frame_bytes == 0 or sample_rate == 0:
        raise AudioError("WAV fmt chunk describes zero-width frames")
    return round(len(chunks[b"data"]) / frame_bytes / sample_rate * 1000)
