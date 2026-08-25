"""Grounding on the conversational path: advisory, but never absent — motet#10.

The decision Tadas made is that a conversational reply is not gated on grounding the way
narration is. The risk in implementing that decision is not that the gate comes back; it is
that "advisory" quietly decays into "nothing runs". So these tests assert the three things
that keep the difference real:

* the reply is **still checked** — a fabricated number is found, a sourced one is not;
* the check **does not block the audio** — an ungrounded reply is still spoken;
* the verdict **survives the turn** — as a counter an operator queries, a warning carrying
  the offending text, an event on the wire, and a count in the session summary.

Nothing here reaches a vendor: the checker is ours and deterministic, and the arm is the
composed one with a scripted model leg.
"""

from __future__ import annotations

import asyncio
import logging
import threading
from collections.abc import Mapping
from dataclasses import dataclass, replace
from typing import Any

import pytest
from motet_inference.fakes import FakeSpeechSynthesizer
from motet_voice import obs
from motet_voice.contract import StartSessionRequest
from motet_voice.grounding import (
    GroundingVerdict,
    SpecificsGroundingChecker,
    build_grounding_checker,
    material_for,
)
from motet_voice.realtime import AssistantTurn, PendingToolCall, TurnRequest
from motet_voice.realtime.composed import ComposedArm
from motet_voice.session import VoiceSession
from motet_voice.tools import AVAILABLE, ToolAvailability, ToolRegistry, ToolResult

#: What the caller that owns the database put in the session config. Narration that already
#: passed invariant 3's hard gate on the batch path.
MATERIAL = (
    "Helion raised 425 million dollars in a round led by SoftBank, the company said on "
    "Tuesday. The reactor is scheduled for 2028."
)

CHECKER = SpecificsGroundingChecker()


# -- the checker itself -----------------------------------------------------------------


def test_a_reply_drawn_from_the_material_is_grounded() -> None:
    verdict = CHECKER.check("Helion raised 425 million, led by SoftBank.", MATERIAL)
    assert verdict.grounded
    assert verdict.checked > 0, "a grounded verdict over no specifics would prove nothing"


def test_a_number_the_material_does_not_contain_is_named() -> None:
    """Invariant 3's own stated failure mode — an invented funding number — arriving by voice."""
    verdict = CHECKER.check("Helion raised 900 million dollars.", MATERIAL)
    assert not verdict.grounded
    assert [(item.kind, item.text) for item in verdict.unsupported] == [("number", "900")]
    assert "900" in verdict.summarize()


def test_a_name_nobody_mentioned_is_unsupported() -> None:
    verdict = CHECKER.check("The round was led by Sequoia.", MATERIAL)
    assert [(item.kind, item.text) for item in verdict.unsupported] == [("name", "Sequoia")]


def test_a_fabricated_quotation_is_unsupported() -> None:
    verdict = CHECKER.check('They said "the reactor is already running".', MATERIAL)
    assert [item.kind for item in verdict.unsupported] == ["quote"]


def test_a_quotation_that_is_in_the_material_survives_a_line_wrap() -> None:
    """Material arrives as prose the caller assembled; a wrap in it is not a fabrication."""
    material = "Helion raised 425 million in a round\nled by SoftBank."
    assert CHECKER.check('It says "in a round led by SoftBank".', material).grounded


def test_a_refusal_checks_nothing_and_is_grounded() -> None:
    """The behaviour the system prompt asks for must not read as a check that failed to run."""
    verdict = CHECKER.check("I don't have that in front of me — want me to look it up?", MATERIAL)
    assert verdict.grounded
    assert verdict.checked == 0


def test_a_sentence_opening_word_is_not_mistaken_for_a_name() -> None:
    """Otherwise every reply is ungrounded and the signal is worth nothing."""
    assert CHECKER.check("Yes. Helion did. Tuesday, they said.", MATERIAL).grounded


def test_a_fabricated_name_is_caught_even_as_the_first_word_of_a_sentence() -> None:
    """The subject of a sentence is where a made-up name most often lands."""
    verdict = CHECKER.check("Sequoia led that round.", MATERIAL)
    assert [(item.kind, item.text) for item in verdict.unsupported] == [("name", "Sequoia")]


def test_a_repeated_fabrication_counts_once() -> None:
    verdict = CHECKER.check("Sequoia led it. Sequoia also led the last one.", MATERIAL)
    assert len(verdict.unsupported) == 1


