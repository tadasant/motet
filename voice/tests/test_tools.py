"""The platform tools — one MCP ``tools/call`` each, never a query and never a template.

These cover argument handling, defaults merging, span resolution and error mapping against
a recording transport. What a *fake* cannot tell you is whether the wire shape is right —
that is ``test_mcp_binding.py`` here, and ``api/tests/test_mcp_voice_binding.py`` against
Motet's own server.
"""

from __future__ import annotations

import asyncio
from typing import Any

from motet_voice.config import VoiceSettings
from motet_voice.contract import PLATFORM_TOOLS, SessionContext
from motet_voice.tools import (
    FailingToolTransport,
    RecordingToolTransport,
    ToolRegistry,
    ToolResponse,
    ToolState,
    build_platform_tools,
    locate_claim,
)

CONTEXT = SessionContext.model_validate(
    {
        "episode_id": "ep1",
        "transcript": [
            {
                "title": "A funding round",
                "start_ms": 0,
                "end_ms": 20_000,
                "news_item_id": "n1",
                "claims": [
                    {
                        "start_ms": 0,
                        "end_ms": 10_000,
                        "spoken_text": "Acme raised forty million dollars.",
                        "source_item_id": "si1",
                        "span_start": 12,
                        "span_end": 44,
                    },
                    {
                        "start_ms": 10_000,
                        "end_ms": 20_000,
                        "spoken_text": "The round was led by Initech.",
                        "source_item_id": "si1",
                        "span_start": 44,
                        "span_end": 70,
                    },
                ],
            },
            {
                "title": "Something else",
                "start_ms": 20_000,
                "end_ms": 30_000,
                "news_item_id": "n2",
                "claims": [
                    {
                        "start_ms": 20_000,
                        "end_ms": 30_000,
                        # No span: a claim a caller could not anchor, so not a candidate.
                        "spoken_text": "Acme raised forty million dollars.",
                    }
                ],
            },
        ],
    }
)


def _registry(
    settings: VoiceSettings, transport: Any, *, context: SessionContext | None = None
) -> ToolRegistry:
    return ToolRegistry(build_platform_tools(settings, transport=transport, context=context))


def test_the_platform_tools_are_the_two_that_can_actually_run(settings: VoiceSettings) -> None:
    tools = build_platform_tools(settings, transport=RecordingToolTransport())
    assert tuple(sorted(tools)) == tuple(sorted(PLATFORM_TOOLS)) == ("mark_read", "save_highlight")


def test_mark_read_calls_the_servers_own_tool_name(settings: VoiceSettings) -> None:
    """The persona's name and the server's name are two contracts, deliberately."""
    transport = RecordingToolTransport(
        responses={"set_news_item_read": ToolResponse(200, {"id": "n1", "read": True})}
    )
    result = asyncio.run(_registry(settings, transport).invoke("mark_read", {"news_item_id": "n1"}))

    assert result.ok and result.result["read"] is True
    assert transport.calls == [("set_news_item_read", {"news_item_id": "n1", "read": True})], (
        "the tool's own default must be sent"
    )


def test_caller_defaults_never_override_the_models_own_arguments(settings: VoiceSettings) -> None:
    transport = RecordingToolTransport(
        responses={"set_news_item_read": ToolResponse(200, {"id": "bound"})}
    )
    tools = build_platform_tools(
        settings,
        transport=transport,
        defaults={"mark_read": {"news_item_id": "bound", "read": True}},
    )
    asyncio.run(ToolRegistry(tools).invoke("mark_read", {"read": False}))

    _, arguments = transport.calls[0]
    assert arguments["news_item_id"] == "bound", "an unspecified argument falls back to the binding"
    assert arguments["read"] is False, "the model's argument wins over the binding"


def test_save_highlight_sends_the_span_the_claim_came_from(settings: VoiceSettings) -> None:
    """The model names a line; the span is read out of the session's own transcript."""
    transport = RecordingToolTransport(
        responses={"save_highlight": ToolResponse(200, {"id": "h1"})}
    )
    result = asyncio.run(
        _registry(settings, transport, context=CONTEXT).invoke(
            "save_highlight",
            {"quote": "the round was led by initech", "note": "who led it"},
        )
    )

    assert result.ok
    assert transport.calls == [
        (
            "save_highlight",
            {
                "news_item_id": "n1",
                "source_item_id": "si1",
                "span_start": 44,
                "span_end": 70,
                "note": "who led it",
                "episode_id": "ep1",
                "anchor_ms": 10_000,
            },
        )
    ]


