"""What a conversational turn spent — motet#58.

Two questions, two shapes, exactly as ``motet_inference.accounting`` splits them for the
pipeline stages:

* *"How is the voice fleet doing?"* is ``motet.llm.tokens{stage="voice"}``, and it carries
  no session id. Until this landed the series did not exist at all, so a Grafana panel split
  by ``stage`` showed three of the enum's four members and a voice fleet spending money
  looked exactly like a voice fleet nobody had used.
* *"What did **that** session cost?"* is a log line, because the answer needs an id in it
  and ids are what a metric must not carry.

Driven through the **real** ``LlmConversationModel`` over ``FakeLlmClient``, for the reason
``inference/tests/test_accounting.py`` gives: the fake conversation model calls no model and
so correctly reports no cost, and a test built on it would pass with the recording call
missing.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

import pytest
from motet_inference.accounting import collect_usage
from motet_inference.fakes import FakeSpeechSynthesizer
from motet_inference.llm import (
    FakeLlmClient,
    LlmBudgetExhaustedError,
    LlmClient,
    LlmRequest,
    LlmResponse,
    LlmStage,
    Usage,
    load_config,
)
from motet_voice.contract import StartSessionRequest
from motet_voice.realtime.composed import ComposedArm, LlmConversationModel
from motet_voice.session import VoiceSession
from motet_voice.tools import ToolRegistry

CONFIG = StartSessionRequest.model_validate(
    {
        "persona": {"name": "Briefing", "instructions": "Be brief."},
        "context": {"notes": "Acme raised $20M led by Northwind Ventures."},
    }
)


def llm_model(client: LlmClient | None = None) -> LlmConversationModel:
    """The real LLM leg, wired to the fake client. No vendor, no key, no spend."""
    config = load_config({"MOTET_INFERENCE_MODE": "fake"})
    return LlmConversationModel(client=client or FakeLlmClient(), config=config)


def session(
    model: LlmConversationModel | ComposedArm, *, session_id: str = "vs_cost"
) -> VoiceSession:
    arm = (
        model
        if isinstance(model, ComposedArm)
        else ComposedArm(model=model, synthesizer=FakeSpeechSynthesizer(), conversational=True)
    )
    return VoiceSession.create(
        session_id=session_id, config=CONFIG, arm=arm, tools=ToolRegistry({})
    )


class _FailingSynthesisArm(ComposedArm):
    """The LLM leg answers — and is billed — and the TTS leg after it raises."""

    def __init__(self, model: LlmConversationModel) -> None:
        super().__init__(model=model, conversational=True)

    async def respond(self, request: Any) -> Any:
        self.model.reply(request, request.user_text or "")
        raise RuntimeError("synthesis fell over after the reply was billed")


async def _turns(voice_session: VoiceSession, *texts: str) -> None:
    for text in texts:
        await voice_session.respond_to_text(text)


class TestTheFleetWideHalf:
    def test_a_turn_puts_a_stage_voice_series_on_the_obs_stack(self, metrics: Any) -> None:
        """The series the enum's fourth member had no points on.

        Read back through a real in-memory reader rather than by asserting a function was
        called: the claim is that a point with these attributes leaves the process, and a
        mock would pass for a counter never wired to a meter at all.
        """
        asyncio.run(_turns(session(llm_model()), "who led the round"))

        voice = [
            point
            for point in metrics.points("motet.llm.tokens")
            if point.attributes and point.attributes.get("stage") == LlmStage.VOICE.value
        ]
        assert voice, 'nothing was exported under stage="voice"'

        # The same three labels the pipeline stages carry, and the same kinds.
        for point in voice:
            assert point.attributes is not None
            assert set(point.attributes) == {"stage", "model", "kind"}
            assert point.attributes["model"]
        kinds = {point.attributes["kind"] for point in voice}
        assert {"input", "output"} <= kinds

        requests = [
            point
            for point in metrics.points("motet.llm.requests")
            if point.attributes and point.attributes.get("stage") == LlmStage.VOICE.value
        ]
        assert requests, "a completion was made and motet.llm.requests did not count it"

    def test_the_stage_label_is_voice_and_the_model_is_the_one_configured(self) -> None:
        """The ledger is the same evidence without the metrics pipeline in the way."""
        model = llm_model()

        with collect_usage() as spend:
            model.reply(_turn_request(), "who led the round")

        (entry,) = spend.entries
        assert entry.stage is LlmStage.VOICE
        assert entry.model == model.model
        assert entry.usage.input_tokens > 0
        assert entry.usage.output_tokens > 0

    def test_a_billed_turn_that_produced_nothing_is_still_counted(self) -> None:
        """Billed and useless is still billed.

        Reachable on this path only once somebody sets ``MOTET_LLM_EFFORT_VOICE`` — a voice
        turn sends no ``response_format``, so the empty-answer-at-``length`` branch is the
        one that raises — and it is the most expensive turn there is, which is exactly why
        leaving it out would put the costliest completions where no metric sees them.
        """
        model = llm_model(_ExhaustedClient())

        with collect_usage() as spend, pytest.raises(LlmBudgetExhaustedError):
            model.reply(_turn_request(), "who led the round")

        (entry,) = spend.entries
        assert entry.stage is LlmStage.VOICE
        assert entry.usage.reasoning_tokens == 400


class TestThePerSessionHalf:
    def test_the_session_summary_carries_what_its_turns_cost(self) -> None:
        """The line keyed by session id — the voice shape of "what did that one cost"."""
        voice_session = session(llm_model())

        asyncio.run(_turns(voice_session, "who led the round", "how much was it"))

        summary = voice_session.summary()
        assert summary["session_id"] == "vs_cost"
        assert summary["llm_completions"] == 2
        assert summary["llm_tokens"].startswith("input=")
        # Every field every time, zeros included: a field that vanishes when it is zero is
        # a field a log query cannot aggregate.
        assert "cache_read=0" in summary["llm_tokens"]

    def test_the_total_is_the_sum_of_the_turns(self) -> None:
        voice_session = session(llm_model())

        asyncio.run(_turns(voice_session, "who led the round", "how much was it"))

        assert [entry.stage for entry in voice_session.spend.entries] == [
            LlmStage.VOICE,
            LlmStage.VOICE,
        ]
        total = voice_session.spend.total()
        assert total.input_tokens == sum(e.usage.input_tokens for e in voice_session.spend.entries)
        assert total.output_tokens > 0

    def test_each_turn_logs_its_own_cost_beside_the_session_id(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        """Per turn, because a turn is the unit a person waits on and would be priced by."""
        voice_session = session(llm_model(), session_id="vs_walk")

        with caplog.at_level(logging.INFO, logger="motet.voice.session"):
            asyncio.run(_turns(voice_session, "who led the round", "how much was it"))

        lines = [line for line in caplog.messages if line.startswith("voice turn cost")]
        assert len(lines) == 2
        assert "session=vs_walk" in lines[0]
        assert "turn=1" in lines[0]
        assert "turn=2" in lines[1]
        assert "cache_read=" in lines[1]

    def test_an_arm_with_no_llm_leg_reports_a_seam_that_was_never_used(self) -> None:
        """The fake conversation model calls nothing, so there is nothing to attribute.

        The same property ``inference/tests/test_accounting.py`` relies on for the stage
        fakes: no completion, no entry, and no log line claiming a turn was billed.

        The zero is about **the LLM seam**, not about the session's bill — the realtime arm
        spends through its own provider socket and is not in this number. ``summary`` says
        so at the field; the name of this test is deliberately not "reports no cost".
        """
        voice_session = VoiceSession.create(
            session_id="vs_fake",
            config=CONFIG,
            arm=ComposedArm(synthesizer=FakeSpeechSynthesizer(), conversational=True),
            tools=ToolRegistry({}),
        )

        asyncio.run(_turns(voice_session, "who led the round"))

        assert voice_session.spend.requests == 0
        assert voice_session.summary()["llm_completions"] == 0

    def test_a_turn_that_failed_after_its_completion_still_reports_what_it_spent(self) -> None:
        """The reply arrived and was billed; the leg after it fell over.

        The completion is on the metric either way — it is recorded where the call is made
        — but the session total is the only place the id meets the number, so folding it in
        only on the happy path would lose exactly the turns worth asking about.
        """
        voice_session = session(_FailingSynthesisArm(llm_model()))

        with pytest.raises(RuntimeError):
            asyncio.run(voice_session.respond_to_text("who led the round"))

        assert voice_session.spend.requests == 1
        assert voice_session.spend.entries[0].stage is LlmStage.VOICE


def _turn_request() -> Any:
    from motet_voice.realtime import TurnRequest

    return TurnRequest(
        persona_instructions="You are Motet.",
        voice="sonic-en",
        user_text="who led the round",
        context_notes="Acme raised $20M led by Northwind Ventures.",
        history=[],
        tools=[],
    )


def _voice_requests(metrics: Any) -> int:
    return sum(
        point.value
        for point in metrics.points("motet.llm.requests")
        if point.attributes and point.attributes.get("stage") == LlmStage.VOICE.value
    )


class _ExhaustedClient:
    """A client that spends the whole ceiling on reasoning and returns no answer."""

    def complete(self, request: LlmRequest) -> LlmResponse:
        raise LlmBudgetExhaustedError(
            "spent the whole budget before writing an answer",
            usage=Usage(input_tokens=120, output_tokens=0, reasoning_tokens=400),
            model=request.model,
        )
