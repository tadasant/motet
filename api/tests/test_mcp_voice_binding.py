"""The voice service reaching Motet over ``/mcp`` (motet#120), end to end.

**This is the test the hand-maintained path templates never had.** The voice service used
to name routes in its own tree — and three of its four names were wrong, against a route
that does not exist or a body that is not accepted, with nothing anywhere going red. So
this drives the *real* client (`motet_voice.tools.McpToolTransport`, the class a deployment
runs) and the *real* platform tools against the *real* `/mcp` on the *real* API over a real
Postgres, and asserts the rows the calls were supposed to write.

``api/tests`` imports ``motet_voice`` here, which is the one direction the packages do not
depend in. That is deliberate and is why this file exists rather than the same test living
in ``voice/tests``: the claim is about two services meeting, the API is the half that owns
the database, and the voice service must never import it (invariant 2, and
``voice/tests/test_no_database_access.py``). Nothing in ``motet_api`` imports
``motet_voice``; this test does, as a client would.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Iterator
from contextlib import asynccontextmanager
from typing import Any

import httpx2
import psycopg
import pytest
from mcp.client.streamable_http import streamable_http_client
from motet_api import app
from motet_api.deps import reset_drain_trigger, reset_store
from motet_api.voice import SESSION_MCP_SERVERS, SESSION_TOOLS
from motet_db import phase2, repo
from motet_voice.config import DEFAULT_MCP_TOOL_GROUPS, VoiceSettings
from motet_voice.contract import PLATFORM_TOOLS, SessionContext
from motet_voice.tools import (
    McpToolTransport,
    ToolRegistry,
    build_platform_tools,
    motet_mcp_url,
)

TOKEN = "test-api-token"
#: Not localhost, on purpose: the SDK's DNS-rebinding protection answers a non-localhost
#: `Host` with 421 unless it is off, and a test on localhost could never tell.
HOST = "api.motet.test"


@pytest.fixture
def env(
    db: psycopg.Connection[Any], _migrated: str, object_store: Any, monkeypatch: pytest.MonkeyPatch
) -> Iterator[None]:
    monkeypatch.setenv("MOTET_API_TOKEN", TOKEN)
    monkeypatch.setenv("DATABASE_URL", _migrated)
    reset_store()
    yield
    reset_store()
    reset_drain_trigger()


@pytest.fixture
def settings() -> VoiceSettings:
    """The voice service as a deployment would configure it under option (a) of motet#120:
    its existing API base URL and its existing owner token, and nothing new."""
    return VoiceSettings.from_env(
        {
            "MOTET_INFERENCE_MODE": "fake",
            "MOTET_VOICE_API_BASE_URL": f"http://{HOST}",
            "MOTET_VOICE_API_TOKEN": TOKEN,
        }
    )


@pytest.fixture
def story(db: psycopg.Connection[Any]) -> dict[str, Any]:
    """One source item and the news item dedup made from it, with a real span into the text."""
    text = "Acme Corp announced today that it raised forty million dollars in a Series B."
    source = repo.insert_source_item(
        db, user_id=repo.OWNER_USER_ID, title="A funding round", text=text
    )
    news_item_id = repo.insert_news_item(
        db,
        user_id=repo.OWNER_USER_ID,
        title="Acme raises $40m",
        summary="A Series B.",
        source_item_id_=source.id,
    )
    # A real episode, because `highlights.episode_id` is a foreign key: the provenance the
    # voice tool sends has to name an episode that exists, which in a session it always
    # does — `SessionContext.episode_id` is the episode being listened to.
    episode_id = repo.create_episode(
        db, user_id=repo.OWNER_USER_ID, title="Episode under test", max_duration_ms=600_000
    )
    db.commit()
    span_start = text.index("it raised")
    span_end = text.index("Series B.") + len("Series B.")
    return {
        "episode_id": episode_id,
        "news_item_id": news_item_id,
        "source_item_id": source.id,
        "span_start": span_start,
        "span_end": span_end,
        "quote": text[span_start:span_end],
    }


def context_for(story: dict[str, Any]) -> SessionContext:
    """What `motet_api.voice.session_config` hands the session for this story — a timed
    transcript whose claims carry the span they were copied from."""
    return SessionContext.model_validate(
        {
            "episode_id": story["episode_id"],
            "transcript": [
                {
                    "title": "Acme raises $40m",
                    "start_ms": 0,
                    "end_ms": 12_000,
                    "news_item_id": story["news_item_id"],
                    "claims": [
                        {
                            "start_ms": 0,
                            "end_ms": 12_000,
                            # What the narrator *said*, which is not the source's wording:
                            # the highlight has to come back verbatim from the source.
                            "spoken_text": "Acme raised forty million dollars in a Series B.",
                            "source_item_id": story["source_item_id"],
                            "span_start": story["span_start"],
                            "span_end": story["span_end"],
                        }
                    ],
                }
            ],
        }
    )


@asynccontextmanager
async def bound(settings: VoiceSettings) -> AsyncIterator[McpToolTransport]:
    """The voice service's own transport, pointed at this app over ASGI.

    Only the HTTP hop is swapped for ASGI. The URL — path, `?tool_groups=` and all — is
    built by `motet_voice`, the bearer is the one its settings resolved, and the client is
    the SDK's.
    """
    url = motet_mcp_url(settings.api_base_url or "", settings.mcp_tool_groups)
    http = httpx2.AsyncClient(
        transport=httpx2.ASGITransport(app=app),
        headers={"Authorization": f"Bearer {settings.mcp_token}"},
    )
    transport = McpToolTransport(
        lambda: streamable_http_client(url, http_client=http), on_close=http.aclose
    )
    try:
        yield transport
    finally:
        await transport.aclose()


def run(settings: VoiceSettings, work: Any) -> Any:
    """Run ``work(registry)`` with the API's lifespan up, as a deployed pair would be."""

    async def go() -> Any:
        async with app.router.lifespan_context(app), bound(settings) as transport:
            return await work(transport)

    return asyncio.run(go())