def test_a_quote_that_was_never_narrated_saves_nothing(settings: VoiceSettings) -> None:
    """A model that paraphrases loosely must not write its own words into a highlight."""
    transport = RecordingToolTransport()
    result = asyncio.run(
        _registry(settings, transport, context=CONTEXT).invoke(
            "save_highlight", {"quote": "Acme is definitely going to fail"}
        )
    )
    assert not result.ok
    assert result.error is not None and "could not find that line" in result.error
    assert transport.calls == [], "nothing reaches Motet when there is no span to send"


def test_a_news_item_id_narrows_which_claims_are_candidates(settings: VoiceSettings) -> None:
    """Two stories can quote the same sentence; the one the listener means wins."""
    assert locate_claim(CONTEXT, "Acme raised forty million dollars.") is not None
    # n2's copy of that sentence carries no span, so it is not a candidate at all.
    assert locate_claim(CONTEXT, "Acme raised forty million dollars.", news_item_id="n2") is None
    found = locate_claim(CONTEXT, "Acme raised forty million dollars.", news_item_id="n1")
    assert found is not None and found[1].span_start == 12


def test_a_short_claim_does_not_swallow_a_long_quote(settings: VoiceSettings) -> None:
    """ "It was announced." is contained in almost any paraphrase. Whichever claim came
    first would win, and the highlight would hold the wrong span while reading verbatim."""
    context = SessionContext.model_validate(
        {
            "transcript": [
                {
                    "title": "A funding round",
                    "start_ms": 0,
                    "end_ms": 20_000,
                    "news_item_id": "n1",
                    "claims": [
                        {
                            "start_ms": 0,
                            "end_ms": 5_000,
                            "spoken_text": "It was announced.",
                            "source_item_id": "si1",
                            "span_start": 0,
                            "span_end": 10,
                        },
                        {
                            "start_ms": 5_000,
                            "end_ms": 20_000,
                            "spoken_text": "Acme raised forty million dollars.",
                            "source_item_id": "si1",
                            "span_start": 10,
                            "span_end": 80,
                        },
                    ],
                }
            ]
        }
    )
    found = locate_claim(context, "It was announced. Acme raised forty million dollars. Big news.")
    assert found is not None
    assert found[1].span_start == 10, "the longest claim inside the quote wins, not the first"


def test_the_tightest_containing_claim_wins_when_the_quote_is_a_fragment(
    settings: VoiceSettings,
) -> None:
    """Two claims can both contain the fragment; the shorter one is the more specific."""
    context = SessionContext.model_validate(
        {
            "transcript": [
                {
                    "title": "A funding round",
                    "start_ms": 0,
                    "end_ms": 20_000,
                    "news_item_id": "n1",
                    "claims": [
                        {
                            "start_ms": 0,
                            "end_ms": 10_000,
                            "spoken_text": ("Acme raised forty million dollars, the company said."),
                            "source_item_id": "si1",
                            "span_start": 0,
                            "span_end": 60,
                        },
                        {
                            "start_ms": 10_000,
                            "end_ms": 20_000,
                            "spoken_text": "Acme raised forty million dollars.",
                            "source_item_id": "si1",
                            "span_start": 60,
                            "span_end": 90,
                        },
                    ],
                }
            ]
        }
    )
    found = locate_claim(context, "raised forty million")
    assert found is not None
    assert found[1].span_start == 60, "the shortest claim containing the fragment wins"


def test_a_session_with_no_transcript_cannot_save_a_highlight(settings: VoiceSettings) -> None:
    result = asyncio.run(
        _registry(settings, RecordingToolTransport()).invoke("save_highlight", {"quote": "x"})
    )
    assert not result.ok and result.error is not None and "could not find that line" in result.error


def test_a_404_reads_as_a_missing_item_rather_than_a_missing_route(
    settings: VoiceSettings,
) -> None:
    """A name the server does not know is now its refusal, not ours: this is about ids."""
    registry = _registry(settings, RecordingToolTransport())
    result = asyncio.run(registry.invoke("mark_read", {"news_item_id": "nope"}))
    assert not result.ok
    assert result.error is not None and "does not exist" in result.error


