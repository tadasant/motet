"""Vendor adapters — the real implementations of each stage.

**Nothing here may be imported into a test.** The registry refuses to hand these out
unless ``MOTET_INFERENCE_MODE=real`` is set explicitly, which never happens in CI.

Claude covers dedup/integrate, script generation, and grounding; Cartesia Sonic covers
TTS. Credentials arrive from the environment, resolved by infrastructure that lives in
the private repo — never read a key from a file in this tree.

The three stages here reach their model through ``motet_inference.llm``: ``build_client()``
plus ``build_request(cls.stage, ...)`` hands each class the model and thinking depth
configured for *its* stage, so filling one in never involves picking a model. The prompts
and the response schemas live in ``motet_inference.prompts``; what is left here is wiring.

Each class holds **one** client for its lifetime, because ``Stages`` is built once per
process. A client per call would leak connection pools and throw away the sticky upstream
routing that keeps prompt-cache hit rates up on the dedup loop.
"""

from __future__ import annotations

import logging
import secrets
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any, ClassVar

from .accounting import record_budget_exhausted, record_script_drop, record_usage
from .cartesia import CartesiaSpeechSynthesizer
from .interfaces import IntegrationResult
from .llm import LlmBudgetExhaustedError, LlmClient, LlmStage, build_client, build_request
from .prompts import (
    GROUNDING_SCHEMA,
    INTEGRATE_SCHEMA,
    SCRIPT_SCHEMA,
    grounding_messages,
    integrate_messages,
    locate_quote,
    parse_json_object,
    require_str,
    script_messages,
)
from .types import (
    Claim,
    GroundingFailure,
    GroundingReport,
    NewsItem,
    Script,
    ScriptSegment,
    SourceItem,
    SourceSpan,
)

logger = logging.getLogger("motet.inference")


def _proposed_news_item_id() -> str:
    """An id for a story the integrator says is new.

    A *proposal*: the persistence layer assigns the real primary key when it inserts the
    row. It exists because ``IntegrationResult`` needs a whole ``NewsItem``, and because
    the golden set — which runs the fakes through the same type — needs ids to compare.
    """
    return f"ni_{secrets.token_hex(6)}"


#: Dedup answers with a title, a summary, and a decision. It is the volume stage, so the
#: ceiling is set to what the answer needs rather than to what the model allows.
INTEGRATE_MAX_TOKENS = 2_000

#: A script for a duration-capped episode. Generous, because truncation here costs a
#: whole episode's worth of upstream work.
SCRIPT_MAX_TOKENS = 32_000

#: How many claims one grounding call judges at once.
#:
#: **The bound is on the work, not on the budget, and that is the fix for motet#42.** A
#: single call carrying every claim in an episode needs an output ceiling that grows with
#: the backlog, so any constant is a backlog size beyond which the stage cannot complete
#: -- and 8k was reached at 19 news items, which is a normal morning. Chunking bounds the
#: request instead: the number of calls grows with the episode and the size of each one
#: does not. Verdicts are independent per claim, so nothing is lost by splitting them.
#:
#: **Eight, and then four: motet#52.** Carrying fewer claims is how a claim is bought more
#: room to be thought about, because the alternative is lifting the ceiling. At eight
#: claims a 14,000-token ceiling is 1,750 a claim, and reaching the ~4,000 a claim the
#: staging exhaustions imply would have meant a 32,000-token call; at four it costs 16,000.
#: Same headroom per claim, and a per-call bound that moves 14,000 -> 16,000 rather than
#: doubling. The cost of the trade is that the flat term below is paid twice as often.
GROUNDING_CLAIMS_PER_CALL = 4

#: A second bound on the same call, because claims are not the same size. Evidence spans
#: are whole sentences out of a newsletter, so four long ones are a much bigger ask than
#: four short ones, and the reasoning that has to chew through them scales with the text
#: rather than with the count. Halved alongside the claim bound above, so that the
#: characters a chunk may carry *per claim* are unchanged.
GROUNDING_CHARS_PER_CALL = 6_000

