"""Vendor adapters — the real implementations of each stage.

**Nothing here may be imported into a test.** The registry refuses to hand these out
unless ``MOTET_INFERENCE_MODE=real`` is set explicitly, which never happens in CI.

Claude covers dedup/integrate, script generation, and grounding; Cartesia Sonic covers
TTS. Credentials arrive from the environment, resolved by infrastructure that lives in
the private repo — never read a key from a file in this tree.

The three text stages reach their model through ``motet_inference.llm``: ``build_client()``
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

from .cartesia import CartesiaSpeechSynthesizer
from .interfaces import IntegrationResult
from .llm import LlmClient, LlmStage, build_client, build_request
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

#: One short verdict per claim.
GROUNDING_MAX_TOKENS = 8_000


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
        data = parse_json_object(response, what="script generation")
        known = {item.id: item for item in news_items}

        segments: list[ScriptSegment] = []
        spoken_for: set[str] = set()
        for raw_segment in _list_of_objects(data.get("segments"), what="script segments"):
            news_item_id = require_str(raw_segment, "news_item_id", what="script segment")
            news_item = known.get(news_item_id)
            if news_item is None:
                logger.warning("script names unknown news item %r; dropping segment", news_item_id)
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
                continue
            claims = self._claims_for(raw_segment, news_item, sources)
            if not claims:
                logger.warning(
                    "every claim in the segment for %s was unlocatable; dropping segment",
                    news_item_id,
                )
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
                continue
            source = sources.get(source_item_id)
            if source is None:
                logger.warning("claim cites unavailable source %r; dropping claim", source_item_id)
                continue
            span = locate_quote(source.text, quote)
            if span is None:
                logger.warning(
                    "claim on %s quotes text not found in source %s; dropping claim",
                    news_item.id,
                    source_item_id,
                )
                continue
            if not text:
                continue
            claims.append(
                Claim(
                    text=text,
                    span=SourceSpan(source_item_id=source_item_id, start=span[0], end=span[1]),
                )
            )
        return tuple(claims)


class ClaudeGroundingValidator:
    """Judge whether each claim is supported by the span it cites, paraphrase included.

    **Invariant 3.** This runs before synthesis, never after, and a report with failures
    means nothing gets spoken.

    Two checks, in order. First a mechanical one — does the span resolve to real text at
    all — which needs no model and catches a corrupted or stale span. Then one model call
    for every surviving claim at once, asking whether the evidence actually supports what
    would be said. Batching is deliberate: a per-claim call would multiply the most
    expensive per-token stage in the pipeline by the number of claims in an episode, and
    the verdicts are independent, so there is nothing to gain from isolating them.
    """

    stage: ClassVar[LlmStage] = LlmStage.GROUNDING

    def __init__(self, client: LlmClient | None = None) -> None:
        self._client = client if client is not None else build_client()

    def validate(self, script: Script, sources: Mapping[str, SourceItem]) -> GroundingReport:
        failures: list[GroundingFailure] = []
        judgeable: list[tuple[int, str, str]] = []
        origins: dict[int, tuple[str, str]] = {}

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
                index = len(judgeable)
                judgeable.append((index, claim.text, resolved))
                origins[index] = (segment.news_item_id, claim.text)

        if not judgeable:
            return GroundingReport(failures=tuple(failures))

        response = self._client.complete(
            build_request(
                self.stage,
                grounding_messages(judgeable),
                max_output_tokens=GROUNDING_MAX_TOKENS,
                response_format=GROUNDING_SCHEMA,
            )
        )
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
            if raw_index in verdicts and not verdicts[raw_index][0]:
                # A rejection already stands for this claim, and a later "supported" for
                # the same index must not overwrite it. Every other branch here fails
                # closed — an unparseable verdict, a missing one — and last-write-wins
                # would make this the single path by which an unsupported claim reaches
                # audio. Invariant 3 is only as strong as its most forgiving branch.
                continue
            reason = raw.get("reason")
            verdicts[raw_index] = (supported, reason if isinstance(reason, str) else "")

        for index, (news_item_id, claim_text) in origins.items():
            verdict = verdicts.get(index)
            if verdict is None:
                # Fail closed. A claim the validator did not answer for is a claim nobody
                # checked, and "unchecked" must never be spoken as if it were "supported".
                failures.append(
                    GroundingFailure(
                        news_item_id=news_item_id,
                        claim_text=claim_text,
                        reason="grounding validation returned no verdict for this claim",
                    )
                )
                continue
            supported, reason = verdict
            if not supported:
                failures.append(
                    GroundingFailure(
                        news_item_id=news_item_id,
                        claim_text=claim_text,
                        reason=reason or "the cited span does not support this claim",
                    )
                )
        return GroundingReport(failures=tuple(failures))


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
