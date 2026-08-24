"""Barge-in: the decision, the policy that makes it, and the record it leaves behind.

**This module is the point of the whole exercise.** The provider question is settled by one
number — *false barge-ins per minute of open mic, outdoors, with wind and traffic and a dog* —
and a number is only worth having if the thing that produced it can be reviewed afterwards.
So every decision carries the evidence that produced it: the offset, the VAD reading, the
adaptive noise floor at that instant, how many consecutive frames it took, and a pointer to
a WAV snippet of the moment. An impression from a walk is worthless. A labelled log is the
deliverable.

**A decision is a record, not a side effect.** The detector returns one; it does not
interrupt anything. That is what lets the identical code path run live in the service and
offline against a captured recording, which is in turn what makes one walk settle several
config variants instead of one.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any, Protocol, runtime_checkable

from .audio import DEFAULT_FRAME_MS, PcmFrame
from .vad import Vad, VadReading


@dataclass(frozen=True)
class BargeInPolicy:
    """The tunable half of a barge-in decision — one point in the config sweep.

    Named, frozen, and serialized into every decision, so a log line says which
    configuration produced it and a report can rank variants against each other.
    """

    name: str = "default"

    #: Per-frame speech probability a frame must reach to count towards a trigger.
    speech_probability_threshold: float = 0.6
    #: How many *consecutive* qualifying frames trigger. At 20 ms a frame, 6 is 120 ms —
    #: long enough that a car door or a footfall cannot reach it, short enough that
    #: interrupting still feels immediate. This is the single most important dial in the
    #: sweep and the reason the harness exists.
    consecutive_speech_frames: int = 6
    #: A qualifying frame must also be this far above the adaptive noise floor. Largely
    #: redundant with the probability for :class:`~motet_voice.vad.EnergyVad`, which derives
    #: probability from SNR — but not for a binary VAD such as WebRTC's, where it is the only
    #: loudness gate there is.
    min_snr_db: float = 12.0
    #: Once triggered, ignore everything for this long. Without it a single utterance
    #: produces a decision every frame and the headline metric counts syllables.
    refractory_ms: int = 2_000
    #: Only count a barge-in while narration is playing. ``False`` is the measurement
    #: setting — the walk has an open mic and nothing playing, and every trigger is a false
    #: positive by construction. ``True`` is the deployed setting, where interrupting
    #: silence is meaningless.
    require_narration_playing: bool = False

    def to_json(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "speech_probability_threshold": self.speech_probability_threshold,
            "consecutive_speech_frames": self.consecutive_speech_frames,
            "min_snr_db": self.min_snr_db,
            "refractory_ms": self.refractory_ms,
            "require_narration_playing": self.require_narration_playing,
        }


@dataclass(frozen=True)
class BargeInDecision:
    """One barge-in, with everything needed to judge it after the fact.

    ``at_ms`` is the offset into the recording (or the session) at which the trigger
    *completed* — the end of the last qualifying frame. ``onset_ms`` is where the run of
    qualifying frames began, which is where a snippet should start and what a latency
    measurement is taken against.
    """

    at_ms: int
    onset_ms: int
    arm: str
    variant: str
    trigger: str
    spoken_through_ms: int
    narration_playing: bool
    consecutive_frames: int
    speech_probability: float
    rms_dbfs: float
    noise_floor_dbfs: float
    snr_db: float
    zero_crossing_rate: float
    snippet: str | None = None

    @property
    def latency_ms(self) -> int:
        """How long the detector took to commit, from onset to trigger."""
        return self.at_ms - self.onset_ms

    def with_snippet(self, snippet: str) -> BargeInDecision:
        return BargeInDecision(**{**self.to_fields(), "snippet": snippet})

    def to_fields(self) -> dict[str, Any]:
        return {
            "at_ms": self.at_ms,
            "onset_ms": self.onset_ms,
            "arm": self.arm,
            "variant": self.variant,
            "trigger": self.trigger,
            "spoken_through_ms": self.spoken_through_ms,
            "narration_playing": self.narration_playing,
            "consecutive_frames": self.consecutive_frames,
            "speech_probability": self.speech_probability,
            "rms_dbfs": self.rms_dbfs,
            "noise_floor_dbfs": self.noise_floor_dbfs,
            "snr_db": self.snr_db,
            "zero_crossing_rate": self.zero_crossing_rate,
            "snippet": self.snippet,
        }

    def to_json(self) -> dict[str, Any]:
        fields = self.to_fields()
        fields["latency_ms"] = self.latency_ms
        # Rounded on the way out: these are logged as JSONL and read by a human on a phone.
        # Sixteen significant figures of a logistic output is noise wearing a lab coat.
        for key in ("speech_probability", "rms_dbfs", "noise_floor_dbfs", "snr_db"):
            fields[key] = round(float(fields[key]), 2)
        fields["zero_crossing_rate"] = round(float(fields["zero_crossing_rate"]), 4)
        return fields

    @classmethod
    def from_json(cls, payload: Mapping[str, Any]) -> BargeInDecision:
        known = {key: payload[key] for key in cls.__dataclass_fields__ if key in payload}
        return cls(**known)


@runtime_checkable
class TurnDetector(Protocol):
    """Decide, frame by frame, whether the listener has started talking to us.

    **This is the seam the two provider arms differ at**, and it is deliberately narrower
    than "a realtime session": conversation is not what the walk measures. Turn detection
    is. A hosted realtime provider does it server-side inside its own socket; a composed
    pipeline does it locally with a VAD. Both reduce to this one method, so the harness can
    replay a recording through either without knowing which it has.
    """

    @property
    def arm(self) -> str: ...

    @property
    def variant(self) -> str: ...

    def reset(self) -> None: ...

    def observe(
        self, frame: PcmFrame, *, narration_playing: bool, spoken_through_ms: int
    ) -> BargeInDecision | None: ...


@dataclass
class VadTurnDetector:
    """A :class:`TurnDetector` built from a VAD and a policy.

    Both shipped arms are instances of this — the composed arm wires it to a local VAD, and
    the offline OpenAI arm wires it to an emulation of that vendor's documented server-VAD
    parameters. Keeping one implementation means a difference between two arms in a report
    is a difference between their *detectors*, never between two hand-written state
    machines that drifted apart.
    """

    vad: Vad
    policy: BargeInPolicy = field(default_factory=BargeInPolicy)
    arm_name: str = "composed"
    trigger: str = "local_vad"
    frame_ms: int = DEFAULT_FRAME_MS

    _run: int = field(default=0, init=False)
    _run_started_ms: int = field(default=0, init=False)
    _last_fired_ms: int | None = field(default=None, init=False)

    @property
    def arm(self) -> str:
        return self.arm_name

    @property
    def variant(self) -> str:
        return self.policy.name

    def reset(self) -> None:
        self.vad.reset()
        self._run = 0
        self._run_started_ms = 0
        self._last_fired_ms = None

    def observe(
        self, frame: PcmFrame, *, narration_playing: bool, spoken_through_ms: int
    ) -> BargeInDecision | None:
        reading = self.vad.observe(frame)
        qualifies = (
            reading.speech_probability >= self.policy.speech_probability_threshold
            and reading.snr_db >= self.policy.min_snr_db
        )

        if not qualifies:
            self._run = 0
            return None

        if self._run == 0:
            self._run_started_ms = frame.start_ms
        self._run += 1

        if self._run < self.policy.consecutive_speech_frames:
            return None
        if self.policy.require_narration_playing and not narration_playing:
            return None
        if self._suppressed(frame.end_ms):
            return None

        self._last_fired_ms = frame.end_ms
        # The run is *not* cleared here. Clearing it would make the very next frame start
        # counting towards a second trigger, and the refractory window is what stops that —
        # keeping the run intact means a continuous utterance stays one utterance.
        return self._decide(frame, reading, narration_playing, spoken_through_ms)

    def _suppressed(self, now_ms: int) -> bool:
        return (
            self._last_fired_ms is not None
            and now_ms - self._last_fired_ms < self.policy.refractory_ms
        )

    def _decide(
        self,
        frame: PcmFrame,
        reading: VadReading,
        narration_playing: bool,
        spoken_through_ms: int,
    ) -> BargeInDecision:
        return BargeInDecision(
            at_ms=frame.end_ms,
            onset_ms=self._run_started_ms,
            arm=self.arm,
            variant=self.variant,
            trigger=self.trigger,
            spoken_through_ms=spoken_through_ms,
            narration_playing=narration_playing,
            consecutive_frames=self._run,
            speech_probability=reading.speech_probability,
            rms_dbfs=reading.rms_dbfs,
            noise_floor_dbfs=reading.noise_floor_dbfs,
            snr_db=reading.snr_db,
            zero_crossing_rate=reading.zero_crossing_rate,
        )
