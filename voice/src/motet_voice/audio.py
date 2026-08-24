"""PCM in, PCM out — the small audio layer the whole harness stands on.

Deliberately stdlib-only (``wave``, ``array``, ``math``). Two reasons, and the second is
the load-bearing one:

* **The harness has to run in CI**, offline and free, on the same code path Tadas runs
  outdoors. A numpy/scipy/librosa stack would work and would also mean the thing measured
  in CI is not quite the thing measured on the walk.
* **``audioop`` is gone in Python 3.13.** The obvious "just use the stdlib" answer for RMS
  and resampling was removed, so the arithmetic is here, written out, rather than
  imported from somewhere that will surprise the next reader.

Everything downstream works in **16 kHz mono signed 16-bit** frames, because that is what
every realtime provider and every VAD wants. :func:`to_mono_16k` is the one place a phone
recording's 48 kHz stereo becomes that, so an ingest is the only step that pays for it.
"""

from __future__ import annotations

import math
import wave
from array import array
from collections.abc import Iterator, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Final

#: What the arms, the VADs, and the capture format all speak.
TARGET_SAMPLE_RATE: Final = 16_000

#: 20 ms is the frame every realtime provider quantizes to, so the harness measures in the
#: same unit the arms decide in. A policy may consume several frames before it triggers;
#: it may not see a finer grain than this.
DEFAULT_FRAME_MS: Final = 20

#: Full-scale for signed 16-bit. Used to express energy in dBFS, which is the unit that
#: makes a wind gust and a spoken word comparable across recordings of different gain.
_FULL_SCALE: Final = 32_768.0

#: dBFS reported for a genuinely silent frame. Digital silence is negative infinity, which
#: poisons every average it touches; this floor is below anything a microphone produces.
SILENCE_DBFS: Final = -100.0


class AudioError(ValueError):
    """Audio was not the shape it claimed to be."""


@dataclass(frozen=True)
class PcmFormat:
    """The three facts that make a byte string interpretable as sound."""

    sample_rate: int = TARGET_SAMPLE_RATE
    channels: int = 1
    sample_width: int = 2

    @property
    def frame_bytes(self) -> int:
        return self.channels * self.sample_width

    @property
    def is_target(self) -> bool:
        return self == TARGET_FORMAT

    def describe(self) -> str:
        return f"{self.sample_rate}Hz/{self.channels}ch/{self.sample_width * 8}bit"


#: The one format everything downstream speaks. A module-level singleton rather than a
#: ``PcmFormat()`` default argument: constructing a value in a signature is a footgun in
#: general, and here it also gives the canonical format a name that reads at call sites.
TARGET_FORMAT: Final = PcmFormat()


@dataclass(frozen=True)
class PcmFrame:
    """One fixed-length window of mono 16-bit audio, with its place in the recording.

    ``start_ms`` is an offset into the *recording*, not a wall-clock time. That is what
    makes a decision log replayable: the same frame index in the same file is the same
    moment, on every arm, on every re-run, forever.
    """

    index: int
    start_ms: int
    duration_ms: int
    samples: array[int]

    def __len__(self) -> int:
        return len(self.samples)

    @property
    def end_ms(self) -> int:
        return self.start_ms + self.duration_ms


def rms(samples: Sequence[int]) -> float:
    """Root-mean-square amplitude, in raw sample units."""
    if not samples:
        return 0.0
    total = 0
    for value in samples:
        total += value * value
    return math.sqrt(total / len(samples))


def dbfs(samples: Sequence[int]) -> float:
    """Energy relative to full scale, floored at :data:`SILENCE_DBFS`."""
    level = rms(samples) / _FULL_SCALE
    if level <= 0.0:
        return SILENCE_DBFS
    return max(SILENCE_DBFS, 20.0 * math.log10(level))


def zero_crossing_rate(samples: Sequence[int]) -> float:
    """Fraction of adjacent sample pairs that change sign.

    A crude but genuinely discriminating spectral hint, and it costs one pass. Wind and
    handling noise are dominated by very low frequencies and cross zero rarely; hiss and
    tyre noise cross constantly; voiced speech sits in a band between the two. The VAD
    uses it to reject the two things a pure energy gate cannot tell from a sentence.
    """
    if len(samples) < 2:
        return 0.0
    crossings = 0
    previous = samples[0]
    for value in samples[1:]:
        if (previous < 0) != (value < 0):
            crossings += 1
        previous = value
    return crossings / (len(samples) - 1)


def samples_from_pcm(pcm: bytes) -> array[int]:
    """Decode mono 16-bit little-endian bytes into signed integers."""
    if len(pcm) % 2:
        raise AudioError("16-bit PCM must have an even number of bytes")
    decoded: array[int] = array("h")
    decoded.frombytes(pcm)
    if _BIG_ENDIAN:  # pragma: no cover — every runner this ships to is little-endian
        decoded.byteswap()
    return decoded


def pcm_from_samples(samples: Sequence[int]) -> bytes:
    encoded: array[int] = array("h", samples)
    if _BIG_ENDIAN:  # pragma: no cover
        encoded.byteswap()
    return encoded.tobytes()


_BIG_ENDIAN: Final = array("h", [1]).tobytes()[0] == 0


def duration_ms(pcm: bytes, fmt: PcmFormat = TARGET_FORMAT) -> int:
    frames = len(pcm) // fmt.frame_bytes
    return round(frames * 1000 / fmt.sample_rate)