#: Per claim: one verdict — an index, a boolean and one short sentence — plus the thinking
#: that produces it.
#:
#: **motet#52 is the second observation that :func:`grounding_max_tokens` was waiting for**,
#: and it moves the weight of the ceiling onto this term. Staging gives three anchors: eight
#: claims exhausted a 14,000 ceiling, four exhausted 10,000, and the cascades stopped at two
#: under 8,000 with no claim dropped for budget. Written as ``demand(n) = F + c*n`` those say
#: ``F + 2c <= 8000`` and ``F + 4c > 10000``, hence ``c > 1000`` — the old value was exactly
#: the boundary it had to be above — and ``F <= 8000 - 2c``. Across that whole region
#: ``demand(4)`` peaks at 16,000, which is what ``grounding_max_tokens(4)`` now allows: not a
#: guess above the observations but the largest demand consistent with all of them.
GROUNDING_TOKENS_PER_CLAIM = 2_750

#: The part of the budget that is not per claim: reading the instructions and settling into
#: the task. Flat because it does not repeat per claim — unlike the term above, which is
#: what the first draft of the motet#42 fix got wrong by making the whole allowance flat.
#:
#: It is the smaller half of the ceiling now, and that is the point. A flat term is the part
#: halving cannot reduce, so a ceiling dominated by it is one where splitting a chunk removes
#: budget faster than it removes work — which is precisely the 8 -> 4 -> 2 cascade motet#52
#: reported, where halving the claims did not stop the halves exhausting too.
GROUNDING_REASONING_HEADROOM = 5_000

#: What a claim's failure says when the validator could not get a verdict out of the model
#: within its budget. Prefix-matched by ``accounting.classify_grounding_reason``, so keep
#: the two in step.
GROUNDING_BUDGET_REASON = "grounding validation ran out of token budget for this claim"


def grounding_max_tokens(claims: int) -> int:
    """The output ceiling for a grounding call judging ``claims`` claims.

    A function rather than a constant because the work is a function of the claim count and
    a constant is not. Both terms were estimates against a single truncated observation
    when motet#42 wrote them; motet#52 is the second observation, and the constants above
    say what it moved. They are still estimates, which is why the halving in
    :meth:`ClaudeGroundingValidator._judge` stays: it is what makes a wrong estimate cost a
    retry instead of an episode. What is new is that a wrong estimate is now paid *once* an
    episode rather than once a chunk — see the narrowing in
    :meth:`ClaudeGroundingValidator.validate`.
    """
    return GROUNDING_REASONING_HEADROOM + GROUNDING_TOKENS_PER_CLAIM * claims


