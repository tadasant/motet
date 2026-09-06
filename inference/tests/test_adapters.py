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

import httpx
import pytest
from motet_inference import adapters
from motet_inference.accounting import classify_grounding_reason
from motet_inference.adapters import (
    GROUNDING_BUDGET_REASON,
    GROUNDING_CLAIMS_PER_CALL,
    GROUNDING_CONTEXT_CHARS,
    ClaudeGroundingValidator,
    ClaudeIntegrator,
    ClaudeScriptGenerator,
    grounding_max_tokens,
)
from motet_inference.llm import (
    Credential,
    CredentialKind,
    FakeLlmClient,
    LlmBudgetExhaustedError,
    LlmRequest,
    LlmResponse,
    LlmStage,
    LlmTransportError,
    ReasoningNotAppliedError,
    Usage,
    build_request,
)
from motet_inference.llm.openrouter import OpenRouterClient
from motet_inference.prompts import (
    GROUNDING_SCHEMA,
    GroundingClaim,
    PromptResponseError,
    excerpt_around,
    grounding_messages,
    locate_quote,
)
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
    def test_same_event_folds_the_source_into_the_named_story(self) -> None:
        client = canned(
            {
                "closest_news_item_id": "ni_1",
                "relation": "same_event",
                "reason": "One round, two write-ups.",
                "title": "Acme raises $20M Series A",
                "summary": "Two newsletters, one round.",
            }
        )
        result = ClaudeIntegrator(client).integrate(EVENING, [STORY])

        assert result.merged
        assert result.news_item.id == "ni_1"
        assert result.news_item.source_item_ids == ("si_1", "si_2")
        assert result.news_item.summary == "Two newsletters, one round."
        assert len(client.calls) == 1, "a confident answer must not buy a second look"

    def test_unrelated_gets_a_proposed_id_and_only_its_own_source(self) -> None:
        client = canned(
            {
                "closest_news_item_id": "ni_1",
                "relation": "unrelated",
                "reason": "A regulator, not a funding round.",
                "title": "Regulator opens inquiry",
                "summary": "An inquiry into data retention.",
            }
        )
        result = ClaudeIntegrator(client).integrate(EVENING, [STORY])

        assert not result.merged
        assert result.news_item.source_item_ids == ("si_2",)
        assert result.news_item.id.startswith("ni_")
        assert result.news_item.id != STORY.id
        assert len(client.calls) == 1, "the cheap answer must stay one call"

    def test_same_event_with_a_story_outside_the_window_degrades_to_new(self) -> None:
        """A model error that must not stop ingestion.

        Under-merging costs one duplicate story in a briefing. Raising costs every
        subsequent paste-in, because the queue would retry the same poisoned job.
        """
        client = canned(
            {
                "closest_news_item_id": "ni_does_not_exist",
                "relation": "same_event",
                "reason": "r",
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
        client = canned(
            {
                "closest_news_item_id": None,
                "relation": "unrelated",
                "reason": "r",
                "title": "t",
                "summary": "s",
            }
        )
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

    def test_the_whole_stage_survives_a_sonnet_5_answer_with_no_thinking_in_it(self) -> None:
        """motet#31 end to end: the real adapter, the real OpenRouter client, one item.

        Everything below the transport is production code — ``ClaudeIntegrator`` building
        the dedup request through ``build_request`` at the configured effort, and
        ``OpenRouterClient`` parsing a body shaped exactly like the twenty-one that failed
        on staging: a well-formed dedup answer with ``reasoning_tokens: 0``, no
        ``reasoning_details`` and no reasoning text. Only the network is stubbed, because
        invariant 7 says no test here reaches a vendor.

        Before the fix this raised ``ReasoningNotAppliedError`` and the job was retried
        until its attempts ran out, so nothing could enter the pipeline at all.
        """
        body = {
            "id": "gen-1",
            "model": "anthropic/claude-sonnet-5",
            "choices": [
                {
                    "finish_reason": "stop",
                    "message": {
                        "role": "assistant",
                        "content": json.dumps(
                            {
                                "closest_news_item_id": "ni_1",
                                "relation": "same_event",
                                "reason": "One round, two write-ups.",
                                "title": "Acme raises $20M Series A",
                                "summary": "Two newsletters, one round.",
                            }
                        ),
                    },
                }
            ],
            "usage": {
                "prompt_tokens": 1227,
                "completion_tokens": 96,
                "prompt_tokens_details": {"cached_tokens": 0},
                "completion_tokens_details": {"reasoning_tokens": 0},
            },
        }
        sent: list[dict[str, object]] = []

        def handle(request: httpx.Request) -> httpx.Response:
            sent.append(json.loads(request.content))
            return httpx.Response(200, json=body)

        client = OpenRouterClient(
            Credential(kind=CredentialKind.API_KEY, secret="sk-or-test"),
            transport=httpx.MockTransport(handle),
        )
        result = ClaudeIntegrator(client).integrate(EVENING, [STORY])

        assert sent[0]["reasoning"] == {"enabled": True, "effort": "low"}
        assert result.merged
        assert result.news_item.source_item_ids == ("si_1", "si_2")


class TestTheSecondLook:
    """motet#41's other half: the band where the first pass says it is unsure.

    Two of three write-ups of one story merged on staging and the third did not. The
    deterministic identical-title backstop in ``motet_workers.handlers`` catches that only
    when the headlines happen to match; when the model writes a different headline for the
    same event, nothing did. These tests drive the real adapter over the deterministic LLM
    fake, so what is pinned is the *decision procedure* — which answers merge, which do
    not, and what a failure costs — rather than any claim about what a real model says.
    """

    def _first_pass(self, relation: str, *, closest: str | None = "ni_1") -> dict[str, object]:
        return {
            "closest_news_item_id": closest,
            "relation": relation,
            "reason": "Both are about Acme's Series A, but the second adds detail.",
            "title": "Acme's Series A closes",
            "summary": "Acme closed its Series A.",
        }

    def _scripted(
        self, relation: str, second: object, *, closest: str | None = "ni_1"
    ) -> FakeLlmClient:
        """A fake keyed on each prompt's own system text, so the two calls differ."""
        return FakeLlmClient(
            responses={
                "deduplication stage of a personal news briefing": json.dumps(
                    self._first_pass(relation, closest=closest)
                ),
                "second look of a news briefing": json.dumps(second),
            }
        )

    def test_an_unsure_first_pass_that_the_second_look_confirms_merges(self) -> None:
        """The reported failure, at the level the fix lives at.

        The first pass declines to commit and the second look, shown only this pair,
        says it is one event — so the story does not become a second news item. Note the
        headline the first pass wrote is *different* from the existing one, which is what
        puts this case outside the identical-title backstop's reach.
        """
        client = self._scripted(
            "related", {"same_event": True, "reason": "One round, two write-ups."}
        )
        result = ClaudeIntegrator(client).integrate(EVENING, [STORY])

        assert result.merged
        assert result.news_item.id == "ni_1"
        assert result.news_item.source_item_ids == ("si_1", "si_2")
        assert len(client.calls) == 2
        # The stored copy survives: the first pass wrote that headline for this source
        # item alone, having not decided the story was already in the backlog.
        assert result.news_item.title == STORY.title
        assert result.news_item.summary == STORY.summary

    def test_an_unsure_first_pass_the_second_look_rejects_stays_separate(self) -> None:
        """The other direction, which is the one a threshold change would have broken.

        A second look exists to *ask again*, not to merge. Two genuinely distinct events
        that share actors reach it and come back out as two stories.
        """
        client = self._scripted(
            "related", {"same_event": False, "reason": "A later round, not this one."}
        )
        result = ClaudeIntegrator(client).integrate(EVENING, [STORY])

        assert not result.merged
        assert result.news_item.source_item_ids == ("si_2",)
        assert len(client.calls) == 2

    def test_the_second_look_sees_one_pair_and_not_the_window(self) -> None:
        """What makes it a different question, rather than the same one asked twice.

        The first pass scans the whole backlog *and* writes a headline and a summary, at
        the shallowest effort in the system. This call is handed one pair and one question,
        at ``dedup_confirm``'s depth.
        """
        other = NewsItem(
            id="ni_2", title="Regulator opens inquiry", summary="An inquiry.", source_item_ids=()
        )
        client = self._scripted("related", {"same_event": True, "reason": "r"})
        ClaudeIntegrator(client).integrate(EVENING, [STORY, other])

        second: LlmRequest = client.calls[1]
        rendered = "\n".join(part.text for message in second.messages for part in message.parts)
        assert STORY.title in rendered
        assert EVENING.text in rendered
        assert other.id not in rendered, "the second look is about one pair"
        assert second.reasoning is not None and second.reasoning.effort == "medium"

    def test_an_unsure_answer_with_no_candidate_costs_no_second_call(self) -> None:
        """`related` to *what*? A missing candidate is nothing to compare against."""
        client = self._scripted(
            "related", {"same_event": True, "reason": "r"}, closest="ni_does_not_exist"
        )
        result = ClaudeIntegrator(client).integrate(EVENING, [STORY])

        assert not result.merged
        assert len(client.calls) == 1

    @pytest.mark.parametrize(
        "second",
        [
            {"reason": "no verdict at all"},
            {"same_event": "true", "reason": "a string, not a boolean"},
        ],
        ids=["missing", "not-a-boolean"],
    )
    def test_an_unreadable_second_look_leaves_the_story_separate(self, second: object) -> None:
        """Every failure of the second look answers "no", and that direction is chosen.

        A merge is the side that cannot be undone from outside — a story folded into
        another leaves a log line and nothing a re-paste would reverse — so an answer this
        cannot read must not become one.
        """
        client = self._scripted("related", second)
        result = ClaudeIntegrator(client).integrate(EVENING, [STORY])

        assert not result.merged
        assert result.news_item.source_item_ids == ("si_2",)

    def test_a_second_look_that_runs_out_of_budget_leaves_the_story_separate(self) -> None:
        """Same direction, and it must not turn a succeeded first pass into a retry.

        Raising here would send the job back to the queue, where the volume call would be
        made and billed again to discover exactly the same thing.
        """

        class Exhausted:
            def __init__(self) -> None:
                self.calls: list[LlmRequest] = []
                self._inner = FakeLlmClient(
                    responses={
                        "deduplication stage": json.dumps(
                            {
                                "closest_news_item_id": "ni_1",
                                "relation": "related",
                                "reason": "r",
                                "title": "t",
                                "summary": "s",
                            }
                        )
                    }
                )

            def complete(self, request: LlmRequest) -> LlmResponse:
                self.calls.append(request)
                if len(self.calls) == 1:
                    return self._inner.complete(request)
                raise LlmBudgetExhaustedError("spent it all", model=request.model)

        client = Exhausted()
        result = ClaudeIntegrator(client).integrate(EVENING, [STORY])

        assert not result.merged
        assert len(client.calls) == 2

    def test_every_answer_is_counted_by_relation_and_by_outcome(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The pair is the instrument, and without it this design is unfalsifiable.

        A false merge and a false split both look like a working pipeline from outside, so
        the only way to tell whether the ``related`` band is worth its extra completions is
        to count how often it fires and how often the second look flips it. ``related`` and
        ``merged`` together is the flip.
        """
        recorded: list[tuple[str, str]] = []
        monkeypatch.setattr(
            adapters,
            "record_dedup_decision",
            lambda *, relation, outcome: recorded.append((relation, outcome)),
        )

        ClaudeIntegrator(self._scripted("related", {"same_event": True, "reason": "r"})).integrate(
            EVENING, [STORY]
        )
        ClaudeIntegrator(self._scripted("related", {"same_event": False, "reason": "r"})).integrate(
            EVENING, [STORY]
        )
        ClaudeIntegrator(self._scripted("unrelated", {})).integrate(EVENING, [STORY])
        ClaudeIntegrator(self._scripted("same_event", {})).integrate(EVENING, [STORY])

        assert recorded == [
            ("related", "merged"),
            ("related", "new"),
            ("unrelated", "new"),
            ("same_event", "merged"),
        ]

    def test_a_transport_failure_leaves_the_story_separate(self) -> None:
        """The third of the three failure modes, so all of them are covered.

        A socket that dies mid-call is the one that will actually happen, and it must land
        on the same side as the other two: not merged, not raised.
        """

        class Broken:
            def __init__(self) -> None:
                self.calls: list[LlmRequest] = []
                self._inner = FakeLlmClient(
                    responses={
                        "deduplication stage": json.dumps(
                            {
                                "closest_news_item_id": "ni_1",
                                "relation": "related",
                                "reason": "r",
                                "title": "t",
                                "summary": "s",
                            }
                        )
                    }
                )

            def complete(self, request: LlmRequest) -> LlmResponse:
                self.calls.append(request)
                if len(self.calls) == 1:
                    return self._inner.complete(request)
                raise LlmTransportError("connection reset")

        client = Broken()
        result = ClaudeIntegrator(client).integrate(EVENING, [STORY])

        assert not result.merged
        assert len(client.calls) == 2

    def test_a_stage_fault_is_not_swallowed_as_a_failed_second_look(self) -> None:
        """The one thing that must *not* answer "no".

        ``ReasoningNotAppliedError`` reports that a stage ran without thinking, and
        ``LlmConfigError`` reports that it is pointed at a model that cannot do what it
        asks. Catching either would leave the second look permanently disabled behind a
        warning line indistinguishable from a network blip — which is exactly the "never
        switch the reasoning guard off" rule in AGENTS.md, wearing a different hat.
        """

        class Unthinking:
            def __init__(self) -> None:
                self.calls: list[LlmRequest] = []
                self._inner = FakeLlmClient(
                    responses={
                        "deduplication stage": json.dumps(
                            {
                                "closest_news_item_id": "ni_1",
                                "relation": "related",
                                "reason": "r",
                                "title": "t",
                                "summary": "s",
                            }
                        )
                    }
                )

            def complete(self, request: LlmRequest) -> LlmResponse:
                self.calls.append(request)
                if len(self.calls) == 1:
                    return self._inner.complete(request)
                raise ReasoningNotAppliedError("no reasoning tokens")

        with pytest.raises(ReasoningNotAppliedError):
            ClaudeIntegrator(Unthinking()).integrate(EVENING, [STORY])

    def test_a_relation_the_schema_forbids_is_asked_again_rather_than_guessed(self) -> None:
        """The one value that decides nothing on its own is the safe place to land.

        Reading a garbled answer as ``same_event`` would be a wrong merge and as
        ``unrelated`` a wrong split; reading it as ``related`` costs one completion and
        then asks a question that has an answer.
        """
        client = self._scripted("SAME STORY", {"same_event": True, "reason": "r"})
        result = ClaudeIntegrator(client).integrate(EVENING, [STORY])

        assert result.merged
        assert len(client.calls) == 2


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

    def test_a_small_episode_is_still_judged_in_one_call(self) -> None:
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
        # Grounding runs at the highest effort in the system, so claims are batched up to
        # a bound rather than sent one at a time. Below the bound that is still one call.
        assert len(client.calls) == 1


class BudgetBoundClient:
    """A model that thinks per claim and answers only if the ceiling outlasts the thinking.

    motet#42 reduced to arithmetic. On the staging run the grounding stage came back with
    ``output_tokens == reasoning_tokens == 8000`` and not one verdict, for a request
    carrying twelve claims — so reasoning cost at least ~660 tokens a claim and the answer
    never started. This client reproduces that shape offline: it spends
    ``reasoning_per_claim`` before writing anything, and a call whose ceiling runs out
    first raises exactly what the real adapter raises.

    It is deliberately *not* a ``FakeLlmClient``: the fake is honest about prompts and
    knows nothing about budgets, and a budget is the whole subject here.

    ``reasoning_flat`` is motet#52's addition: the part of a call's thinking that does not
    scale with the claim count. It is zero here, which is the motet#42 shape, and non-zero
    in :class:`StagingShapedClient` — a flat term is what makes halving a chunk take budget
    away faster than it takes work away, so a model that has one cascades and a model that
    does not never will.
    """

    reasoning_flat = 0
    reasoning_per_claim = 700
    answer_per_claim = 40

    def __init__(self, *, exhaust_on: str | None = None, max_claims_answered: int = 10_000):
        self.calls: list[LlmRequest] = []
        #: The claim count of every call that ran out of budget — motet#52's wasted calls.
        self.exhausted: list[int] = []
        #: Output tokens billed across every call, answered or not.
        self.spent = 0
        #: The part of ``spent`` that bought no verdict: a call that hit its ceiling is
        #: billed for the whole of it and returns nothing. This is motet#52's waste.
        self.wasted = 0
        self._exhaust_on = exhaust_on
        self._max_claims_answered = max_claims_answered

    def complete(self, request: LlmRequest) -> LlmResponse:
        self.calls.append(request)
        prompt = request.messages[-1].text
        claims = sum(1 for line in prompt.splitlines() if line.startswith("CLAIM "))
        spent = self.reasoning_flat + (self.reasoning_per_claim + self.answer_per_claim) * claims
        starved = self._exhaust_on is not None and self._exhaust_on in prompt
        if spent > request.max_output_tokens or starved or claims > self._max_claims_answered:
            self.exhausted.append(claims)
            self.spent += request.max_output_tokens
            self.wasted += request.max_output_tokens
            raise LlmBudgetExhaustedError(
                f"budget exhausted: {claims} claims would spend {spent} of "
                f"{request.max_output_tokens}"
            )
        self.spent += spent
        text = json.dumps(
            {"verdicts": [{"index": i, "supported": True, "reason": ""} for i in range(claims)]}
        )
        return LlmResponse(
            text=text,
            model=request.model,
            usage=Usage(
                output_tokens=spent,
                reasoning_tokens=self.reasoning_flat + self.reasoning_per_claim * claims,
            ),
            reasoning_applied=True,
            finish_reason="stop",
        )


class StagingShapedClient(BudgetBoundClient):
    """motet#52's staging run, fitted to ``demand(n) = 4000 + 1800n`` output tokens.

    Three anchors came out of that run. They define a *region* of ``F + c*n`` rather than a
    point — ``F=3000, c=2000`` fits them too — and this is one interior point of it, chosen
    because it clears every anchor without hugging any of them:

    ====================  ===============  ==================  ==========
    Observed on staging   Old ceiling      ``demand(n)``       Outcome
    ====================  ===============  ==================  ==========
    8 claims, exhausted   14,000           18,400              runs out
    4 claims, exhausted   10,000           11,200              runs out
    2 claims, answered    8,000            7,600               answers
    ====================  ===============  ==================  ==========

    The third row is an inference rather than a quoted log line: the issue's cascade stops
    at two, and it reports no claim dropped for budget, which the floor of the halving
    would have produced had a single claim run out. The two exhausting rows are quoted
    verbatim from the issue, ceilings included.

    That the fit also *reproduces the headline numbers* is the check on it. Five chunks of
    eight cascading all the way down cost 5 x 7 = 35 calls and 5 x 3 = **15** exhaustions,
    and burn 5 x (14,000 + 2 x 10,000) = **170,000** discarded output tokens; the issue
    reports 15 exhaustions and "~180,000 output tokens produced and discarded" out of 58
    calls, the remaining 23 being chunks the character bound had already made small enough
    to answer first time.
    """

    reasoning_flat = 4_000
    reasoning_per_claim = 1_760
    answer_per_claim = 40


class UnderestimatedClient(BudgetBoundClient):
    """A model the *corrected* constants are still too small for: ``demand(n) = 4500 + 3000n``.

    Four claims want 16,500 against a 16,000 ceiling, two want 10,500 against 10,500, and
    one wants 7,500 against 7,750. So the first chunk of an episode runs out and its halves
    do not — which is the only interesting case, because it is the one where the estimate is
    wrong and the episode still has to be judged.
    """

    reasoning_flat = 4_500
    reasoning_per_claim = 2_960
    answer_per_claim = 40


def a_backlog(news_items: int, claims_each: int = 3) -> tuple[Script, dict[str, SourceItem]]:
    """A script the size of a real morning: one segment per news item, several claims each.

    The evidence spans are paragraph-sized because that is what a newsletter sentence
    resolves to, and the size of the evidence is half of what a grounding call has to
    chew through.
    """
    sources: dict[str, SourceItem] = {}
    segments = []
    for item in range(news_items):
        source_id = f"si_{item}"
        sentences = [
            f"Story {item} sentence {n}: the company said something specific and checkable "
            f"about its plans for the coming quarter, with a number in it ({n * 7}%)."
            for n in range(claims_each)
        ]
        text = " ".join(sentences)
        sources[source_id] = SourceItem(id=source_id, title=f"Story {item}", text=text)
        claims = []
        offset = 0
        for sentence in sentences:
            start = text.index(sentence, offset)
            offset = start + len(sentence)
            claims.append(
                Claim(
                    text=f"Story {item}: {sentence[:60]}",
                    span=SourceSpan(source_item_id=source_id, start=start, end=offset),
                )
            )
        segments.append(ScriptSegment(news_item_id=f"ni_{item}", claims=tuple(claims)))
    return Script(segments=tuple(segments)), sources


def _claims_per_call(client: BudgetBoundClient) -> list[int]:
    """How many claims each call actually carried, read off the prompts it was sent."""
    return [
        sum(1 for line in call.messages[-1].text.splitlines() if line.startswith("CLAIM "))
        for call in client.calls
    ]


class TestGroundingAtRealisticScale:
    """motet#42: the stage that could not finish an episode of nineteen news items."""

    @pytest.mark.parametrize("news_items", [13, 19, 20])
    def test_one_batched_call_at_a_fixed_ceiling_returns_nothing(self, news_items: int) -> None:
        """The shape this stage had before the fix, asserted rather than described.

        Every claim in one request against the old ``GROUNDING_MAX_TOKENS = 8_000``. The
        model spends the whole ceiling thinking and produces no verdict, which is what
        staging saw at 19 news items and again at 13 — deterministically, so every retry
        did it again and the episode never left ``scripting``.
        """
        script, sources = a_backlog(news_items)
        one_batched_call = build_request(
            LlmStage.GROUNDING,
            grounding_messages(_grounding_claims(script, sources)),
            max_output_tokens=8_000,
            response_format=GROUNDING_SCHEMA,
        )
        with pytest.raises(LlmBudgetExhaustedError):
            BudgetBoundClient().complete(one_batched_call)

    @pytest.mark.parametrize("news_items", [13, 19, 20])
    def test_the_same_backlog_is_fully_judged_once_it_is_chunked(self, news_items: int) -> None:
        """The fix: the same model, the same claims, every one of them judged.

        Nothing about the model changed between this test and the one above it. What
        changed is that no single call is asked for more than it can answer, and the
        ceiling is a function of how much work the call carries.
        """
        script, sources = a_backlog(news_items)
        client = BudgetBoundClient()

        report = ClaudeGroundingValidator(client).validate(script, sources)

        assert report.ok, [failure.reason for failure in report.failures]
        assert len(client.calls) > 1
        judged = 0
        for call in client.calls:
            in_call = sum(
                1 for line in call.messages[-1].text.splitlines() if line.startswith("CLAIM ")
            )
            judged += in_call
            assert in_call <= GROUNDING_CLAIMS_PER_CALL
            # The ceiling tracks the work rather than a constant. This is the property
            # that has no backlog size beyond which the stage stops working.
            assert call.max_output_tokens == grounding_max_tokens(in_call)
        assert judged == news_items * 3

    def test_a_chunk_that_still_exhausts_is_halved_rather_than_lost(self) -> None:
        """Chunk size is a guess about the model; halving is what survives a wrong guess."""
        script, sources = a_backlog(4)
        client = BudgetBoundClient(max_claims_answered=2)

        report = ClaudeGroundingValidator(client).validate(script, sources)

        assert report.ok, [failure.reason for failure in report.failures]
        sizes = _claims_per_call(client)
        assert max(sizes) == GROUNDING_CLAIMS_PER_CALL  # the first attempt, before halving
        assert min(sizes) <= 2  # and what it came down to

    def test_long_evidence_makes_chunks_smaller_than_the_claim_bound(self) -> None:
        """The character bound, which the claim count on its own cannot reach.

        A chunk of paragraph-sized evidence spans is a far bigger ask than a chunk of short
        ones,
        and a count cannot tell them apart. Newsletters produce both.
        """
        parts = [f"Paragraph {n}. " + ("word " * 1_000) for n in range(6)]
        source = SourceItem(id="si_long", title="Long", text="".join(parts))
        claims, offset = [], 0
        for part in parts:
            claims.append(
                Claim(
                    text=f"A claim about {part[:11]}",
                    span=SourceSpan("si_long", offset, offset + len(part)),
                )
            )
            offset += len(part)
        script = Script(segments=(ScriptSegment(news_item_id="ni_long", claims=tuple(claims)),))
        client = BudgetBoundClient()

        report = ClaudeGroundingValidator(client).validate(script, {source.id: source})

        assert report.ok, [failure.reason for failure in report.failures]
        sizes = _claims_per_call(client)
        assert sum(sizes) == len(parts)
        assert max(sizes) < GROUNDING_CLAIMS_PER_CALL

    def test_one_claim_larger_than_the_whole_bound_is_sent_on_its_own(self) -> None:
        """The bound is a bound on chunks, not a promise about any single claim.

        A claim whose evidence is larger than the whole character budget cannot be made to
        fit, so it goes alone rather than being dropped or looping forever.
        """
        huge = "word " * 4_000
        source = SourceItem(id="si_huge", title="Huge", text=huge + "and a short tail.")
        script = Script(
            segments=(
                ScriptSegment(
                    news_item_id="ni_huge",
                    claims=(
                        Claim(text="the long one", span=SourceSpan("si_huge", 0, len(huge))),
                        Claim(
                            text="the short one",
                            span=SourceSpan("si_huge", len(huge), len(huge) + 17),
                        ),
                    ),
                ),
            )
        )
        client = BudgetBoundClient()

        report = ClaudeGroundingValidator(client).validate(script, {source.id: source})

        assert report.ok, [failure.reason for failure in report.failures]
        assert _claims_per_call(client) == [1, 1]

    def test_a_single_claim_that_cannot_be_judged_fails_closed(self) -> None:
        """The floor of the halving, and the one place invariant 3 could have been bent.

        A claim nobody could get a verdict for is dropped, exactly as a claim with no
        verdict already was. It is never approved, and it never costs the other claims:
        ``handle_script`` ships what survived, so the episode is a story short rather than
        absent altogether.
        """
        script, sources = a_backlog(2)
        poisoned = script.segments[1].claims[0].text
        client = BudgetBoundClient(exhaust_on=poisoned)

        report = ClaudeGroundingValidator(client).validate(script, sources)

        assert [failure.claim_text for failure in report.failures] == [poisoned]
        assert report.failures[0].reason == GROUNDING_BUDGET_REASON
        assert classify_grounding_reason(report.failures[0].reason) == "budget_exhausted"


class TestGroundingSplitCascade:
    """motet#52: the split that is discovered by burning a whole output budget.

    The stage survived a full backlog (motet#46) at a price: fifteen calls on one staging
    episode spent ``output_tokens == max_output_tokens`` with ``reasoning_tokens`` right
    behind it and produced no verdict at all, and the work was then redone on two halves
    that could exhaust in turn. Everything here is measured against
    :class:`StagingShapedClient`, which is that run reduced to arithmetic.
    """

    def test_the_two_staging_ceilings_are_reproduced_exactly(self) -> None:
        """The quoted log lines, executed: 8 claims at 14,000 and 4 at 10,000 both run out.

        This is the calibration check on everything below it. If the fitted demand did not
        exhaust at these two ceilings it would not be a model of the reported defect.
        """
        script, sources = a_backlog(4, claims_each=2)
        client = StagingShapedClient()

        for claims, ceiling in ((8, 14_000), (4, 10_000)):
            with pytest.raises(LlmBudgetExhaustedError):
                client.complete(_a_grounding_call(script, sources, claims, ceiling))

        # And the floor the cascade stopped at, which is why no claim was dropped.
        answered = client.complete(_a_grounding_call(script, sources, 2, 8_000))
        assert answered.usage.output_tokens == 7_600

    def test_the_corrected_ceiling_answers_a_chunk_the_old_one_could_not(self) -> None:
        """Four claims: 10,000 was not enough for them and ``grounding_max_tokens(4)`` is.

        The ceiling moved because the demand is per-claim dominated and the formula was
        headroom dominated — not because the number was simply raised until it fit. 16,000
        is the largest ``demand(4)`` consistent with all three staging observations, so
        nothing inside that region can exhaust a first attempt.
        """
        script, sources = a_backlog(4, claims_each=2)
        client = StagingShapedClient()

        assert grounding_max_tokens(4) == 16_000
        answered = client.complete(_a_grounding_call(script, sources, 4, grounding_max_tokens(4)))
        assert answered.usage.output_tokens == 11_200
        assert client.exhausted == []

    def test_eight_claims_used_to_cost_seven_calls_and_now_cost_two(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """One rung of the staging cascade, before and after, through the real validator.

        Eight claims is exactly one chunk under the old constants and exactly two under the
        new ones, so the whole difference is visible without any narrowing: with eight
        claims there is never a *later* chunk for the narrowing to help.
        """
        script, sources = a_backlog(4, claims_each=2)

        with monkeypatch.context() as old:
            old.setattr(adapters, "GROUNDING_CLAIMS_PER_CALL", 8)
            old.setattr(adapters, "GROUNDING_CHARS_PER_CALL", 12_000)
            old.setattr(adapters, "GROUNDING_TOKENS_PER_CLAIM", 1_000)
            old.setattr(adapters, "GROUNDING_REASONING_HEADROOM", 6_000)
            before_client = StagingShapedClient()
            before = ClaudeGroundingValidator(before_client).validate(script, sources)

        after_client = StagingShapedClient()
        after = ClaudeGroundingValidator(after_client).validate(script, sources)

        assert before.ok and after.ok, "every claim is judged either way; this is about cost"
        # 8 -> 4 -> 2, twice over: one call at 14,000 and two at 10,000 bought nothing.
        assert _claims_per_call(before_client) == [8, 4, 2, 2, 4, 2, 2]
        assert before_client.exhausted == [8, 4, 4]
        assert (before_client.spent, before_client.wasted) == (64_400, 34_000)

        assert _claims_per_call(after_client) == [4, 4]
        assert after_client.exhausted == []
        assert (after_client.spent, after_client.wasted) == (22_400, 0)

    def test_a_full_backlog_makes_no_call_that_buys_nothing(self) -> None:
        """The issue's own case: 21 news items, judged without one probe.

        This is the sentence the issue asks for — grounding a full backlog no longer
        discovers an undersized chunk by spending a whole output budget and receiving
        nothing back — asserted rather than described.
        """
        script, sources = a_backlog(21)
        client = StagingShapedClient()

        report = ClaudeGroundingValidator(client).validate(script, sources)

        assert report.ok, [failure.reason for failure in report.failures]
        assert sum(_claims_per_call(client)) == 63
        assert client.exhausted == []
        assert client.wasted == 0
        assert len(client.calls) == 16  # 63 claims, four to a call
        assert client.spent == 15 * 11_200 + 9_400  # fifteen full calls and one of three

    def test_an_underestimate_is_paid_once_an_episode_rather_than_once_a_chunk(self) -> None:
        """The backstop, and the reason the corrected constants do not have to be right.

        :class:`UnderestimatedClient` needs more than the new ceiling allows, so the first
        chunk of the episode still runs out — that is unavoidable, and it is what the
        halving is for. What must not happen is the next fifteen chunks each paying the
        same probe, which is the whole of motet#52.
        """
        script, sources = a_backlog(21)
        client = UnderestimatedClient()

        report = ClaudeGroundingValidator(client).validate(script, sources)

        assert report.ok, [failure.reason for failure in report.failures]
        # 63 claims, and the one call that bought nothing carried four of them twice.
        assert sum(_claims_per_call(client)) == 63 + 4
        # One probe, at four claims, for the whole episode. Without the narrowing every one
        # of the sixteen four-claim chunks would have bought its own.
        assert client.exhausted == [4]
        assert _claims_per_call(client)[:3] == [4, 2, 2]
        assert set(_claims_per_call(client)[3:]) == {1, 2}
        assert client.wasted == grounding_max_tokens(4)

    def test_narrowing_does_not_survive_the_episode_it_was_learned_in(self) -> None:
        """Per ``validate`` call, not per adapter: one bad episode must not shrink the next.

        A limit that lived on the adapter would ratchet down over a process's lifetime and
        never recover, quietly converting a single pathological claim into a permanently
        more expensive stage.
        """
        script, sources = a_backlog(4, claims_each=2)
        validator = ClaudeGroundingValidator(UnderestimatedClient())
        validator.validate(script, sources)

        second = StagingShapedClient()
        ClaudeGroundingValidator(second).validate(script, sources)

        assert _claims_per_call(second) == [4, 4]

    def test_one_unjudgeable_claim_does_not_narrow_the_rest_of_the_episode(self) -> None:
        """A claim that runs out *on its own* says nothing about how many claims fit.

        There is no smaller chunk to retreat to, so the only thing a size-one exhaustion
        establishes is that this claim cannot be judged — and it is dropped for exactly
        that reason. Narrowing on it would let one pathological claim put every remaining
        claim of the episode on a call of its own, which is the most expensive shape the
        stage has.
        """
        script, sources = _claims_of_size([7_000] + [200] * 8)
        poisoned = script.segments[0].claims[0].text
        client = BudgetBoundClient(exhaust_on=poisoned)

        report = ClaudeGroundingValidator(client).validate(script, sources)

        assert [failure.reason for failure in report.failures] == [GROUNDING_BUDGET_REASON]
        # The oversized claim is chunked alone by the character bound, runs out, and is
        # dropped. The eight ordinary claims that follow are still judged four to a call.
        assert _claims_per_call(client) == [1, 4, 4]

    def test_an_odd_chunk_narrows_to_a_size_it_answered_not_below_it(self) -> None:
        """Halving three gives one and two, so two is answered — and two is the new size.

        Narrowing by halving the size that *failed* would say one here, throwing away the
        larger size the episode had just seen work.
        """
        script, sources = _claims_of_size([950] * 9)
        client = BudgetBoundClient(max_claims_answered=2)

        report = ClaudeGroundingValidator(client).validate(script, sources)

        assert report.ok, [failure.reason for failure in report.failures]
        # Three at a time by the character bound; the first runs out and splits 1 + 2, and
        # the six that remain go two at a time rather than one.
        assert _claims_per_call(client) == [3, 1, 2, 2, 2, 2]
        assert client.exhausted == [3]


def _claims_of_size(evidence_chars: list[int]) -> tuple[Script, dict[str, SourceItem]]:
    """One claim per entry, each quoting the whole of a source item of the requested size.

    The character bound is what decides chunk sizes here, so the sizes are the input: a
    span far larger than the whole budget forces a chunk of one, and smaller ones force
    chunks of two or three.

    One source item **per claim**, quoted in full, so that a size is the whole of what its
    claim costs a chunk: the citation and the context around it are then the same text and
    no two claims share a block, which makes a chunk cost a little over twice the sizes in
    it. That is the arithmetic the expectations are written against — the realistic shape,
    where a story's claims share one source item and therefore one block, is what
    :func:`a_backlog` covers.
    """
    sources: dict[str, SourceItem] = {}
    claims = []
    for index, size in enumerate(evidence_chars):
        source_id = f"si_sized_{index}"
        sources[source_id] = SourceItem(
            id=source_id, title=f"Sized {index}", text=f"[{index}]".ljust(size, "x")
        )
        claims.append(
            Claim(
                text=f"Claim {index} about a span of {size} characters",
                span=SourceSpan(source_id, 0, size),
            )
        )
    segment = ScriptSegment(news_item_id="ni_sized", claims=tuple(claims))
    return Script(segments=(segment,)), sources


def _grounding_claims(script: Script, sources: dict[str, SourceItem]) -> list[GroundingClaim]:
    """Every claim of ``script`` as the validator would present it — citation and context.

    Built the way ``ClaudeGroundingValidator`` builds it rather than by hand, so a change
    to what travels with a claim shows up in these cost simulations too.
    """
    return [
        GroundingClaim(
            index=index,
            spoken=claim.text,
            cited=claim.span.resolve(sources) or "",
            context=excerpt_around(
                sources[claim.span.source_item_id].text,
                claim.span.start,
                claim.span.end,
                GROUNDING_CONTEXT_CHARS,
            ),
        )
        for index, claim in enumerate(
            claim for segment in script.segments for claim in segment.claims
        )
    ]


def _a_grounding_call(
    script: Script, sources: dict[str, SourceItem], claims: int, ceiling: int
) -> LlmRequest:
    """One grounding request carrying ``claims`` real claims under an explicit ceiling."""
    resolved = _grounding_claims(script, sources)[:claims]
    assert len(resolved) == claims
    return build_request(
        LlmStage.GROUNDING,
        grounding_messages(resolved),
        max_output_tokens=ceiling,
        response_format=GROUNDING_SCHEMA,
    )


DISPATCH = SourceItem(
    id="si_dispatch",
    title="County certifies results after a week of provisional-ballot review",
    text=(
        "County certifies results after a week of provisional-ballot review\n"
        "\n"
        "Elections officials in Warren County finished their canvass on Wednesday. Staff\n"
        "flagged 185 provisional ballots for identification review, most of them cast by\n"
        "voters who had moved within the county since the last election.\n"
        "\n"
        "Reviewers cleared the backlog over four days. The clerk's office said the pace\n"
        "was steady and that no ballot was set aside for want of time.\n"
        "\n"
        "The board voted to certify the count on Thursday evening, with all five members\n"
        "present. The certified totals moved no race by more than a dozen votes.\n"
    ),
)

CERTIFIED = "The board voted to certify the count on Thursday evening"


def _one_claim(spoken: str, item: SourceItem, quote: str) -> tuple[Script, dict[str, SourceItem]]:
    located = locate_quote(item.text, quote)
    assert located is not None
    claim = Claim(text=spoken, span=SourceSpan(item.id, *located))
    segment = ScriptSegment(news_item_id="ni_1", claims=(claim,))
    return Script(segments=(segment,)), {item.id: item}


class TestGroundingEvidence:
    """motet#45: what the gate is shown when it judges a claim.

    The staging rehearsal refused a claim about 185 flagged ballots while ``185`` sat a
    paragraph away in the very source item the claim cites — because the prompt carried
    the resolved span and nothing else, which makes support outside the quotation
    indistinguishable from a fabrication. These assert the evidence rather than a verdict:
    a verdict here would be the fake's opinion, and the evidence is the defect.
    """

    def test_the_evidence_reaches_beyond_the_cited_span(self) -> None:
        """The reproduction. ``185`` is in the source item and not in the quotation."""
        script, sources = _one_claim(
            "The board certified the count on Thursday after reviewing 185 flagged ballots.",
            DISPATCH,
            CERTIFIED,
        )
        client = canned({"verdicts": []})

        ClaudeGroundingValidator(client).validate(script, sources)

        prompt = client.calls[0].messages[-1].text
        assert "185" not in CERTIFIED
        assert f"CITED: {CERTIFIED}" in prompt
        assert "flagged 185 provisional ballots" in prompt

    def test_the_evidence_stops_at_the_source_item_the_claim_cites(self) -> None:
        """The widening's own bound, and the one that would be a real loosening to cross.

        A claim grounded in a *different* story's source is exactly the fabrication the
        gate exists to catch, so a source item nothing in the chunk cites must not travel
        with it.
        """
        script, sources = _one_claim("The board certified the count.", DISPATCH, CERTIFIED)
        other = SourceItem(id="si_other", title="Lakeside", text="Lakeside flagged 185 ballots.")
        client = canned({"verdicts": []})

        ClaudeGroundingValidator(client).validate(script, {**sources, other.id: other})

        assert "Lakeside" not in client.calls[0].messages[-1].text

    def test_a_source_item_longer_than_the_budget_is_excerpted_around_the_span(self) -> None:
        """The bound that keeps a long article from becoming the whole of one call."""
        filler = "\n\n".join(f"Paragraph {n}. " + "word " * 200 for n in range(20))
        long_item = SourceItem(
            id="si_long", title="Long", text=f"{filler}\n\n{CERTIFIED}, with five present."
        )
        script, sources = _one_claim("The board certified the count.", long_item, CERTIFIED)
        client = canned({"verdicts": []})

        ClaudeGroundingValidator(client).validate(script, sources)

        prompt = client.calls[0].messages[-1].text
        assert CERTIFIED in prompt
        assert len(prompt) < len(long_item.text)
        assert "Paragraph 0." not in prompt

    def test_claims_sharing_a_source_item_share_one_block(self) -> None:
        """What stops the widening from being paid once per claim.

        Three claims out of one newsletter are three citations and one block. Sending the
        block per claim would change no verdict and would triple the input the most
        expensive stage in the system reads.
        """
        quotes = [
            "Elections officials in Warren County finished their canvass on Wednesday",
            "Reviewers cleared the backlog over four days",
            CERTIFIED,
        ]
        claims = []
        for quote in quotes:
            located = locate_quote(DISPATCH.text, quote)
            assert located is not None
            claims.append(Claim(text=f"Spoken: {quote}.", span=SourceSpan(DISPATCH.id, *located)))
        script = Script(segments=(ScriptSegment(news_item_id="ni_1", claims=tuple(claims)),))
        client = canned({"verdicts": []})

        ClaudeGroundingValidator(client).validate(script, {DISPATCH.id: DISPATCH})

        prompt = client.calls[0].messages[-1].text
        assert prompt.count("SOURCE 1\n") == 1
        assert "SOURCE 2" not in prompt
        assert prompt.count("CITED: ") == 3


class TestExcerptAround:
    """The window itself, away from any prompt: what a budget buys and where it cuts."""

    def test_a_source_item_inside_the_budget_travels_whole(self) -> None:
        text = "One. Two. Three."
        assert excerpt_around(text, 5, 9, 100) == text

    def test_the_window_keeps_whole_paragraphs_on_both_sides(self) -> None:
        paragraphs = [f"Paragraph {n} " + "word " * 20 for n in range(12)]
        text = "\n\n".join(paragraphs)
        start = text.index(paragraphs[6])
        excerpt = excerpt_around(text, start, start + len(paragraphs[6]), 500)

        assert paragraphs[6] in excerpt
        assert len(excerpt) <= 500
        # Whole paragraphs, so nothing begins or ends mid-sentence.
        assert excerpt.startswith("Paragraph ")
        assert excerpt.strip().endswith("word")

    def test_a_span_at_the_very_top_still_gets_a_full_budget_of_context(self) -> None:
        """A window that would run off an end is shifted rather than truncated.

        The lead sentence of a newsletter is the most-quoted sentence there is, and half a
        budget of context is exactly what it must not get.
        """
        text = "Lead sentence. " + "".join(f"Sentence {n}. " for n in range(400))
        excerpt = excerpt_around(text, 0, 14, 600)

        assert excerpt.startswith("Lead sentence.")
        assert len(excerpt) > 500

    def test_a_span_larger_than_the_budget_is_returned_whole(self) -> None:
        """The budget bounds the context, never the citation.

        Returning less than the span would mean judging a claim against part of its own
        quotation, which is a stricter gate than the one before the change rather than the
        looser one it is meant to be.
        """
        text = "x" * 5_000
        assert excerpt_around(text, 100, 3_000, 1_000) == text[100:3_000]

    def test_the_excerpt_is_a_verbatim_slice_of_the_source(self) -> None:
        """Nothing is normalized, joined, or elided on the way in.

        The evidence a claim is judged against has to be the source's own bytes, or the
        gate is judging a rendering of the source rather than the source.
        """
        text = "\n\n".join(f"Para {n}. " + "word " * 40 for n in range(20))
        excerpt = excerpt_around(text, 900, 950, 800)
        assert excerpt in text


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
