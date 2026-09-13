"""Connectors: the sites and remote MCP servers agentic enrichment may use (motet#102)."""

from __future__ import annotations

from typing import Annotated, Literal

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
    acknowledge_risk: Annotated[
        bool,
        Field(
            description=(
                "mcp: required, and only the owner can give it. The agent this server is "
                "handed to also reads untrusted web pages, which can steer it into using it."
            )
        ),
    ] = False,
) -> ConnectorResponse:
    """Add a site or a remote MCP server for agentic enrichment to use.

    A site needs only its domain. An MCP server is refused unless `acknowledge_risk` is true,
    which is the owner's decision to make, not an agent's, and it starts in `needs_auth`:
    nothing about it works until `authorize_connector` and a person's consent.
    """

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
                acknowledge_risk=acknowledge_risk,
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