def registry_for(
    settings: VoiceSettings, transport: McpToolTransport, story: dict[str, Any]
) -> ToolRegistry:
    return ToolRegistry(
        build_platform_tools(settings, transport=transport, context=context_for(story))
    )


class TestATraceThroughTheWholeBinding:
    """A platform tool call goes out as MCP and the database changes."""

    def test_mark_read_marks_the_story_read(
        self,
        env: None,
        settings: VoiceSettings,
        story: dict[str, Any],
        db: psycopg.Connection[Any],
    ) -> None:
        async def work(transport: McpToolTransport) -> Any:
            return await registry_for(settings, transport, story).invoke(
                "mark_read", {"news_item_id": story["news_item_id"]}
            )

        result = run(settings, work)

        assert result.ok, result.error
        assert result.result["id"] == story["news_item_id"]
        assert result.result["read"] is True
        items = repo.list_news_items(db, repo.OWNER_USER_ID)
        assert [(item.id, item.read_at is not None) for item in items] == [
            (story["news_item_id"], True)
        ]

    def test_save_highlight_writes_the_quote_out_of_the_source_text(
        self,
        env: None,
        settings: VoiceSettings,
        story: dict[str, Any],
        db: psycopg.Connection[Any],
    ) -> None:
        """The model quotes the *narration*; the row holds the *source*, verbatim.

        That is the property the span resolution exists for — and the one the old HTTP
        template could not have had, because it posted the model's own words to a route
        that does not accept them.
        """

        async def work(transport: McpToolTransport) -> Any:
            return await registry_for(settings, transport, story).invoke(
                "save_highlight",
                {
                    "quote": "Acme raised forty million dollars in a Series B.",
                    "note": "worth remembering",
                },
            )

        result = run(settings, work)

        assert result.ok, result.error
        assert (
            result.result["quote"]
            == story["quote"]
            != ("Acme raised forty million dollars in a Series B.")
        )
        assert result.result["note"] == "worth remembering"
        rows = phase2.list_highlights(db, user_id=repo.OWNER_USER_ID)
        assert [
            (row.news_item_id, row.span_start, row.span_end, row.episode_id) for row in rows
        ] == [(story["news_item_id"], story["span_start"], story["span_end"], story["episode_id"])]

    def test_an_id_that_is_not_there_is_a_sentence_the_persona_can_say(
        self, env: None, settings: VoiceSettings, story: dict[str, Any]
    ) -> None:
        async def work(transport: McpToolTransport) -> Any:
            return await registry_for(settings, transport, story).invoke(
                "mark_read", {"news_item_id": "no-such-item"}
            )

        result = run(settings, work)

        assert not result.ok
        assert result.error is not None and "does not exist" in result.error, (
            "the route's own 404 has to survive the tool error and the MCP envelope"
        )


