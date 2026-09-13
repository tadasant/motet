"""The settings rung, and pricing what a completion cost (motet#92).

Three properties the admin screen rests on, each pinned without a database: the
precedence chain resolves and *says* which rung won; a ``settings`` row is gated more
strictly than an environment variable; and a completion is priced at the rate it was
actually billed at — including a cache write at the TTL the request asked for, and a model
reported under its dated snapshot.
"""

from __future__ import annotations

import json

import pytest
from motet_inference.accounting import StageUsage, record_usage, usage_sink
from motet_inference.adapters import (
    CONFIRM_MAX_TOKENS,
    INTEGRATE_MAX_TOKENS,
    SCRIPT_MAX_TOKENS,
    ClaudeIntegrator,
)
from motet_inference.llm import (
    ALLOW_UNLISTED_ENV,
    DEFAULT_MODEL,
    KNOWN_MODELS,
    STAGES_CACHING_ONE_HOUR,
    CacheControl,
    ConfigSource,
    FakeLlmClient,
    LlmConfigError,
    LlmRequest,
    LlmResponse,
    LlmStage,
    Message,
    TextPart,
    Usage,
    find_model,
    llm_overrides,
    load_config,
    models_for,
    usage_cost_usd,
    validate_overrides,
)
from motet_inference.llm.check_models import PRICE_FIELDS, drift
from motet_inference.prompts import integrate_messages, script_messages, second_look_messages
from motet_inference.types import NewsItem, SourceItem

OPUS = "anthropic/claude-opus-5"
HAIKU = "anthropic/claude-haiku-4.5"
SONNET_46 = "anthropic/claude-sonnet-4.6"
MODE = {"MOTET_INFERENCE_MODE": "fake"}


class TestPrecedence:
    def test_each_rung_wins_over_the_ones_below_it_and_says_so(self) -> None:
        env = {**MODE}
        dedup = load_config(env).for_stage(LlmStage.DEDUP)
        assert (dedup.model, dedup.model_source) == (DEFAULT_MODEL, ConfigSource.DEFAULT)
        assert (dedup.effort, dedup.effort_source) == ("low", ConfigSource.DEFAULT)

        env["MOTET_LLM_MODEL"] = SONNET_46
        env["MOTET_LLM_EFFORT"] = "medium"
        dedup = load_config(env).for_stage(LlmStage.DEDUP)
        assert (dedup.model, dedup.model_source) == (SONNET_46, ConfigSource.GLOBAL_ENV)
        assert (dedup.effort, dedup.effort_source) == ("medium", ConfigSource.GLOBAL_ENV)

        env["MOTET_LLM_MODEL_DEDUP"] = OPUS
        env["MOTET_LLM_EFFORT_DEDUP"] = "high"
        dedup = load_config(env).for_stage(LlmStage.DEDUP)
        assert (dedup.model, dedup.model_source) == (OPUS, ConfigSource.STAGE_ENV)
        assert (dedup.effort, dedup.effort_source) == ("high", ConfigSource.STAGE_ENV)

        rows = {"llm.model.dedup": HAIKU, "llm.effort.dedup": "off"}
        dedup = load_config(env, overrides=rows).for_stage(LlmStage.DEDUP)
        assert (dedup.model, dedup.model_source) == (HAIKU, ConfigSource.SETTINGS)
        assert (dedup.effort, dedup.effort_source) == (None, ConfigSource.SETTINGS)

        # A row for one stage moves that stage and nothing else.
        script = load_config(env, overrides=rows).for_stage(LlmStage.SCRIPT)
        assert script.model_source is ConfigSource.GLOBAL_ENV

    def test_the_context_var_is_consulted_only_inside_the_block(self) -> None:
        rows = {"llm.model.script": OPUS}
        assert load_config(MODE).for_stage(LlmStage.SCRIPT).model == DEFAULT_MODEL
        with llm_overrides(rows):
            assert load_config(MODE).for_stage(LlmStage.SCRIPT).model == OPUS
            # Nested blocks shadow rather than merge.
            with llm_overrides({}):
                assert load_config(MODE).for_stage(LlmStage.SCRIPT).model == DEFAULT_MODEL
            assert load_config(MODE).for_stage(LlmStage.SCRIPT).model == OPUS
        assert load_config(MODE).for_stage(LlmStage.SCRIPT).model == DEFAULT_MODEL

    def test_an_explicit_empty_mapping_beats_the_context_var(self) -> None:
        with llm_overrides({"llm.model.script": OPUS}):
            assert load_config(MODE, overrides={}).for_stage(LlmStage.SCRIPT).model == (
                DEFAULT_MODEL
            )


