"""Motet's MCP server, served by ``motet-api`` at ``/mcp`` (motet#111).

**Placement and shape were Tadas's picks** (2026-09-13, on motet#111): mounted on the
existing API rather than a separate service (A1), hand-written in-process tools held at
parity by a test over the route table (B1), per-user OAuth (C2), admin as an opt-in group
(D1), tools only — no resources and no prompts (E1, F1).

**Stateless, and JSON rather than a stream.** A 2026-07-28 request is one self-contained
POST with no session, and ``stateless_http`` plus ``json_response`` give a legacy client
the same, so any API instance serves any request and nothing needs affinity.

Three things here are traps rather than style, each verified against the installed SDK:

* **DNS-rebinding protection is off.** With it on, every request whose ``Host`` is not
  localhost is a ``421 Misdirected Request``. Cloud Run's frontend owns ``Host``, the
  credential is an explicit header rather than an ambient cookie, and the hostnames are a
  private-repo fact this public repo cannot allowlist.
* **The session manager runs inside the API's lifespan** (``motet_api.main.lifespan``,
  through :meth:`McpMount.running`). Without it the first request fails with ``Task group
  is not initialized``.
* **``/mcp`` is a ``Route``, not a ``Mount``.** A mount answers ``POST /mcp`` with a 307 to
  ``/mcp/``, and a client that does not replay a POST body across a redirect breaks.

**Authentication happens before the transport sees a byte**, in :class:`McpEndpoint`, and it
is ``deps.require_caller`` itself: the shared API token in constant time, else a session row
with the allowlist re-checked. An MCP client's OAuth access token *is* a session row, so the
same check covers it. The feed token is not a bearer credential there and is refused. An
unauthenticated request is a 401 carrying RFC 9728's ``resource_metadata`` pointer when
authorization is configured, which is what starts a client's OAuth discovery.
"""

from __future__ import annotations

import contextlib
import logging
from collections.abc import AsyncIterator
from typing import Any

import anyio
from fastapi import HTTPException
from mcp.server import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.server.transport_security import TransportSecuritySettings
from mcp_types import ToolAnnotations
from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.routing import BaseRoute, Route
from starlette.types import ASGIApp, Receive, Scope, Send

from ..config import Settings
from ..deps import Caller, connection, drain_trigger, require_caller
from ..drain import DrainNudge
from . import registry
from .context import request_scope, selected_tools
from .oauth import MCP_PATH, PROTECTED_RESOURCE_PATH, oauth_routes, oauth_setup
from .tools import IMPLEMENTATIONS

logger = logging.getLogger("motet.api.mcp")

INSTRUCTIONS = """Motet turns a reading backlog (newsletters, pastes) into a private podcast.

How the pieces fit: text arrives as source items (paste_text, or a connected Gmail source whose
messages wait in list_held_source_items until integrate_source_items). Integration deduplicates
them into news items, the backlog (list_news_items). An episode (create_episode,
create_smart_episode) scripts and narrates unread news items; it renders on a queue over
minutes, so poll get_episode until state is ready. The podcast feed (get_feed) is how it is
heard. Listening marks stories read (set_playback_position), the same fact as
set_news_item_read.

Tools that queue work (paste_text, integrate_source_items, create_episode, create_smart_episode)
spend inference and text-to-speech money. Consent steps (connect_source, reauthorize_source)
return a URL that only a person can approve. If something seems stuck, get_ingestion_status and
get_processing_status say why."""


class MotetMCPServer(MCPServer):
    """An ``MCPServer`` that lists and calls only the tools this connection selected."""

    async def list_tools(self) -> Any:
        visible = selected_tools()
        return [tool for tool in await super().list_tools() if tool.name in visible]

    async def call_tool(self, name: str, arguments: dict[str, Any], context: Any = None) -> Any:
        if name not in selected_tools():
            raise ToolError(
                f"Unknown tool {name!r} on this connection. Tools outside the connection's "
                f"groups are not callable; groups are chosen with ?{registry.GROUPS_PARAM}=."
            )
        return await super().call_tool(name, arguments, context)


def build_server() -> MotetMCPServer:
    """The server, with every registered tool and nothing else.

    Refuses to build when the registry and the tool modules disagree, so a tool that was
    written but never registered, or registered but never written, stops the process at
    import rather than shipping a surface the parity test was not looking at.
    """
    registered = {tool.name for tool in registry.ALL_TOOLS}
    if registered != set(IMPLEMENTATIONS):
        raise RuntimeError(
            "motet_api.mcp: registry and tool modules disagree: "
            f"registered without a function {sorted(registered - set(IMPLEMENTATIONS))}, "
            f"a function without a registration {sorted(set(IMPLEMENTATIONS) - registered)}"
        )
    server = MotetMCPServer(name="motet", title="Motet", instructions=INSTRUCTIONS)
    for tool in registry.ALL_TOOLS:
        server.add_tool(
            IMPLEMENTATIONS[tool.name],
            name=tool.name,
            annotations=ToolAnnotations(
                read_only_hint=not tool.write,
                destructive_hint=tool.destructive if tool.write else None,
                idempotent_hint=tool.idempotent if tool.write else None,
                # Every tool acts on this deployment's own data. The few that reach a
                # vendor (a consent URL, a poll) do it through Motet, not on the model's say.
                open_world_hint=False,
            ),
        )
    return server


