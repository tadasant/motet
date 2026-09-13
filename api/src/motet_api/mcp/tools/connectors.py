"""Connectors: the sites and remote MCP servers agentic enrichment may use (motet#102)."""

from __future__ import annotations

from typing import Annotated, Literal

from mcp.server.mcpserver.exceptions import ToolError
from pydantic import Field

from ...deps import dek_wrapper
from ...schemas import (
    AuthorizeConnectorRequest,
    AuthorizeConnectorResponse,
    ConnectorResponse,
    CreateConnectorRequest,
)
from ..context import ActionResult, RouteCall, routes, run
from .sources import consent_redirect

_CONNECTOR_ID = Field(description="The connector's id, from list_connectors.")


def list_connectors() -> list[ConnectorResponse]:
    """List every connector: sites Motet may fetch articles from, and remote MCP servers.

    Each says whether a secret is stored (never the secret itself) and its `status`:
    `ready`, `needs_auth` (an MCP server nobody has authorized yet) or `error`.
    """
    return run("list_connectors", lambda c: routes.list_connectors(conn=c.conn, user_id=c.user_id))


def create_connector(
    kind: Annotated[
        Literal["site", "mcp"],
        Field(description="'site': a domain to fetch articles from. 'mcp': a remote MCP server."),
    ],
    label: Annotated[
        str, Field(description="A name to show. Omit to use the domain or host.")
    ] = "",
    domain: Annotated[
        str | None, Field(description="site: the domain, in any spelling, e.g. example.com.")
    ] = None,
    username: Annotated[str | None, Field(description="site: the login, if it needs one.")] = None,
    password: Annotated[
        str | None,
        Field(
            description=(
                "site: the password, if it needs one. Sealed on arrival and never readable "
                "again; the Credentials screen is the better place to type one."
            )
        ),
    ] = None,
    url: Annotated[str | None, Field(description="mcp: the server's https URL.")] = None,
    domains: Annotated[
        list[str] | None,
        Field(description="mcp: the sites this server is used for. Omit for all."),
    ] = None,
) -> ConnectorResponse:
    """Add a site for agentic enrichment to fetch articles from.

    A site needs only its domain. A remote MCP server (`kind='mcp'`) is always refused here:
    adding one needs the owner to acknowledge its risk on the Credentials screen, and no
    MCP client can give that acknowledgement on the owner's behalf.
    """
    if kind == "mcp":
        # The route's `acknowledge_risk` is the control, and a tool argument is not a person
        # reading the warning: an agent that reads untrusted pages could tick it itself.
        raise ToolError(
            "403: An MCP server connector is added on the Credentials screen, where a person "
            "acknowledges its risk. An MCP client cannot acknowledge it for them."
        )

    def call(c: RouteCall) -> ConnectorResponse:
        return routes.create_connector(
            body=CreateConnectorRequest(
                kind=kind,
                label=label,
                domain=domain,
                username=username,
                password=password,
                url=url,
                domains=domains or [],
            ),
            conn=c.conn,
            user_id=c.user_id,
            wrapper=dek_wrapper(),
        )

    return run("create_connector", call)


def delete_connector(connector_id: Annotated[str, _CONNECTOR_ID]) -> ActionResult:
    """Delete a connector and the secret stored with it. There is no undo."""

    def call(c: RouteCall) -> ActionResult:
        routes.delete_connector(conn=c.conn, user_id=c.user_id, connector_id=connector_id)
        return ActionResult(done=True, detail=f"Deleted {connector_id}.")

    return run("delete_connector", call)


def authorize_connector(
    connector_id: Annotated[str, _CONNECTOR_ID],
    redirect_uri: Annotated[
        str | None,
        Field(
            description=(
                "Where the server's consent returns the person. Omit it to use this "
                "deployment's web app, which is almost always right."
            )
        ),
    ] = None,
) -> AuthorizeConnectorResponse:
    """Start authorizing a remote MCP server connector. Returns a consent URL a person must open.

    Discovers the server's authorization server and registers Motet with it; nothing about
    the connector changes until the person approves at `authorization_url`.
    """

    def call(c: RouteCall) -> AuthorizeConnectorResponse:
        return routes.authorize_connector(
            body=AuthorizeConnectorRequest(redirect_uri=consent_redirect(c.config, redirect_uri)),
            conn=c.conn,
            user_id=c.user_id,
            config=c.config,
            connector_id=connector_id,
        )

    return run("authorize_connector", call)


TOOLS = (list_connectors, create_connector, delete_connector, authorize_connector)
