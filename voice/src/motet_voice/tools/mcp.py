"""The MCP client that reaches Motet's own server at ``/mcp`` (motet#120).

**One slug, resolved here and never by a client.** :class:`~motet_voice.contract.McpServerBinding`
carries a name and a slug; where ``motet`` points, which tool groups it asks for, and what
credential it presents are all this service's configuration. A client that could supply a
URL would have turned the voice service into an open proxy, and one that could supply a
token would have made it a confused deputy.

**The SDK's client, not a hand-rolled one.** The binding exists because Zimmer will point it
at servers nobody here wrote, so the half of MCP this service speaks has to be the real
protocol rather than the subset Motet's own server happens to accept today. It is also the
same client the API's own ``/mcp`` tests drive, which is what makes those tests evidence
about this path.

**The connection is held open across calls, and reopened after a failure.** A ``tools/call``
inside a conversational turn is a listener standing on a pavement, so the handshake is paid
once per process rather than once per question. Motet's server is stateless, so a held
client is an HTTP connection and a negotiated protocol version — there is no server-side
session to go stale, and a dropped connection costs one reopen on the next call.
"""

from __future__ import annotations

import asyncio
import logging
import re
from collections.abc import Awaitable, Callable, Mapping
from contextlib import AsyncExitStack
from typing import Any, Final
from urllib.parse import urlencode

# Imported at module scope, not lazily inside the call. **A lazy import is a statement
# about when, never about whether** — AGENTS.md's `motet-vault[kms]` lesson, which cost a
# production Gmail connect: the SDK went missing at *build* time and the first line of code
# to notice was an import inside a request. Deferred here, a voice image built without
# `mcp` would look healthy and fail as a 599 inside a listener's turn. `bin/build-images`
# asks the real container.
import httpx2
from mcp import Client
from mcp.client.streamable_http import streamable_http_client

from ..config import DEFAULT_MCP_TOOL_GROUPS, MOTET_MCP_SLUG, VoiceSettings
from .spec import ToolResponse

logger = logging.getLogger("motet.voice.tools.mcp")

#: Short on purpose. A tool call happens inside a conversational turn — a listener is
#: standing on a pavement waiting for an answer. Ten seconds of silence is a failed turn
#: whatever the eventual status says.
DEFAULT_TOOL_TIMEOUT_SECONDS: Final = 8.0

#: The path Motet's MCP server is mounted at. It is a ``Route`` rather than a ``Mount`` on
#: the API side, so this must not carry a trailing slash: ``/mcp/`` is a 404 there.
MCP_PATH: Final = "/mcp"

#: Motet's tools raise ``ToolError(f"{status}: {detail}")`` — see ``motet_api.mcp.context``.
#: Recovering the number is what lets a 404 still read as "the item does not exist" rather
#: than as an opaque failure.
#:
#: Searched rather than anchored, because the SDK prefixes a tool's own message with
#: ``"Error executing tool <name>: "`` before it reaches a client. The first such group
#: wins; a server that is not Motet simply has none, and its errors keep their own words
#: under :data:`_UNKNOWN_STATUS`.
_STATUS_PREFIX: Final = re.compile(r"(?<!\d)(\d{3}):\s*")

#: What a tool error is recorded as when the server said nothing a number could be read out
#: of. 400 rather than 500: the tool refused, and this service did reach it.
_UNKNOWN_STATUS: Final = 400


def motet_mcp_url(base_url: str, tool_groups: str = DEFAULT_MCP_TOOL_GROUPS) -> str:
    """``https://api.example`` → ``https://api.example/mcp?tool_groups=backlog,highlights``.

    The groups ride in the query string because that is the only place the server reads
    them from: a request body cannot widen what a connection may call.
    """
    return f"{base_url.rstrip('/')}{MCP_PATH}?{urlencode({'tool_groups': tool_groups})}"


