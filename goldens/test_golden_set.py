"""The golden set, wired into `bin/ci`.

**What this is for.** Two newsletters describing the same funding round have to become one
news item, every claim in the resulting script has to trace to a real span, and the whole
thing has to come out the same way twice. Those are properties of the *contract*, and they
hold whether the stages are fakes or real models — which is why the harness can gate CI
today, before a single vendor is wired up.

**What it is not.** It runs against the fakes, so it cannot say whether a briefing is any
*good* in the sense a listener would mean. Judging that means running the same cases
through the real adapters and scoring the output, which is a separate, slower,
non-blocking job — not this one. Growing the corpus is adding directories under
``fixtures/``; the harness needs no changes.
"""

from __future__ import annotations

import pytest
from motet_inference import build_briefing, fake_stages

from .harness import GoldenCase, load_cases

CASES = list(load_cases())


def test_the_corpus_is_not_empty() -> None:
    """Guards against a fixture path change silently reducing this file to a no-op."""
    assert CASES


@pytest.mark.parametrize("case", CASES, ids=str)
def test_dedup_matches_the_expected_news_items(case: GoldenCase) -> None:
    briefing = build_briefing(case.sources, fake_stages())
    assert [item.title for item in briefing.news_items] == [e.title for e in case.expected], (
        case.why
    )
    assert [len(item.source_item_ids) for item in briefing.news_items] == [
        e.source_count for e in case.expected
    ], case.why


@pytest.mark.parametrize("case", CASES, ids=str)
def test_every_claim_in_the_script_is_grounded(case: GoldenCase) -> None:
    """Invariant 3, asserted end to end: an ungrounded briefing must never be speakable."""
    briefing = build_briefing(case.sources, fake_stages())
    assert briefing.grounding.ok, [f.reason for f in briefing.grounding.failures]
    assert briefing.speakable


@pytest.mark.parametrize("case", CASES, ids=str)
def test_every_news_item_reaches_the_script(case: GoldenCase) -> None:
    """A deduped story that never gets spoken is a silent drop — catch it here."""
    briefing = build_briefing(case.sources, fake_stages())
    scripted = {segment.news_item_id for segment in briefing.script.segments}
    assert scripted == {item.id for item in briefing.news_items}


@pytest.mark.parametrize("case", CASES, ids=str)
def test_the_pipeline_is_deterministic(case: GoldenCase) -> None:
    """Same input, same briefing. Without this the corpus cannot be a regression test."""
    assert build_briefing(case.sources, fake_stages()) == build_briefing(
        case.sources, fake_stages()
    )


@pytest.mark.parametrize("case", [c for c in CASES if c.script is not None], ids=str)
def test_the_script_matches_the_one_this_case_considers_good(case: GoldenCase) -> None:
    """The other half of the golden set: not just *which* stories, but what gets said.

    Only the cases that declare a script are checked, because pinning spoken copy is
    worth its maintenance cost where the wording is the thing under test and not
    everywhere. Where it is declared, this is what catches a script stage that quietly
    drops a claim or reorders the briefing.
    """
    assert case.script is not None
    briefing = build_briefing(case.sources, fake_stages())
    titles = {item.id: item.title for item in briefing.news_items}

    actual = [
        (titles[segment.news_item_id], [claim.text for claim in segment.claims])
        for segment in briefing.script.segments
    ]
    assert actual == [(s.news_item_title, list(s.claims)) for s in case.script], case.why


@pytest.mark.parametrize("case", CASES, ids=str)
def test_the_briefing_can_be_synthesized(case: GoldenCase) -> None:
    """The last leg: validated copy reaches TTS and comes back with a duration."""
    briefing = build_briefing(case.sources, fake_stages())
    audio = fake_stages().speech_synthesizer.synthesize(briefing.script.text)
    assert audio.duration_ms > 0
