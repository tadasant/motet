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

**A connection lasts one call, and that is a correctness decision rather than a cost one.**
Holding one open across calls buys a handshake — one extra POST against a stateless server —
and costs the three things that made the first draft of this module wrong. The SDK's
transport is an ``anyio`` task group, and **anyio refuses to let a task close a cancel scope
another task entered**: this transport is process-wide, so the session task that opened the
connection is almost never the one that closes it, and the close raised
``RuntimeError: Attempted to exit cancel scope in a different task`` every time — swallowed,
because what else can a teardown do, which left a leak indistinguishable from a clean close.
The lifespan's own ``aclose`` on SIGTERM had the same fault. And one session's failed call
tore down a connection other sessions had calls in flight on. A connection owned by exactly
the task that uses it has none of those, needs no lock and no generation counter, and its
whole lifetime is one ``async with``.

**The httpx client is the thing that is shared**, which is what makes a per-call connection
cheap: the TCP connection and the TLS session live in its pool and are reused, so the
per-call cost is a JSON-RPC ``initialize`` round trip rather than a new socket.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Callable, Mapping
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
#: standing on a pavement waiting for an answer. Eight seconds of silence is a failed turn
#: whatever the eventual status says.
DEFAULT_TOOL_TIMEOUT_SECONDS: Final = 8.0

#: What the *HTTP client* allows a read to take, which is a different question from how long
#: this service waits for a tool. The streamable-HTTP transport holds a server→client GET
#: stream open for as long as the connection lasts, so an 8-second read deadline there would
#: expire it on a schedule, burn the SDK's two reconnection attempts, and then drop
#: server-sent messages with a debug line. Motet is stateless and opens no such stream, but
#: the reason this module uses the SDK at all is the servers it does not own.
#: :data:`DEFAULT_TOOL_TIMEOUT_SECONDS` is what bounds a call, per call.
STREAM_READ_TIMEOUT_SECONDS: Final = 300.0

#: The path Motet's MCP server is mounted at. It is a ``Route`` rather than a ``Mount`` on
#: the API side, so this must not carry a trailing slash: ``/mcp/`` is a 404 there.
MCP_PATH: Final = "/mcp"

#: Motet's tools raise ``ToolError(f"{status}: {detail}")`` — see ``motet_api.mcp.context``.
#: Recovering the number is what lets a 404 still read as "the item does not exist" rather
#: than as an opaque failure.
#:
#: Searched rather than anchored, because the SDK prefixes a tool's own message with
#: ``"Error executing tool <name>: "`` before it reaches a client. The first such group
#: wins, and only if it is a *failure* status — see :func:`decode`.
_STATUS_PREFIX: Final = re.compile(r"(?<!\d)([1-5]\d\d):\s*")

#: What a tool error is recorded as when the server said nothing a number could be read out
#: of. 400 rather than 500: the tool refused, and this service did reach it.
_UNKNOWN_STATUS: Final = 400

#: What the persona is told when the call did not reach Motet at all. Fixed prose rather
#: than the exception's own text, which is read out loud to a listener, handed to a model,
#: and sent to a browser: an SDK exception is often meaningless there ("unhandled errors in
#: a TaskGroup"), and an HTTP error's string carries the deployment's API hostname and this
#: connection's tool-group selection with it. The detail goes to the log.
UNREACHABLE = "the connection to Motet failed"


def motet_mcp_url(base_url: str, tool_groups: str = DEFAULT_MCP_TOOL_GROUPS) -> str:
    """``https://api.example`` → ``https://api.example/mcp?tool_groups=backlog,highlights``.

    The groups ride in the query string because that is the only place the server reads
    them from: a request body cannot widen what a connection may call.
    """
    return f"{base_url.rstrip('/')}{MCP_PATH}?{urlencode({'tool_groups': tool_groups})}"


class McpToolTransport:
    """Calls one MCP server, a connection per call, in the caller's own task.

    ``connect`` returns whatever :class:`mcp.Client` accepts — a fresh transport in a
    deployment, an in-process server object in a test — and is called once per tool call,
    which is also what the SDK's streamable-HTTP transport requires: it is a generator that
    can be entered once. Passing that in rather than a URL is what lets a test drive this
    class itself against a real server over ASGI, instead of asserting against a stub of it.
    """

    def __init__(
        self,
        connect: Callable[[], Any],
        *,
        timeout_seconds: float = DEFAULT_TOOL_TIMEOUT_SECONDS,
        close: Callable[[], Any] | None = None,
    ) -> None:
        self._connect = connect
        self._timeout = timeout_seconds
        self._close = close

    async def call_tool(self, name: str, arguments: Mapping[str, Any]) -> ToolResponse:
        try:
            async with Client(self._connect()) as client:
                result = await client.call_tool(
                    name, dict(arguments), read_timeout_seconds=self._timeout
                )
        except Exception as exc:  # noqa: BLE001 — a tool's errors are values, never raises
            # Mapped to a status rather than raised: the registry turns a failed result into
            # something the persona can say, and a transport blow-up mid-turn is the one
            # thing a voice session cannot recover from gracefully. Logged unwrapped,
            # because the SDK raises an ExceptionGroup whose own text says nothing.
            logger.warning("MCP tool %s could not be called: %s", name, explain_exception(exc))
            return ToolResponse(status=599, payload={"detail": UNREACHABLE})
        return decode(result)

    async def aclose(self) -> None:
        """Let go of the HTTP client. No connection is held, so there is nothing else."""
        if self._close is not None:
            await self._close()


def explain_exception(exc: BaseException) -> str:
    """An exception as a log line, with ``ExceptionGroup``s unwrapped.

    The SDK's transport is a task group, so a transport fault arrives as
    ``unhandled errors in a TaskGroup (1 sub-exception)`` — true, and useless.
    """
    if isinstance(exc, BaseExceptionGroup):
        return " | ".join(explain_exception(inner) for inner in exc.exceptions)
    return f"{type(exc).__name__}: {exc}"


def decode(result: Any) -> ToolResponse:
    """An SDK ``CallToolResult`` as the HTTP-shaped answer the tools are written against."""
    text = " ".join(
        part.text for part in (result.content or []) if getattr(part, "text", None)
    ).strip()
    if result.is_error:
        match = _STATUS_PREFIX.search(text)
        status = int(match.group(1)) if match else _UNKNOWN_STATUS
        # A status the server's prose happens to contain is only believed when it is a
        # failure: `ToolResponse.ok` is true below 400, so a "201" in an error's text would
        # report a failed call to the persona as a success — the one decoding mistake
        # nothing downstream can see.
        if match is None or status < 400:
            return ToolResponse(status=_UNKNOWN_STATUS, payload={"detail": text})
        return ToolResponse(status=status, payload={"detail": text[match.end() :]})
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
        timeout=httpx2.Timeout(DEFAULT_TOOL_TIMEOUT_SECONDS, read=STREAM_READ_TIMEOUT_SECONDS),
        headers={"Authorization": f"Bearer {settings.mcp_token}"} if settings.mcp_token else {},
    )
    logger.info(
        "slug %r resolves to the Motet API's %s, tool groups %s",
        MOTET_MCP_SLUG,
        MCP_PATH,
        settings.mcp_tool_groups,
    )
    return McpToolTransport(
        lambda: streamable_http_client(url, http_client=http), close=http.aclose
    )