class TestASettingsRowIsGatedHarderThanEnv:
    def test_a_valid_row_resolves(self) -> None:
        config = validate_overrides({"llm.model.dedup": HAIKU, "llm.effort.dedup": "off"}, MODE)
        assert config.for_stage(LlmStage.DEDUP).model == HAIKU

    def test_an_unlisted_slug_is_refused_even_where_env_may_use_one(self) -> None:
        env = {**MODE, ALLOW_UNLISTED_ENV: "true", "MOTET_LLM_MODEL": "vendor/brand-new"}
        load_config(env)  # the environment may, for the hour the escape hatch exists for
        with pytest.raises(LlmConfigError, match="not in the model catalogue"):
            validate_overrides({"llm.model.dedup": "vendor/brand-new"}, env)

    def test_an_effort_the_slug_cannot_take_is_refused_naming_the_row(self) -> None:
        with pytest.raises(LlmConfigError, match="no selectable effort"):
            validate_overrides({"llm.model.dedup": HAIKU}, MODE)  # dedup defaults to low
        with pytest.raises(LlmConfigError, match="llm.effort.script"):
            validate_overrides({"llm.effort.script": "turbo"}, MODE)

    def test_a_key_that_names_no_stage_is_ignored_rather_than_poisoning_the_rest(
        self,
    ) -> None:
        """What a removed stage leaves behind. Refusing it would disable every other row and
        block every save, with no route that could delete it."""
        config = validate_overrides(
            {"llm.model.grounding": "vendor/withdrawn", "llm.model.script": OPUS}, MODE
        )
        assert config.for_stage(LlmStage.SCRIPT).model == OPUS

    def test_a_model_that_cannot_cache_for_an_hour_is_refused_for_dedup(self) -> None:
        """Every dedup request asks for a 1h cache TTL; gpt-5.1 has none, so the pairing
        would have every paste refused at request time. Refused for env and row alike."""
        with pytest.raises(LlmConfigError, match="1h cache TTL"):
            validate_overrides({"llm.model.dedup": "openai/gpt-5.1"}, MODE)
        with pytest.raises(LlmConfigError, match="MOTET_LLM_MODEL_DEDUP"):
            load_config({**MODE, "MOTET_LLM_MODEL_DEDUP": "openai/gpt-5.1"})
        validate_overrides({"llm.model.script": "openai/gpt-5.1"}, MODE)
        assert "openai/gpt-5.1" not in models_for(LlmStage.DEDUP)
        assert "openai/gpt-5.1" in models_for(LlmStage.SCRIPT)

    def test_keys_outside_the_llm_namespace_are_not_its_business(self) -> None:
        validate_overrides({"other.thing": "x"}, MODE)


def _request(*ttls: str | None) -> LlmRequest:
    parts = tuple(
        TextPart(text=f"part {i}", cache=None if ttl is None else CacheControl(ttl=ttl))  # type: ignore[arg-type]
        for i, ttl in enumerate(ttls)
    )
    return LlmRequest(
        model=DEFAULT_MODEL, messages=(Message(role="user", parts=parts),), max_output_tokens=10
    )


class TestPricing:
    def test_a_request_reports_the_longest_ttl_it_asked_for(self) -> None:
        assert _request(None).cache_ttl is None
        assert _request("5m").cache_ttl == "5m"
        assert _request("5m", "1h").cache_ttl == "1h"

    def test_the_fake_carries_the_ttl_onto_the_response(self) -> None:
        assert FakeLlmClient().complete(_request("1h")).cache_ttl == "1h"

    def test_each_token_kind_is_billed_at_its_own_rate(self) -> None:
        usage = Usage(
            input_tokens=1_000_000,  # of which 200k read from cache and 300k written to it
            cache_read_tokens=200_000,
            cache_write_tokens=300_000,
            output_tokens=100_000,
            reasoning_tokens=40_000,  # already inside output_tokens; not billed twice
        )
        # Sonnet 5: $2 in, $10 out, $0.20 cache read, $2.50 (5m) / $4.00 (1h) cache write.
        base = 0.5 * 2.0 + 0.2 * 0.2 + 0.1 * 10.0
        assert usage_cost_usd(DEFAULT_MODEL, usage, "5m") == pytest.approx(base + 0.3 * 2.5)
        assert usage_cost_usd(DEFAULT_MODEL, usage, "1h") == pytest.approx(base + 0.3 * 4.0)

    def test_a_dated_snapshot_is_priced_as_the_slug_it_is_a_snapshot_of(self) -> None:
        """OpenRouter reports the served snapshot, in a shape no suffix rule recovers."""
        usage = Usage(input_tokens=1_000_000)
        assert usage_cost_usd("anthropic/claude-4.6-sonnet-20260217", usage) == pytest.approx(3.0)
        assert find_model("anthropic/claude-4.6-sonnet-20260217") is KNOWN_MODELS[SONNET_46]

    def test_an_unknown_model_is_unpriced_rather_than_free(self) -> None:
        assert usage_cost_usd("vendor/brand-new", Usage(input_tokens=5)) is None

    def test_every_catalogue_row_has_a_price_and_a_snapshot(self) -> None:
        for spec in KNOWN_MODELS.values():
            assert spec.canonical_slug, spec.slug
            assert spec.input_usd_per_mtok > 0 and spec.output_usd_per_mtok > 0, spec.slug