def test_a_tool_that_was_not_granted_is_refused(settings: VoiceSettings) -> None:
    tools = build_platform_tools(settings, transport=RecordingToolTransport())
    registry = ToolRegistry({"mark_read": tools["mark_read"]})
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


def test_an_unbound_session_gets_dormant_tools_rather_than_a_crash(
    settings: VoiceSettings,
) -> None:
    tools = build_platform_tools(settings, transport=None)
    for tool in tools.values():
        availability = tool.availability()
        assert availability.state is ToolState.DORMANT
        assert "not bound" in availability.reason and "motet" in availability.reason


def test_every_outcome_reaches_the_counter(settings: VoiceSettings, metrics: Any) -> None:
    """`motet.voice.tool_calls` is what makes the binding falsifiable (invariant 11): a
    binding that resolves to nothing, a credential the API refuses, and a deployment nobody
    has spoken to are otherwise the same silence. Read back through a real in-memory reader
    rather than by asserting a function was called."""
    transport = RecordingToolTransport(
        responses={"set_news_item_read": ToolResponse(200, {"id": "n1", "read": True})}
    )
    granted = _registry(settings, transport, context=CONTEXT)

    def counted() -> dict[tuple[str, str], int]:
        return {
            (str(point.attributes["tool"]), str(point.attributes["outcome"])): int(point.value)
            for point in metrics.points("motet.voice.tool_calls")
        }

    # The reader is session-scoped and a counter is cumulative, so what this test owns is
    # the delta rather than the total.
    before = counted()

    async def go() -> None:
        await granted.invoke("mark_read", {"news_item_id": "n1"})  # ok
        await granted.invoke("save_highlight", {"quote": "nothing like this was said"})  # failed
        await granted.invoke("no_such_tool", {})  # not_granted
        unbound = ToolRegistry(build_platform_tools(settings, transport=None))
        await unbound.invoke("mark_read", {"news_item_id": "n1"})  # dormant

    asyncio.run(go())
    after = counted()

    for key in (
        ("mark_read", "ok"),
        ("save_highlight", "failed"),
        ("no_such_tool", "not_granted"),
        ("mark_read", "dormant"),
    ):
        assert after.get(key, 0) - before.get(key, 0) == 1, key


def test_a_story_that_is_not_in_this_episode_says_so(settings: VoiceSettings) -> None:
    """Distinct from "I couldn't find that line": no requoting fixes a wrong story id."""
    result = asyncio.run(
        _registry(settings, RecordingToolTransport(), context=CONTEXT).invoke(
            "save_highlight", {"quote": "Acme raised forty million dollars.", "news_item_id": "n9"}
        )
    )
    assert not result.ok
    assert result.error is not None and "not in this episode" in result.error


def test_an_under_quoted_fragment_does_not_save_an_arbitrary_span(
    settings: VoiceSettings,
) -> None:
    """A model that says `quote="that"` must not land inside some claim and write a
    highlight — which would then read as verbatim source text nobody asked for. The
    paraphrase direction is only half of what this path has to refuse."""
    transport = RecordingToolTransport()
    for vague in ("that", "Acme", "dollars"):
        result = asyncio.run(
            _registry(settings, transport, context=CONTEXT).invoke(
                "save_highlight", {"quote": vague}
            )
        )
        assert not result.ok, f"{vague!r} matched a claim by containment"
    assert transport.calls == []
    # An exact match is still a match, however short: that is the model repeating what was
    # said rather than guessing.
    assert locate_claim(CONTEXT, "The round was led by Initech.") is not None


def test_an_em_dash_or_a_hyphen_does_not_break_the_match(settings: VoiceSettings) -> None:
    """Deleting punctuation joins the words either side, so `forty-million` became
    `fortymillion` and matched no paraphrase of itself. This pipeline's prose is full of
    em dashes and hyphenated compounds."""
    context = SessionContext.model_validate(
        {
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
                            "spoken_text": "Acme — the maker of things — raised forty-million.",
                            "source_item_id": "si1",
                            "span_start": 5,
                            "span_end": 40,
                        }
                    ],
                }
            ]
        }
    )
    found = locate_claim(context, "Acme the maker of things raised forty million.")
    assert found is not None and found[1].span_start == 5


def test_a_missing_required_argument_is_explained(settings: VoiceSettings) -> None:
    result = asyncio.run(_registry(settings, RecordingToolTransport()).invoke("mark_read", {}))
    assert not result.ok
    assert result.error is not None and "news_item_id" in result.error
