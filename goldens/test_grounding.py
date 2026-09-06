"""The grounding corpus: what the gate is allowed to call support, and what it is not.

**What this is for.** motet#45 — a claim whose supporting figure sits one sentence outside
the span it quotes used to be indistinguishable, to the gate, from a claim that invented
the figure, and was dropped as one. The remedy widens the *evidence* to the source item
while leaving the *citation* exactly as tight as the script stage made it. Widening what
counts as support is the one direction in which this stage fails silently, so the corpus
carries the accepting case and the refusing cases together and neither is optional.

**What it is not.** The stand-in model judges numbers, not entailment (see
:class:`~goldens.grounding_harness.NumericJudge`), so this says nothing about whether a
real model reads a paraphrase correctly. It says what the gate was *shown* and what it did
with that, which is exactly the axis the defect lived on. Judging real verdict quality
needs real calls and is the same separate, slower job the dedup corpus defers.
"""

from __future__ import annotations

import pytest
from motet_inference.adapters import ClaudeGroundingValidator
from motet_inference.prompts import locate_quote
from motet_inference.types import Claim, Script, ScriptSegment, SourceItem, SourceSpan

from .grounding_harness import (
    GroundingCase,
    NumericJudge,
    evidence_shown,
    load_cases,
    source_blocks,
)

CASES = list(load_cases())


def _build(case: GroundingCase) -> tuple[Script, dict[str, SourceItem]]:
    """The case as a one-segment script, with every citation located the way the pipeline does.

    ``locate_quote`` rather than hand-written offsets, so a fixture whose quotation drifted
    away from its source fails here instead of quietly becoming a different span.
    """
    sources = {item.id: item for item in case.sources}
    claims = []
    for expected in case.claims:
        item = sources[expected.source] if expected.source else case.sources[0]
        span = locate_quote(item.text, expected.cited)
        assert span is not None, f"{case.name}: {expected.cited!r} is not verbatim in {item.id}"
        claims.append(Claim(text=expected.spoken, span=SourceSpan(item.id, span[0], span[1])))
    script = Script(segments=(ScriptSegment(news_item_id="ni_1", claims=tuple(claims)),))
    return script, sources


def test_the_corpus_is_not_empty() -> None:
    """Guards against a fixture path change silently reducing this file to a no-op."""
    assert CASES


@pytest.mark.parametrize("case", CASES, ids=str)
def test_each_claim_gets_the_verdict_the_case_expects(case: GroundingCase) -> None:
    """The whole corpus in one assertion: accepted where support exists, refused where not."""
    script, sources = _build(case)
    judge = NumericJudge()

    report = ClaudeGroundingValidator(judge).validate(script, sources)

    refused = {failure.claim_text for failure in report.failures}
    assert [claim.spoken not in refused for claim in case.claims] == [
        claim.supported for claim in case.claims
    ], case.why


@pytest.mark.parametrize(
    "case", [c for c in CASES if c.evidence_contains or c.evidence_excludes], ids=str
)
def test_the_gate_is_shown_what_the_case_says_it_should_be(case: GroundingCase) -> None:
    """The verdict's other half: *why* it came out that way.

    A case that only pinned the verdict would pass just as well against a validator that
    had stopped calling a model at all. These pin the evidence itself — and the excludes
    are how a case states a bound, which is a claim no verdict can make.
    """
    script, sources = _build(case)
    judge = NumericJudge()

    ClaudeGroundingValidator(judge).validate(script, sources)

    shown = evidence_shown(judge.prompts)
    for text in case.evidence_contains:
        assert text in shown, f"{case.name}: the gate never saw {text!r} — {case.why}"
    for text in case.evidence_excludes:
        assert text not in shown, f"{case.name}: the gate saw {text!r} — {case.why}"


@pytest.mark.parametrize("case", [c for c in CASES if c.source_blocks is not None], ids=str)
def test_a_source_block_is_sent_once_however_many_claims_cite_it(case: GroundingCase) -> None:
    """The cost half of the widening, which is the half that fails quietly.

    Nothing about a verdict would change if every claim carried its own copy of the same
    newsletter; the bill would.
    """
    script, sources = _build(case)
    judge = NumericJudge()

    ClaudeGroundingValidator(judge).validate(script, sources)

    assert source_blocks(judge.prompts) == case.source_blocks, case.why


@pytest.mark.parametrize("case", CASES, ids=str)
def test_every_citation_is_still_verbatim_in_its_source(case: GroundingCase) -> None:
    """Invariant 3's mechanical half, unchanged by any of this.

    The span is still resolved before a model is asked anything, and what the gate is shown
    beside it does not make it any less a verbatim quotation of the source it names.
    """
    script, sources = _build(case)
    for claim in script.segments[0].claims:
        assert claim.span.resolve(sources) in claim_texts(case, claim.text)


def claim_texts(case: GroundingCase, spoken: str) -> set[str]:
    return {expected.cited for expected in case.claims if expected.spoken == spoken}
