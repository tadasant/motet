"""The real Claude adapters, driven against the deterministic LLM fake.

These are the *real* stage adapters — the ones the registry hands out when
``MOTET_INFERENCE_MODE=real`` — with a fake model underneath. That combination is what
makes the interesting behaviour testable offline: prompt construction, response parsing,
and above all what each adapter does with an answer that is wrong.

The failure modes matter more than the happy path. A model that cites a quote it did not
copy, names a source that is not in the story, or returns no verdict for a claim is not
hypothetical, and every one of those has to end with something *not* being spoken.
"""

from __future__ import annotations

import json

import pytest
from motet_inference.adapters import (
    ClaudeGroundingValidator,
    ClaudeIntegrator,
    ClaudeScriptGenerator,
)
from motet_inference.llm import FakeLlmClient, LlmRequest
from motet_inference.prompts import PromptResponseError, locate_quote
from motet_inference.types import (
    Claim,
    NewsItem,
    Script,
    ScriptSegment,
    SourceItem,
    SourceSpan,
)

MORNING = SourceItem(
    id="si_1",
    title="Acme raises $20M Series A",
    text=(
        "Acme raises $20M Series A. Acme announced the round on Tuesday, led by Northwind\n"
        "Ventures, bringing total funding to $31M. The company says the money goes to hiring."
    ),
)
EVENING = SourceItem(
    id="si_2",
    title="Acme Series A closes",
    text="ACME SERIES A. Acme's Series A closed this week with Northwind Ventures leading.",
)
STORY = NewsItem(
    id="ni_1",
    title="Acme raises $20M Series A",
    summary="Acme raised $20M led by Northwind Ventures.",
    source_item_ids=("si_1",),
)
SOURCES = {MORNING.id: MORNING, EVENING.id: EVENING}


def canned(payload: object) -> FakeLlmClient:
    """A fake client that answers every request with one JSON document."""
    return FakeLlmClient(responses={"": json.dumps(payload)})


class TestIntegrator:
    def test_merge_folds_the_source_into_the_named_story(self) -> None:
        client = canned(
            {
                "decision": "merge",
                "news_item_id": "ni_1",
                "title": "Acme raises $20M Series A",
                "summary": "Two newsletters, one round.",
            }
        )
        result = ClaudeIntegrator(client).integrate(EVENING, [STORY])

        assert result.merged
        assert result.news_item.id == "ni_1"
        assert result.news_item.source_item_ids == ("si_1", "si_2")
        assert result.news_item.summary == "Two newsletters, one round."

    def test_new_story_gets_a_proposed_id_and_only_its_own_source(self) -> None:
        client = canned(
            {
                "decision": "new",
                "news_item_id": None,
                "title": "Regulator opens inquiry",
                "summary": "An inquiry into data retention.",
            }
        )
        result = ClaudeIntegrator(client).integrate(EVENING, [STORY])

        assert not result.merged
        assert result.news_item.source_item_ids == ("si_2",)
        assert result.news_item.id.startswith("ni_")
        assert result.news_item.id != STORY.id

    def test_merging_into_a_story_outside_the_window_degrades_to_new(self) -> None:
        """A model error that must not stop ingestion.

        Under-merging costs one duplicate story in a briefing. Raising costs every
        subsequent paste-in, because the queue would retry the same poisoned job.
        """
        client = canned(
            {
                "decision": "merge",
                "news_item_id": "ni_does_not_exist",
                "title": "Acme",
                "summary": "s",
            }
        )
        result = ClaudeIntegrator(client).integrate(EVENING, [STORY])

        assert not result.merged
        assert result.news_item.source_item_ids == ("si_2",)

    def test_the_window_is_the_cacheable_prefix_and_the_item_is_not(self) -> None:
        """Prompt caching is the largest LLM cost lever, and dedup is the volume stage.

        The breakpoint has to fall after the window and before the source item, or the
        cache misses on every call and the saving never materializes.
        """
        client = canned({"decision": "new", "news_item_id": None, "title": "t", "summary": "s"})
        ClaudeIntegrator(client).integrate(EVENING, [STORY])

        request: LlmRequest = client.calls[0]
        user = request.messages[1]
        assert "ni_1" in user.parts[0].text  # the window
        assert user.parts[0].cache is not None
        assert user.parts[0].cache.ttl == "1h"
        assert EVENING.text in user.parts[1].text  # the volatile item
        assert user.parts[1].cache is None

    def test_a_response_that_is_not_json_is_an_error_rather_than_a_guess(self) -> None:
        with pytest.raises(PromptResponseError):
            ClaudeIntegrator(FakeLlmClient()).integrate(EVENING, [STORY])


