"""``motet-voice`` — the command Tadas runs before and after the walk.

Five verbs, and the whole flow fits on a phone screen:

```
motet-voice demo                          prove the harness works, with synthetic audio
motet-voice ingest walk.wav --run runs/quiet    a phone recording -> a run directory
motet-voice replay runs/quiet                   every arm x every variant, offline
motet-voice report runs/quiet                   the table, printed
motet-voice upload runs/quiet                   push the whole run to object storage
```

**Nothing here needs a network, a credential, or a laptop outdoors.** The phone records; a
laptop ingests and replays afterwards. That is the design constraint the walk imposed, and
it is why there is no ``record`` verb: a live capture would need a machine running in the
rain for the duration of the measurement.

``print`` is the product of this file, which is why it is the one module in the package
exempted from the repo's ``print`` ban — the same exemption, for the same reason, as
``bin/check-openrouter-models``.
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from pathlib import Path
from typing import Final

from .config import load_settings
from .harness import (
    RunError,
    SpeechLabel,
    create_run,
    ingest_recording,
    load_run,
    render_report,
    replay_run,
    synthesize_walk,
)
from .harness.capture import RUN_REPORT
from .harness.metrics import ScoredRun

logger = logging.getLogger("motet.voice.cli")

#: Long enough for the adaptive noise floor to settle and for a few gusts, short enough to
#: run in CI in a second or two.
DEMO_DURATION_MS: Final = 45_000
DEMO_SPEECH_AT_MS: Final = (12_000, 26_000, 38_000)


def main(argv: list[str] | None = None) -> int:
    parser = _parser()
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.WARNING, format="%(message)s")

    try:
        return int(args.handler(args))
    except (RunError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="motet-voice", description=__doc__.split("\n")[0])
    sub = parser.add_subparsers(required=True)

    demo = sub.add_parser("demo", help="synthesize a walk, replay it, print the report")
    demo.add_argument("--run", type=Path, default=Path("runs/demo"))
    demo.add_argument("--duration-ms", type=int, default=DEMO_DURATION_MS)
    demo.set_defaults(handler=_demo)

    ingest = sub.add_parser("ingest", help="turn a phone recording into a run directory")
    ingest.add_argument("recording", type=Path)
    ingest.add_argument("--run", type=Path, required=True)
    ingest.add_argument("--label", default="")
    ingest.add_argument(
        "--spoke",
        action="store_true",
        help="the recording contains speech; expects --label-window, or scores every "
        "detection as a false positive if none are given",
    )
    ingest.add_argument(
        "--label-window",
        action="append",
        default=[],
        metavar="START_MS:END_MS",
        help="ground truth for a spoken stretch; repeatable",
    )
    ingest.add_argument("--notes", default="")
    ingest.set_defaults(handler=_ingest)

    replay = sub.add_parser("replay", help="replay a run through every arm and variant")
    replay.add_argument("run", type=Path)
    replay.add_argument("--variant", action="append", default=[])
    replay.add_argument("--arm", action="append", default=[])
    replay.add_argument("--no-snippets", action="store_true")
    replay.set_defaults(handler=_replay)

    report = sub.add_parser("report", help="print the report for an already-replayed run")
    report.add_argument("run", type=Path)
    report.set_defaults(handler=_report)

    upload = sub.add_parser("upload", help="push a run directory to object storage")
    upload.add_argument("run", type=Path)
    upload.add_argument("--prefix", default="voice/barge-in")
    upload.set_defaults(handler=_upload)

    return parser


def _demo(args: argparse.Namespace) -> int:
    """Generate synthetic outdoor audio and run the whole pipeline over it.

    Its job is to answer "is the harness working?" without a microphone — so that the only
    unknown on the day of the walk is the weather.
    """
    walk = synthesize_walk(duration_ms=args.duration_ms, speech_at_ms=DEMO_SPEECH_AT_MS)
    labels = [SpeechLabel(start_ms=w.start_ms, end_ms=w.end_ms) for w in walk.speech]
    run = create_run(
        args.run,
        walk.pcm,
        label="synthetic-demo",
        ground_truth="labelled",
        notes="Synthetic wind, traffic, footsteps and voiced bursts. Not a real walk.",
        labels=labels,
    )
    scored = replay_run(run, load_settings())
    return _emit(run.path, [scored], title="Barge-in harness — synthetic demo")


def _ingest(args: argparse.Namespace) -> int:
    labels = [_parse_window(window) for window in args.label_window]
    run = ingest_recording(
        args.recording,
        args.run,
        label=args.label or args.recording.stem,
        ground_truth="labelled" if (args.spoke or labels) else "silent",
        notes=args.notes,
        labels=labels,
    )
    print(f"{run.path}: {run.duration_ms / 60_000:.1f} min, ground truth {run.ground_truth}")
    if run.ground_truth == "silent":
        print("Every barge-in in this recording will be scored as a false positive.")
    return 0


def _replay(args: argparse.Namespace) -> int:
    run = load_run(args.run)
    scored = replay_run(
        run,
        load_settings(),
        variants=args.variant,
        arms=args.arm,
        write_snippets=not args.no_snippets,
    )
    return _emit(run.path, [scored])


def _report(args: argparse.Namespace) -> int:
    run = load_run(args.run)
    report = run.path / RUN_REPORT
    if not report.is_file():
        raise RunError(f"{report} does not exist yet — run `motet-voice replay {run.path}` first")
    print(report.read_text(encoding="utf-8"))
    return 0


def _upload(args: argparse.Namespace) -> int:
    """Push a whole run directory to object storage, through the storage seam.

    Local backend by default, GCS when configured — the same ``MOTET_STORAGE_BACKEND``
    switch the audio pipeline uses. The point is that a walk's evidence outlives the laptop
    it was replayed on without this file knowing anything about a bucket.
    """
    from motet_storage import build_store  # noqa: PLC0415

    run = load_run(args.run)
    store = build_store()
    uploaded = 0
    for path in sorted(run.path.rglob("*")):
        if not path.is_file():
            continue
        key = f"{args.prefix.strip('/')}/{run.path.name}/{path.relative_to(run.path).as_posix()}"
        store.put(key, path.read_bytes(), content_type=_content_type(path))
        uploaded += 1
    print(f"uploaded {uploaded} file(s) under {args.prefix.strip('/')}/{run.path.name}/")
    return 0


def _emit(path: Path, scored: list[ScoredRun], *, title: str = "Barge-in walk") -> int:
    report = render_report(scored, title=title)
    (path / RUN_REPORT).write_text(report, encoding="utf-8")
    (path / "metrics.json").write_text(
        json.dumps([run.to_json() for run in scored], indent=2) + "\n", encoding="utf-8"
    )
    print(report)
    return 0


def _parse_window(raw: str) -> SpeechLabel:
    try:
        start, end = raw.split(":", 1)
        return SpeechLabel(start_ms=int(start), end_ms=int(end))
    except ValueError as exc:
        raise ValueError(f"--label-window must be START_MS:END_MS, got {raw!r}") from exc


def _content_type(path: Path) -> str:
    return {
        ".wav": "audio/wav",
        ".json": "application/json",
        ".jsonl": "application/x-ndjson",
        ".md": "text/markdown",
    }.get(path.suffix.lower(), "application/octet-stream")


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