def test_a_contraction_is_not_a_name_nobody_mentioned() -> None:
    """Without clitic stripping, almost every reply opening with one reads as ungrounded."""
    verdict = CHECKER.check("It's a big round. That's what Sequoia did.", MATERIAL)
    assert [(item.kind, item.text) for item in verdict.unsupported] == [("name", "Sequoia")]
    assert CHECKER.check("Let's see. Here's the thing: 425 million.", MATERIAL).grounded


def test_a_possessive_matches_the_name_in_the_material() -> None:
    """A false positive on a *correctly sourced* name is worse than the tolerated ones."""
    assert CHECKER.check("SoftBank's stake grew.", MATERIAL).grounded


def test_a_name_outside_latin_1_is_checked_rather_than_skipped() -> None:
    """An ASCII-only word pattern reports ``checked=0``, which reads as "nothing asserted"."""
    verdict = CHECKER.check("Zoë Müller led it.", MATERIAL)
    assert [item.text for item in verdict.unsupported] == ["Zoë", "Müller"]


def test_a_long_quotation_is_checked_and_does_not_manufacture_a_second_one() -> None:
    """A ceiling on the quote pattern restarts the engine on the closing mark."""
    long_quote = "the reactor is already running " * 12
    verdict = CHECKER.check(f'He said "{long_quote}" and then "a short one here".', MATERIAL)
    assert [item.kind for item in verdict.unsupported] == ["quote", "quote"]
    assert verdict.unsupported[0].text.startswith("the reactor")
    assert "and then" not in [item.text for item in verdict.unsupported]


def test_a_comma_is_not_a_different_number() -> None:
    assert CHECKER.check("They raised 425 million.", "a 425 million round").grounded
    assert CHECKER.check("1,200 users.", "1200 users signed up").grounded


# -- what counts as material ------------------------------------------------------------


def test_the_listeners_own_words_ground_a_reply_that_repeats_them() -> None:
    """An echo of the listener's number is grounded in the conversation, not a fabrication."""
    material = material_for(context_notes="", user_text="what about the 425 million round")
    assert CHECKER.check("The 425 million one, yes.", material).grounded


def test_a_tool_result_is_material() -> None:
    """``get_item_detail`` returns spans, which is what a grounded answer reaches for."""
    material = material_for(
        context_notes="",
        user_text="who else covered it",
        tool_results=["Bloomberg and Reuters both ran it"],
    )
    assert CHECKER.check("Bloomberg covered it too.", material).grounded


def test_a_prior_assistant_turn_is_never_material() -> None:
    """The load-bearing omission: a check must not launder its own misses.

    If replies were material, an ungrounded claim on turn three would ground the same claim
    on turn four, and the ungrounded rate would read clean for the rest of the walk.
    """
    material = material_for(context_notes=MATERIAL, user_text="say that again")
    assert "Sequoia" not in material
    assert not CHECKER.check("Sequoia led it.", material).grounded


# -- the session: advisory, and recorded ------------------------------------------------


@dataclass
class ScriptedModel:
    """A conversation leg that says exactly what a test wants said."""

    text: str

    @property
    def name(self) -> str:
        return "scripted"

    def reply(self, request: TurnRequest, user_text: str) -> str:
        return self.text


def _session(reply: str, *, notes: str = MATERIAL) -> VoiceSession:
    arm = ComposedArm(
        model=ScriptedModel(reply),
        synthesizer=FakeSpeechSynthesizer(),
        conversational=True,
    )
    config = StartSessionRequest.model_validate(
        {
            "persona": {"name": "Briefing", "instructions": "Be brief."},
            "context": {"notes": notes},
        }
    )
    return VoiceSession.create(
        session_id="s1", config=config, arm=arm, tools=ToolRegistry({}), grounding=CHECKER
    )


async def _one_turn(session: VoiceSession, text: str) -> list[Any]:
    """One turn, then wait for the advisory check that was scheduled behind it."""
    events = await session.respond_to_text(text)
    await session.drain_grounding_checks()
    return events


def test_a_grounded_reply_is_spoken_and_recorded_as_grounded() -> None:
    session = _session("Helion raised 425 million, led by SoftBank.")
    events = asyncio.run(_one_turn(session, "what was the number"))

    assert [event.type for event in events] == ["transcript", "transcript", "audio_chunk"]
    assert [verdict.grounded for verdict in session.verdicts] == [True]
    assert session.summary()["replies_checked"] == 1
    assert session.summary()["replies_ungrounded"] == 0