class TestScriptGenerator:
    def test_a_quote_becomes_a_span_that_resolves_to_itself(self) -> None:
        quote = "Acme announced the round on Tuesday"
        client = canned(
            {
                "segments": [
                    {
                        "news_item_id": "ni_1",
                        "claims": [
                            {
                                "text": "Acme closed a round on Tuesday.",
                                "quote": quote,
                                "source_item_id": "si_1",
                            }
                        ],
                    }
                ]
            }
        )
        script = ClaudeScriptGenerator(client).generate([STORY], SOURCES)

        (segment,) = script.segments
        (claim,) = segment.claims
        # The spoken text paraphrases; the span is verbatim. That separation is what lets
        # narration read like prose and still be checkable.
        assert claim.text == "Acme closed a round on Tuesday."
        assert claim.span.resolve(dict(SOURCES)) == quote

    def test_a_quote_broken_across_a_line_still_locates(self) -> None:
        """Newsletters are hard-wrapped and models unwrap them.

        Dropping these would discard most claims on any source longer than a line, so a
        whitespace-only difference is forgiven — and nothing else is.
        """
        quote = "led by Northwind Ventures, bringing total funding to $31M"
        client = canned(
            {
                "segments": [
                    {
                        "news_item_id": "ni_1",
                        "claims": [
                            {"text": "Northwind led it.", "quote": quote, "source_item_id": "si_1"}
                        ],
                    }
                ]
            }
        )
        script = ClaudeScriptGenerator(client).generate([STORY], SOURCES)

        (claim,) = script.segments[0].claims
        resolved = claim.span.resolve(dict(SOURCES))
        assert resolved is not None
        assert resolved.split() == quote.split()
        assert "\n" in resolved  # the span covers the real, wrapped source text

    def test_a_fabricated_quote_is_dropped_rather_than_given_a_span(self) -> None:
        client = canned(
            {
                "segments": [
                    {
                        "news_item_id": "ni_1",
                        "claims": [
                            {
                                "text": "Acme raised $90M.",
                                "quote": "Acme raised $90M in its Series A",
                                "source_item_id": "si_1",
                            }
                        ],
                    }
                ]
            }
        )
        script = ClaudeScriptGenerator(client).generate([STORY], SOURCES)

        # The whole segment goes, because a segment with no grounded claim has nothing
        # left that may be spoken.
        assert script.segments == ()

    def test_a_claim_citing_a_source_outside_its_story_is_dropped(self) -> None:
        """Cross-citation is a subtle failure: the quote resolves, but to the wrong story.

        The result would be a segment about Acme's funding evidenced by a sentence from an
        unrelated newsletter — grounded-looking and wrong.
        """
        client = canned(
            {
                "segments": [
                    {
                        "news_item_id": "ni_1",
                        "claims": [
                            {
                                "text": "Acme's round closed.",
                                "quote": "Acme's Series A closed this week",
                                "source_item_id": "si_2",
                            }
                        ],
                    }
                ]
            }
        )
        script = ClaudeScriptGenerator(client).generate([STORY], SOURCES)
        assert script.segments == ()

    def test_a_segment_for_an_unknown_story_is_dropped(self) -> None:
        client = canned({"segments": [{"news_item_id": "ni_ghost", "claims": [], "extra": 1}]})
        assert ClaudeScriptGenerator(client).generate([STORY], SOURCES).segments == ()

    def test_no_news_items_means_no_model_call_at_all(self) -> None:
        client = FakeLlmClient()
        assert ClaudeScriptGenerator(client).generate([], {}).segments == ()
        assert client.calls == []


def script_with(text: str, start: int, end: int, source: str = "si_1") -> Script:
    return Script(
        segments=(
            ScriptSegment(
                news_item_id="ni_1",
                claims=(
                    Claim(text=text, span=SourceSpan(source_item_id=source, start=start, end=end)),
                ),
            ),
        )
    )


