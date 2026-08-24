"""The report — written for someone who has just walked in the door with a dog.

Markdown, one table, headline metric first, and an explicit statement of what the numbers
do *not* say. The last part is not politeness: with no ``OPENAI_API_KEY``, one of the two
arms was emulated rather than measured, and a table that did not say so would be actively
misleading about the thing the whole exercise is for.
"""

from __future__ import annotations

from collections.abc import Sequence

from .metrics import ArmMetrics, ScoredRun

_EMULATED_MARK = " *(emulated)*"
_DEAF_MARK = " **⚠ heard nothing**"


def render_report(scored: Sequence[ScoredRun], *, title: str = "Barge-in walk") -> str:
    lines: list[str] = [f"# {title}", ""]

    for run in scored:
        lines.append(f"## {run.run_label}")
        lines.append("")
        metrics = sorted(run.metrics, key=lambda m: (m.false_per_minute, m.arm, m.variant))
        if not metrics:
            lines.extend(["No arms were replayed for this recording.", ""])
            continue

        first = metrics[0]
        lines.append(
            f"{first.open_mic_minutes:.1f} minutes of open mic, "
            f"ground truth `{first.ground_truth}`."
        )
        lines.append("")
        lines.append("| arm | variant | **false/min** | false | caught | missed | median latency |")
        lines.append("|---|---|---:|---:|---:|---:|---:|")
        for metric in metrics:
            lines.append(_row(metric))
        lines.append("")

        best = run.best()
        if best is not None:
            lines.append(
                f"**Fewest false barge-ins: `{best.arm}` / `{best.variant}` at "
                f"{best.false_per_minute:.2f} per minute"
                + (
                    f", catching {best.detection_rate:.0%} of real speech"
                    if best.detection_rate is not None
                    else ""
                )
                + ".**"
            )
            lines.append("")

    if any(metric.deaf for run in scored for metric in run.metrics):
        lines.extend(
            [
                "## Rows marked ⚠",
                "",
                "That configuration produced **no decisions at all** on a recording that had",
                "speech in it. Its zero false-positives-per-minute is not a result — it is a",
                "detector that is not detecting. It is excluded from the winner above, and it",
                "wants investigating rather than celebrating.",
                "",
            ]
        )

    if any(metric.emulated for run in scored for metric in run.metrics):
        lines.extend(_emulation_caveat())

    lines.extend(_how_to_read())
    return "\n".join(lines).rstrip() + "\n"


def _row(metric: ArmMetrics) -> str:
    latency = "—" if metric.median_latency_ms is None else f"{metric.median_latency_ms} ms"
    caught = "—" if metric.detection_rate is None else f"{metric.detection_rate:.0%}"
    missed = "—" if metric.ground_truth == "silent" else str(metric.missed)
    mark = _EMULATED_MARK if metric.emulated else ""
    variant = f"`{metric.variant}`" + (_DEAF_MARK if metric.deaf else "")
    return (
        f"| `{metric.arm}`{mark} | {variant} | **{metric.false_per_minute:.2f}** | "
        f"{metric.false_positives} | {caught} | {missed} | {latency} |"
    )


def _emulation_caveat() -> list[str]:
    return [
        "## What these numbers do not say",
        "",
        "Rows marked *(emulated)* were **not measured against the vendor.** `OPENAI_API_KEY`",
        "is not provisioned, so that arm's server-side turn detection could not run; the",
        "harness substituted a local emulation of its *documented* parameters. Those rows are",
        "useful for comparing dials — threshold, prefix padding, silence duration — and are",
        "worthless as a claim about how that provider behaves in wind. Set the key and re-run",
        "the same recording to turn them into a measurement; the recording does not change,",
        "so the comparison stays valid.",
        "",
    ]


def _how_to_read() -> list[str]:
    return [
        "## How to read this",
        "",
        "- **false/min is the number that decides it.** Below ~0.1 is comfortable: roughly one",
        "  spurious interruption per ten minutes of walking. Above ~0.5 the product is",
        "  unusable outdoors and the answer is push-to-talk, not a better threshold.",
        "- **A zero with a dash in `caught` is not a win.** It means nothing fired, on a",
        "  recording where nothing was supposed to. Check it against the second, spoken",
        "  recording before believing it.",
        "- **Listen to the snippets before trusting a row.** Each decision wrote a short WAV",
        "  under `replays/<arm>__<variant>/snippets/`, named for when it happened, with two",
        "  seconds of lead-in. Ten minutes with those clips is worth more than any table.",
        "- `decisions.jsonl` beside them has the full evidence per decision: the VAD",
        "  probability, the adaptive noise floor, the SNR and the zero-crossing rate.",
        "",
    ]