class ClaudeIntegrator:
    """Dedup/integrate against the in-prompt window of news items.

    One call per source item, with the whole window in the prompt — a day of news is
    roughly 4.5k tokens, which is why there is no vector store (see the AGENTS.md
    tripwires). The window is the stable cache prefix and the source item is not, which is
    why ``integrate_messages`` puts the breakpoint between them.
    """

    stage: ClassVar[LlmStage] = LlmStage.DEDUP

    def __init__(self, client: LlmClient | None = None) -> None:
        self._client = client if client is not None else build_client()

    def integrate(self, item: SourceItem, window: Sequence[NewsItem]) -> IntegrationResult:
        response = self._client.complete(
            build_request(
                self.stage,
                integrate_messages(item, window),
                max_output_tokens=INTEGRATE_MAX_TOKENS,
                response_format=INTEGRATE_SCHEMA,
            )
        )
        # Dedup is the volume stage and the one that passes the whole window in-prompt, so
        # it is where a missed cache breakpoint costs the most and shows up the soonest.
        record_usage(self.stage, response)
        data = parse_json_object(response, what="dedup/integrate")
        decision = require_str(data, "decision", what="dedup/integrate")
        title = require_str(data, "title", what="dedup/integrate").strip()
        summary = require_str(data, "summary", what="dedup/integrate").strip()

        if decision == "merge":
            target_id = data.get("news_item_id")
            existing = next((n for n in window if n.id == target_id), None)
            if existing is not None:
                return IntegrationResult(
                    news_item=NewsItem(
                        id=existing.id,
                        title=title or existing.title,
                        summary=summary or existing.summary,
                        source_item_ids=(*existing.source_item_ids, item.id),
                    ),
                    merged=True,
                )
            # Merging into a story that is not in the window is a model error. Degrade to
            # "new" rather than raising: the cost of under-merging is one duplicate story
            # in a briefing, and the cost of raising is that ingestion stops entirely.
            logger.warning(
                "dedup asked to merge source %s into unknown news item %r; treating as new",
                item.id,
                target_id,
            )

        return IntegrationResult(
            news_item=NewsItem(
                # A proposal, not an identity. The persistence layer assigns the real id
                # when it inserts the row; only the `merged=True` branch above returns an
                # id that already means something.
                id=_proposed_news_item_id(),
                title=title or item.title,
                summary=summary or item.title,
                source_item_ids=(item.id,),
            ),
            merged=False,
        )


class ClaudeScriptGenerator:
    """Generate briefing copy in which every claim cites a span.

    The model returns a *quote* rather than a character offset, and this class locates the
    quote to derive the span — models copy text reliably and count characters unreliably.
    A claim whose quote cannot be found verbatim is **dropped**, which is what keeps a
    fabricated quotation from becoming a grounded-looking claim. See ``prompts`` for the
    full reasoning.
    """

    stage: ClassVar[LlmStage] = LlmStage.SCRIPT

    def __init__(self, client: LlmClient | None = None) -> None:
        self._client = client if client is not None else build_client()

    def generate(self, news_items: Sequence[NewsItem], sources: Mapping[str, SourceItem]) -> Script:
        if not news_items:
            return Script(segments=())
        response = self._client.complete(
            build_request(
                self.stage,
                script_messages(news_items, sources),
                max_output_tokens=SCRIPT_MAX_TOKENS,
                response_format=SCRIPT_SCHEMA,
            )
        )
        record_usage(self.stage, response)
        data = parse_json_object(response, what="script generation")
        known = {item.id: item for item in news_items}

        segments: list[ScriptSegment] = []
        spoken_for: set[str] = set()
        for raw_segment in _list_of_objects(data.get("segments"), what="script segments"):
            news_item_id = require_str(raw_segment, "news_item_id", what="script segment")
            news_item = known.get(news_item_id)
            if news_item is None:
                logger.warning("script names unknown news item %r; dropping segment", news_item_id)
                record_script_drop("unknown_news_item")
                continue
            if news_item_id in spoken_for:
                # An episode holds at most one segment per news item — the database says so
                # with a UNIQUE constraint. Dropping the repeat here turns a model quirk
                # into a story told once, rather than into a unique violation that fails the
                # whole episode five times over before anyone sees it.
                logger.warning(
                    "script returned a second segment for %s; keeping only the first",
                    news_item_id,
                )
                record_script_drop("duplicate_segment")
                continue
            claims = self._claims_for(raw_segment, news_item, sources)
            if not claims:
                logger.warning(
                    "every claim in the segment for %s was unlocatable; dropping segment",
                    news_item_id,
                )
                record_script_drop("empty_segment")
                continue
            spoken_for.add(news_item_id)
            segments.append(ScriptSegment(news_item_id=news_item_id, claims=claims))
        return Script(segments=tuple(segments))

    def _claims_for(
        self,
        raw_segment: Mapping[str, Any],
        news_item: NewsItem,
        sources: Mapping[str, SourceItem],
    ) -> tuple[Claim, ...]:
        claims: list[Claim] = []
        for raw_claim in _list_of_objects(raw_segment.get("claims"), what="script claims"):
            text = require_str(raw_claim, "text", what="script claim").strip()
            quote = require_str(raw_claim, "quote", what="script claim")
            source_item_id = require_str(raw_claim, "source_item_id", what="script claim")
            if source_item_id not in news_item.source_item_ids:
                logger.warning(
                    "claim on %s cites %r, which is not one of its sources; dropping claim",
                    news_item.id,
                    source_item_id,
                )
                record_script_drop("foreign_source")
                continue
            source = sources.get(source_item_id)
            if source is None:
                logger.warning("claim cites unavailable source %r; dropping claim", source_item_id)
                record_script_drop("unavailable_source")
                continue
            span = locate_quote(source.text, quote)
            if span is None:
                logger.warning(
                    "claim on %s quotes text not found in source %s; dropping claim",
                    news_item.id,
                    source_item_id,
                )
                record_script_drop("quote_not_found")
                continue
            if not text:
                record_script_drop("empty_text")
                continue
            claims.append(
                Claim(
                    text=text,
                    span=SourceSpan(source_item_id=source_item_id, start=span[0], end=span[1]),
                )
            )
        return tuple(claims)


