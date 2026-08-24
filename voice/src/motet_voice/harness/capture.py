"""The run directory: what a walk leaves on disk, and how a phone recording becomes one.

**Designed for someone outdoors with a phone and a dog**, which rules out a laptop, a live
socket, and anything that has to be running while it happens. So the capture device is the
phone's own voice recorder, and everything else is offline:

```
walk on a phone  ->  motet-voice ingest  ->  run directory  ->  motet-voice replay
                                                            ->  motet-voice upload
```

The run directory is the unit of everything downstream:

```
<run>/
  run.json                 what this recording is, and how to read it
  audio.wav                16 kHz mono — the canonical form, converted once at ingest
  labels.jsonl             optional ground truth: when the listener really spoke
  replays/<arm>__<variant>/decisions.jsonl
  replays/<arm>__<variant>/metrics.json
  replays/<arm>__<variant>/snippets/*.wav
  report.md
```

**It is a directory rather than a database** because it has to survive being carried around:
copied off a phone, dropped in a folder, re-run a week later against a new variant, and
uploaded to object storage whole. A row in Postgres would be none of those things — and this
service has no database anyway (invariant 2).
"""

from __future__ import annotations

import json
import logging
from collections.abc import Iterator, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Final

from ..audio import (
    AudioError,
    PcmFormat,
    duration_ms,
    read_wav,
    slice_ms,
    to_mono_16k,
    write_wav,
)
from ..bargein import BargeInDecision

logger = logging.getLogger("motet.voice.capture")

RUN_METADATA: Final = "run.json"
RUN_AUDIO: Final = "audio.wav"
RUN_LABELS: Final = "labels.jsonl"
RUN_REPORT: Final = "report.md"
REPLAYS_DIR: Final = "replays"

#: How much audio to keep either side of a barge-in in its review snippet. Chosen so that a
#: gust that triggered a detector is audible *before* the trigger — a snippet that starts at
#: the decision tells you what the microphone heard next, not what set it off.
SNIPPET_PRE_ROLL_MS: Final = 2_000
SNIPPET_POST_ROLL_MS: Final = 1_000


class RunError(RuntimeError):
    """A run directory is missing or malformed."""


@dataclass(frozen=True)
class SpeechLabel:
    """Ground truth for one interval: the listener really was talking.

    Optional, and the walk instructions are built so that the *headline* recording needs
    none: a recording in which nobody speaks makes every barge-in a false positive by
    construction, which is a far more reliable label than anything a person annotates
    afterwards from memory.
    """

    start_ms: int
    end_ms: int
    kind: str = "speech"

    def overlaps(self, start_ms: int, end_ms: int, *, tolerance_ms: int = 0) -> bool:
        return start_ms <= self.end_ms + tolerance_ms and end_ms >= self.start_ms - tolerance_ms


@dataclass(frozen=True)
class WalkRun:
    """One recording, plus everything known about it."""

    path: Path
    label: str
    duration_ms: int
    #: ``silent`` — nobody spoke, so every barge-in is false. ``labelled`` — read
    #: ``labels.jsonl``. The distinction is what the metrics module branches on.
    ground_truth: str
    notes: str = ""
    labels: tuple[SpeechLabel, ...] = field(default=())
    recorded_at: str = ""

    @property
    def audio_path(self) -> Path:
        return self.path / RUN_AUDIO

    def pcm(self) -> bytes:
        fmt, pcm = read_wav(self.audio_path)
        if fmt != PcmFormat():
            raise RunError(
                f"{self.audio_path} is {fmt.describe()}, not {PcmFormat().describe()}; "
                "re-run `motet-voice ingest` on the original recording"
            )
        return pcm

    def to_json(self) -> dict[str, Any]:
        return {
            "label": self.label,
            "duration_ms": self.duration_ms,
            "ground_truth": self.ground_truth,
            "notes": self.notes,
            "recorded_at": self.recorded_at,
        }


def create_run(
    path: Path,
    pcm: bytes,
    *,
    label: str,
    ground_truth: str = "silent",
    notes: str = "",
    labels: Sequence[SpeechLabel] = (),
    recorded_at: str = "",
) -> WalkRun:
    """Write a run directory from already-converted 16 kHz mono PCM."""
    if ground_truth not in ("silent", "labelled"):
        raise RunError(f"ground_truth must be 'silent' or 'labelled', got {ground_truth!r}")
    path.mkdir(parents=True, exist_ok=True)
    write_wav(path / RUN_AUDIO, pcm)

    run = WalkRun(
        path=path,
        label=label,
        duration_ms=duration_ms(pcm),
        ground_truth=ground_truth,
        notes=notes,
        labels=tuple(labels),
        recorded_at=recorded_at,
    )
    (path / RUN_METADATA).write_text(json.dumps(run.to_json(), indent=2) + "\n", encoding="utf-8")
    if labels:
        (path / RUN_LABELS).write_text(
            "".join(
                json.dumps({"start_ms": w.start_ms, "end_ms": w.end_ms, "kind": w.kind}) + "\n"
                for w in labels
            ),
            encoding="utf-8",
        )
    return run


