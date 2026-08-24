"""The barge-in measurement harness: capture, replay, score, report."""

from .capture import (
    RUN_AUDIO,
    RUN_METADATA,
    RunError,
    SpeechLabel,
    WalkRun,
    create_run,
    ingest_recording,
    load_run,
    read_decisions,
    replay_dir,
)
from .metrics import ArmMetrics, ScoredRun, score
from .replay import policies_for_measurement, replay_detector, replay_run
from .report import render_report
from .synth import SpeechWindow, SyntheticWalk, synthesize_walk
from .variants import DEFAULT_VARIANT, SWEEP, sweep

__all__ = [
    "DEFAULT_VARIANT",
    "RUN_AUDIO",
    "RUN_METADATA",
    "SWEEP",
    "ArmMetrics",
    "RunError",
    "ScoredRun",
    "SpeechLabel",
    "SpeechWindow",
    "SyntheticWalk",
    "WalkRun",
    "create_run",
    "ingest_recording",
    "load_run",
    "policies_for_measurement",
    "read_decisions",
    "render_report",
    "replay_detector",
    "replay_dir",
    "replay_run",
    "score",
    "sweep",
    "synthesize_walk",
]
