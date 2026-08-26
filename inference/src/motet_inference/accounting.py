"""What a stage spent, and what it threw away — recorded rather than dropped on the floor.

Two defects with one shape: the system did the work and discarded the evidence. This module
is where both stop.

Every OpenRouter response already carried its own accounting: the adapter asks for it
(``"usage": {"include": True}``), decodes it, and attaches it to :class:`LlmResponse`.
Nothing read it. So a finished episode left no record of what it cost, and the only way to
answer "did prompt caching pay off" — named in AGENTS.md as the largest cost lever in the
system — was to read a vendor dashboard by hand and attribute spend to an episode by
timestamp. That is motet#25.

**Two shapes, because there are two questions and one number cannot answer both.**

* *"How is the fleet doing?"* is a **metric**: `motet.llm.tokens`, split by stage, model
  and kind. Low cardinality on purpose — no episode id, no source item id. A time series
  per episode is a time series per episode forever.
* *"What did **that** episode cost?"* is a **log line**, because the answer needs an id in
  it and ids are what metrics must not carry. :func:`collect_usage` is how a caller that knows
  the id gets a total to put in one: it accumulates every completion made inside the block,
  across stages, without any stage having to learn what an episode is.

The ledger is a :class:`~contextvars.ContextVar` rather than a parameter, and that is the
whole reason this is cheap: threading a cost accumulator through
:meth:`~motet_inference.interfaces.ScriptGenerator.generate` would put it in the Protocol,
which every fake would then have to implement — for a number none of them has. Outside a
:func:`collect_usage` block :func:`record_usage` still emits the metric and the line; it simply has
nothing to add to.
"""

from __future__ import annotations

import logging
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from typing import Final

from opentelemetry import metrics

from .llm import LlmResponse, LlmStage, Usage

__all__ = [
    "Ledger",
    "StageUsage",
    "classify_grounding_reason",
    "collect_usage",
    "describe_usage",
    "record_grounding",
    "record_script_drop",
    "record_tts_characters",
    "record_usage",
]

logger = logging.getLogger("motet.inference.cost")

# Created at import against OpenTelemetry's *proxy* meter, which resolves to the real one
# the moment `motet_obs.configure` installs a provider. Telemetry stays entirely optional:
# with nothing configured these are no-ops, and no code path has to ask whether obs exists.
_meter = metrics.get_meter("motet.inference")

_tokens = _meter.create_counter(
    "motet.llm.tokens",
    unit="{token}",
    description=(
        "Tokens billed by an LLM completion, by stage, model and kind. `cache_read` is "
        "the one to watch: if it stays at zero across requests that share a prefix, a "
        "cache breakpoint is misplaced and the largest cost line in the system is not "
        "materialising."
    ),
)
_requests = _meter.create_counter(
    "motet.llm.requests",
    unit="{request}",
    description="Completions made, by stage and model.",
)
_characters = _meter.create_counter(
    "motet.tts.characters",
    unit="{character}",
    description=(
        "Characters submitted for speech synthesis. Cartesia bills per character, so this "
        "is the TTS half of an episode's cost."
    ),
)

# --- what got thrown away (motet#24) ------------------------------------------------
#
# A claim can be lost at two different points, and until now the two were indistinguishable
# after the fact: the script adapter drops what it cannot locate in a source, and the
# grounding gate drops what the evidence does not support. They mean opposite things — the
# first is a script-prompt problem, the second is invariant 3 doing its job — so they are
# separate instruments rather than one counter with a label somebody might forget to split
# on.

_script_drops = _meter.create_counter(
    "motet.script.claims_dropped",
    unit="{claim}",
    description=(
        "Claims and segments discarded while parsing the script model's answer, by reason. "
        "This is the layer BEFORE grounding: a claim counted here never reached the gate."
    ),
)
_grounding_claims = _meter.create_counter(
    "motet.grounding.claims",
    unit="{claim}",
    description=(
        "Claims that reached grounding validation, by whether they survived it. The "
        "denominator for the drop rate — a count of drops alone cannot produce one."
    ),
)
_grounding_drops = _meter.create_counter(
    "motet.grounding.claims_dropped",
    unit="{claim}",
    description=(
        "Claims the grounding gate refused, bucketed by KIND of failure. The free-text "
        "reason a model gives is unbounded and belongs in a log line, not in a label that "
        "would mint a time series per sentence."
    ),
)


@dataclass(frozen=True)
class StageUsage:
    """One completion's accounting, tagged with the stage that made it."""

    stage: LlmStage
    model: str
    usage: Usage


@dataclass
class Ledger:
    """Every completion made inside a :func:`collect_usage` block."""

    entries: list[StageUsage] = field(default_factory=list)

    @property
    def requests(self) -> int:
        return len(self.entries)

    def total(self) -> Usage:
        """The sum, which is what goes in a "this episode cost" line."""
        return Usage(
            input_tokens=sum(entry.usage.input_tokens for entry in self.entries),
            output_tokens=sum(entry.usage.output_tokens for entry in self.entries),
            reasoning_tokens=sum(entry.usage.reasoning_tokens for entry in self.entries),
            cache_read_tokens=sum(entry.usage.cache_read_tokens for entry in self.entries),
            cache_write_tokens=sum(entry.usage.cache_write_tokens for entry in self.entries),
        )

    def summary(self) -> str:
        """The totals as one log-line fragment, in a stable field order."""
        return describe_usage(self.total())


