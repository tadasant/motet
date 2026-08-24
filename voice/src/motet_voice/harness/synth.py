"""Synthetic outdoor audio — deterministic, offline, and the reason CI can test this at all.

**This is not a substitute for the walk.** It is a substitute for *needing* the walk in
order to know whether the harness works. Real wind is not a sum of sines, and the numbers
this produces say nothing about a real provider. What it does give is a recording with
known ground truth — every speech burst is at a known offset — so the pipeline that turns
audio into a false-barge-in rate can be exercised end to end, in CI, with no microphone and
no vendor.

The four ingredients are the four things Tadas's walk has in it, which is deliberate: if a
detector fires on the synthetic gusts, it will fire on real ones, and the harness will have
found something before anybody puts a coat on.
"""

from __future__ import annotations

import math
import random
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Final

from ..audio import TARGET_SAMPLE_RATE, pcm_from_samples

#: Roughly what a phone records on a breezy day: the ambient ingredients sit around
#: −27 dBFS and a voice addressed to the phone lands around −11 dBFS, so speech is ~16 dB
#: above the noise floor. That gap is the realistic one — a person talking *to* their phone
#: is much louder than the weather — and getting it wrong in either direction would make the
#: synthetic tests measure the generator rather than the detector.
WIND_LEVEL: Final = 0.06
TRAFFIC_LEVEL: Final = 0.03
FOOTSTEP_LEVEL: Final = 0.18
SPEECH_LEVEL: Final = 0.62

#: The microphone's own noise, always present. Around −48 dBFS.
#:
#: Not decoration. Without it the gust envelopes can multiply out to near-digital-silence,
#: and an adaptive noise floor tracking a signal that briefly reaches −70 dBFS settles far
#: below anything real — after which ordinary ambient reads as a +25 dB event and the
#: detector fires on nothing. That is a property of the *generator*, not of the detector: a
#: real microphone outdoors never goes quiet, and a synthetic adversary that does is testing
#: a situation that cannot happen.
MIC_NOISE_LEVEL: Final = 0.004


@dataclass(frozen=True)
class SpeechWindow:
    """Ground truth: the listener really was talking here."""

    start_ms: int
    end_ms: int


@dataclass(frozen=True)
class SyntheticWalk:
    pcm: bytes
    duration_ms: int
    speech: tuple[SpeechWindow, ...] = field(default=())


def synthesize_walk(
    *,
    duration_ms: int,
    speech_at_ms: Sequence[int] = (),
    speech_duration_ms: int = 1_200,
    wind: bool = True,
    traffic: bool = True,
    footsteps: bool = True,
    seed: int = 20260824,
) -> SyntheticWalk:
    """Build a recording with a known answer.

    ``seed`` makes every run byte-identical, which matters more than realism: a harness
    whose CI numbers move between runs cannot tell a regression from weather.
    """
    rng = random.Random(seed)
    total = int(TARGET_SAMPLE_RATE * duration_ms / 1000)
    samples = [MIC_NOISE_LEVEL * rng.uniform(-1.0, 1.0) for _ in range(total)]

    if wind:
        _add_wind(samples, rng)
    if traffic:
        _add_traffic(samples, rng)
    if footsteps:
        _add_footsteps(samples, rng)

    windows = []
    for start_ms in speech_at_ms:
        end_ms = start_ms + speech_duration_ms
        _add_speech(samples, start_ms, end_ms, rng)
        windows.append(SpeechWindow(start_ms=start_ms, end_ms=end_ms))

    encoded = [max(-32_768, min(32_767, int(value * 32_767))) for value in samples]
    return SyntheticWalk(
        pcm=pcm_from_samples(encoded),
        duration_ms=duration_ms,
        speech=tuple(windows),
    )