def load_run(path: Path) -> WalkRun:
    """Read a run directory back."""
    metadata_path = path / RUN_METADATA
    if not metadata_path.is_file():
        raise RunError(f"{path} is not a run directory ({RUN_METADATA} is missing)")
    try:
        payload = json.loads(metadata_path.read_text(encoding="utf-8"))
    except ValueError as exc:
        raise RunError(f"{metadata_path} is not valid JSON: {exc}") from exc

    labels: list[SpeechLabel] = []
    labels_path = path / RUN_LABELS
    if labels_path.is_file():
        for line_number, line in enumerate(labels_path.read_text(encoding="utf-8").splitlines(), 1):
            if not line.strip():
                continue
            try:
                entry = json.loads(line)
                labels.append(
                    SpeechLabel(
                        start_ms=int(entry["start_ms"]),
                        end_ms=int(entry["end_ms"]),
                        kind=str(entry.get("kind", "speech")),
                    )
                )
            except (ValueError, KeyError, TypeError) as exc:
                raise RunError(f"{labels_path}:{line_number} is malformed: {exc}") from exc

    return WalkRun(
        path=path,
        label=str(payload.get("label", path.name)),
        duration_ms=int(payload.get("duration_ms", 0)),
        ground_truth=str(payload.get("ground_truth", "silent")),
        notes=str(payload.get("notes", "")),
        labels=tuple(labels),
        recorded_at=str(payload.get("recorded_at", "")),
    )


def ingest_recording(
    source: Path,
    destination: Path,
    *,
    label: str,
    ground_truth: str = "silent",
    notes: str = "",
    labels: Sequence[SpeechLabel] = (),
    recorded_at: str = "",
) -> WalkRun:
    """Turn a WAV off a phone into a run directory.

    **WAV only, and deliberately so.** Decoding ``.m4a`` would mean shelling out to ffmpeg,
    which is one more thing to have installed correctly at the moment somebody has just come
    back from a walk and wants a number. Every phone can export or share a WAV, and the
    conversion to 16 kHz mono happens here so that every replay afterwards reads identical
    bytes.
    """
    if not source.is_file():
        raise RunError(f"no such recording: {source}")
    if source.suffix.lower() != ".wav":
        raise RunError(
            f"{source.name} is not a .wav — export or convert the recording to WAV first "
            "(any phone voice-memo app can share as WAV, and so can QuickTime, Audacity, "
            "or `ffmpeg -i in.m4a out.wav`)"
        )
    fmt, raw = read_wav(source)
    try:
        pcm = to_mono_16k(raw, fmt)
    except AudioError as exc:
        raise RunError(f"{source.name}: {exc}") from exc
    logger.info(
        "ingested %s (%s, %dms) into %s", source.name, fmt.describe(), duration_ms(pcm), destination
    )
    return create_run(
        destination,
        pcm,
        label=label,
        ground_truth=ground_truth,
        notes=notes,
        labels=labels,
        recorded_at=recorded_at,
    )


def replay_dir(run: WalkRun, arm: str, variant: str) -> Path:
    return run.path / REPLAYS_DIR / f"{arm}__{variant}"


def write_snippet(run: WalkRun, pcm: bytes, decision: BargeInDecision, target: Path) -> str:
    """Cut the audio around a decision so it can be listened to afterwards.

    This is the single feature that turns "the detector fired 41 times" into something
    reviewable. Forty-one twenty-second listens is a chore; forty-one three-second clips,
    each named for when it happened, is ten minutes on a sofa.
    """
    target.mkdir(parents=True, exist_ok=True)
    name = f"{decision.at_ms:08d}ms.wav"
    write_wav(
        target / name,
        slice_ms(
            pcm,
            decision.onset_ms - SNIPPET_PRE_ROLL_MS,
            decision.at_ms + SNIPPET_POST_ROLL_MS,
        ),
    )
    return name


def write_decisions(path: Path, decisions: Sequence[BargeInDecision]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "".join(json.dumps(decision.to_json()) + "\n" for decision in decisions), encoding="utf-8"
    )


def read_decisions(path: Path) -> Iterator[BargeInDecision]:
    if not path.is_file():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.strip():
            yield BargeInDecision.from_json(json.loads(line))