@dataclass(frozen=True)
class _Judgeable:
    """One claim that survived the mechanical check, with the evidence it cites."""

    news_item_id: str
    claim_text: str
    evidence: str


@dataclass(frozen=True)
class _Judgement:
    """What one chunk found, and what its size cost to discover.

    ``answered`` is the largest chunk size anything in this subtree actually got verdicts
    for; ``cascaded`` says whether a call carrying *more than one* claim ran out of budget.
    Together they are the whole of what an episode can learn about its own chunk size that
    the constants did not already know — a size that worked, and the fact that a bigger one
    did not.

    **A single claim running out sets neither**, and that asymmetry is deliberate: there is
    no smaller chunk to retreat to, so it is evidence about that *claim* rather than about
    how many claims fit in a call. Narrowing on it would let one pathological claim put the
    rest of the episode on one call per claim, which is the most expensive shape there is.
    """

    failures: list[GroundingFailure]
    answered: int | None
    cascaded: bool


def _next_chunk(items: Sequence[_Judgeable], start: int, limit: int) -> int:
    """One past the last claim of the chunk beginning at ``start``, bounded by count and size.

    Order is preserved and never re-sorted: a chunk that follows the script's own order
    keeps claims from one story together, which is the arrangement a reader of the log
    lines expects and costs nothing to maintain.

    Taken one chunk at a time rather than all at once because ``limit`` can narrow partway
    through an episode — see :meth:`ClaudeGroundingValidator.validate`. A claim whose
    evidence is on its own larger than the whole character budget still goes, alone: the
    bound is a bound on chunks, not a promise about any single claim.
    """
    end = start
    size = 0
    while end < len(items):
        cost = len(items[end].claim_text) + len(items[end].evidence)
        if end > start and (end - start >= limit or size + cost > GROUNDING_CHARS_PER_CALL):
            break
        size += cost
        end += 1
    return end