def iter_frames(
    pcm: bytes,
    *,
    frame_ms: int = DEFAULT_FRAME_MS,
    fmt: PcmFormat = TARGET_FORMAT,
    start_index: int = 0,
    start_ms: int = 0,
) -> Iterator[PcmFrame]:
    """Cut mono 16-bit PCM into fixed frames, dropping a short final remainder.

    Dropped rather than zero-padded: a padded tail is a frame whose second half is digital
    silence, which reads to any energy VAD as a sudden drop to the noise floor. At most one
    frame — 20 ms — is lost, and a decision at the very last instant of a recording is not
    a decision anyone can review anyway.

    ``start_index`` and ``start_ms`` exist because **a live session does not arrive as one
    buffer.** Offline the whole recording is a single call and the defaults are right; over
    a socket the audio arrives in packets, and numbering each packet from zero gives every
    frame an offset near zero. Everything downstream keys off those offsets — the refractory
    window compares ``frame.end_ms`` against the last decision, so a detector fed
    packet-local offsets fires once and then believes itself permanently inside its own
    cooldown. A caller streaming audio passes its running offsets here.
    """
    if not fmt.is_target:
        raise AudioError(f"frames must be {TARGET_FORMAT.describe()}, got {fmt.describe()}")
    per_frame = int(fmt.sample_rate * frame_ms / 1000)
    if per_frame <= 0:
        raise AudioError(f"frame_ms={frame_ms} is shorter than one sample")
    samples = samples_from_pcm(pcm)
    total = len(samples) // per_frame
    for index in range(total):
        window = samples[index * per_frame : (index + 1) * per_frame]
        yield PcmFrame(
            index=start_index + index,
            start_ms=start_ms + round(index * per_frame * 1000 / fmt.sample_rate),
            duration_ms=frame_ms,
            samples=window,
        )


def read_wav(path: Path) -> tuple[PcmFormat, bytes]:
    """Read a WAV file into its format and its raw frames."""
    try:
        with wave.open(str(path), "rb") as handle:
            fmt = PcmFormat(
                sample_rate=handle.getframerate(),
                channels=handle.getnchannels(),
                sample_width=handle.getsampwidth(),
            )
            return fmt, handle.readframes(handle.getnframes())
    except wave.Error as exc:
        raise AudioError(f"{path} is not a readable WAV file: {exc}") from exc


def write_wav(path: Path, pcm: bytes, fmt: PcmFormat = TARGET_FORMAT) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(path), "wb") as handle:
        handle.setnchannels(fmt.channels)
        handle.setsampwidth(fmt.sample_width)
        handle.setframerate(fmt.sample_rate)
        handle.writeframes(pcm)


def to_mono_16k(pcm: bytes, fmt: PcmFormat) -> bytes:
    """Convert an arbitrary WAV payload to the target format.

    A phone records 48 kHz stereo; every VAD in the world wants 16 kHz mono. This is the
    single place that gap is crossed, so an ingest pays for it once and every replay
    afterwards is already in the right shape.

    Downmix is a plain average of channels, and resampling is linear interpolation. Both
    are cruder than a windowed-sinc filter would be, and both are *deliberately* crude:
    aliasing from a sharp resample adds high-frequency energy, which would flatter a
    zero-crossing-based VAD in exactly the direction that makes the measurement look
    better than the deployed path. Linear interpolation is lossy in the safe direction —
    it attenuates the top of the band rather than folding it back down.
    """
    if fmt.sample_width != 2:
        raise AudioError(
            f"only 16-bit PCM is supported, got {fmt.sample_width * 8}-bit — "
            "re-export the recording as 16-bit WAV"
        )
    if fmt.channels < 1:
        raise AudioError(f"a recording needs at least one channel, got {fmt.channels}")

    samples = samples_from_pcm(pcm)
    if fmt.channels > 1:
        step = fmt.channels
        usable = (len(samples) // step) * step
        samples = array(
            "h",
            (sum(samples[i : i + step]) // step for i in range(0, usable, step)),
        )

    if fmt.sample_rate == TARGET_SAMPLE_RATE:
        return pcm_from_samples(samples)
    if fmt.sample_rate <= 0:
        raise AudioError(f"nonsensical sample rate {fmt.sample_rate}")

    ratio = fmt.sample_rate / TARGET_SAMPLE_RATE
    out_len = int(len(samples) / ratio)
    resampled: array[int] = array("h", bytes(2 * out_len))
    last = len(samples) - 1
    for index in range(out_len):
        position = index * ratio
        left = int(position)
        if left >= last:
            resampled[index] = samples[last]
            continue
        fraction = position - left
        value = samples[left] * (1.0 - fraction) + samples[left + 1] * fraction
        resampled[index] = max(-32_768, min(32_767, int(round(value))))
    return pcm_from_samples(resampled)


def slice_ms(pcm: bytes, start_ms: int, end_ms: int, fmt: PcmFormat = TARGET_FORMAT) -> bytes:
    """Extract ``[start_ms, end_ms)`` from a recording, clamped to its bounds."""
    total = duration_ms(pcm, fmt)
    start = max(0, min(start_ms, total))
    end = max(start, min(end_ms, total))
    bytes_per_ms = fmt.sample_rate * fmt.frame_bytes / 1000
    return pcm[int(start * bytes_per_ms) : int(end * bytes_per_ms)]
