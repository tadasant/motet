"""The MCP client itself, against a real in-process MCP server (motet#120).

The transport is the one thing a fake cannot cover: a canned dict will answer whatever
shape a test asks it for, and the templates this replaced were wrong in exactly that blind
spot for months. So these drive :class:`~motet_voice.tools.mcp.McpToolTransport` — the
class a deployment runs — against a server built with the SDK, which answers with real
``CallToolResult`` envelopes and refuses a tool it does not have.

The server here is a *stand-in* for Motet's, not Motet's: it lives in this package, so it
proves the protocol and the decoding rather than the tool names. That second claim is
``api/tests/test_mcp_voice_binding.py``, which runs the same transport against the real
``/mcp``.
"""

from __future__ import annotations

import asyncio
from typing import Annotated, Any

import pytest
from mcp.server import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from motet_voice.config import DEFAULT_MCP_TOOL_GROUPS, VoiceSettings
from motet_voice.contract import SessionContext
from motet_voice.tools import (
    McpToolTransport,
    ToolRegistry,
    ToolResponse,
    build_motet_transport,
    build_platform_tools,
    motet_mcp_url,
)
from motet_voice.tools.mcp import decode, explain_exception
from pydantic import BaseModel, Field


class Marked(BaseModel):
    id: str
    read: bool


class Saved(BaseModel):
    id: str
    quote: str


def build_server(*, calls: list[tuple[str, dict[str, Any]]]) -> MCPServer:
    """A stand-in for Motet's ``/mcp``: the two tools a voice session calls, and nothing else.

    The refusal prose is deliberately Motet's own shape — ``motet_api.mcp.context`` raises
    ``ToolError(f"{status}: {detail}")`` — because recovering that number is what lets a
    404 still read as "the item does not exist".
    """
    server = MCPServer(name="stand-in-for-motet")

    @server.tool()
    def set_news_item_read(
        news_item_id: Annotated[str, Field(description="Which story.")],
        read: Annotated[bool, Field(description="Read or unread.")] = True,
    ) -> Marked:
        """Mark one news item read or unread."""
        calls.append(("set_news_item_read", {"news_item_id": news_item_id, "read": read}))
        if news_item_id == "missing":
            raise ToolError("404: No such news item.")
        return Marked(id=news_item_id, read=read)

    @server.tool()
    def save_highlight(
        news_item_id: Annotated[str, Field(description="Which story.")],
        source_item_id: Annotated[str, Field(description="Which source item.")],
        span_start: Annotated[int, Field(description="Start offset.")],
        span_end: Annotated[int, Field(description="End offset, exclusive.")],
        note: Annotated[str | None, Field(description="An optional note.")] = None,
        episode_id: Annotated[str | None, Field(description="Provenance.")] = None,
        anchor_ms: Annotated[int | None, Field(description="Provenance.")] = None,
    ) -> Saved:
        """Save a passage from a source item as a highlight."""
        calls.append(
            (
                "save_highlight",
                {
                    "news_item_id": news_item_id,
                    "source_item_id": source_item_id,
                    "span_start": span_start,
                    "span_end": span_end,
                    "note": note,
                    "episode_id": episode_id,
                    "anchor_ms": anchor_ms,
                },
            )
        )
        # The quote is read out of the source text at the span, never taken from the
        # caller — the property the voice tool's span resolution exists to preserve.
        return Saved(id="h1", quote=f"source[{span_start}:{span_end}]")

    return server


CONTEXT = SessionContext.model_validate(
    {
        "episode_id": "ep1",
        "transcript": [
            {
                "title": "A funding round",
                "start_ms": 0,
                "end_ms": 10_000,
                "news_item_id": "n1",
                "claims": [
                    {
                        "start_ms": 0,
                        "end_ms": 10_000,
                        "spoken_text": "Acme raised forty million dollars.",
                        "source_item_id": "si1",
                        "span_start": 12,
                        "span_end": 44,
                    }
                ],
            }
        ],
    }
)


@pytest.fixture
def calls() -> list[tuple[str, dict[str, Any]]]:
    return []