class TestTheSurfaceTheVoiceServiceAsksFor:
    """What the credential can reach through this connection, and what it cannot."""

    def test_the_connection_lists_exactly_the_tools_the_two_groups_hold(
        self, env: None, settings: VoiceSettings
    ) -> None:
        async def work(transport: McpToolTransport) -> Any:
            client = await transport._connected()
            return sorted(tool.name for tool in (await client.list_tools()).tools)

        assert run(settings, work) == [
            "delete_highlight",
            "list_highlights",
            "list_news_items",
            "save_highlight",
            "set_news_item_read",
        ]

    @pytest.mark.parametrize(
        "tool",
        [
            "paste_text",  # ingestion: a model call per item, and metered
            "create_episode",  # episodes: a script completion and a full TTS render
            "create_smart_episode",
            "connect_source",  # sources: a mailbox consent
            "rotate_feed",  # feed: breaks every subscribed podcast app
            "get_admin_overview",  # admin: every user's data, and opt-in only
            "set_llm_config",
        ],
    )
    def test_nothing_that_spends_or_reaches_across_users_is_on_this_surface(
        self, env: None, settings: VoiceSettings, tool: str
    ) -> None:
        """The metered-spend bound, asserted rather than argued.

        Every pipeline stage that calls a model is reached by a tool in `ingestion`,
        `episodes` or `admin`, and none of those groups is asked for. A voice session
        cannot start a job, an episode or a model call through this connection at all.
        """

        async def work(transport: McpToolTransport) -> Any:
            return await transport.call_tool(tool, {})

        response = run(settings, work)
        assert not response.ok
        assert "Unknown tool" in str(response.payload.get("detail", "")), response.payload

    def test_the_platform_tools_are_all_on_the_surface_the_service_asks_for(
        self, env: None, settings: VoiceSettings, story: dict[str, Any]
    ) -> None:
        """Whatever the persona is offered must be callable, or it spends the session
        apologising — which is why the group selection and `PLATFORM_TOOLS` are one claim."""

        async def work(transport: McpToolTransport) -> Any:
            client = await transport._connected()
            listed = {tool.name for tool in (await client.list_tools()).tools}
            tools = build_platform_tools(settings, transport=transport, context=context_for(story))
            return [tool.tool for tool in tools.values() if tool.tool not in listed]

        assert run(settings, work) == []

    def test_the_api_sends_exactly_the_binding_this_service_resolves(self) -> None:
        """The two halves of the handshake, pinned against each other in one place."""
        assert [dict(server) for server in SESSION_MCP_SERVERS] == [
            {"name": "motet", "slug": "motet"}
        ]
        assert sorted(SESSION_TOOLS) == sorted(PLATFORM_TOOLS)
        assert DEFAULT_MCP_TOOL_GROUPS == "backlog,highlights"
