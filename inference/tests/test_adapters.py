"""The real Claude adapters, driven against the deterministic LLM fake.

These are the *real* stage adapters — the ones the registry hands out when
``MOTET_INFERENCE_MODE=real`` — with a fake model underneath. That combination is what
makes the interesting behaviour testable offline: prompt construction, response parsing,
and above all what each adapter does with an answer that is wrong.

The failure modes matter more than the happy path. A model that cites a quote it did not
copy, or names a source that is not in the story, is not hypothetical, and every one of
those has to end with something *not* being spoken.
"""

from __future__ import annotations

import json

import httpx
import pytest
from motet_inference import adapters
from motet_inference.adapters import (
    ClaudeIntegrator,
    ClaudeScriptGenerator,
)
from motet_inference.llm import (
    Credential,
    CredentialKind,
    FakeLlmClient,
    LlmBudgetExhaustedError,
    LlmRequest,
    LlmResponse,
    LlmTransportError,
    ReasoningNotAppliedError,
)
from motet_inference.llm.openrouter import OpenRouterClient
from motet_inference.prompts import (
    PromptResponseError,
    locate_quote,
)
from motet_inference.types import (
    NewsItem,
    SourceItem,
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

        # The whole segment goes, because a segment with no locatable claim has nothing
        # left to speak.
        assert script.segments == ()

    def test_a_claim_citing_a_source_outside_its_story_is_dropped(self) -> None:
        """Cross-citation is a subtle failure: the quote resolves, but to the wrong story.

        The result would be a segment about Acme's funding evidenced by a sentence from an
        unrelated newsletter — a citation that looks real and points at the wrong story.
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
    def test_does_not_ask_for_an_unquotable_greeting(self) -> None:
        """A greeting cannot be covered by a quote, so the claim carrying it is discarded
        while the answer is parsed — costing the lead story its first sentence."""
        from motet_inference.prompts import SCRIPT_SYSTEM

        assert "greeting" in SCRIPT_SYSTEM  # it is addressed...
        assert "no greeting or sign-off" in SCRIPT_SYSTEM  # ...by forbidding it


class TestTheDecisionItReports:
    """motet#91: the answer's *why* travels on the result, for the handler to persist.

    The mis-merge that motivated it was visible as ``merged`` and unexplainable, because
    ``relation``, ``reason`` and the candidate were logged and discarded here.
    """

    def _answer(self, relation: str, closest: str | None) -> dict[str, object]:
        return {
            "closest_news_item_id": closest,
            "relation": relation,
            "reason": "One round, two write-ups.",
            "title": "Acme raises $20M Series A",
            "summary": "Two newsletters, one round.",
        }

    def test_a_first_pass_merge_reports_its_relation_reason_candidate_and_model(self) -> None:
        result = ClaudeIntegrator(canned(self._answer("same_event", "ni_1"))).integrate(
            EVENING, [STORY]
        )

        assert result.decision is not None
        assert result.decision.relation == "same_event"
        assert result.decision.reason == "One round, two write-ups."
        assert result.decision.candidate_id == "ni_1"
        assert result.decision.model, "the slug that answered"
        assert result.decision.second_look is None, "nobody looked again"

    def test_a_candidate_outside_the_window_is_kept_as_named(self) -> None:
        """The model error most worth reading back later — so it is not normalized away."""
        result = ClaudeIntegrator(canned(self._answer("same_event", "ni_nowhere"))).integrate(
            EVENING, [STORY]
        )

        assert not result.merged
        assert result.decision is not None
        assert result.decision.candidate_id == "ni_nowhere"

    def test_the_second_looks_answer_is_reported_either_way(self) -> None:
        def scripted(same: bool) -> FakeLlmClient:
            return FakeLlmClient(
                responses={
                    "deduplication stage of a personal news briefing": json.dumps(
                        self._answer("related", "ni_1")
                    ),
                    "second look of a news briefing": json.dumps(
                        {"same_event": same, "reason": "r"}
                    ),
                }
            )

        yes = ClaudeIntegrator(scripted(True)).integrate(EVENING, [STORY])
        no = ClaudeIntegrator(scripted(False)).integrate(EVENING, [STORY])

        assert yes.merged and yes.decision is not None and yes.decision.second_look is True
        assert not no.merged and no.decision is not None and no.decision.second_look is False
        assert no.decision.relation == "related"


def test_the_fake_integrator_reports_a_decision_in_the_adapters_vocabulary() -> None:
    """So the persisting path runs in every test and golden-set run, not only in real mode."""
    from motet_inference.fakes import FAKE_MODEL, FakeIntegrator

    created = FakeIntegrator().integrate(EVENING, [])
    assert created.decision is not None
    assert (created.decision.relation, created.decision.candidate_id) == ("unrelated", None)
    assert created.decision.model == FAKE_MODEL

    twin = NewsItem(id="ni_9", title=EVENING.title, summary="s", source_item_ids=("si_1",))
    merged = FakeIntegrator().integrate(EVENING, [twin])
    assert merged.decision is not None
    assert (merged.decision.relation, merged.decision.candidate_id) == ("same_event", "ni_9")