def test_an_ungrounded_reply_is_still_spoken(caplog: pytest.LogCaptureFixture) -> None:
    """**The decision, as a test.** The audio is produced; the verdict is recorded anyway.

    If somebody later turns this into a gate, this is the test that goes red — and the PR
    that does it has to argue with motet#10 rather than quietly re-tighten the invariant.
    """
    session = _session("Sequoia led the 900 million round.")
    with caplog.at_level(logging.WARNING, logger="motet.voice.session"):
        events = asyncio.run(_one_turn(session, "who led it"))

    spoken = [event for event in events if event.type == "audio_chunk"]
    assert len(spoken) == 1, "an ungrounded conversational reply is still spoken — motet#10"
    assert spoken[0].duration_ms > 0

    assert [verdict.grounded for verdict in session.verdicts] == [False]
    assert session.summary()["replies_ungrounded"] == 1

    warnings = [record.getMessage() for record in caplog.records]
    assert any("ungrounded conversational reply" in message for message in warnings)
    assert any("900" in message and "Sequoia" in message for message in warnings), (
        "the log carries the offending specifics; a counter cannot, without minting a "
        "time series per fabricated number"
    )


def test_the_verdict_reaches_the_client_as_its_own_event() -> None:
    session = _session("Sequoia led the 900 million round.")
    asyncio.run(_one_turn(session, "who led it"))

    event = session.outbox.get_nowait()
    assert event.type == "grounding"
    assert event.grounded is False
    assert event.checker == "specifics"
    assert {item["kind"] for item in event.unsupported} == {"name", "number"}
    assert event.reply == "Sequoia led the 900 million round."


def test_the_check_runs_behind_the_turn_rather_than_inside_it() -> None:
    """Advisory means the listener has the answer before anything has judged it.

    The checker here genuinely blocks — on a ``threading.Event`` the test only sets once
    the turn has already returned. If somebody moves the check onto the critical path,
    this deadlocks and the ``wait_for`` fails it. That is the whole point of the test, so
    the checker must block rather than raise.
    """
    entered = threading.Event()
    release = threading.Event()

    @dataclass
    class BlockingChecker:
        @property
        def name(self) -> str:
            return "blocking"

        def check(self, reply: str, material: str) -> GroundingVerdict:
            entered.set()
            release.wait(timeout=10)
            return GroundingVerdict(checker="blocking", checked=1)

    session = _session("Sequoia led it.")
    session.grounding = BlockingChecker()

    async def scenario() -> list[Any]:
        events = await asyncio.wait_for(session.respond_to_text("who led it"), timeout=5)
        # In a thread: a blocking wait here would hold the event loop and stop the very
        # task it is waiting for from ever starting.
        assert await asyncio.to_thread(entered.wait, 5.0), "the check was scheduled"
        assert session.verdicts == [], "...and had not finished when the turn returned"
        release.set()
        await session.drain_grounding_checks()
        return events

    events = asyncio.run(scenario())
    assert [event.type for event in events] == ["transcript", "transcript", "audio_chunk"]
    assert [verdict.checker for verdict in session.verdicts] == ["blocking"]


def test_a_checker_that_raises_records_nothing_and_does_not_end_the_conversation() -> None:
    @dataclass
    class BrokenChecker:
        @property
        def name(self) -> str:
            return "broken"

        def check(self, reply: str, material: str) -> GroundingVerdict:
            raise RuntimeError("the checker fell over")

    session = _session("Sequoia led it.")
    session.grounding = BrokenChecker()
    events = asyncio.run(_one_turn(session, "who led it"))

    assert [event.type for event in events] == ["transcript", "transcript", "audio_chunk"]
    assert session.verdicts == []
    assert session.outbox.empty()


def test_a_dormant_arm_is_not_a_reason_to_stop_checking() -> None:
    """No TTS leg, so no audio — the reply still reaches a transcript and is still checked."""
    session = _session("Sequoia led it.")
    session.arm = ComposedArm(model=ScriptedModel("Sequoia led it."), conversational=True)
    events = asyncio.run(_one_turn(session, "who led it"))

    assert "audio_chunk" not in [event.type for event in events]
    assert [verdict.grounded for verdict in session.verdicts] == [False]


def test_the_checker_this_process_builds_is_the_one_health_names() -> None:
    assert build_grounding_checker().name == "specifics"


# -- tool results as material -----------------------------------------------------------


@dataclass
class StubTool:
    """One tool, with the answer a test wants it to give."""

    tool_name: str
    answer: ToolResult

    @property
    def name(self) -> str:
        return self.tool_name

    @property
    def description(self) -> str:
        return "stub"

    @property
    def parameters(self) -> dict[str, Any]:
        return {}

    def availability(self) -> ToolAvailability:
        return AVAILABLE

    async def invoke(self, arguments: Mapping[str, Any]) -> ToolResult:
        return self.answer