class ClaudeGroundingValidator:
    """Judge whether each claim is supported by the span it cites, paraphrase included.

    **Invariant 3.** This runs before synthesis, never after, and a claim whose evidence
    does not support it is not spoken.

    Two checks, in order. First a mechanical one — does the span resolve to real text at
    all — which needs no model and catches a corrupted or stale span. Then a model call
    per **chunk** of surviving claims, asking whether the evidence actually supports what
    would be said.

    **Chunked rather than batched, and that is motet#42.** Batching every claim into one
    call was deliberate once — verdicts are independent, so isolating them buys nothing,
    and the most expensive stage in the pipeline should not be multiplied by the length of
    an episode. What that reasoning missed is that the *output* it needs grows with the
    episode while its ceiling does not: at 19 news items the model spent all 8,000 tokens
    of a fixed budget thinking and emitted no verdict at all, deterministically, on every
    retry. A bounded chunk is the only shape where the stage's headroom is a property of
    the call rather than of the backlog. The cost argument survives it: chunk size is what
    trades calls against risk, not one call against many.

    **A chunk that still exhausts its budget is halved, and a single claim that exhausts
    it fails closed.** The alternative is what motet#42 actually did — lose the whole
    episode over one call — and that is strictly worse than losing the claims involved:
    ``handle_script`` drops ungrounded claims and ships the rest, so a failure here costs
    the sentences nobody could check and nothing else. It is never an *approval*: an
    unchecked claim is a failure, which is the same rule a missing verdict already
    followed.

    **What the halving costs, and why it is now paid once: motet#52.** Discovering that a
    chunk was too big means spending its whole output budget and getting nothing back, and
    the constants above were doing that on every chunk of a full backlog — fifteen times
    on one staging episode, ~180k output tokens produced and discarded. Halving that chunk
    is a local decision that forgets what it learned the moment the chunk is done, so the
    next chunk pays the same probe. So :meth:`validate` carries it forward: a chunk that
    ran out narrows the size used for *every remaining chunk of this episode*, to the
    largest size this episode has actually seen answered. That turns a per-chunk cost into
    a per-episode one. The constants are still what decide whether the probe happens at
    all; the narrowing is what bounds it when they are wrong.

    **Per episode, deliberately, rather than per process.** One pathological chunk should
    not make every later episode chunk small, and a limit that lived on the adapter would
    ratchet down and never recover — the constants are the estimate, and a single call is
    not enough evidence to overwrite them permanently.
    """

    stage: ClassVar[LlmStage] = LlmStage.GROUNDING

    def __init__(self, client: LlmClient | None = None) -> None:
        self._client = client if client is not None else build_client()

    def validate(self, script: Script, sources: Mapping[str, SourceItem]) -> GroundingReport:
        failures: list[GroundingFailure] = []
        judgeable: list[_Judgeable] = []

        for segment in script.segments:
            for claim in segment.claims:
                resolved = claim.span.resolve(dict(sources))
                if resolved is None:
                    failures.append(
                        GroundingFailure(
                            news_item_id=segment.news_item_id,
                            claim_text=claim.text,
                            reason="span does not resolve to any source text",
                        )
                    )
                    continue
                judgeable.append(
                    _Judgeable(
                        news_item_id=segment.news_item_id,
                        claim_text=claim.text,
                        evidence=resolved,
                    )
                )

        limit = GROUNDING_CLAIMS_PER_CALL
        start = 0
        while start < len(judgeable):
            end = _next_chunk(judgeable, start, limit)
            judged = self._judge(judgeable[start:end])
            failures.extend(judged.failures)
            start = end
            if not judged.cascaded:
                continue
            # The largest size this episode has actually seen answered -- and one, when it
            # answered nothing at any size, because that is the floor the halving stops at
            # anyway.
            narrowed = judged.answered if judged.answered is not None else 1
            if narrowed < limit:
                logger.warning(
                    "grounding narrowing chunks from %d claims to %d after a bigger call "
                    "ran out of budget; %d claims of this episode remain",
                    limit,
                    narrowed,
                    len(judgeable) - start,
                )
                limit = narrowed
        return GroundingReport(failures=tuple(failures))

    def _judge(self, chunk: Sequence[_Judgeable]) -> _Judgement:
        """Judge one chunk, halving it if the model cannot answer within its budget.

        The :class:`_Judgement` carries what :meth:`validate` narrows on as well as the
        failures — see that class for why a lone claim running out is not part of it.
        """
        try:
            verdicts = self._ask(chunk)
        except LlmBudgetExhaustedError as exc:
            # Billed and useless is still billed, and this is the most expensive call
            # shape in the system. Counting it here is also what makes an under-sized
            # chunk visible while it is still only costing money.
            record_budget_exhausted(self.stage, exc)
            if len(chunk) == 1:
                logger.warning(
                    "grounding could not judge a claim on %s within its budget; "
                    "dropping the claim: %s",
                    chunk[0].news_item_id,
                    exc,
                )
                return _Judgement(
                    failures=[
                        GroundingFailure(
                            news_item_id=chunk[0].news_item_id,
                            claim_text=chunk[0].claim_text,
                            reason=GROUNDING_BUDGET_REASON,
                        )
                    ],
                    answered=None,
                    cascaded=False,
                )
            middle = len(chunk) // 2
            logger.warning(
                "grounding ran out of budget on %d claims; splitting into %d and %d: %s",
                len(chunk),
                middle,
                len(chunk) - middle,
                exc,
            )
            left = self._judge(chunk[:middle])
            right = self._judge(chunk[middle:])
            answered = [size for size in (left.answered, right.answered) if size is not None]
            return _Judgement(
                failures=left.failures + right.failures,
                answered=max(answered) if answered else None,
                cascaded=True,
            )

        failures: list[GroundingFailure] = []
        for index, item in enumerate(chunk):
            verdict = verdicts.get(index)
            if verdict is None:
                # Fail closed. A claim the validator did not answer for is a claim nobody
                # checked, and "unchecked" must never be spoken as if it were "supported".
                failures.append(
                    GroundingFailure(
                        news_item_id=item.news_item_id,
                        claim_text=item.claim_text,
                        reason="grounding validation returned no verdict for this claim",
                    )
                )
                continue
            supported, reason = verdict
            if not supported:
                failures.append(
                    GroundingFailure(
                        news_item_id=item.news_item_id,
                        claim_text=item.claim_text,
                        reason=reason or "the cited span does not support this claim",
                    )
                )
        return _Judgement(failures=failures, answered=len(chunk), cascaded=False)

    def _ask(self, chunk: Sequence[_Judgeable]) -> dict[int, tuple[bool, str]]:
        """One call, indexed from zero *within this chunk*.

        Local indices rather than the claim's position in the episode: a model that
        renumbered a list starting at CLAIM 17 would file its verdicts against the wrong
        claims, and an index that is also a position in the chunk cannot be mis-mapped.
        """
        response = self._client.complete(
            build_request(
                self.stage,
                grounding_messages(
                    [(index, item.claim_text, item.evidence) for index, item in enumerate(chunk)]
                ),
                max_output_tokens=grounding_max_tokens(len(chunk)),
                response_format=GROUNDING_SCHEMA,
            )
        )
        record_usage(self.stage, response)
        data = parse_json_object(response, what="grounding validation")
        verdicts: dict[int, tuple[bool, str]] = {}
        for raw in _list_of_objects(data.get("verdicts"), what="grounding verdicts"):
            raw_index = raw.get("index")
            supported = raw.get("supported")
            # `isinstance(True, int)` is True in Python, and a verdict indexed by `True`
            # would silently land on claim 1. Both checks are load-bearing.
            if not isinstance(raw_index, int) or isinstance(raw_index, bool):
                continue
            if not isinstance(supported, bool):
                continue
            reason = raw.get("reason")
            verdicts[raw_index] = (supported, reason if isinstance(reason, str) else "")
        return verdicts


def _list_of_objects(value: object, *, what: str) -> list[Mapping[str, Any]]:
    if value is None:
        return []
    if not isinstance(value, list):
        logger.warning("%s: expected a list, got %s", what, type(value).__name__)
        return []
    return [entry for entry in value if isinstance(entry, dict)]


#: Re-exported so that ``registry.real_stages`` has one import site for "the real
#: implementations", regardless of which module each of them lives in.
__all__ = [
    "CartesiaSpeechSynthesizer",
    "ClaudeGroundingValidator",
    "ClaudeIntegrator",
    "ClaudeScriptGenerator",
]