class TestGroundingValidator:
    def test_a_supported_claim_passes(self) -> None:
        client = canned({"verdicts": [{"index": 0, "supported": True, "reason": "ok"}]})
        report = ClaudeGroundingValidator(client).validate(
            script_with("Acme raised money.", 0, 26), SOURCES
        )
        assert report.ok

    def test_an_unsupported_claim_fails_with_the_model_s_reason(self) -> None:
        client = canned(
            {"verdicts": [{"index": 0, "supported": False, "reason": "no such number"}]}
        )
        report = ClaudeGroundingValidator(client).validate(
            script_with("Acme raised $90M.", 0, 26), SOURCES
        )
        assert not report.ok
        assert report.failures[0].reason == "no such number"

    def test_an_unresolvable_span_fails_without_asking_a_model(self) -> None:
        """The mechanical check runs first, and needs no model to be right.

        A span past the end of its source is a corrupted or stale citation. Paying a model
        to have an opinion about it would be both slower and less certain.
        """
        client = FakeLlmClient()
        report = ClaudeGroundingValidator(client).validate(
            script_with("Acme raised money.", 0, 99_999), SOURCES
        )
        assert not report.ok
        assert "does not resolve" in report.failures[0].reason
        assert client.calls == []

    def test_a_claim_with_no_verdict_fails_closed(self) -> None:
        """ "Nobody checked this" must never be treated as "this is fine".

        A truncated or partial verdict list is exactly how an unchecked claim would
        otherwise slip through to audio.
        """
        client = canned({"verdicts": []})
        report = ClaudeGroundingValidator(client).validate(
            script_with("Acme raised money.", 0, 26), SOURCES
        )
        assert not report.ok
        assert "no verdict" in report.failures[0].reason

    def test_a_boolean_index_is_not_mistaken_for_claim_one(self) -> None:
        """``isinstance(True, int)`` is True in Python.

        Without the explicit bool check, a verdict indexed ``true`` would be filed against
        claim 1 — approving a claim nobody judged.
        """
        client = canned({"verdicts": [{"index": True, "supported": True, "reason": "x"}]})
        report = ClaudeGroundingValidator(client).validate(
            Script(
                segments=(
                    ScriptSegment(
                        news_item_id="ni_1",
                        claims=(
                            Claim(text="a", span=SourceSpan("si_1", 0, 4)),
                            Claim(text="b", span=SourceSpan("si_1", 5, 10)),
                        ),
                    ),
                )
            ),
            SOURCES,
        )
        assert len(report.failures) == 2

    def test_every_claim_is_judged_in_one_call(self) -> None:
        client = canned(
            {
                "verdicts": [
                    {"index": 0, "supported": True, "reason": ""},
                    {"index": 1, "supported": True, "reason": ""},
                ]
            }
        )
        script = Script(
            segments=(
                ScriptSegment(
                    news_item_id="ni_1",
                    claims=(
                        Claim(text="a", span=SourceSpan("si_1", 0, 4)),
                        Claim(text="b", span=SourceSpan("si_1", 5, 10)),
                    ),
                ),
            )
        )
        assert ClaudeGroundingValidator(client).validate(script, SOURCES).ok
        # Grounding runs at the highest effort in the system; a call per claim would
        # multiply the most expensive stage by the length of the episode.
        assert len(client.calls) == 1


class TestLocateQuote:
    def test_returns_none_for_whitespace(self) -> None:
        assert locate_quote("some text", "   ") is None

    def test_finds_the_first_occurrence(self) -> None:
        assert locate_quote("ab ab", "ab") == (0, 2)

    def test_regex_metacharacters_in_a_quote_are_literal(self) -> None:
        """A newsletter full of ``$`` and ``(`` is normal, not an attack.

        The located span is built from a pattern, so an unescaped quote would either fail
        to match or match somewhere else entirely.
        """
        text = "Revenue (annualized) hit $1.2M+ in Q3."
        quote = "(annualized) hit $1.2M+"
        span = locate_quote(text, quote)
        assert span is not None
        assert text[span[0] : span[1]] == quote


class TestScriptSegmentDedup:
    def test_a_second_segment_for_one_story_is_dropped(self) -> None:
        """`UNIQUE (episode_id, news_item_id)` means the database refuses the repeat.

        Letting it through turns a model quirk into a unique violation that fails the whole
        episode five times over before anyone sees it, instead of a story told once.
        """
        claim = {
            "text": "Acme closed a round.",
            "quote": "Acme announced the round on Tuesday",
            "source_item_id": "si_1",
        }
        client = canned(
            {
                "segments": [
                    {"news_item_id": "ni_1", "claims": [claim]},
                    {"news_item_id": "ni_1", "claims": [claim]},
                ]
            }
        )
        script = ClaudeScriptGenerator(client).generate([STORY], SOURCES)

        assert [segment.news_item_id for segment in script.segments] == ["ni_1"]


class TestScriptPrompt:
    def test_does_not_ask_for_an_ungroundable_greeting(self) -> None:
        """A greeting cannot be covered by a quote, so the grounding gate would drop the
        claim carrying it — costing the lead story its first sentence."""
        from motet_inference.prompts import SCRIPT_SYSTEM

        assert "greeting" in SCRIPT_SYSTEM  # it is addressed...
        assert "no greeting or sign-off" in SCRIPT_SYSTEM  # ...by forbidding it


class TestGroundingFailsClosed:
    """The verdict-collision case, which is the one way an unsupported claim could ship.

    Every other branch of the validator fails closed. Last-write-wins on a duplicated
    index was the exception, and an exception is all invariant 3 needs to stop being true.
    """

    def test_a_later_supported_verdict_cannot_overturn_a_rejection(self) -> None:
        client = canned(
            {
                "verdicts": [
                    {"index": 0, "supported": False, "reason": "invented figure"},
                    {"index": 0, "supported": True, "reason": "looks fine"},
                ]
            }
        )
        report = ClaudeGroundingValidator(client).validate(
            script_with("Acme raised $90M.", 0, 26), SOURCES
        )
        assert not report.ok
        assert report.failures[0].reason == "invented figure"

    def test_a_rejection_arriving_second_still_rejects(self) -> None:
        client = canned(
            {
                "verdicts": [
                    {"index": 0, "supported": True, "reason": "looks fine"},
                    {"index": 0, "supported": False, "reason": "invented figure"},
                ]
            }
        )
        report = ClaudeGroundingValidator(client).validate(
            script_with("Acme raised $90M.", 0, 26), SOURCES
        )
        assert not report.ok