@dataclass
class ToolCallingArm(ComposedArm):
    """The composed arm, plus a scripted tool call — the shape a real turn has."""

    tool_calls: tuple[PendingToolCall, ...] = ()

    async def respond(self, request: TurnRequest) -> AssistantTurn:
        turn = await super().respond(request)
        return replace(turn, tool_calls=self.tool_calls)


def _session_with_tool(reply: str, answer: ToolResult) -> VoiceSession:
    arm = ToolCallingArm(
        model=ScriptedModel(reply),
        synthesizer=FakeSpeechSynthesizer(),
        conversational=True,
        tool_calls=(PendingToolCall(call_id="c1", name="get_item_detail", arguments={}),),
    )
    config = StartSessionRequest.model_validate(
        {"persona": {"name": "Briefing", "instructions": "Be brief."}, "context": {"notes": ""}}
    )
    return VoiceSession.create(
        session_id="s2",
        config=config,
        arm=arm,
        tools=ToolRegistry({"get_item_detail": StubTool("get_item_detail", answer)}),
        grounding=CHECKER,
    )


def test_what_a_tool_returned_this_turn_grounds_the_reply() -> None:
    session = _session_with_tool(
        "Reuters covered it too.",
        ToolResult(ok=True, result={"spans": [{"text": "Reuters ran the story on Tuesday"}]}),
    )
    asyncio.run(_one_turn(session, "who else covered it"))
    assert [verdict.grounded for verdict in session.verdicts] == [True]


def test_a_failed_tool_call_is_not_material() -> None:
    """An error message is not source text; treating it as such would ground a name on it."""
    session = _session_with_tool(
        "Reuters covered it too.",
        ToolResult.failure("Reuters is not reachable right now"),
    )
    asyncio.run(_one_turn(session, "who else covered it"))
    assert [verdict.grounded for verdict in session.verdicts] == [False]


# -- the signal an operator actually reads ----------------------------------------------


def test_an_ungrounded_reply_increments_the_counter_an_operator_queries() -> None:
    """The metric is the answer to "how often does Motet say something it cannot source?".

    Read back through a real in-memory reader rather than by asserting that a function was
    called: the claim being made is that a point with these attributes leaves the process,
    and a mock would pass for a counter that was never wired to a meter at all.
    """
    reader = _install_in_memory_reader()
    if reader is None:
        pytest.skip("a MeterProvider is already installed in this process")

    session = _session("Sequoia led the 900 million round.")
    asyncio.run(_one_turn(session, "who led it"))

    points = _points(reader, "motet.voice.conversational_replies")
    assert points, "nothing was exported; the counter is not attached to a meter"
    ungrounded = [
        point
        for point in points
        if point.attributes and point.attributes.get("grounded") == "false"
    ]
    assert ungrounded, "an ungrounded reply must be countable on its own"
    assert ungrounded[0].attributes is not None
    assert ungrounded[0].attributes["arm"] == "composed"
    assert ungrounded[0].value == 1

    kinds = {
        point.attributes["kind"]
        for point in _points(reader, "motet.voice.unsupported_specifics")
        if point.attributes
    }
    assert kinds == {"name", "number"}


def _install_in_memory_reader() -> Any:
    """A real SDK MeterProvider for this process, or ``None`` if one is already set.

    OpenTelemetry's provider is process-global and may be set once. Nothing else in the
    voice tests sets one — ``motet_obs.configure`` installs nothing without an OTLP
    endpoint — so in practice this succeeds; the guard is there so that a future test that
    does install one turns this into a skip rather than a confusing failure.
    """
    from opentelemetry import metrics
    from opentelemetry.sdk.metrics import MeterProvider
    from opentelemetry.sdk.metrics.export import InMemoryMetricReader

    reader = InMemoryMetricReader()
    metrics.set_meter_provider(MeterProvider(metric_readers=[reader]))
    if metrics.get_meter_provider().__class__ is not MeterProvider:
        return None
    # Touch the module so its counters exist even if no other test imported it first.
    assert obs.SERVICE_NAME == "motet-voice"
    return reader


def _points(reader: Any, metric_name: str) -> list[Any]:
    collected = reader.get_metrics_data()
    if collected is None:
        return []
    return [
        point
        for resource in collected.resource_metrics
        for scope in resource.scope_metrics
        for metric in scope.metrics
        if metric.name == metric_name
        for point in metric.data.data_points
    ]
