"""Both arms, behind one interface — and the honesty of the dormant one."""

from __future__ import annotations

import asyncio

import pytest
from motet_voice.bargein import BargeInPolicy, VadTurnDetector
from motet_voice.config import COMPOSED_ARM, OPENAI_REALTIME_ARM, VoiceSettings
from motet_voice.realtime import (
    DEFAULT_SERVER_VAD,
    ArmDormant,
    DormantSpeechRecognizer,
    ScriptedRealtimeTransport,
    ServerVadRelay,
    TurnRequest,
    build_all_arms,
    build_arm,
    build_composed_arm,
    build_openai_arm,
)
from motet_voice.realtime.openai_realtime import RealtimeProtocolError


def test_both_arms_build_and_expose_a_turn_detector(settings: VoiceSettings) -> None:
    """The measurable half must work for every arm with no credential at all."""
    arms = build_all_arms(settings)
    assert set(arms) == {COMPOSED_ARM, OPENAI_REALTIME_ARM}
    for arm in arms.values():
        detector = arm.build_turn_detector(BargeInPolicy(name="probe"))
        assert detector.variant == "probe"
        assert arm.capabilities().replayable


def test_the_openai_arm_is_dormant_and_says_why(settings: VoiceSettings) -> None:
    capabilities = build_openai_arm(settings).capabilities()
    assert not capabilities.conversational
    assert "OPENAI_API_KEY" in capabilities.dormant_reason
    assert capabilities.turn_detection == "server"


def test_the_dormant_openai_arm_still_replays_but_is_labelled_as_emulated(
    settings: VoiceSettings,
) -> None:
    """The distinction the whole report hangs on: emulated dials, not a vendor measurement."""
    arm = build_openai_arm(settings)
    detector = arm.build_turn_detector(BargeInPolicy(name="probe"))
    assert getattr(detector, "trigger", "") == "openai_server_vad_emulated"


def test_a_key_never_swaps_the_replayable_detector_for_the_relay(
    settings: VoiceSettings,
) -> None:
    """The relay decides nothing on its own, so handing it to a replay is a silent zero.

    A replay through the relay produces no decisions, scores a perfect zero false positives
    per minute, and wins the comparison the harness exists to run — a silently wrong
    measurement, which is worse than none. The relay stays behind its own constructor.
    """
    arm = build_openai_arm(settings, transport=ScriptedRealtimeTransport())
    assert arm.capabilities().conversational

    detector = arm.build_turn_detector(BargeInPolicy(name="probe"))
    assert not isinstance(detector, ServerVadRelay)
    assert getattr(detector, "trigger", "") == "openai_server_vad_emulated"
    assert arm.capabilities().turn_detection_emulated, (
        "a key must not make the emulation stop announcing itself as one"
    )
    assert isinstance(arm.build_live_turn_detector(BargeInPolicy(name="probe")), ServerVadRelay)


def test_the_emulated_dials_actually_do_something(settings: VoiceSettings) -> None:
    """`prefix_padding_ms` and `silence_duration_ms` were stored and never read."""
    arm = build_openai_arm(settings)
    detector = arm.build_turn_detector(BargeInPolicy(name="probe", refractory_ms=100))
    assert isinstance(detector, VadTurnDetector)

    assert detector.onset_back_off_ms == int(DEFAULT_SERVER_VAD["prefix_padding_ms"])
    assert detector.policy.refractory_ms >= int(DEFAULT_SERVER_VAD["silence_duration_ms"]), (
        "the vendor needs this much quiet to end a turn, so it cannot start one sooner"
    )


def test_the_relay_reports_the_providers_decision_with_local_evidence() -> None:
    relay = ServerVadRelay(policy=BargeInPolicy(name="live"))
    decision = relay.on_speech_started(
        audio_start_ms=4_200, narration_playing=True, spoken_through_ms=4_100
    )
    assert decision is not None
    assert decision.trigger == "openai_server_vad"
    assert decision.spoken_through_ms == 4_100
    assert (
        relay.on_speech_started(
            audio_start_ms=4_300, narration_playing=True, spoken_through_ms=4_200
        )
        is None
    ), "the refractory window applies to provider decisions too"


def test_the_openai_session_update_carries_everything_and_fetches_nothing(
    settings: VoiceSettings,
) -> None:
    """Invariant 2 as a wire format: no second channel to look anything up through."""
    transport = ScriptedRealtimeTransport()
    arm = build_openai_arm(settings, transport=transport)
    payload = arm.session_update(
        TurnRequest(
            persona_instructions="Be brief.",
            voice="narrator",
            context_notes="Story A funded at $12m.",
            tools=[{"name": "mark_read", "description": "d", "parameters": {}}],
        )
    )
    assert "Story A funded at $12m." in payload["session"]["instructions"]
    assert payload["session"]["turn_detection"]["type"] == "server_vad"
    assert [tool["name"] for tool in payload["session"]["tools"]] == ["mark_read"]


def test_the_openai_client_parses_a_scripted_vendor_stream(settings: VoiceSettings) -> None:
    transport = ScriptedRealtimeTransport(
        scripted=[
            {"type": "conversation.item.input_audio_transcription.completed", "transcript": "hi"},
            {"type": "response.audio_transcript.delta", "delta": "Sure"},
            {"type": "response.audio_transcript.delta", "delta": ", saving it."},
            {
                "type": "response.function_call_arguments.done",
                "call_id": "c1",
                "name": "save_highlight",
                "arguments": '{"news_item_id": "n1", "quote": "q"}',
            },
            {"type": "response.done"},
            {"type": "response.audio_transcript.delta", "delta": "never reached"},
        ]
    )
    arm = build_openai_arm(settings, transport=transport)
    turn = asyncio.run(arm.respond(TurnRequest(persona_instructions="p", voice="narrator")))

    assert turn.text == "Sure, saving it."
    assert turn.user_transcript == "hi"
    assert turn.tool_calls[0].name == "save_highlight"
    assert turn.tool_calls[0].arguments == {"news_item_id": "n1", "quote": "q"}
    assert any(sent["type"] == "session.update" for sent in transport.sent)


def test_a_provider_error_event_is_raised_not_swallowed(settings: VoiceSettings) -> None:
    transport = ScriptedRealtimeTransport(scripted=[{"type": "error", "error": "rate limited"}])
    arm = build_openai_arm(settings, transport=transport)
    with pytest.raises(RealtimeProtocolError, match="rate limited"):
        asyncio.run(arm.respond(TurnRequest(persona_instructions="p", voice="narrator")))


def test_the_composed_arm_is_conversational_on_fakes(settings: VoiceSettings) -> None:
    arm = build_composed_arm(settings)
    turn = asyncio.run(
        arm.respond(TurnRequest(persona_instructions="p", voice="narrator", user_text="hello"))
    )
    assert turn.text
    assert turn.audio is not None, "the fake TTS leg should have produced audio"


def test_a_dormant_stt_leg_raises_rather_than_returning_empty_text() -> None:
    """Silently returning "" makes the model answer a question nobody asked."""
    with pytest.raises(ArmDormant, match="speech-to-text"):
        DormantSpeechRecognizer().transcribe(b"\x00\x00")


def test_the_configured_arm_is_what_build_arm_returns() -> None:
    for name in (COMPOSED_ARM, OPENAI_REALTIME_ARM):
        settings = VoiceSettings.from_env({"MOTET_VOICE_ARM": name, "MOTET_INFERENCE_MODE": "fake"})
        assert build_arm(settings).name == name
