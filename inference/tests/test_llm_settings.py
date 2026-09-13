"""PROTOTYPE — settings-table overrides above the environment, and a price on every token.

Two additions to the LLM seam for the admin screen: a ``settings`` mapping that outranks
every environment variable in :func:`load_config`, with the winning rung reported per
stage; and a per-completion sink beside the ledger, so a caller with a table can keep one
row per call. Both are tested through the real code paths — the resolver itself, and a
real stage adapter over the LLM fake — because a helper tested alone passes while the call
site is missing.
"""

from __future__ import annotations

import json

import pytest
from motet_inference.accounting import StageUsage, collect_usage, usage_sink
from motet_inference.adapters import ClaudeIntegrator
from motet_inference.llm import (
    DEFAULT_MODEL,
    ConfigSource,
    FakeLlmClient,
    LlmConfigError,
    LlmStage,
    Usage,
    llm_overrides,
    load_config,
    usage_cost_usd,
)
from motet_inference.types import SourceItem

FAKE_ENV = {"MOTET_INFERENCE_MODE": "fake"}
HAIKU = "anthropic/claude-haiku-4.5"
OPUS = "anthropic/claude-opus-5"
SONNET_46 = "anthropic/claude-sonnet-4.6"


class TestPrecedence:
    """settings > MOTET_LLM_MODEL_<STAGE> > MOTET_LLM_MODEL > default, and likewise for effort."""

    def test_default_when_nothing_is_set(self) -> None:
        dedup = load_config(FAKE_ENV, overrides={}).for_stage(LlmStage.DEDUP)
        assert dedup.model == DEFAULT_MODEL
        assert dedup.model_source is ConfigSource.DEFAULT
        assert dedup.effort == "low"
        assert dedup.effort_source is ConfigSource.DEFAULT

    def test_global_env_beats_default(self) -> None:
        env = {**FAKE_ENV, "MOTET_LLM_MODEL": OPUS, "MOTET_LLM_EFFORT": "high"}
        dedup = load_config(env, overrides={}).for_stage(LlmStage.DEDUP)
        assert (dedup.model, dedup.model_source) == (OPUS, ConfigSource.GLOBAL_ENV)
        assert (dedup.effort, dedup.effort_source) == ("high", ConfigSource.GLOBAL_ENV)

    def test_stage_env_beats_global_env(self) -> None:
        env = {
            **FAKE_ENV,
            "MOTET_LLM_MODEL": OPUS,
            "MOTET_LLM_MODEL_DEDUP": SONNET_46,
            "MOTET_LLM_EFFORT": "high",
            "MOTET_LLM_EFFORT_DEDUP": "medium",
        }
        config = load_config(env, overrides={})
        dedup = config.for_stage(LlmStage.DEDUP)
        assert (dedup.model, dedup.model_source) == (SONNET_46, ConfigSource.STAGE_ENV)
        assert (dedup.effort, dedup.effort_source) == ("medium", ConfigSource.STAGE_ENV)
        # The other stages still see the global.
        script = config.for_stage(LlmStage.SCRIPT)
        assert (script.model, script.model_source) == (OPUS, ConfigSource.GLOBAL_ENV)

    def test_settings_beat_everything(self) -> None:
        env = {
            **FAKE_ENV,
            "MOTET_LLM_MODEL": OPUS,
            "MOTET_LLM_MODEL_DEDUP": SONNET_46,
            "MOTET_LLM_EFFORT_DEDUP": "medium",
        }
        settings = {"llm.model.dedup": HAIKU, "llm.effort.dedup": "off"}
        dedup = load_config(env, overrides=settings).for_stage(LlmStage.DEDUP)
        assert (dedup.model, dedup.model_source) == (HAIKU, ConfigSource.SETTINGS)
        assert (dedup.effort, dedup.effort_source) == (None, ConfigSource.SETTINGS)

    def test_a_setting_for_one_axis_leaves_the_other_alone(self) -> None:
        env = {**FAKE_ENV, "MOTET_LLM_EFFORT_SCRIPT": "medium"}
        script = load_config(env, overrides={"llm.model.script": OPUS}).for_stage(LlmStage.SCRIPT)
        assert (script.model, script.model_source) == (OPUS, ConfigSource.SETTINGS)
        assert (script.effort, script.effort_source) == ("medium", ConfigSource.STAGE_ENV)

    def test_settings_are_validated_like_the_environment(self) -> None:
        with pytest.raises(LlmConfigError, match="llm.model.dedup"):
            load_config(FAKE_ENV, overrides={"llm.model.dedup": "anthropic/claude-typo"})
        # An effort on a model with none is the misconfiguration the catalogue exists for.
        with pytest.raises(LlmConfigError, match="no selectable effort"):
            load_config(FAKE_ENV, overrides={"llm.model.dedup": HAIKU})
        with pytest.raises(LlmConfigError, match="llm.effort.dedup"):
            load_config(FAKE_ENV, overrides={"llm.effort.dedup": "enormous"})

    def test_the_context_manager_is_the_worker_shaped_way_in(self) -> None:
        """A stage adapter calls ``load_config()`` with no arguments; this is what it sees."""
        assert load_config(FAKE_ENV).for_stage(LlmStage.DEDUP).model == DEFAULT_MODEL
        with llm_overrides({"llm.model.dedup": OPUS}):
            assert load_config(FAKE_ENV).for_stage(LlmStage.DEDUP).model == OPUS
            # An explicit mapping still wins over the installed one.
            assert (
                load_config(FAKE_ENV, overrides={}).for_stage(LlmStage.DEDUP).model == DEFAULT_MODEL
            )
        assert load_config(FAKE_ENV).for_stage(LlmStage.DEDUP).model == DEFAULT_MODEL

    def test_every_stage_in_the_enum_has_a_settings_key(self) -> None:
        for stage in LlmStage:
            assert stage.model_setting == f"llm.model.{stage.value}"
            assert stage.effort_setting == f"llm.effort.{stage.value}"


