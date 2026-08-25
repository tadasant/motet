"""The accounting seam: what a stage spent, and what it threw away.

Two defects with one shape (motet#24, motet#25) — the system did the work and discarded
the evidence. These tests assert the evidence survives, which is the only property that
matters: a counter nobody increments and a ledger nobody appends to look exactly like a
pipeline that never ran.

Driven through the *real* stage adapters against the LLM fake, because recording is done
by the adapters and a test of the helper alone would pass while the call site was missing.
"""

from __future__ import annotations

import json
import logging

import pytest
from motet_inference import classify_grounding_reason, collect_usage
from motet_inference.accounting import Ledger, StageUsage, describe_usage
from motet_inference.adapters import (
    ClaudeGroundingValidator,
    ClaudeIntegrator,
    ClaudeScriptGenerator,
)
from motet_inference.llm import FakeLlmClient, LlmStage, Usage
from motet_inference.types import NewsItem, SourceItem

MORNING = SourceItem(
    id="si_1",
    title="Acme raises $20M Series A",
    text=(
        "Acme raises $20M Series A. Acme announced the round on Tuesday, led by Northwind "
        "Ventures, bringing total funding to $31M."
    ),
)
STORY = NewsItem(
    id="ni_1",
    title="Acme raises $20M Series A",
    summary="Acme raised $20M led by Northwind Ventures.",
    source_item_ids=("si_1",),
)


def canned(payload: object) -> FakeLlmClient:
    return FakeLlmClient(responses={"": json.dumps(payload)})


class TestUsageSurvivesTheStage:
    def test_dedup_usage_reaches_a_collecting_caller(self) -> None:
        """motet#25 in one assertion: the number was always there, nobody caught it."""
        client = canned({"decision": "new", "title": "Acme", "summary": "Acme raised money."})

        with collect_usage() as spend:
            ClaudeIntegrator(client).integrate(MORNING, [])

        assert spend.requests == 1
        (entry,) = spend.entries
        assert entry.stage is LlmStage.DEDUP
        assert entry.usage.input_tokens > 0
        assert entry.usage.output_tokens > 0

    def test_script_and_grounding_add_up_in_one_ledger(self) -> None:
        """The shape an episode's cost line needs: several stages, one total.

        Scripting and grounding an episode are two completions and one question — "what
        did that episode cost" — so the block spans both and the totals are summed.
        """
        script_client = canned(
            {
                "segments": [
                    {
                        "news_item_id": "ni_1",
                        "claims": [
                            {
                                "text": "Acme raised twenty million dollars.",
                                "quote": "Acme raises $20M Series A",
                                "source_item_id": "si_1",
                            }
                        ],
                    }
                ]
            }
        )
        grounding_client = canned({"verdicts": [{"index": 0, "supported": True}]})

        with collect_usage() as spend:
            script = ClaudeScriptGenerator(script_client).generate([STORY], {MORNING.id: MORNING})
            ClaudeGroundingValidator(grounding_client).validate(script, {MORNING.id: MORNING})

        assert spend.requests == 2
        assert {entry.stage for entry in spend.entries} == {LlmStage.SCRIPT, LlmStage.GROUNDING}
        total = spend.total()
        assert total.input_tokens == sum(e.usage.input_tokens for e in spend.entries)
        assert total.output_tokens == sum(e.usage.output_tokens for e in spend.entries)

    def test_a_stage_outside_a_block_still_logs_what_it_spent(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        """No ledger is not an excuse to record nothing.

        The context var is a convenience for a caller that has an id to attribute cost to.
        A stage reached from anywhere else must still leave the line behind, or the seam
        has a hole in it exactly where nobody is looking.
        """
        client = canned({"decision": "new", "title": "Acme", "summary": "Acme raised money."})

        with caplog.at_level(logging.INFO, logger="motet.inference.cost"):
            ClaudeIntegrator(client).integrate(MORNING, [])

        (line,) = [record.getMessage() for record in caplog.records]
        assert line.startswith("llm dedup on ")
        assert "cache_read=" in line

    def test_nested_blocks_do_not_double_count(self) -> None:
        client = canned({"decision": "new", "title": "Acme", "summary": "Acme raised money."})

        with collect_usage() as outer:
            with collect_usage() as inner:
                ClaudeIntegrator(client).integrate(MORNING, [])
            assert inner.requests == 1
        assert outer.requests == 0


class TestDescribeUsage:
    def test_every_field_is_present_even_at_zero(self) -> None:
        """``cache_read=0`` is precisely the observation worth being able to search for.

        AGENTS.md calls prompt caching the largest cost lever in the system and says never
        to assume a hit. A field that vanishes when it is zero is a field a log query
        cannot aggregate, so a miss would be invisible in exactly the way the warning is
        about.
        """
        rendered = describe_usage(Usage(input_tokens=10, output_tokens=2))

        assert "cache_read=0" in rendered
        assert "cache_write=0" in rendered
        assert "reasoning=0" in rendered

    def test_an_empty_ledger_totals_to_zero_rather_than_raising(self) -> None:
        assert Ledger().total() == Usage()
        assert Ledger().requests == 0

    def test_the_summary_of_a_ledger_is_the_sum_of_its_entries(self) -> None:
        ledger = Ledger(
            entries=[
                StageUsage(LlmStage.SCRIPT, "m", Usage(input_tokens=3, cache_read_tokens=7)),
                StageUsage(LlmStage.GROUNDING, "m", Usage(input_tokens=4, cache_read_tokens=1)),
            ]
        )

        assert ledger.total() == Usage(input_tokens=7, cache_read_tokens=8)
        assert "input=7" in ledger.summary()
        assert "cache_read=8" in ledger.summary()


class TestGroundingReasonKinds:
    def test_the_two_reasons_we_write_ourselves_are_recognised(self) -> None:
        """These come from `ClaudeGroundingValidator`, not from a model.

        They mean different things from a model's refusal — one is a script-stage bug and
        one is a validator-response bug — so they must not land in the same bucket as
        "the evidence does not support this", which is the gate working as designed.
        """
        assert classify_grounding_reason("span does not resolve to any source text") == (
            "span_unresolved"
        )
        assert (
            classify_grounding_reason("grounding validation returned no verdict for this claim")
            == "no_verdict"
        )

    def test_a_model_s_own_sentence_becomes_one_bounded_bucket(self) -> None:
        """A sentence as a metric label is a time series per claim, forever."""
        assert classify_grounding_reason("The $31M total appears nowhere in the source.") == (
            "unsupported"
        )
        assert classify_grounding_reason("") == "unsupported"