_ledger: ContextVar[Ledger | None] = ContextVar("motet_llm_ledger", default=None)


@contextmanager
def collect_usage() -> Iterator[Ledger]:
    """Accumulate the usage of every completion made inside this block.

    Nested blocks are independent: the inner one shadows the outer for its duration, so a
    caller cannot accidentally double-count by wrapping a block that already wraps itself.
    """
    ledger = Ledger()
    token = _ledger.set(ledger)
    try:
        yield ledger
    finally:
        _ledger.reset(token)


def record_usage(stage: LlmStage, response: LlmResponse) -> None:
    """Count one completion: on the obs stack, in the log, and in the ledger if there is one.

    Called by the stage adapters rather than inside the OpenRouter client, because the
    *stage* is what an operator splits cost by and :class:`~.types.LlmRequest` deliberately
    does not carry one — a request knows its model, which is already decided by the time it
    exists.
    """
    usage = response.usage
    attributes = {"stage": stage.value, "model": response.model}
    _requests.add(1, attributes)
    for kind, value in (
        ("input", usage.input_tokens),
        ("output", usage.output_tokens),
        ("reasoning", usage.reasoning_tokens),
        ("cache_read", usage.cache_read_tokens),
        ("cache_write", usage.cache_write_tokens),
    ):
        if value:
            _tokens.add(value, {**attributes, "kind": kind})

    logger.info("llm %s on %s: %s", stage.value, response.model, describe_usage(usage))

    ledger = _ledger.get()
    if ledger is not None:
        ledger.entries.append(StageUsage(stage=stage, model=response.model, usage=usage))


def record_tts_characters(count: int) -> None:
    """Count characters submitted for synthesis — the other half of an episode's bill.

    Counted where the text is handed over rather than inside the Cartesia adapter, because
    the adapter sends the string it is given unchanged (``build_payload``) and the caller
    is the one that knows how many segments an episode had. No model label: the voice and
    the model id are read from the environment in ``motet_inference.cartesia`` and nowhere
    else, and a second reader of those variables is how the two quietly disagree.
    """
    if count > 0:
        _characters.add(count)


def record_script_drop(reason: str) -> None:
    """Count one claim or segment the script parser could not use.

    ``reason`` is one of a small fixed set chosen at the call site, never model text.
    """
    _script_drops.add(1, {"reason": reason})


def record_grounding(*, kept: int, dropped: int, reasons: Sequence[str]) -> None:
    """Count one episode's grounding verdicts, with the drops bucketed by kind.

    Both halves are recorded because a drop count with no denominator is not a drop
    *rate*, and the rate is the number the question was about.

    **``dropped`` is counted, not inferred from ``reasons``, and the two can legitimately
    differ.** ``_drop_ungrounded`` matches a failure on ``(news_item_id, claim_text)``
    rather than on identity — a report is not a reference — so two identical claim texts
    under one story are dropped together on one verdict. Deriving the count from the number
    of reasons would therefore understate the drop rate in exactly that case, and the rate
    is the headline number. The reason breakdown is per *verdict* and stays that way: a
    claim co-dropped with its twin has no reason of its own to attribute.
    """
    if kept:
        _grounding_claims.add(kept, {"outcome": "kept"})
    if dropped:
        _grounding_claims.add(dropped, {"outcome": "dropped"})
    for kind in reasons:
        _grounding_drops.add(1, {"reason": kind})


#: The reasons :class:`~motet_inference.adapters.ClaudeGroundingValidator` produces
#: itself, matched on prefix. Anything else came out of the model's mouth as free text.
_MECHANICAL_REASONS: Final = (
    ("span does not resolve", "span_unresolved"),
    ("grounding validation returned no verdict", "no_verdict"),
    ("grounding validation ran out of token budget", "budget_exhausted"),
)


def classify_grounding_reason(reason: str) -> str:
    """Bucket a failure reason into something a metric label can hold.

    A model's own reason is a sentence, and a sentence as a label is a new time series per
    claim — which is how a cardinality problem is built. The distinction that matters for
    the metric is only *which kind* of failure it was: a span that would not resolve is a
    script-stage bug, a missing verdict is a validator-response bug, an exhausted budget
    is a claim nobody managed to judge at all (motet#42), and an unsupported claim is the
    gate working as designed. The sentence itself survives in the log line.

    ``budget_exhausted`` is the one to watch rather than merely count: it is the only
    reason here that costs a claim without any judgement having been made, so a rate that
    stops being ~0 says the chunk size in ``adapters`` no longer fits the model.
    """
    lowered = reason.lower()
    for prefix, kind in _MECHANICAL_REASONS:
        if lowered.startswith(prefix):
            return kind
    return "unsupported"


def describe_usage(usage: Usage) -> str:
    """A stable rendering of one accounting block, for a log line.

    Every field every time, zeros included: a field that disappears when it is zero is a
    field a log query cannot aggregate, and ``cache_read=0`` is precisely the observation
    worth being able to search for.
    """
    return (
        f"input={usage.input_tokens} output={usage.output_tokens} "
        f"reasoning={usage.reasoning_tokens} cache_read={usage.cache_read_tokens} "
        f"cache_write={usage.cache_write_tokens}"
    )