def www_authenticate(config: Settings, presented: bool) -> str:
    """The challenge on a 401: RFC 9728's metadata pointer when authorization is configured."""
    parts = ['error="invalid_token"'] if presented else []
    setup = oauth_setup(config)
    if setup is not None:
        parts.append(f'resource_metadata="{setup.issuer}{PROTECTED_RESOURCE_PATH}"')
    return "Bearer" + (" " + ", ".join(parts) if parts else "")


def authenticate(authorization: str | None) -> Caller | Response:
    """``require_caller``, on a connection of its own, answered as an HTTP response on refusal.

    A refusal here lands in the same failed-auth throttle a ``/v1`` refusal does
    (``motet_api.throttle``): ``/mcp`` is reachable by anyone who can reach the service,
    and a bearer guessed at here costs exactly what one guessed at there does. That needs
    nothing from the request — the throttle has no key — so nothing is threaded across
    the thread boundary below for it.
    """
    config = Settings.from_env()
    try:
        with contextlib.contextmanager(connection)(config, DrainNudge(drain_trigger())) as conn:
            return require_caller(config=config, conn=conn, authorization=authorization)
    except HTTPException as exc:
        headers = dict(exc.headers or {})
        if exc.status_code == 401:
            headers["WWW-Authenticate"] = www_authenticate(config, presented=bool(authorization))
        return JSONResponse(
            {
                "error": "invalid_token" if exc.status_code == 401 else "unavailable",
                "error_description": exc.detail,
            },
            status_code=exc.status_code,
            headers=headers,
        )


class McpEndpoint:
    """``/mcp``: pick the tool groups, authenticate, then hand the request to the transport."""

    def __init__(self) -> None:
        #: The transports of the lifespans running now, oldest first. Almost always zero or
        #: one; a list, because a lifespan that ends must take away its own transport and
        #: not whichever one a lifespan started after it installed.
        self.running: list[ASGIApp] = []

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        transport = self.running[-1] if self.running else None
        if transport is None:
            await JSONResponse(
                {
                    "error": "unavailable",
                    "error_description": "The MCP transport is not running in this process.",
                },
                status_code=503,
            )(scope, receive, send)
            return
        request = Request(scope, receive)
        try:
            # The query string only, never the body: a JSON-RPC message must not be able
            # to widen what the connection it arrived on may call.
            tools = registry.select_tools(request.query_params.get(registry.GROUPS_PARAM))
        except registry.ToolGroupsError as exc:
            await JSONResponse(
                {"error": "invalid_request", "error_description": str(exc)}, status_code=400
            )(scope, receive, send)
            return
        outcome = await anyio.to_thread.run_sync(authenticate, request.headers.get("authorization"))
        if isinstance(outcome, Response):
            await outcome(scope, receive, send)
            return
        with request_scope(outcome, tools, request):
            await transport(scope, receive, send)


class McpMount:
    """``/mcp`` on one app: the server, its endpoint, and the transport a lifespan runs."""

    def __init__(self, server: MotetMCPServer) -> None:
        self.server = server
        self.endpoint = McpEndpoint()

    @contextlib.asynccontextmanager
    async def running(self) -> AsyncIterator[None]:
        """Run a fresh transport for one lifespan of the app.

        **Fresh each time, because a session manager runs once.** The SDK refuses a second
        ``run()`` on one instance, and a process can start the app's lifespan many times —
        every test that opens a ``TestClient`` does. Building the transport here rather than
        at import is what lets the second start work instead of failing on it.
        """
        transport = self.server.streamable_http_app(
            streamable_http_path=MCP_PATH,
            stateless_http=True,
            json_response=True,
            transport_security=TransportSecuritySettings(enable_dns_rebinding_protection=False),
        )
        async with self.server.session_manager.run():
            self.endpoint.running.append(transport)
            try:
                yield
            finally:
                self.endpoint.running.remove(transport)


def mount(routes: list[BaseRoute]) -> McpMount:
    """Add ``/mcp`` and the OAuth endpoints to an app's routes.

    The caller owns the lifespan and must enter :meth:`McpMount.running` inside it: without
    it every ``/mcp`` request is a 503, because there is no transport to hand it to.
    """
    mcp = McpMount(build_server())
    routes.append(Route(MCP_PATH, endpoint=mcp.endpoint, methods=["GET", "POST", "DELETE"]))
    routes.extend(oauth_routes())
    return mcp