class TestTheSinkSeesEachCompletion:
    def test_dedup_hands_one_entry_with_its_one_hour_ttl(self) -> None:
        client = FakeLlmClient(
            responses={
                "": json.dumps(
                    {
                        "closest_news_item_id": None,
                        "relation": "unrelated",
                        "reason": "New.",
                        "title": "Acme raises",
                        "summary": "Acme raised.",
                    }
                )
            }
        )
        seen: list[StageUsage] = []
        with usage_sink(seen.append):
            ClaudeIntegrator(client).integrate(
                SourceItem(id="si_1", title="Acme", text="Acme raises $20M."), []
            )
        assert [(entry.stage, entry.cache_ttl) for entry in seen] == [(LlmStage.DEDUP, "1h")]
        assert seen[0].usage.input_tokens > 0

    def test_a_sink_that_raises_costs_only_its_own_entry(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        def broken(_: StageUsage) -> None:
            raise RuntimeError("disk full")

        with usage_sink(broken):
            record_usage(LlmStage.SCRIPT, LlmResponse(text="x", model=DEFAULT_MODEL))
        assert "usage sink raised" in caplog.text


class TestDriftCheck:
    """``bin/check-openrouter-models`` compares prices offline-testably, via ``drift``."""

    @staticmethod
    def _live(slug: str) -> dict[str, object]:
        spec = KNOWN_MODELS[slug]
        pricing = {
            key: str(getattr(spec, field) / 1_000_000)
            for field, key in PRICE_FIELDS
            if getattr(spec, field)
        }
        return {
            "id": slug,
            "canonical_slug": spec.canonical_slug,
            "context_length": spec.context_tokens,
            "reasoning": {
                "supported_efforts": list(spec.efforts),
                "default_enabled": spec.reasoning_on_by_default,
            },
            "pricing": pricing,
        }

    def test_a_matching_entry_has_no_drift(self) -> None:
        for slug in KNOWN_MODELS:
            assert drift(KNOWN_MODELS[slug], self._live(slug)) == [], slug

    def test_a_moved_price_and_a_moved_snapshot_are_reported(self) -> None:
        live = self._live(DEFAULT_MODEL)
        live["pricing"] = {**live["pricing"], "completion": "0.000012"}  # type: ignore[dict-item]
        live["canonical_slug"] = "anthropic/claude-sonnet-5-20261231"
        notes = drift(KNOWN_MODELS[DEFAULT_MODEL], live)
        assert any(note.startswith("output_usd_per_mtok 10 -> 12") for note in notes), notes
        assert any("canonical slug" in note for note in notes), notes


class TestAValidRowCannotProduceARefusedRequest:
    """``validate_overrides`` is only a gate if it covers every check ``build_request``
    makes. The two request-shaped checks are the cache TTL and the output ceiling; these
    pin both to what the stages really send, so a new breakpoint or a bigger ceiling
    cannot quietly reopen "a saved dropdown fails every job"."""

    ITEM = SourceItem(id="si_1", title="Acme", text="Acme raises $20M.")
    STORY = NewsItem(id="ni_1", title="Acme", summary="Acme raised.", source_item_ids=("si_1",))

    def test_the_one_hour_stages_are_the_ones_whose_prompts_cache_for_an_hour(self) -> None:
        built = {
            LlmStage.DEDUP: integrate_messages(self.ITEM, [self.STORY]),
            LlmStage.DEDUP_CONFIRM: second_look_messages(self.ITEM, self.STORY),
            LlmStage.SCRIPT: script_messages([self.STORY], {"si_1": self.ITEM}),
        }
        for stage, messages in built.items():
            request = LlmRequest(model=DEFAULT_MODEL, messages=messages, max_output_tokens=1)
            assert (request.cache_ttl == "1h") == (stage in STAGES_CACHING_ONE_HOUR), stage

    def test_every_stage_ceiling_fits_every_catalogue_model(self) -> None:
        smallest = min(spec.max_output_tokens for spec in KNOWN_MODELS.values())
        assert max(INTEGRATE_MAX_TOKENS, CONFIRM_MAX_TOKENS, SCRIPT_MAX_TOKENS) <= smallest
