"""The four platform tools — HTTP to Motet's API, never a query."""

from __future__ import annotations

import asyncio

from motet_voice.config import VoiceSettings
from motet_voice.contract import PLATFORM_TOOLS
from motet_voice.tools import (
    FailingToolTransport,
    RecordingToolTransport,
    ToolRegistry,
    ToolResponse,
    ToolState,
    build_platform_tools,
)


def _registry(settings: VoiceSettings, transport: object) -> ToolRegistry:
    return ToolRegistry(build_platform_tools(settings, transport=transport))  # type: ignore[arg-type]


def test_all_four_platform_tools_exist(settings: VoiceSettings) -> None:
    tools = build_platform_tools(settings, transport=RecordingToolTransport())
    assert tuple(sorted(tools)) == tuple(sorted(PLATFORM_TOOLS))


def test_mark_read_hits_the_route_that_already_ships(settings: VoiceSettings) -> None:
    transport = RecordingToolTransport(
        responses={"POST /v1/news-items/n1/read": ToolResponse(200, {"id": "n1", "read": True})}
    )
    result = asyncio.run(_registry(settings, transport).invoke("mark_read", {"news_item_id": "n1"}))

    assert result.ok and result.result["read"] is True
    method, path, body = transport.calls[0]
    assert (method, path) == ("POST", "/v1/news-items/n1/read")
    assert body == {"read": True}, "the tool's own default must be sent"


def test_a_path_argument_is_url_quoted(settings: VoiceSettings) -> None:
    transport = RecordingToolTransport()
    asyncio.run(_registry(settings, transport).invoke("mark_read", {"news_item_id": "a/b c"}))
    assert transport.calls[0][1] == "/v1/news-items/a%2Fb%20c/read"


def test_caller_defaults_never_override_the_models_own_arguments(settings: VoiceSettings) -> None:
    tools = build_platform_tools(
        settings,
        transport=(transport := RecordingToolTransport()),
        defaults={"save_highlight": {"news_item_id": "bound", "quote": "bound-quote"}},
    )
    asyncio.run(ToolRegistry(tools).invoke("save_highlight", {"quote": "what was said"}))

    _, _, body = transport.calls[0]
    assert body["news_item_id"] == "bound", "an unspecified argument falls back to the binding"
    assert body["quote"] == "what was said", "the model's argument wins over the binding"


def test_a_route_that_has_not_shipped_yet_is_reported_clearly(settings: VoiceSettings) -> None:
    """`save_highlight` and `get_item_detail` land against another session's work."""
    registry = _registry(settings, RecordingToolTransport())
    result = asyncio.run(registry.invoke("get_item_detail", {"news_item_id": "n1"}))
    assert not result.ok
    assert result.error is not None and "has not shipped yet" in result.error


def test_start_research_is_dormant_without_exa(settings: VoiceSettings) -> None:
    tools = build_platform_tools(settings, transport=RecordingToolTransport())
    availability = tools["start_research"].availability()
    assert availability.state is ToolState.DORMANT
    assert "EXA_API_KEY" in availability.reason

    result = asyncio.run(_registry(settings, RecordingToolTransport()).invoke("start_research", {}))
    assert not result.ok and result.error is not None and "Exa" in result.error


def test_start_research_wakes_up_when_exa_is_provisioned() -> None:
    settings = VoiceSettings.from_env(
        {"MOTET_INFERENCE_MODE": "fake", "EXA_API_KEY": "throwaway-key-for-a-unit-test"}
    )
    tools = build_platform_tools(settings, transport=RecordingToolTransport())
    assert tools["start_research"].availability().available


def test_a_tool_that_was_not_granted_is_refused(settings: VoiceSettings) -> None:
    registry = ToolRegistry(
        {
            "mark_read": build_platform_tools(settings, transport=RecordingToolTransport())[
                "mark_read"
            ]
        }
    )
    result = asyncio.run(registry.invoke("save_highlight", {}))
    assert not result.ok
    assert result.error is not None and "not a tool this session was granted" in result.error


def test_an_unreachable_api_is_a_failed_result_not_an_exception(settings: VoiceSettings) -> None:
    """A raised exception mid-turn is a dropped conversation; a failed result is a sentence."""
    result = asyncio.run(
        _registry(settings, FailingToolTransport()).invoke("mark_read", {"news_item_id": "n1"})
    )
    assert not result.ok
    assert result.error is not None and "599" in result.error


def test_no_transport_means_dormant_rather_than_a_crash(settings: VoiceSettings) -> None:
    tools = build_platform_tools(settings, transport=None)
    assert tools["mark_read"].availability().state is ToolState.DORMANT


def test_a_missing_required_path_argument_is_explained(settings: VoiceSettings) -> None:
    result = asyncio.run(_registry(settings, RecordingToolTransport()).invoke("mark_read", {}))
    assert not result.ok
    assert result.error is not None and "news_item_id" in result.error
