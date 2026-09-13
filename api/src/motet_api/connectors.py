"""PROTOTYPE — the Credentials screen's routes: connectors for the agentic enrichment step.

Kept out of ``main.py`` for ``admin_llm``'s reason: the prototype is one file to delete.

Four things about these routes are the design rather than the implementation:

* **No route ever returns a secret.** ``GET /v1/connectors`` reports ``has_secret`` and
  nothing about what it is; the repository functions the routes call cannot return the
  envelope either. The API holds the vault's encrypt-only half (invariant 8).
* **A site connector with no password is a complete row.** The Information logs in by
  emailed code; a required password field would make the one site this was asked for
  impossible to store.
* **Authorizing an MCP server is discovery → registration → consent, and it stops at the
  first step the server does not support.** A server without dynamic client registration
  leaves the row ``needs_auth`` with the reason in ``last_error`` and a 409 with the
  same sentence — a pre-registered client id is a human-owned step (invariant 9).
* **The callback lands on the SPA's one path and the ``connector.`` state prefix is what
  routes it here.** The state row lives in ``oauth_states`` beside sign-in's and Gmail's;
  ``consume_oauth_state`` spends it once, exactly as the other two flows do.
"""

from __future__ import annotations

import logging
from typing import Annotated, Any

import psycopg
from fastapi import APIRouter, Depends, HTTPException, Path, Response, status
from motet_db import connectors as repo
from motet_db import phase2
from motet_sources import new_pkce_pair
from motet_vault import DekWrapper, VaultError

from .auth import is_login_state
from .deps import connection, dek_wrapper, require_api_token
from .mcp_oauth import (
    PROVIDER,
    AuthorizationServer,
    McpOAuthError,
    RegistrationUnsupportedError,
    authorization_url,
    build_mcp_oauth_client,
    is_connector_state,
    new_connector_state,
)
from .schemas import (
    AuthorizeConnectorRequest,
    AuthorizeConnectorResponse,
    ConnectorOAuthCallbackRequest,
    ConnectorResponse,
    CreateConnectorRequest,
)

logger = logging.getLogger("motet.api.connectors")

router = APIRouter(tags=["connectors"])

Conn = Annotated[psycopg.Connection[Any], Depends(connection, scope="function")]
User = Annotated[str, Depends(require_api_token)]
Wrapper = Annotated[DekWrapper, Depends(dek_wrapper)]


@router.get("/v1/connectors", response_model=list[ConnectorResponse])
def list_connectors(conn: Conn, user_id: User) -> list[ConnectorResponse]:
    return [_response(c) for c in repo.list_connectors(conn, user_id)]


@router.post(
    "/v1/connectors", response_model=ConnectorResponse, status_code=status.HTTP_201_CREATED
)
def create_connector(
    body: CreateConnectorRequest, conn: Conn, user_id: User, wrapper: Wrapper
) -> ConnectorResponse:
    label = body.label.strip()
    if body.kind == repo.SITE:
        domain = repo.normalize_domain(body.domain or "")
        username = (body.username or "").strip()
        if not domain or "." not in domain:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, "A site connector needs a domain.")
        if not username:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, "A site connector needs a username.")
        password = (body.password or "").strip() or None
        try:
            created = repo.create_connector(
                conn,
                wrapper,
                user_id=user_id,
                kind=repo.SITE,
                label=label,
                domain=domain,
                username=username,
                secret=password,
            )
        except psycopg.errors.UniqueViolation as exc:
            raise HTTPException(
                status.HTTP_409_CONFLICT,
                f"There is already a site login for {domain}. Remove it to replace it.",
            ) from exc
        except VaultError as exc:
            raise _VAULT_REFUSED from exc
        return _response(created)

    if body.kind == repo.MCP:
        url = (body.url or "").strip()
        if not url.startswith(("https://", "http://localhost", "http://127.0.0.1")):
            raise HTTPException(status.HTTP_400_BAD_REQUEST, "An MCP server URL must be https.")
        domains = [d for d in (repo.normalize_domain(d) for d in body.domains) if d]
        created = repo.create_connector(
            conn, wrapper, user_id=user_id, kind=repo.MCP, label=label, url=url, domains=domains
        )
        return _response(created)

    raise HTTPException(status.HTTP_400_BAD_REQUEST, "kind must be 'site' or 'mcp'.")