@pytest.fixture
def transport(calls: list[tuple[str, dict[str, Any]]]) -> McpToolTransport:
    server = build_server(calls=calls)
    return McpToolTransport(lambda: server)


def _registry(settings: VoiceSettings, transport: McpToolTransport) -> ToolRegistry:
    return ToolRegistry(build_platform_tools(settings, transport=transport, context=CONTEXT))


def test_mark_read_goes_out_over_mcp_and_comes_back_decoded(
    settings: VoiceSettings, transport: McpToolTransport, calls: list[Any]
) -> None:
    async def go() -> Any:
        try:
            return await _registry(settings, transport).invoke("mark_read", {"news_item_id": "n1"})
        finally:
            await transport.aclose()

    result = asyncio.run(go())
    assert calls == [("set_news_item_read", {"news_item_id": "n1", "read": True})]
    assert result.ok, result.error
    assert result.result == {"id": "n1", "read": True}, "structured content, not prose"


def test_save_highlight_sends_a_span_the_model_never_saw(
    settings: VoiceSettings, transport: McpToolTransport, calls: list[Any]
) -> None:
    async def go() -> Any:
        try:
            return await _registry(settings, transport).invoke(
                "save_highlight", {"quote": "Acme raised forty million dollars."}
            )
        finally:
            await transport.aclose()

    result = asyncio.run(go())
    assert result.ok, result.error
    name, arguments = calls[0]
    assert name == "save_highlight"
    assert (arguments["source_item_id"], arguments["span_start"], arguments["span_end"]) == (
        "si1",
        12,
        44,
    )
    assert result.result["quote"] == "source[12:44]", (
        "the quote came from the source, not the model"
    )


def test_a_tool_error_keeps_its_status_on_the_way_back(
    settings: VoiceSettings, transport: McpToolTransport
) -> None:
    async def go() -> Any:
        try:
            return await _registry(settings, transport).invoke(
                "mark_read", {"news_item_id": "missing"}
            )
        finally:
            await transport.aclose()

    result = asyncio.run(go())
    assert not result.ok
    assert result.error is not None and "does not exist" in result.error, (
        "the 404 the server raised has to survive the MCP envelope"
    )


def test_a_tool_the_server_does_not_have_is_a_failed_result(
    settings: VoiceSettings, transport: McpToolTransport
) -> None:
    """The old templates could name a route nobody had; a name is now the server's to refuse."""
    from motet_voice.tools.platform import McpTool

    tool = McpTool(
        name="mark_read",
        description="…",
        parameters={"type": "object", "properties": {}},
        tool="a_tool_that_does_not_exist",
        transport=transport,
    )

    async def go() -> Any:
        try:
            return await ToolRegistry({"mark_read": tool}).invoke("mark_read", {})
        finally:
            await transport.aclose()

    result = asyncio.run(go())
    assert not result.ok and result.error is not None


def test_two_sessions_calling_at_once_each_own_their_connection(
    settings: VoiceSettings, calls: list[Any]
) -> None:
    """The transport is process-wide and every caller is a different websocket task — which
    is the shape the first draft got wrong, because a connection opened in one task cannot
    be closed from another (anyio refuses to exit a cancel scope in a task that did not
    enter it). A connection per call, in the caller's own task, is what makes that a
    non-question; this drives it from two tasks at once to say so."""
    opened: list[int] = []
    server = build_server(calls=calls)

    def connect() -> Any:
        opened.append(1)
        return server

    transport = McpToolTransport(connect)

    async def go() -> list[Any]:
        registry = _registry(settings, transport)
        results = await asyncio.gather(
            registry.invoke("mark_read", {"news_item_id": "n1"}),
            registry.invoke("mark_read", {"news_item_id": "n2"}),
            registry.invoke("save_highlight", {"quote": "Acme raised forty million dollars."}),
        )
        # Closing from a *third* task is the SIGTERM case, and must not raise either.
        await asyncio.create_task(transport.aclose())
        return results

    results = asyncio.run(go())
    assert [result.ok for result in results] == [True, True, True], [r.error for r in results]
    assert len(opened) == 3, "one connection per call, owned by the task that made it"
    assert sorted(name for name, _ in calls) == [
        "save_highlight",
        "set_news_item_read",
        "set_news_item_read",
    ]