def _add_wind(samples: list[float], rng: random.Random) -> None:
    """Low-frequency rumble with slow gusts.

    Wind is the adversary that breaks naive energy gates: it is *loud* and it is *not
    speech*. Modelled as smoothed noise below ~120 Hz, amplitude-modulated by a couple of
    slow gust envelopes, which reproduces both properties — high energy, near-zero
    zero-crossing rate.
    """
    smoothed = 0.0
    for index in range(len(samples)):
        seconds = index / TARGET_SAMPLE_RATE
        # One-pole lowpass over white noise: cheap, and its spectrum is the right shape.
        smoothed += 0.02 * (rng.uniform(-1.0, 1.0) - smoothed)
        gust = 0.5 + 0.5 * math.sin(2 * math.pi * 0.07 * seconds + 1.1)
        gust *= 0.6 + 0.4 * math.sin(2 * math.pi * 0.23 * seconds)
        samples[index] += WIND_LEVEL * smoothed * 18.0 * gust


def _add_traffic(samples: list[float], rng: random.Random) -> None:
    """Broadband hiss that swells and fades, like a car going past."""
    for pass_index in range(3):
        centre = int(len(samples) * (0.2 + 0.3 * pass_index))
        width = int(TARGET_SAMPLE_RATE * 2.5)
        for offset in range(-width, width):
            index = centre + offset
            if not 0 <= index < len(samples):
                continue
            envelope = math.exp(-((offset / width) ** 2) * 3.0)
            samples[index] += TRAFFIC_LEVEL * envelope * rng.uniform(-1.0, 1.0)


def _add_footsteps(samples: list[float], rng: random.Random) -> None:
    """Short transients twice a second — the walker's own shoes, and a dog's tags."""
    step_period = int(TARGET_SAMPLE_RATE * 0.55)
    tail = int(TARGET_SAMPLE_RATE * 0.06)
    index = step_period
    while index < len(samples):
        strength = FOOTSTEP_LEVEL * rng.uniform(0.7, 1.3)
        for offset in range(tail):
            position = index + offset
            if position >= len(samples):
                break
            decay = math.exp(-offset / (tail * 0.25))
            samples[position] += strength * decay * rng.uniform(-1.0, 1.0)
        index += step_period + rng.randint(-800, 800)


def _add_speech(samples: list[float], start_ms: int, end_ms: int, rng: random.Random) -> None:
    """A voiced burst: a harmonic stack, some aspiration, and a syllable envelope.

    Not intelligible, and it does not need to be — barge-in detection asks whether somebody
    is talking, never what they said. What matters is that the *spectrum* sits where a voice
    sits, so the zero-crossing band in :class:`~motet_voice.vad.EnergyVad` is exercised
    honestly rather than sidestepped.

    The aspiration term is why this is not just a harmonic stack. A pure stack at a 120 Hz
    fundamental crosses zero about 240 times a second — a zero-crossing rate of 0.015, which
    is *below* the band the VAD considers plausible for speech, so a purely periodic "voice"
    gets rejected as rumble. Real speech carries breath and fricatives; without them the
    generator would be testing the detector against a signal no larynx produces.
    """
    start = int(TARGET_SAMPLE_RATE * start_ms / 1000)
    end = min(len(samples), int(TARGET_SAMPLE_RATE * end_ms / 1000))
    f0 = rng.uniform(105.0, 135.0)
    previous = [0.0]
    for index in range(start, end):
        if index < 0:
            continue
        seconds = (index - start) / TARGET_SAMPLE_RATE
        # Syllables at ~3.5 Hz. The modulation is shallow — about 6 dB — because that is what
        # continuous speech looks like when measured in 20 ms frames. An earlier version swung
        # 20 dB every 125 ms, which is what a *single word* looks like, and it made every
        # consecutive-frame policy look broken for a reason that was purely generator artifact.
        syllable = 0.75 + 0.25 * math.sin(2 * math.pi * 3.5 * seconds)
        ramp = min(1.0, seconds / 0.05, (end - index) / TARGET_SAMPLE_RATE / 0.05)
        value = 0.0
        for harmonic in range(1, 9):
            value += math.sin(2 * math.pi * f0 * harmonic * seconds) / harmonic
        # Aspiration: high-frequency noise, as a first difference of white noise. Small in
        # energy, decisive in zero-crossing rate.
        noise = rng.uniform(-1.0, 1.0)
        aspiration = noise - previous[0]
        previous[0] = noise
        samples[index] += (
            SPEECH_LEVEL * syllable * max(0.0, ramp) * (value * 0.5 + aspiration * 0.35)
        )