@router.delete("/v1/connectors/{connector_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_connector(conn: Conn, user_id: User, connector_id: Annotated[str, Path()]) -> Response:
    if not repo.delete_connector(conn, user_id=user_id, connector_id=connector_id):
        raise HTTPException(status.HTTP_404_NOT_FOUND, "No such connector.")
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.post("/v1/connectors/{connector_id}/authorize", response_model=AuthorizeConnectorResponse)
def authorize_connector(
    body: AuthorizeConnectorRequest,
    conn: Conn,
    user_id: User,
    connector_id: Annotated[str, Path()],
) -> AuthorizeConnectorResponse:
    """Discover the server's authorization server, register a client, mint a consent URL.

    Discovery runs on every authorize rather than once: the row records what it found so
    a *refresh* can skip it, but a human re-authorizing is the moment to notice a server
    that moved. Registration is skipped when the row already carries a client id — strad's
    are stateless and long-lived, and re-registering would mint one per click.
    """
    connector = repo.get_connector(conn, connector_id, user_id=user_id)
    if connector is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "No such connector.")
    if connector.kind != repo.MCP or connector.url is None:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Only MCP connectors are authorized.")

    client = build_mcp_oauth_client()
    try:
        server = client.discover(connector.url)
    except McpOAuthError as exc:
        raise _recorded(conn, connector.id, "error", status.HTTP_502_BAD_GATEWAY, exc) from exc

    client_id = connector.oauth_client_id if connector.oauth_issuer == server.issuer else None
    if client_id is None:
        try:
            client_id = client.register(server, redirect_uri=body.redirect_uri)
        except RegistrationUnsupportedError as exc:
            raise _recorded(
                conn, connector.id, "needs_auth", status.HTTP_409_CONFLICT, exc
            ) from exc
        except McpOAuthError as exc:
            raise _recorded(conn, connector.id, "error", status.HTTP_502_BAD_GATEWAY, exc) from exc
    repo.set_connector_oauth_client(
        conn,
        connector.id,
        issuer=server.issuer,
        client_id=client_id,
        token_endpoint=server.token_endpoint,
        resource=server.resource,
    )

    phase2.purge_expired_oauth_states(conn)
    verifier, challenge = new_pkce_pair()
    state = new_connector_state()
    phase2.start_oauth(
        conn,
        state=state,
        user_id=user_id,
        provider=PROVIDER,
        source_id_=None,
        connector_id_=connector.id,
        code_verifier=verifier,
        redirect_uri=body.redirect_uri,
        scopes=server.scopes,
    )
    url = authorization_url(
        server,
        client_id=client_id,
        redirect_uri=body.redirect_uri,
        state=state,
        code_challenge=challenge,
    )
    return AuthorizeConnectorResponse(authorization_url=url, state=state)


@router.post("/v1/connectors/oauth/callback", response_model=ConnectorResponse)
def connector_oauth_callback(
    body: ConnectorOAuthCallbackRequest, conn: Conn, user_id: User, wrapper: Wrapper
) -> ConnectorResponse:
    """Exchange the code, seal the token set onto the connector, mark it ready.

    The token set exists as a local variable and nowhere else; it is sealed under
    ``user_id:connector_id:mcp`` and the API cannot read it back.
    """
    state = body.state.strip()
    if is_login_state(state) or not is_connector_state(state):
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            "That callback does not belong to a connector authorization.",
        )
    pending = phase2.consume_oauth_state(conn, state)
    if pending is None or pending["user_id"] != user_id or pending["provider"] != PROVIDER:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            "This authorization is unknown, already used, or expired. Start again.",
        )
    connector = repo.get_connector(conn, pending["connector_id"] or "", user_id=user_id)
    if connector is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "The connector being authorized is gone.")
    if not (
        connector.oauth_client_id and connector.oauth_token_endpoint and connector.oauth_resource
    ):
        raise HTTPException(status.HTTP_409_CONFLICT, "This connector was never sent to consent.")
    if body.iss and connector.oauth_issuer and body.iss.rstrip("/") != connector.oauth_issuer:
        # RFC 9207: a code delivered under another issuer is a mix-up, not a grant.
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST, "The authorization response came from a different issuer."
        )

    server = AuthorizationServer(
        issuer=connector.oauth_issuer or "",
        authorization_endpoint="",
        token_endpoint=connector.oauth_token_endpoint,
        registration_endpoint=None,
        resource=connector.oauth_resource,
        scopes=tuple((pending.get("scopes") or "").split()),
        iss_parameter_supported=False,
    )
    try:
        tokens = build_mcp_oauth_client().exchange_code(
            server,
            client_id=connector.oauth_client_id,
            code=body.code,
            redirect_uri=pending["redirect_uri"],
            code_verifier=pending["code_verifier"],
        )
    except McpOAuthError as exc:
        raise _recorded(conn, connector.id, "needs_auth", status.HTTP_400_BAD_REQUEST, exc) from exc

    try:
        stored = repo.store_connector_secret(
            conn,
            wrapper,
            connector_id=connector.id,
            secret=tokens.to_json(),
            expires_at=tokens.expires_at,
        )
    except VaultError as exc:
        logger.exception("could not seal the token set for connector %s: %s", connector.id, exc)
        raise _VAULT_REFUSED from exc
    return _response(stored)


def _recorded(
    conn: psycopg.Connection[Any],
    connector_id: str,
    outcome: repo.ConnectorStatus,
    http_status: int,
    exc: McpOAuthError,
) -> HTTPException:
    """Write the reason onto the row and *commit it* before the request fails.

    ``deps.connection`` rolls back on the exception about to be raised, which would undo
    the very ``last_error`` the screen needs to explain the pill — the same reason
    ``require_caller`` commits its revoke before raising.
    """
    repo.set_connector_status(conn, connector_id, status=outcome, last_error=str(exc))
    conn.commit()
    return HTTPException(http_status, str(exc))


#: The vault refused to seal — never fall back to storing plaintext (invariant 8).
_VAULT_REFUSED = HTTPException(
    status.HTTP_503_SERVICE_UNAVAILABLE,
    "This credential could not be stored securely, so it was not stored at all.",
)


def _response(c: repo.StoredConnector) -> ConnectorResponse:
    return ConnectorResponse(
        id=c.id,
        kind=c.kind,
        label=c.label,
        domain=c.domain,
        domains=list(c.domains),
        url=c.url,
        username=c.username,
        has_secret=c.has_secret,
        secret_expires_at=c.secret_expires_at,
        oauth_issuer=c.oauth_issuer,
        oauth_registered=c.oauth_client_id is not None,
        status=c.status,
        last_error=c.last_error,
        created_at=c.created_at,
        updated_at=c.updated_at,
    )