class TestCost:
    def test_cache_is_priced_at_its_own_rate_and_reasoning_is_inside_output(self) -> None:
        # Sonnet 5: $2 in, $10 out, $0.20 cache read, $2.50 cache write, per Mtok.
        usage = Usage(
            input_tokens=1_000_000,
            output_tokens=100_000,
            reasoning_tokens=40_000,
            cache_read_tokens=400_000,
            cache_write_tokens=100_000,
        )
        # 500k uncached × 2 + 400k × 0.20 + 100k × 2.50 + 100k × 10 = 1.00 + .08 + .25 + 1.00
        assert usage_cost_usd(DEFAULT_MODEL, usage) == pytest.approx(2.33)

    def test_an_unknown_slug_costs_nothing_rather_than_raising(self) -> None:
        assert usage_cost_usd("vendor/unlisted", Usage(input_tokens=10, output_tokens=10)) == 0.0

    def test_zero_usage_is_free(self) -> None:
        assert usage_cost_usd(OPUS, Usage()) == 0.0


class TestUsageSink:
    def test_a_real_adapter_hands_each_completion_to_the_sink(self) -> None:
        """The shape the worker's ledger relies on: one call per completion, stage-tagged."""
        client = FakeLlmClient(
            responses={
                "": json.dumps(
                    {
                        "closest_news_item_id": None,
                        "relation": "unrelated",
                        "reason": "Empty backlog.",
                        "title": "Acme",
                        "summary": "Acme raised money.",
                    }
                )
            }
        )
        rows: list[StageUsage] = []
        with usage_sink(rows.append), collect_usage() as spend:
            ClaudeIntegrator(client).integrate(
                SourceItem(id="si_1", title="Acme", text="Acme."), []
            )

        assert len(rows) == 1
        (row,) = rows
        assert row.stage is LlmStage.DEDUP
        assert row.model == DEFAULT_MODEL
        assert row.usage.input_tokens > 0
        # The sink and the ledger see the same entry — neither is derived from the other.
        assert spend.entries == rows

    def test_no_sink_outside_the_block(self) -> None:
        client = FakeLlmClient(
            responses={
                "": json.dumps(
                    {
                        "closest_news_item_id": None,
                        "relation": "unrelated",
                        "reason": "",
                        "title": "A",
                        "summary": "B",
                    }
                )
            }
        )
        rows: list[StageUsage] = []
        with usage_sink(rows.append):
            pass
        ClaudeIntegrator(client).integrate(SourceItem(id="si_1", title="Acme", text="Acme."), [])
        assert rows == []