def test_an_unreachable_server_says_nothing_about_where_it_was(
    settings: VoiceSettings,
) -> None:
    """The failure text is read out to a listener and handed to a model, so it carries no
    URL, no hostname and no tool-group selection — only the log does."""

    def connect() -> Any:
        raise RuntimeError("connect to https://api.internal.example/mcp?tool_groups=x failed")

    transport = McpToolTransport(connect)
    result = asyncio.run(
        ToolRegistry(build_platform_tools(settings, transport=transport, context=CONTEXT)).invoke(
            "mark_read", {"news_item_id": "n1"}
        )
    )
    assert not result.ok
    assert result.error is not None
    assert "api.internal.example" not in result.error and "tool_groups" not in result.error
    assert "599" in result.error


def test_an_exception_group_is_unwrapped_for_the_log(settings: VoiceSettings) -> None:
    """The SDK's transport is a task group, so a fault arrives as "unhandled errors in a
    TaskGroup (1 sub-exception)" — true, and useless in an obs query."""
    inner = ConnectionRefusedError("nothing is listening")
    assert explain_exception(ExceptionGroup("unhandled errors in a TaskGroup", [inner])) == (
        "ConnectionRefusedError: nothing is listening"
    )


def test_an_error_whose_prose_holds_a_success_number_is_still_an_error() -> None:
    """`ToolResponse.ok` is true below 400, so believing a "201" in an error's text would
    report a failed call to the persona as a success — which nothing downstream can see."""

    class Part:
        text = "Error executing tool paste_text: could not queue job 201: retry later"

    class Result:
        is_error = True
        content = (Part(),)
        structured_content = None

    response = decode(Result())
    assert not response.ok and response.status == 400
    assert "201" in str(response.payload["detail"]), "the prose is kept, the number is not read"


def test_a_result_with_no_structured_content_still_reaches_the_persona() -> None:
    class Part:
        text = "Deleted hl_1."

    class Result:
        is_error = False
        content = (Part(),)
        structured_content = None

    assert decode(Result()) == ToolResponse(status=200, payload={"data": "Deleted hl_1."})


def test_the_slug_resolves_to_the_apis_mcp_path_with_the_tight_group_selection() -> None:
    """Where ``motet`` points is configuration, and the groups ride in the query string —
    the only place the server reads them from, so a body cannot widen the surface."""
    assert (
        motet_mcp_url("https://api.example/")
        == f"https://api.example/mcp?tool_groups={DEFAULT_MCP_TOOL_GROUPS.replace(',', '%2C')}"
    )
    assert DEFAULT_MCP_TOOL_GROUPS == "backlog,highlights"


def test_no_api_base_url_resolves_no_transport(settings: VoiceSettings) -> None:
    assert build_motet_transport(settings) is None


def test_the_scoped_credential_swaps_in_as_a_variable() -> None:
    """Option (b) of motet#120 must be a configuration change, not a rewrite."""
    owner = VoiceSettings.from_env(
        {
            "MOTET_INFERENCE_MODE": "fake",
            "MOTET_VOICE_API_BASE_URL": "https://api.example",
            "MOTET_VOICE_API_TOKEN": "owner-token",
        }
    )
    assert owner.mcp_token == "owner-token" and owner.mcp_token_dedicated is False
    assert owner.describe_mcp() == "backlog,highlights/api_token"

    scoped = VoiceSettings.from_env(
        {
            "MOTET_INFERENCE_MODE": "fake",
            "MOTET_VOICE_API_BASE_URL": "https://api.example",
            "MOTET_VOICE_API_TOKEN": "owner-token",
            "MOTET_VOICE_MCP_TOKEN": "scoped-token",
            "MOTET_VOICE_MCP_TOOL_GROUPS": "backlog_readonly",
        }
    )
    assert scoped.mcp_token == "scoped-token" and scoped.mcp_token_dedicated is True
    assert scoped.describe_mcp() == "backlog_readonly/scoped"
    assert "scoped-token" not in scoped.describe(), "describe() never carries a secret"
