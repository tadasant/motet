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

**The id is not always an episode's.** The voice service's conversational turn is a
:class:`~.llm.LlmStage` like the other three (motet#6), and it is billed the same way — but
the thing an operator attributes its cost to is a *session*, and a session is a socket's
lifetime spanning many tasks rather than one call inside one. So the voice service opens a
block per **turn** — a `ContextVar` holds for a ``with`` in one task, and a turn is also the
unit a person waits on — and sums the turns onto the session, whose totals go in the line
logged on close. Same mechanism, one scope smaller; ``voice/src/motet_voice/session.py`` is
where it lives, named as a path rather than as a cross-reference because ``motet-inference``
knows nothing about ``motet-voice`` and must not start.

**A third caller wants each completion as a row rather than as a total**, which is the
worker writing ``llm_usage`` (motet#92): per-user spend is the one number neither shape
above can produce, because the metric must not carry an id and a log line cannot be summed
from a route. :func:`usage_sink` is that hook, beside :func:`collect_usage` and independent
of it. The voice service installs none — it has no database (invariant 2) — so its turns
stay a metric and a log line.

The ledger is a :class:`~contextvars.ContextVar` rather than a parameter, and that is the
whole reason this is cheap: threading a cost accumulator through
:meth:`~motet_inference.interfaces.ScriptGenerator.generate` would put it in the Protocol,
which every fake would then have to implement — for a number none of them has. Outside a
:func:`collect_usage` block :func:`record_usage` still emits the metric and the line; it simply has
nothing to add to.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field

from opentelemetry import metrics

from .llm import CacheTtl, LlmBudgetExhaustedError, LlmResponse, LlmStage, Usage

__all__ = [
    "Ledger",
    "StageUsage",
    "UsageSink",
    "collect_usage",
    "describe_usage",
    "record_budget_exhausted",
    "record_dedup_decision",
    "record_script_drop",
    "record_tts_characters",
    "record_usage",
    "usage_sink",
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
_budget_exhausted = _meter.create_counter(
    "motet.llm.budget_exhausted",
    unit="{request}",
    description=(
        "Completions that hit max_output_tokens before finishing an answer, by stage and "
        "model. Billed and useless is still billed, and a rate that stops being ~0 says a "
        "stage's ceiling no longer fits the work it is being asked to do."
    ),
)
_dedup_decisions = _meter.create_counter(
    "motet.dedup.decisions",
    unit="{decision}",
    description=(
        "Dedup decisions, by the relation the first pass reported and what the stage "
        "finally did with it. This is the instrument motet#41 was missing: a false merge "
        "and a false split both look like a working pipeline from outside, and without a "
        "count of the `related` band nobody can tell whether the second look is firing "
        "often, never, or on everything."
    ),
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
# A claim the script stage wrote can still fail to reach an episode: the adapter discards
# what it cannot locate verbatim in a source. That is a *script-prompt* problem, and the
# counter is what makes it visible — a claim silently dropped while parsing an answer
# leaves no row anywhere, and the model writes a different script on every run.

_script_drops = _meter.create_counter(
    "motet.script.claims_dropped",
    unit="{claim}",
    description=(
        "Claims and segments discarded while parsing the script model's answer, by reason. "
        "A claim counted here never reached an episode."
    ),
)


@dataclass(frozen=True)
class StageUsage:
    """One completion's accounting, tagged with the stage that made it."""

    stage: LlmStage
    model: str
    usage: Usage
    #: Which rate ``usage.cache_write_tokens`` was billed at — see
    #: :attr:`~motet_inference.llm.LlmRequest.cache_ttl`.
    cache_ttl: CacheTtl | None = None


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


#: Something that wants every completion as it is recorded, one call each.
UsageSink = Callable[[StageUsage], None]

_sink: ContextVar[UsageSink | None] = ContextVar("motet_llm_usage_sink", default=None)


@contextmanager
def usage_sink(sink: UsageSink) -> Iterator[None]:
    """Hand every completion recorded inside the block to ``sink``, one call per completion.

    **The sink is called last, after the metric and the log line, and it may not raise.**
    Last so that a sink with a bug cannot cost the two signals that already existed; and an
    exception out of it is caught and logged here rather than propagated, because it would
    otherwise surface inside a stage adapter as if the *completion* had failed — and a
    stage that retried on that would bill the call a second time to record it once.
    """
    token = _sink.set(sink)
    try:
        yield
    finally:
        _sink.reset(token)


def record_usage(stage: LlmStage, response: LlmResponse) -> None:
    """Count one completion: on the obs stack, in the log, and in the ledger if there is one.

    Called by the stage adapters rather than inside the OpenRouter client, because the
    *stage* is what an operator splits cost by and :class:`~.types.LlmRequest` deliberately
    does not carry one — a request knows its model, which is already decided by the time it
    exists.
    """
    _record(stage, response.model, response.usage, response.cache_ttl)


def record_budget_exhausted(stage: LlmStage, error: LlmBudgetExhaustedError) -> None:
    """Count a completion that was billed and produced nothing usable.

    **The bill does not care that the answer was unusable.** A call that spends its whole
    ceiling on reasoning is the most expensive kind there is, so leaving it out of
    ``motet.llm.tokens`` would mean the costliest completions in the system are the ones no
    metric ever sees — motet#24's defect, on the one path where it costs the most.

    ``motet.llm.budget_exhausted`` is the separate question: *how often does a call not fit
    its ceiling?* A rate that stops being ~0 says a stage's ceiling no longer fits the work
    the model is being asked to do — and it says so while the only cost is money, which is
    the warning that arrives before a stage starts failing outright.
    """
    model = error.model or "unknown"
    _budget_exhausted.add(1, {"stage": stage.value, "model": model})
    logger.warning(
        "llm %s on %s spent its budget without producing an answer: %s", stage.value, model, error
    )
    if error.usage is not None:
        _record(stage, model, error.usage, error.cache_ttl)


def _record(stage: LlmStage, model: str, usage: Usage, cache_ttl: CacheTtl | None) -> None:
    attributes = {"stage": stage.value, "model": model}
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

    logger.info("llm %s on %s: %s", stage.value, model, describe_usage(usage))

    entry = StageUsage(stage=stage, model=model, usage=usage, cache_ttl=cache_ttl)
    ledger = _ledger.get()
    if ledger is not None:
        ledger.entries.append(entry)
    sink = _sink.get()
    if sink is not None:
        try:
            sink(entry)
        except Exception:  # noqa: BLE001 — recording a bill must not look like a failed call
            logger.exception("a usage sink raised for %s on %s; the entry is dropped", stage, model)


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


def record_dedup_decision(*, relation: str, outcome: str) -> None:
    """One dedup answer, by what the first pass said and what the stage did about it.

    Both labels are drawn from small fixed sets chosen at the call site — the three
    ``relation`` values the schema allows, and ``merged``/``new`` — so this stays a handful
    of series rather than one per story. The model's free-text ``reason`` never comes near
    a label; it goes in the log line beside it, which is where an unbounded sentence
    belongs.

    **The pair is the point, not either half.** ``related`` split by outcome is the only
    view that says whether the second look is doing anything: all ``new`` means it is
    agreeing with the first pass every time and is pure cost, all ``merged`` means the
    first pass has stopped committing to ``same_event`` at all, and the ``related`` rate
    itself is the extra spend this design buys the accuracy with.
    """
    _dedup_decisions.add(1, {"relation": relation, "outcome": outcome})


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
