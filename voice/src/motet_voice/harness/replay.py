"""Replay a recorded walk through every arm and every variant.

**This is what turns one walk into a repeatable comparison.** The recording is fixed, so
every arm sees byte-identical audio, every variant sees byte-identical audio, and a re-run
next week against a new variant is comparable with the run from tonight. A live A/B outdoors
can never claim any of that: the wind changes between arms.

The replay drives the *same* :class:`~motet_voice.bargein.TurnDetector` objects the live
service uses. There is no offline reimplementation of the decision logic — if there were, the
thing measured would not be the thing deployed, and the whole exercise would be theatre.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Sequence
from dataclasses import replace
from pathlib import Path

from ..audio import DEFAULT_FRAME_MS, iter_frames
from ..bargein import BargeInDecision, BargeInPolicy
from ..clock import PlaybackClock
from ..config import VoiceSettings
from ..realtime import RealtimeArm, build_all_arms
from .capture import WalkRun, replay_dir, write_decisions, write_snippet
from .metrics import ArmMetrics, ScoredRun, score
from .variants import sweep

logger = logging.getLogger("motet.voice.replay")


def replay_detector(
    run: WalkRun,
    pcm: bytes,
    arm: RealtimeArm,
    policy: BargeInPolicy,
    *,
    frame_ms: int = DEFAULT_FRAME_MS,
) -> tuple[BargeInDecision, ...]:
    """Push one recording through one arm's turn detector, frame by frame.

    The clock is wound forward from the *recording's* offsets rather than from wall time, so
    a decision's ``spoken_through_ms`` is reproducible. That is invariant 5 doing something
    useful offline: because we own the clock, we can rewind it.
    """
    detector = arm.build_turn_detector(policy)
    detector.reset()

    # A replay is an open mic over silence: nothing is being narrated, so `narration_playing`
    # is False throughout and every trigger counts. A variant that only fires while narration
    # plays would score zero here for the wrong reason, which is why the harness overrides
    # `require_narration_playing` — see `policies_for_measurement`.
    clock = PlaybackClock(now=lambda: 0.0)
    decisions: list[BargeInDecision] = []
    for frame in iter_frames(pcm, frame_ms=frame_ms):
        decision = detector.observe(
            frame, narration_playing=False, spoken_through_ms=clock.spoken_through_ms
        )
        if decision is not None:
            decisions.append(decision)
    return tuple(decisions)


def policies_for_measurement(policies: Sequence[BargeInPolicy]) -> tuple[BargeInPolicy, ...]:
    """Force ``require_narration_playing`` off for a measurement run.

    Stated as its own function because it is the one place the harness deliberately differs
    from the deployed configuration, and a silent difference there would invalidate every
    number the harness produces.
    """
    return tuple(replace(policy, require_narration_playing=False) for policy in policies)


def replay_run(
    run: WalkRun,
    settings: VoiceSettings,
    *,
    variants: Sequence[str] = (),
    arms: Sequence[str] = (),
    write_snippets: bool = True,
) -> ScoredRun:
    """Replay every arm × variant, writing decisions, snippets and metrics to disk."""
    pcm = run.pcm()
    built = build_all_arms(settings)
    wanted = {name.strip() for name in arms if name.strip()} or set(built)
    unknown = wanted - set(built)
    if unknown:
        raise ValueError(f"unknown arm(s): {', '.join(sorted(unknown))}")

    policies = policies_for_measurement(sweep(variants))
    scored = ScoredRun(run_label=run.label)

    for arm_name in sorted(wanted):
        arm = built[arm_name]
        capabilities = arm.capabilities()
        emulated = bool(capabilities.dormant_reason) and capabilities.turn_detection == "server"
        for policy in policies:
            decisions = replay_detector(run, pcm, arm, policy)
            target = replay_dir(run, arm_name, policy.name)
            if write_snippets:
                decisions = tuple(
                    decision.with_snippet(write_snippet(run, pcm, decision, target / "snippets"))
                    for decision in decisions
                )
            write_decisions(target / "decisions.jsonl", decisions)
            metrics = score(
                run,
                decisions,
                arm=arm_name,
                variant=policy.name,
                emulated=emulated,
                note=capabilities.dormant_reason,
            )
            _write_metrics(target / "metrics.json", metrics)
            scored.metrics.append(metrics)
            logger.info(
                "replayed %s/%s: %d decisions, %.2f false/min",
                arm_name,
                policy.name,
                metrics.decisions,
                metrics.false_per_minute,
            )
    return scored


def _write_metrics(path: Path, metrics: ArmMetrics) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(metrics.to_json(), indent=2) + "\n", encoding="utf-8")