class McpToolTransport:
    """One MCP connection to one server, opened on demand and reused.

    ``connect`` returns whatever :class:`mcp.Client` accepts — a fresh transport in a
    deployment, an in-process server object in a test — and is called again on every
    reopen, because the SDK's streamable-HTTP transport is a generator that can be entered
    once. Passing that in rather than a URL is what lets a test drive this class itself
    against a real server over ASGI, instead of asserting against a stub of it.
    """

    def __init__(
        self,
        connect: Callable[[], Any],
        *,
        timeout_seconds: float = DEFAULT_TOOL_TIMEOUT_SECONDS,
        on_close: Callable[[], Awaitable[None]] | None = None,
    ) -> None:
        self._connect = connect
        self._timeout = timeout_seconds
        self._on_close = on_close
        self._client: Any | None = None
        self._stack: AsyncExitStack | None = None
        self._lock = asyncio.Lock()

    async def _connected(self) -> Any:
        """The open client, opening one if there is none.

        Serialized, so two turns racing for the first tool call of a session open one
        connection rather than two.
        """
        async with self._lock:
            if self._client is not None:
                return self._client
            stack = AsyncExitStack()
            try:
                client = await stack.enter_async_context(Client(self._connect()))
            except BaseException:
                await stack.aclose()
                raise
            self._stack, self._client = stack, client
            return client

    async def _drop(self) -> None:
        """Let go of a connection that failed, so the next call opens a fresh one."""
        async with self._lock:
            stack, self._stack, self._client = self._stack, None, None
        if stack is not None:
            try:
                await stack.aclose()
            except Exception:  # noqa: BLE001 — closing a broken connection is best effort
                logger.debug("closing a failed MCP connection raised", exc_info=True)

    async def call_tool(self, name: str, arguments: Mapping[str, Any]) -> ToolResponse:
        try:
            client = await self._connected()
            result = await client.call_tool(
                name, dict(arguments), read_timeout_seconds=self._timeout
            )
        except Exception as exc:  # noqa: BLE001 — a tool's errors are values, never raises
            # Mapped to a status rather than raised: the registry turns a failed result into
            # something the persona can say, and a transport blow-up mid-turn is the one
            # thing a voice session cannot recover from gracefully.
            logger.warning("MCP tool %s could not be called: %r", name, exc)
            await self._drop()
            return ToolResponse(status=599, payload={"detail": f"could not reach Motet: {exc}"})
        return decode(result)

    async def aclose(self) -> None:
        await self._drop()
        if self._on_close is not None:
            await self._on_close()


def decode(result: Any) -> ToolResponse:
    """An SDK ``CallToolResult`` as the HTTP-shaped answer the tools are written against."""
    text = " ".join(
        part.text for part in (result.content or []) if getattr(part, "text", None)
    ).strip()
    if result.is_error:
        match = _STATUS_PREFIX.search(text)
        status = int(match.group(1)) if match else _UNKNOWN_STATUS
        detail = text[match.end() :] if match else text
        return ToolResponse(status=status, payload={"detail": detail})
    structured = result.structured_content
    if isinstance(structured, dict):
        return ToolResponse(status=200, payload=structured)
    # A tool that returned no structured content still said something; the persona can read
    # it out, and a tool result a model cannot see is worse than a loosely typed one.
    return ToolResponse(status=200, payload={"data": text})


def build_motet_transport(settings: VoiceSettings) -> McpToolTransport | None:
    """The ``motet`` slug, resolved. ``None`` when this deployment has no API to reach."""
    if not settings.api_base_url:
        return None
    url = motet_mcp_url(settings.api_base_url, settings.mcp_tool_groups)
    http = httpx2.AsyncClient(
        timeout=DEFAULT_TOOL_TIMEOUT_SECONDS,
        headers={"Authorization": f"Bearer {settings.mcp_token}"} if settings.mcp_token else {},
    )
    logger.info(
        "slug %r resolves to the Motet API's %s, tool groups %s",
        MOTET_MCP_SLUG,
        MCP_PATH,
        settings.mcp_tool_groups,
    )
    return McpToolTransport(
        lambda: streamable_http_client(url, http_client=http), on_close=http.aclose
    )
