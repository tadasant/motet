"""Contract tests for the inference seam.

These assert the properties invariant 7 depends on — that the fakes satisfy the Protocols,
that they are deterministic, and that the registry defaults to fake — rather than testing
any particular briefing's wording.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import replace

import pytest
from motet_inference import (
    MODE_ENV_VAR,
    GroundingValidator,
    Integrator,
    ScriptGenerator,
    SourceItem,
    SpeechSynthesizer,
    build_briefing,
    fake_stages,
    get_stages,
)
from motet_inference.interfaces import IntegrationResult
from motet_inference.types import Claim, NewsItem, Script, ScriptSegment, SourceSpan

ITEM_A = SourceItem(id="si_a", title="Acme raises $20M", text="Acme raised $20M. More text.")
ITEM_A_DUP = SourceItem(id="si_b", title="$20M — ACME Raises!", text="Acme's round closed.")
ITEM_C = SourceItem(id="si_c", title="Beta ships v2", text="Beta shipped v2 today. Notes.")


def test_fakes_satisfy_the_protocols() -> None:
    stages = fake_stages()
    assert isinstance(stages.integrator, Integrator)
    assert isinstance(stages.script_generator, ScriptGenerator)
    assert isinstance(stages.grounding_validator, GroundingValidator)
    assert isinstance(stages.speech_synthesizer, SpeechSynthesizer)


def test_registry_defaults_to_fake(monkeypatch: pytest.MonkeyPatch) -> None:
    """A missing mode must fail toward the free, offline implementations."""
    monkeypatch.delenv(MODE_ENV_VAR, raising=False)
    stages = get_stages()
    assert type(stages.integrator).__name__ == "FakeIntegrator"


def test_registry_rejects_an_unknown_mode(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(MODE_ENV_VAR, "sortof")
    with pytest.raises(ValueError, match="must be 'fake' or 'real'"):
        get_stages()


def test_real_stages_refuse_to_build_without_vendor_configuration(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The real stages exist now, and they fail *before* doing any work without a key.

    This is the safe direction for the mode switch to fail in: asking for real adapters in
    an environment that cannot pay for them stops at construction, not partway through an
    episode. The fake path below needs no configuration at all, which is what lets CI run
    the whole pipeline (invariant 7).

    The LLM credential is what fails, and only that one: TTS credentials are checked by the
    TTS worker's own entry point, so a dedup worker never has to hold a secret it does not
    use. The mode is set rather than passed as an argument because `get_stages(mode=...)`
    selects *stages* — the model still follows `MOTET_INFERENCE_MODE`, so passing it would
    prove something weaker than it looks.
    """
    monkeypatch.setenv("MOTET_INFERENCE_MODE", "real")
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    with pytest.raises(Exception, match="OPENROUTER_API_KEY"):
        get_stages()

    # And the fake path builds with nothing set.
    monkeypatch.setenv("MOTET_INFERENCE_MODE", "fake")
    assert get_stages(mode="fake").speech_synthesizer.synthesize("hello there").duration_ms > 0


def test_integrate_dedups_titles_differing_only_in_case_order_and_punctuation() -> None:
    briefing = build_briefing([ITEM_A, ITEM_A_DUP, ITEM_C], fake_stages())
    assert len(briefing.news_items) == 2
    first = briefing.news_items[0]
    assert first.source_item_ids == ("si_a", "si_b")


def test_briefing_is_deterministic() -> None:
    stages = fake_stages()
    first = build_briefing([ITEM_A, ITEM_A_DUP, ITEM_C], stages)
    second = build_briefing([ITEM_A, ITEM_A_DUP, ITEM_C], stages)
    assert first == second


def test_generated_script_is_grounded() -> None:
    briefing = build_briefing([ITEM_A, ITEM_C], fake_stages())
    assert briefing.grounding.ok
    assert briefing.speakable


def test_validator_rejects_a_claim_its_span_does_not_support() -> None:
    """The check that makes invariant 3 enforceable, exercised directly."""
    fabricated = Script(
        segments=(
            ScriptSegment(
                news_item_id="ni_x",
                claims=(
                    Claim(
                        text="Acme raised $900M.",
                        span=SourceSpan(source_item_id="si_a", start=0, end=18),
                    ),
                ),
            ),
        )
    )
    report = fake_stages().grounding_validator.validate(fabricated, {"si_a": ITEM_A})
    assert not report.ok
    assert "does not match" in report.failures[0].reason


def test_validator_rejects_a_span_pointing_at_a_missing_source() -> None:
    orphan = Script(
        segments=(
            ScriptSegment(
                news_item_id="ni_x",
                claims=(Claim(text="x", span=SourceSpan("si_missing", 0, 1)),),
            ),
        )
    )
    report = fake_stages().grounding_validator.validate(orphan, {})
    assert not report.ok
    assert "does not resolve" in report.failures[0].reason


def test_synthesizer_returns_wav_bytes_with_a_duration() -> None:
    audio = fake_stages().speech_synthesizer.synthesize("one two three four five")
    assert audio.media_type == "audio/wav"
    assert audio.data.startswith(b"RIFF")
    assert audio.duration_ms > 0


def test_a_lying_integrator_fails_loudly_rather_than_duplicating() -> None:
    """An integrator that claims a merge into an item not in the window is a bug.

    Appending instead would put two news items with the same id in the window, and the
    story would be spoken twice — a failure that surfaces far from its cause.
    """

    class LyingIntegrator:
        def integrate(self, item: SourceItem, window: Sequence[NewsItem]) -> IntegrationResult:
            return IntegrationResult(
                news_item=NewsItem(id="ni_never_added", title="t", summary="s", source_item_ids=()),
                merged=True,
            )

    stages = replace(fake_stages(), integrator=LyingIntegrator())
    with pytest.raises(ValueError, match="unknown news item"):
        build_briefing([ITEM_A], stages)
