"""Vendor adapters — the real implementations of each stage.

**Nothing here may be imported into a test.** The registry refuses to hand these out
unless ``MOTET_INFERENCE_MODE=real`` is set explicitly, which never happens in CI.

Claude covers dedup/integrate and script generation; Cartesia Sonic covers TTS.
Credentials arrive from the environment, resolved by infrastructure that lives in
the private repo — never read a key from a file in this tree.

The text stages here reach their model through ``motet_inference.llm``: ``build_client()``
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
from typing import Any, ClassVar

from .accounting import (
    record_budget_exhausted,
    record_dedup_decision,
    record_script_drop,
    record_usage,
)
from .cartesia import CartesiaSpeechSynthesizer
from .interfaces import IntegrationResult
from .llm import (
    LlmBudgetExhaustedError,
    LlmClient,
    LlmStage,
    LlmTransportError,
    build_client,
    build_request,
)
from .prompts import (
    INTEGRATE_SCHEMA,
    RELATED,
    SAME_EVENT,
    SCRIPT_SCHEMA,
    SECOND_LOOK_SCHEMA,
    UNRELATED,
    PromptResponseError,
    integrate_messages,
    locate_quote,
    parse_json_object,
    require_bool,
    require_str,
    script_messages,
    second_look_messages,
)
from .types import (
    Claim,
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


#: Dedup answers with a candidate id, a relation, a one-sentence reason, a title and a
#: summary. It is the volume stage, so the ceiling is set to what the answer needs rather
#: than to what the model allows.
#:
#: Unchanged by motet#41's second half, and rechecked rather than assumed: the answer grew
#: by an id, an enum and one sentence — tens of tokens against a ceiling that is already an
#: order of magnitude above the prose. Worth rechecking at all because a first-pass
#: ``LlmBudgetExhaustedError`` is not caught: the runner fails such a job permanently, and
#: the pasted item would never reach the backlog.
INTEGRATE_MAX_TOKENS = 2_000

#: The second look's ceiling. Its answer is a boolean and a sentence; almost all of this is
#: room to think about one pair at ``medium`` effort.
#:
#: **A constant is legitimate here**, and the distinction is worth keeping: a constant
#: ceiling is only safe on a call whose required output does not grow with the backlog.
#: This call always judges exactly one pair against one summary, so its work does not grow
#: with anything — and if it exhausts anyway, :meth:`ClaudeIntegrator._is_same_event`
#: treats that as "not the same event", which costs a duplicate story rather than a
#: stalled episode.
CONFIRM_MAX_TOKENS = 4_000

#: A script for a duration-capped episode. Generous, because truncation here costs a
#: whole episode's worth of upstream work.
SCRIPT_MAX_TOKENS = 32_000


class ClaudeIntegrator:
    """Dedup/integrate against the in-prompt window of news items.

    One call per source item, with the whole window in the prompt — a day of news is
    roughly 4.5k tokens, which is why there is no vector store (see the AGENTS.md
    tripwires). The window is the stable cache prefix and the source item is not, which is
    why ``integrate_messages`` puts the breakpoint between them.

    **Two passes, and the second one is motet#41's other half.** The first pass answers a
    three-way ``relation`` against the single window item it says is closest, rather than a
    yes/no over the whole backlog. ``same_event`` merges, ``unrelated`` does not, and
    ``related`` — the band where the first pass says it is unsure — buys one focused
    pairwise re-ask at :attr:`confirm_stage`'s depth before the story is allowed to become
    a second news item.

    **This does not move a threshold, and that is the design.** Moving one would trade
    false merges for false splits, and both of those fail silently. What changes is that
    the uncertain band gets looked at *again*, and a merge still needs an affirmative
    judgement to happen: a second look that says "no" leaves the story separate, exactly as
    before. The cost is bounded at one extra completion per source item, only in the band,
    and :func:`record_dedup_decision` is what makes the band's size and the second look's
    hit rate visible instead of a thing to guess at.
    """

    stage: ClassVar[LlmStage] = LlmStage.DEDUP
    confirm_stage: ClassVar[LlmStage] = LlmStage.DEDUP_CONFIRM

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
        relation = require_str(data, "relation", what="dedup/integrate")
        title = require_str(data, "title", what="dedup/integrate").strip()
        summary = require_str(data, "summary", what="dedup/integrate").strip()
        reason = str(data.get("reason", "")).strip()

        candidate_id = data.get("closest_news_item_id")
        candidate = next((n for n in window if n.id == candidate_id), None)

        if relation not in (SAME_EVENT, RELATED, UNRELATED):
            # The schema constrains this, so reaching here means a provider that did not
            # enforce it. Treated as `related` rather than as either answer: it is the one
            # value that decides nothing on its own, so the cost of a garbled answer is one
            # extra completion instead of a wrong merge or a wrong split.
            logger.warning(
                "dedup returned relation %r for source %s, which the schema does not "
                "allow; treating it as %r",
                relation,
                item.id,
                RELATED,
            )
            relation = RELATED

        if relation == SAME_EVENT:
            if candidate is not None:
                return self._merged(item, candidate, title, summary, relation=relation)
            # Naming a story that is not in the window is a model error. Degrade to "new"
            # rather than raising: the cost of under-merging is one duplicate story in a
            # briefing, and the cost of raising is that ingestion stops entirely.
            logger.warning(
                "dedup called source %s the same event as unknown news item %r; treating as new",
                item.id,
                candidate_id,
            )
        elif relation == RELATED and candidate is not None:
            logger.info(
                "dedup is unsure whether source %s is news item %s (%s); looking again",
                item.id,
                candidate.id,
                reason or "no reason given",
            )
            if self._is_same_event(item, candidate):
                # The stored copy travels, not the copy the first pass wrote. It wrote a
                # headline and a summary for this source item *alone*, because it had not
                # decided the story was already in the backlog — the same reason
                # ``_merge_target``'s title backstop keeps the stored title. Only a
                # ``same_event`` answer is asked for copy that reflects both sources.
                return self._merged(
                    item, candidate, candidate.title, candidate.summary, relation=relation
                )
        elif relation == RELATED:
            # Symmetric with the `same_event` warning above: "related to what?" is a model
            # error too, and without a line here it is indistinguishable in the metric from
            # a second look that ran and said no.
            logger.warning(
                "dedup called source %s related to unknown news item %r; "
                "there is nothing to look at again",
                item.id,
                candidate_id,
            )

        record_dedup_decision(relation=relation, outcome="new")
        return IntegrationResult(
            news_item=NewsItem(
                # A proposal, not an identity. The persistence layer assigns the real id
                # when it inserts the row; only the merged branch returns an id that
                # already means something.
                id=_proposed_news_item_id(),
                title=title or item.title,
                summary=summary or item.title,
                source_item_ids=(item.id,),
            ),
            merged=False,
        )

    def _merged(
        self,
        item: SourceItem,
        existing: NewsItem,
        title: str,
        summary: str,
        *,
        relation: str,
    ) -> IntegrationResult:
        record_dedup_decision(relation=relation, outcome="merged")
        return IntegrationResult(
            news_item=NewsItem(
                id=existing.id,
                title=title or existing.title,
                summary=summary or existing.summary,
                source_item_ids=(*existing.source_item_ids, item.id),
            ),
            merged=True,
        )

    def _is_same_event(self, item: SourceItem, candidate: NewsItem) -> bool:
        """The second look: one pair, one question, one short answer.

        **Every failure here answers "no".** A merge is the side that cannot be undone from
        the outside — a story folded into another leaves a log line and nothing a re-paste
        would reverse — so an unreadable, truncated or refused second look must leave the
        story where the first pass put it. That is also why this swallows rather than
        raises: the first pass already succeeded, and turning its answer into a retried job
        would re-run the volume call and re-bill it to discover the same thing.

        **What it does not swallow is a fault in the stage itself.** The caught set is
        ``LlmTransportError`` and the parse errors below — a call that reached a vendor and
        came back unusable. A ``LlmConfigError`` (this stage pointed at a model that cannot
        honour the request) and a ``ReasoningNotAppliedError`` (the effort was dropped and
        the model did not think) are both ``LlmError`` and are both *deliberately* outside
        it: catching them would turn "the second look is misconfigured" and "the second
        look ran without thinking" into a permanently disabled feature whose only trace is
        a warning that reads like a network blip. AGENTS.md says the reasoning guard is
        reporting a real fault and must not be switched off; this is the shape of switching
        it off.

        **Merging into an already-*read* window item is in scope here, and that is
        decided rather than overlooked.** ``repo.news_item_window`` carries recently read
        stories as well as unread ones, and folding a fresh source item into one the
        listener has already heard means assembly never speaks it. AGENTS.md permits that
        for the *model-driven* merge — it is what the window is for — and denies it to
        ``_merge_target``'s string match, because a string match is not a judgement about
        two texts. This is a judgement about two texts, made on one pair at more depth than
        the pass that produced the uncertain answer, so it sits on the permitted side. The
        adapter also cannot see ``read_at``: ``NewsItem`` does not carry it, and giving the
        inference seam a read-state opinion would put episode policy in the wrong layer.
        """
        # Built outside the `try`, deliberately. `build_request` raises `LlmConfigError`
        # for a stage pointed at a model that cannot honour what it asks for, and that is
        # a misconfiguration every other stage crashes on rather than degrades through.
        request = build_request(
            self.confirm_stage,
            second_look_messages(item, candidate),
            max_output_tokens=CONFIRM_MAX_TOKENS,
            response_format=SECOND_LOOK_SCHEMA,
        )
        try:
            response = self._client.complete(request)
        except LlmBudgetExhaustedError as exc:
            record_budget_exhausted(self.confirm_stage, exc)
            logger.warning(
                "dedup second look for source %s ran out of budget; leaving it separate",
                item.id,
            )
            return False
        except LlmTransportError:
            logger.warning(
                "dedup second look for source %s failed in transport; leaving it separate",
                item.id,
                exc_info=True,
            )
            return False

        record_usage(self.confirm_stage, response)
        try:
            data = parse_json_object(response, what="dedup/confirm")
            same = require_bool(data, "same_event", what="dedup/confirm")
        except PromptResponseError:
            logger.warning(
                "dedup second look for source %s was unreadable; leaving it separate",
                item.id,
                exc_info=True,
            )
            return False

        logger.info(
            "dedup second look: source %s %s news item %s (%s)",
            item.id,
            "is" if same else "is not",
            candidate.id,
            str(data.get("reason", "")).strip() or "no reason given",
        )
        return same


class ClaudeScriptGenerator:
    """Generate briefing copy in which every claim cites a span.

    The model returns a *quote* rather than a character offset, and this class locates the
    quote to derive the span — models copy text reliably and count characters unreliably.
    A claim whose quote cannot be found verbatim is **dropped**, which is what keeps a
    fabricated quotation from becoming a real-looking citation. See ``prompts`` for the
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
    "ClaudeIntegrator",
    "ClaudeScriptGenerator",
]
