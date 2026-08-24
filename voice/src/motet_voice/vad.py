"""Voice activity detection, behind a Protocol, with a fake — invariant 9.

**The VAD is the thing being measured, so it is the thing that has to be swappable.** The
provider question the walk settles is really two questions stacked: does a hosted realtime
model's server-side turn detection beat a VAD we run ourselves, and — if we run it
ourselves — which VAD and which thresholds. One interface makes both a config sweep instead
of a rewrite.

Three implementations ship:

* :class:`EnergyVad` — adaptive-noise-floor SNR gating with a zero-crossing plausibility
  band. Pure stdlib, deterministic, and therefore the one that runs in CI and the one whose
  numbers a replay can be trusted to reproduce.
* :class:`WebrtcVad` — the WebRTC GMM detector, if ``webrtcvad`` happens to be installed.
  An **optional** dependency on purpose: it is a C extension, it is not needed to run the
  harness, and a hard dependency would make the offline path fail to install for the sake
  of one arm of one sweep.
* :class:`ScriptedVad` — the fake. Reads its answers off a list, so a test can construct
  the exact sequence of frames a policy is supposed to react to without synthesizing audio
  that happens to produce it.

**Why an energy VAD at all, when the interesting adversary is wind.** Because wind is not
loud in the band speech occupies — it is loud, and low. An energy-only gate fires on every
gust; energy *relative to an adaptive floor*, filtered by whether the frame's zero-crossing
rate is even plausible for speech, does not. That claim is exactly what the walk tests, and
if it is wrong the harness will say so.
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any, Protocol, runtime_checkable

from .audio import SILENCE_DBFS, PcmFrame, dbfs, zero_crossing_rate


class VadUnavailable(RuntimeError):
    """This VAD cannot run here — usually an optional dependency that is not installed."""


@dataclass(frozen=True)
class VadReading:
    """What a VAD saw in one frame.

    Every field is logged with each barge-in decision. That is the difference between "it
    fired" and a log Tadas can review after the walk: the reading says whether the detector
    heard a loud frame, a frame far above a quiet floor, or a frame whose spectrum was
    never plausibly a voice.
    """

    speech_probability: float
    rms_dbfs: float
    noise_floor_dbfs: float
    snr_db: float
    zero_crossing_rate: float


@runtime_checkable
class Vad(Protocol):
    """Score one frame for the presence of speech.

    Stateful on purpose: a detector that adapts to the noise floor has to remember it, and
    that memory is most of what distinguishes a usable outdoor VAD from a gate.
    """

    @property
    def name(self) -> str: ...

    def reset(self) -> None: ...

    def observe(self, frame: PcmFrame) -> VadReading: ...


@dataclass
class EnergyVad:
    """SNR over an adaptive noise floor, gated by a zero-crossing plausibility band.

    **The floor is a quantile tracker, not an average.** Each frame nudges it up by
    :attr:`up_step_db` if the frame is louder and down by :attr:`down_step_db` if it is
    quieter, so it converges on the ``up / (up + down)`` quantile of recent frame levels —
    a quarter, at the defaults. Two properties fall out of that and both are load-bearing:

    * **It cannot be dragged by a loud event.** The step is bounded in dB, so a 1.5-second
      utterance can lift the floor by at most ~4 dB however loud it is. A voice cannot
      raise the floor to itself and cut itself off.
    * **It cannot latch.** Every frame moves it, in one direction or the other, always.

    Both of those were learned the hard way, and the earlier attempts are worth recording
    because they are the obvious ideas:

    * An **exponential average** tracks the *mean*, which a single loud second moves a long
      way — the speaker raises the floor to their own voice mid-sentence and the SNR
      collapses.
    * Adding a **freeze** ("stop adapting while the frame is loud") fixes that and
      introduces a deadlock: if the floor is ever left too low, every subsequent frame looks
      loud, so the freeze never releases and the detector fires on ambient forever. That bug
      was visible in the harness as a run whose false-positive rate *rose* over its length.
    * Making the freeze **asymmetric** ("quick to follow quiet, slow to follow loud") parks
      the floor at the quietest instant of the recording, after which every gust is a large
      positive SNR.

    A quantile tracker has none of those failure modes, and it is the standard answer.
    """

    #: dB the floor moves per frame toward a louder level. Small on purpose: this is the
    #: rate at which a *sustained* change in the environment is accepted, and it bounds how
    #: far any single loud event can shift the floor.
    up_step_db: float = 0.05
    #: dB per frame toward a quieter level. Three times the up step, which puts the floor at
    #: roughly the 25th percentile of recent frame levels — comfortably below ordinary
    #: ambient, comfortably above the microphone's own noise.
    down_step_db: float = 0.15
    #: SNR at which speech probability crosses 0.5. A person addressing their own phone
    #: lands well above the ambient the floor settles to; a gust or a passing car lifts it a
    #: little. **This is a hypothesis, and the walk is what tests it** — which is why the
    #: sweep in ``harness/variants.py`` varies the gates around it rather than trusting it.
    snr_midpoint_db: float = 18.0
    #: Logistic width. Larger is a softer decision; smaller approaches a hard threshold.
    snr_scale_db: float = 3.0
    #: Below this dBFS nothing counts, whatever the SNR says. A 60 dB SNR over a -95 dBFS
    #: floor is a recording of a quiet room, not somebody talking.
    absolute_floor_dbfs: float = -55.0
    #: Voiced speech at 16 kHz sits roughly in this zero-crossing band. Below it is wind,
    #: handling noise and engine rumble; above it is hiss, tyre noise and clothing rustle.
    min_zcr: float = 0.02
    max_zcr: float = 0.32
    #: Frames whose ZCR is outside the band keep this fraction of their probability rather
    #: than none of it. Not zero, because a real barge-in clipped by a gust should still be
    #: able to fire if it is loud enough — a hard veto turns a spectral hint into a mute.
    out_of_band_weight: float = 0.25
    #: Frames at the start of a recording, during which the floor converges at speed. Without
    #: a warm-up the floor starts at the first frame's level and takes tens of seconds to
    #: walk to the right place at 0.05 dB a frame — and a barge-in inside that window is
    #: measured against a floor that is still wrong. On a walk that is a curiosity; in a
    #: *session*, the first two seconds are exactly when a listener is most likely to
    #: interrupt.
    #: **Consequence worth knowing:** for a second or two at the start of a recording the
    #: floor is still finding the environment, and a barge-in in that window can be missed.
    #: The walk instructions therefore start with "walk for a bit before you say anything",
    #: and a deployed session should stream ambient audio briefly before opening the mic.
    warmup_frames: int = 50
    warmup_multiplier: float = 12.0

    _floor_dbfs: float | None = field(default=None, init=False)
    _seen: int = field(default=0, init=False)

    @property
    def name(self) -> str:
        return "energy"

    def reset(self) -> None:
        self._floor_dbfs = None
        self._seen = 0

    def observe(self, frame: PcmFrame) -> VadReading:
        level = dbfs(frame.samples)
        zcr = zero_crossing_rate(frame.samples)

        if self._floor_dbfs is None:
            # Seed from the first frame rather than from a constant: recordings differ by
            # 30 dB of gain between a phone in a pocket and a headset, and a constant seed
            # means the first seconds of every recording are measured against the wrong scale.
            self._floor_dbfs = level

        snr = level - self._floor_dbfs
        probability = _logistic((snr - self.snr_midpoint_db) / max(self.snr_scale_db, 1e-6))
        if level < self.absolute_floor_dbfs:
            probability = 0.0
        elif not (self.min_zcr <= zcr <= self.max_zcr):
            probability *= self.out_of_band_weight

        self._seen += 1
        scale = self.warmup_multiplier if self._seen <= self.warmup_frames else 1.0
        if level > self._floor_dbfs:
            self._floor_dbfs += self.up_step_db * scale
        else:
            self._floor_dbfs -= self.down_step_db * scale

        return VadReading(
            speech_probability=probability,
            rms_dbfs=level,
            noise_floor_dbfs=self._floor_dbfs,
            snr_db=snr,
            zero_crossing_rate=zcr,
        )


@dataclass
class WebrtcVad:
    """The WebRTC GMM VAD, when ``webrtcvad`` is installed.

    Included because it is the incumbent every voice stack reaches for, which makes it the
    honest baseline for "is our own gate any good". It is binary — speech or not — so its
    probability is 0.0 or 1.0 and a policy's probability threshold degenerates to its
    consecutive-frame count. dBFS and ZCR are still measured and logged, so a decision from
    this arm is reviewable in the same terms as one from :class:`EnergyVad`.

    Not a declared dependency: see the module docstring.
    """

    aggressiveness: int = 2
    _detector: Any = field(default=None, init=False)

    @property
    def name(self) -> str:
        return f"webrtc{self.aggressiveness}"

    def reset(self) -> None:
        self._detector = None

    def _ensure(self) -> Any:
        if self._detector is None:
            try:
                import webrtcvad  # noqa: PLC0415 — optional dependency, imported on use
            except ImportError as exc:  # pragma: no cover — depends on the environment
                raise VadUnavailable(
                    "the 'webrtcvad' package is not installed, so this arm cannot run; "
                    "`uv pip install webrtcvad` to include it in a sweep"
                ) from exc
            self._detector = webrtcvad.Vad(self.aggressiveness)
        return self._detector

    def observe(self, frame: PcmFrame) -> VadReading:
        from .audio import TARGET_SAMPLE_RATE, pcm_from_samples  # noqa: PLC0415

        detector = self._ensure()
        speech = bool(detector.is_speech(pcm_from_samples(frame.samples), TARGET_SAMPLE_RATE))
        level = dbfs(frame.samples)
        return VadReading(
            speech_probability=1.0 if speech else 0.0,
            rms_dbfs=level,
            noise_floor_dbfs=SILENCE_DBFS,
            snr_db=level - SILENCE_DBFS,
            zero_crossing_rate=zero_crossing_rate(frame.samples),
        )


@dataclass
class ScriptedVad:
    """The fake: a fixed sequence of probabilities, indexed by frame.

    Frames past the end of the script read as silence, so a test can supply five values and
    let the recording run on.
    """

    probabilities: Sequence[float] = ()
    level_dbfs: float = -30.0

    @property
    def name(self) -> str:
        return "scripted"

    def reset(self) -> None:
        return None

    def observe(self, frame: PcmFrame) -> VadReading:
        probability = (
            self.probabilities[frame.index] if frame.index < len(self.probabilities) else 0.0
        )
        return VadReading(
            speech_probability=probability,
            rms_dbfs=self.level_dbfs,
            noise_floor_dbfs=self.level_dbfs - 20.0,
            snr_db=20.0,
            zero_crossing_rate=0.1,
        )


def _logistic(value: float) -> float:
    if value < -60.0:  # exp overflows below this, and the answer is zero anyway
        return 0.0
    return 1.0 / (1.0 + math.exp(-value))
