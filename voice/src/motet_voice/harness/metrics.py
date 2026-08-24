"""The number the spike exists to produce, and the arithmetic behind it.

> **false barge-ins per minute of open mic**

Everything else in this package is in service of that one figure. It is defined here rather
than in a report template so that it is testable, and so that there is exactly one
definition of it.

**Two ground-truth modes, and the simpler one is the headline.**

* ``silent`` — the recording contains no speech at all, because the instruction was "walk
  and don't say a word". Then *every* decision is a false positive, no annotation is needed,
  and the metric is exact. This is the recording the walk instructions lead with, and the
  reason is that the alternative — annotating twenty minutes of audio from memory — is the
  step that does not happen.
* ``labelled`` — ``labels.jsonl`` says when the listener spoke. A decision overlapping a
  labelled window is a true positive; everything else is false; a labelled window with no
  decision is a miss. This is how the second, shorter recording measures whether a variant
  that never false-fires can still hear an actual interruption.

**Both halves matter and a report that shows only one is misleading.** A detector that never
fires has a perfect false-positive rate and is useless, which is precisely why
:class:`ArmMetrics` carries the detection rate next to it and the report prints them side by
side.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from statistics import median
from typing import Any, Final

from ..bargein import BargeInDecision
from .capture import SpeechLabel, WalkRun

#: How far outside a labelled window a decision may land and still count as catching it.
#: Generous, because a hand-written label is accurate to about a second and a detector that
#: fires 300 ms early has not made a mistake.
LABEL_TOLERANCE_MS: Final = 1_000


@dataclass(frozen=True)
class ArmMetrics:
    """One arm × one variant, scored against one recording."""

    arm: str
    variant: str
    open_mic_ms: int
    decisions: int
    false_positives: int
    true_positives: int
    missed: int
    median_latency_ms: int | None
    ground_truth: str
    #: Set when the arm's turn detection was emulated rather than measured — the OpenAI arm
    #: with no key. Carried through to the report so a number can never be read as a
    #: measurement of a vendor that was never called.
    emulated: bool = False
    note: str = ""

    @property
    def open_mic_minutes(self) -> float:
        return self.open_mic_ms / 60_000

    @property
    def false_per_minute(self) -> float:
        """**The headline.** Zero is the target; anything above ~0.5 is unusable outdoors."""
        minutes = self.open_mic_minutes
        return self.false_positives / minutes if minutes > 0 else 0.0

    @property
    def detection_rate(self) -> float | None:
        """Fraction of labelled utterances caught, or ``None`` when nothing was labelled."""
        total = self.true_positives + self.missed
        return self.true_positives / total if total else None

    def to_json(self) -> dict[str, Any]:
        return {
            "arm": self.arm,
            "variant": self.variant,
            "open_mic_ms": self.open_mic_ms,
            "open_mic_minutes": round(self.open_mic_minutes, 3),
            "decisions": self.decisions,
            "false_positives": self.false_positives,
            "true_positives": self.true_positives,
            "missed": self.missed,
            "false_per_minute": round(self.false_per_minute, 3),
            "detection_rate": (
                None if self.detection_rate is None else round(self.detection_rate, 3)
            ),
            "median_latency_ms": self.median_latency_ms,
            "ground_truth": self.ground_truth,
            "emulated": self.emulated,
            "note": self.note,
        }


@dataclass
class ScoredRun:
    """Every arm × variant scored against one recording."""

    run_label: str
    metrics: list[ArmMetrics] = field(default_factory=list)

    def best(self) -> ArmMetrics | None:
        """Fewest false positives per minute, breaking ties toward catching real speech.

        The tiebreak is not decoration: on a silent recording several variants routinely
        reach zero, and the useful answer among them is the most responsive one, not
        whichever happened to be listed first.
        """
        if not self.metrics:
            return None
        return min(
            self.metrics,
            key=lambda m: (
                m.false_per_minute,
                -(m.detection_rate if m.detection_rate is not None else 0.0),
                m.median_latency_ms if m.median_latency_ms is not None else 10**9,
            ),
        )

    def to_json(self) -> dict[str, Any]:
        return {
            "run": self.run_label,
            "metrics": [metric.to_json() for metric in self.metrics],
        }


def score(
    run: WalkRun,
    decisions: Sequence[BargeInDecision],
    *,
    arm: str,
    variant: str,
    emulated: bool = False,
    note: str = "",
) -> ArmMetrics:
    """Score one arm × variant against a recording's ground truth."""
    if run.ground_truth == "silent":
        false_positives = len(decisions)
        true_positives = 0
        missed = 0
    else:
        false_positives, true_positives, missed = _score_against_labels(run.labels, decisions)

    latencies = [decision.latency_ms for decision in decisions]
    return ArmMetrics(
        arm=arm,
        variant=variant,
        open_mic_ms=run.duration_ms,
        decisions=len(decisions),
        false_positives=false_positives,
        true_positives=true_positives,
        missed=missed,
        median_latency_ms=int(median(latencies)) if latencies else None,
        ground_truth=run.ground_truth,
        emulated=emulated,
        note=note,
    )


def _score_against_labels(
    labels: Sequence[SpeechLabel], decisions: Sequence[BargeInDecision]
) -> tuple[int, int, int]:
    """Return ``(false_positives, true_positives, missed)``.

    A window counts as caught once, however many times a detector fired inside it: the
    refractory window already collapses an utterance to one decision, and counting the rest
    as extra true positives would let a chattery variant score better for being chattery.
    Extra decisions inside an already-caught window are simply not counted at all — they are
    neither a new catch nor a false alarm.
    """
    caught: set[int] = set()
    false_positives = 0
    for decision in decisions:
        hit = next(
            (
                index
                for index, label in enumerate(labels)
                if label.overlaps(
                    decision.onset_ms, decision.at_ms, tolerance_ms=LABEL_TOLERANCE_MS
                )
            ),
            None,
        )
        if hit is None:
            false_positives += 1
        else:
            caught.add(hit)
    return false_positives, len(caught), len(labels) - len(caught)
