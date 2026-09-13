"""Motet as the OAuth authorization server for its own ``/mcp`` (motet#111, pick C2).

Not to be confused with ``motet_sources.mcp_oauth`` (motet#102), which is the opposite
direction: Motet as a *client* of somebody else's MCP server.

**Why Motet is the issuer.** Per-user auth needs something to issue tokens a client can
obtain on its own, and Google cannot be that: its access tokens are not audience-bound to
Motet, and it offers no dynamic client registration, so a generic MCP client could not even
begin. So Motet runs the authorization server the MCP spec describes — RFC 8414 metadata,
RFC 7591 registration, PKCE, RFC 8707 resource indicators, revocation — using the SDK's own
handlers, and uses Google only for what it already uses it for: proving which allowlisted
person is at the keyboard.

**The flow, end to end**:

1. The client registers (``/register``) and sends its user to ``/authorize``.
2. :meth:`MotetOAuthProvider.authorize` stores the client's request on an ``oauth_states``
   row with an ``mcp.``-prefixed state, and redirects to Google sign-in — returning to the
   SPA's one registered ``/oauth/callback``, so no new redirect URI has to be registered on
   the Google OAuth client (a human-owned step, invariant 9).
3. The SPA sees the ``mcp.`` state and posts the code to ``/v1/auth/mcp/callback``, which
   verifies the identity exactly as sign-in does, checks the allowlist, and mints an
   authorization code for the client (:func:`complete_authorization`).
4. **The SPA shows the person which client is asking and where the grant will go**, and only
   navigates to the client's redirect URI if they press Allow. Without that step a link to
   ``/authorize`` from anyone's registered client would complete silently for somebody
   already signed in to Google.
5. The client redeems the code at ``/token`` for an access token — an ``auth_sessions`` row
   with an hour's life — and a refresh token.

**Dormant until configured**, like every other path here that depends on a deployment fact.
It needs ``MOTET_PUBLIC_BASE_URL`` (the issuer: where clients reach this API),
``MOTET_APP_BASE_URL`` (where Google returns the person) and a working sign-in. Without
them the OAuth endpoints answer 404, ``/mcp`` still takes the ``/v1`` bearer, and
``/internal/health`` says ``mcp_oauth_configured: false``.
"""

from __future__ import annotations

import functools
import json
import logging
import re
import secrets
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, Final
from urllib.parse import urlsplit

import anyio
import psycopg
from fastapi import HTTPException, status
from mcp.server.auth.provider import (
    AccessToken,
    AuthorizationCode,
    AuthorizationParams,
    AuthorizeError,
    IdentityAssertionParams,
    RefreshToken,
    RegistrationError,
    TokenError,
    construct_redirect_uri,
)
from mcp.server.auth.routes import create_auth_routes, create_protected_resource_routes
from mcp.server.auth.settings import ClientRegistrationOptions, RevocationOptions
from mcp.shared.auth import OAuthClientInformationFull, OAuthToken
from motet_db import auth as auth_repo
from motet_db import mcp_oauth, phase2, repo
from motet_sources import new_pkce_pair
from pydantic import AnyHttpUrl, AnyUrl
from starlette.responses import JSONResponse
from starlette.routing import Route
from starlette.types import ASGIApp, Receive, Scope, Send

from ..auth import (
    ALLOWED_EMAILS_ENV,
    LOGIN_SCOPES,
    IdentityConfigError,
    IdentityError,
    IdentityUnavailableError,
    build_identity_provider,
    is_allowed,
    new_nonce,
)
from ..auth import PROVIDER as GOOGLE_PROVIDER
from ..config import CALLBACK_PATH, Settings
from ..schemas import McpAuthorizationResponse

logger = logging.getLogger("motet.api.mcp")

MCP_PATH: Final = "/mcp"

#: Marks a Google sign-in ``state`` as belonging to an MCP client's authorization. A dot
#: cannot occur in ``secrets.token_urlsafe`` output, so no other state can look like one.
#: Keep in step with ``MCP_STATE_PREFIX`` in ``web/src/oauth.ts``.
MCP_STATE_PREFIX: Final = "mcp."

PROTECTED_RESOURCE_PATH: Final = "/.well-known/oauth-protected-resource" + MCP_PATH

#: Schemes that run or render in the browser's own origin rather than hand off to a client.
REFUSED_REDIRECT_SCHEMES: Final = frozenset(
    {"javascript", "data", "vbscript", "file", "blob", "about"}
)

#: What one unauthenticated registration may store. A client needs a handful of redirect URIs
#: and a name; anything larger is a table-filling request rather than a client.
MAX_REDIRECT_URIS: Final = 10
MAX_CLIENT_METADATA_BYTES: Final = 8_000


def redirect_uri_allowed(uri: str) -> bool:
    """Whether Motet will ever send a browser to this MCP client redirect URI.

    **Registration is unauthenticated, and the SPA navigates to this URI with the code in it**,
    so a ``javascript:`` or ``data:`` URI would be a stranger's script running on the SPA's
    origin, where the session token lives, whichever consent button was pressed. Allowed:
    ``https``; ``http`` on a loopback address, RFC 8252's native-app redirect; and a
    private-use scheme such as ``vscode:``. Checked at registration and again when the code
    is minted, because a row stored before this check existed is still a row.

    **A username or password is refused too**, because the consent screen names where the
    grant goes: ``https://claude.ai@attacker.example/cb`` is a redirect to
    ``attacker.example`` that reads as ``claude.ai``.

    Keep in step with ``isSafeClientRedirect`` in ``web/src/oauth.ts``.
    """
    try:
        parts = urlsplit(uri)
        credentials = parts.username is not None or parts.password is not None
    except ValueError:
        return False
    scheme = parts.scheme.lower()
    if not scheme or scheme in REFUSED_REDIRECT_SCHEMES or credentials:
        return False
    if scheme == "https":
        return bool(parts.hostname)
    if scheme == "http":
        return parts.hostname in ("localhost", "127.0.0.1", "::1")
    return re.fullmatch(r"[a-z][a-z0-9+.\-]*", scheme) is not None


AUTHORIZATION_SERVER_PATH: Final = "/.well-known/oauth-authorization-server"


@dataclass(frozen=True)
class OAuthSetup:
    #: Where clients reach this API. The issuer, and the base of every endpoint below.
    issuer: str
    #: The protected resource: ``/mcp`` on the issuer. RFC 8707's audience.
    resource: str
    #: Where Google returns the person: the SPA's registered callback.
    consent_callback: str


def oauth_setup(config: Settings) -> OAuthSetup | None:
    """The authorization server's configuration, or ``None`` when it cannot run here."""
    if not (config.public_base_url and config.app_base_url and config.login_configured):
        return None
    issuer = config.public_base_url.rstrip("/")
    parts = urlsplit(issuer)
    # RFC 8414 requires an HTTPS issuer, and the SDK raises on anything else unless it is a
    # loopback address. Checked here so that a deployment configured with `http://` reads
    # as "not configured" on /internal/health rather than as a 500 on every OAuth request.
    if parts.scheme != "https" and parts.hostname not in ("localhost", "127.0.0.1", "::1"):
        logger.error(
            "mcp: MOTET_PUBLIC_BASE_URL is not https, so OAuth for MCP clients is off; an "
            "authorization server's issuer must be https (RFC 8414)"
        )
        return None
    # The endpoints are served at the root, so an issuer with a path would advertise
    # `{issuer}/authorize` at an address nothing answers.
    if parts.path not in ("", "/") or parts.query or parts.fragment:
        return None
    return OAuthSetup(
        issuer=issuer,
        resource=f"{issuer}{MCP_PATH}",
        consent_callback=f"{config.app_base_url.rstrip('/')}{CALLBACK_PATH}",
    )


def _with_connection[T](work: Callable[[psycopg.Connection[Any]], T]) -> T:
    config = Settings.from_env()
    if not config.database_url:
        raise TokenError("invalid_request", "DATABASE_URL is not configured on this deployment.")
    conn = repo.connect(config.database_url)
    try:
        result = work(conn)
        conn.commit()
        return result
    except BaseException:
        conn.rollback()
        raise
    finally:
        conn.close()


async def _db[T](work: Callable[[psycopg.Connection[Any]], T]) -> T:
    return await anyio.to_thread.run_sync(_with_connection, work)


def _issue(
    conn: psycopg.Connection[Any],
    *,
    client_id: str,
    user_id: str,
    email: str,
    scopes: list[str],
    resource: str | None,
) -> OAuthToken:
    """Mint an access token (a session row) and the refresh token beside it.

    **The allowlist is checked here, at every issue**, not only when the person approved the
    client. A refresh token lives thirty days; taking an address off
    ``MOTET_ALLOWED_EMAILS`` has to stop it minting the next access token, just as
    ``require_caller`` stops the current one.
    """
    if not is_allowed(email, Settings.from_env().allowed_emails):
        logger.warning("refused an MCP token for %s: not on %s", email, ALLOWED_EMAILS_ENV)
        raise TokenError("invalid_grant", "That account is no longer allowed to use this Motet.")
    access = auth_repo.new_session_token()
    session = auth_repo.create_session(
        conn,
        user_id=user_id,
        email=email,
        token=access,
        ttl_seconds=mcp_oauth.ACCESS_TOKEN_TTL_SECONDS,
        mcp_client_id=client_id,
    )
    refresh = auth_repo.new_session_token()
    mcp_oauth.create_refresh_token(
        conn,
        token=refresh,
        client_id=client_id,
        user_id=user_id,
        email=email,
        scopes=scopes,
        resource=resource,
        session_id=session.id,
    )
    logger.info("issued an MCP grant to client %s for %s", client_id, email)
    return OAuthToken(
        access_token=access,
        token_type="Bearer",
        expires_in=mcp_oauth.ACCESS_TOKEN_TTL_SECONDS,
        refresh_token=refresh,
        scope=" ".join(scopes) or None,
    )


class MotetOAuthProvider:
    """The SDK's ``OAuthAuthorizationServerProvider``, backed by Postgres and Google sign-in."""

    def __init__(self, setup: OAuthSetup) -> None:
        self.setup = setup

    async def get_client(self, client_id: str) -> OAuthClientInformationFull | None:
        info = await _db(lambda conn: mcp_oauth.get_client(conn, client_id))
        return None if info is None else OAuthClientInformationFull.model_validate(info)

    async def register_client(self, client_info: OAuthClientInformationFull) -> None:
        uris = [str(uri) for uri in client_info.redirect_uris or []]
        if not uris or len(uris) > MAX_REDIRECT_URIS:
            raise RegistrationError(
                "invalid_redirect_uri", f"Register between 1 and {MAX_REDIRECT_URIS} redirect URIs."
            )
        if not all(redirect_uri_allowed(uri) for uri in uris):
            raise RegistrationError(
                "invalid_redirect_uri",
                "A redirect URI must be https, http on a loopback address, "
                "or a private-use scheme.",
            )
        stored = client_info.model_dump(mode="json", exclude_none=True)
        if len(json.dumps(stored)) > MAX_CLIENT_METADATA_BYTES:
            raise RegistrationError(
                "invalid_client_metadata",
                f"Client metadata is limited to {MAX_CLIENT_METADATA_BYTES} bytes.",
            )

        def work(conn: psycopg.Connection[Any]) -> None:
            # Registration is unauthenticated, so it is also where the table is bounded.
            mcp_oauth.purge_unused_clients(conn)
            mcp_oauth.register_client(
                conn,
                client_id=client_info.client_id or "",
                client_info=stored,
            )

        await _db(work)

    async def authorize(
        self, client: OAuthClientInformationFull, params: AuthorizationParams
    ) -> str:
        if params.resource is not None and params.resource.rstrip("/") != self.setup.resource:
            raise AuthorizeError(
                "invalid_target", f"This server's resource is {self.setup.resource}."
            )
        verifier, challenge = new_pkce_pair()
        state = f"{MCP_STATE_PREFIX}{secrets.token_urlsafe(32)}"
        nonce = new_nonce()
        request = {
            "client_id": client.client_id,
            "redirect_uri": str(params.redirect_uri),
            "redirect_uri_provided_explicitly": params.redirect_uri_provided_explicitly,
            "code_challenge": params.code_challenge,
            "state": params.state,
            "scopes": list(params.scopes or []),
            "resource": params.resource,
        }

        def work(conn: psycopg.Connection[Any]) -> None:
            phase2.purge_expired_oauth_states(conn)
            phase2.start_oauth(
                conn,
                state=state,
                user_id=repo.OWNER_USER_ID,
                provider=GOOGLE_PROVIDER,
                source_id_=None,
                code_verifier=verifier,
                redirect_uri=self.setup.consent_callback,
                scopes=LOGIN_SCOPES,
                nonce=nonce,
                mcp_request=request,
            )

        await _db(work)
        try:
            return build_identity_provider().authorization_url(
                redirect_uri=self.setup.consent_callback,
                state=state,
                nonce=nonce,
                code_challenge=challenge,
            )
        except IdentityError as exc:
            raise AuthorizeError("temporarily_unavailable", str(exc)) from exc

    async def load_authorization_code(
        self, client: OAuthClientInformationFull, authorization_code: str
    ) -> AuthorizationCode | None:
        client_id = client.client_id or ""
        stored = await _db(lambda conn: mcp_oauth.load_code(conn, authorization_code, client_id))
        if stored is None:
            return None
        return AuthorizationCode(
            code=authorization_code,
            scopes=stored.scopes,
            expires_at=stored.expires_at.timestamp(),
            client_id=stored.client_id,
            code_challenge=stored.code_challenge,
            redirect_uri=AnyUrl(stored.redirect_uri),
            redirect_uri_provided_explicitly=stored.redirect_uri_provided_explicitly,
            resource=stored.resource,
            subject=stored.email,
        )

    async def exchange_authorization_code(
        self, client: OAuthClientInformationFull, authorization_code: AuthorizationCode
    ) -> OAuthToken:
        client_id = client.client_id or ""

        def work(conn: psycopg.Connection[Any]) -> OAuthToken:
            # Consumed here, in the transaction that issues, so two concurrent redemptions
            # of one code produce one grant and one `invalid_grant`.
            stored = mcp_oauth.consume_code(conn, authorization_code.code, client_id)
            if stored is None:
                raise TokenError("invalid_grant", "This authorization code was already used.")
            return _issue(
                conn,
                client_id=client_id,
                user_id=stored.user_id,
                email=stored.email,
                scopes=stored.scopes,
                resource=stored.resource,
            )

        return await _db(work)

    async def load_refresh_token(
        self, client: OAuthClientInformationFull, refresh_token: str
    ) -> RefreshToken | None:
        client_id = client.client_id or ""
        stored = await _db(
            lambda conn: mcp_oauth.load_refresh_token(conn, refresh_token, client_id)
        )
        if stored is None:
            return None
        return RefreshToken(
            token=refresh_token,
            client_id=stored.client_id,
            scopes=stored.scopes,
            expires_at=int(stored.expires_at.timestamp()),
            resource=stored.resource,
            subject=stored.email,
        )

    async def exchange_refresh_token(
        self, client: OAuthClientInformationFull, refresh_token: RefreshToken, scopes: list[str]
    ) -> OAuthToken:
        client_id = client.client_id or ""

        def work(conn: psycopg.Connection[Any]) -> OAuthToken:
            stored = mcp_oauth.consume_refresh_token(conn, refresh_token.token, client_id)
            if stored is None:
                raise TokenError("invalid_grant", "This refresh token was already used.")
            return _issue(
                conn,
                client_id=client_id,
                user_id=stored.user_id,
                email=stored.email,
                scopes=scopes,
                resource=stored.resource,
            )

        return await _db(work)

    async def load_access_token(self, token: str) -> AccessToken | None:
        session = await _db(lambda conn: auth_repo.session_for_token(conn, token))
        if session is None or session.mcp_client_id is None:
            return None
        return AccessToken(
            token=token,
            client_id=session.mcp_client_id,
            scopes=[],
            expires_at=int(session.expires_at.timestamp()),
            resource=self.setup.resource,
            subject=session.email,
        )

    async def exchange_identity_assertion(
        self, client: OAuthClientInformationFull, params: IdentityAssertionParams
    ) -> OAuthToken:
        """SEP-990's enterprise identity-assertion grant. Not offered: there is no IdP to trust."""
        raise TokenError(
            "unsupported_grant_type", "This server does not accept identity assertions."
        )

    async def revoke_token(self, token: AccessToken | RefreshToken) -> None:
        def work(conn: psycopg.Connection[Any]) -> None:
            if isinstance(token, AccessToken):
                session = auth_repo.session_for_token(conn, token.token)
                if session is not None:
                    mcp_oauth.revoke_session_grant(conn, session.id)
            else:
                mcp_oauth.consume_refresh_token(conn, token.token)

        await _db(work)


@functools.lru_cache(maxsize=8)
def _endpoints(setup: OAuthSetup) -> dict[str, ASGIApp]:
    """The SDK's handlers for one configuration, by path. Cached: they hold no request state."""
    routes = create_auth_routes(
        provider=MotetOAuthProvider(setup),
        issuer_url=AnyHttpUrl(setup.issuer),
        client_registration_options=ClientRegistrationOptions(enabled=True),
        revocation_options=RevocationOptions(enabled=True),
    )
    routes += create_protected_resource_routes(
        resource_url=AnyHttpUrl(setup.resource),
        authorization_servers=[AnyHttpUrl(setup.issuer)],
        resource_name="Motet",
    )
    return {route.path: route.app for route in routes}


class _OAuthEndpoint:
    """One OAuth path, resolved against the configuration of the moment.

    Resolved per request rather than at import so that the routes exist on every app, and a
    deployment (or a test) that sets the variables later gets the endpoints without a
    rebuild. Unconfigured is a 404, the same as a path that was never there.
    """

    def __init__(self, path: str) -> None:
        self.path = path

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        setup = oauth_setup(Settings.from_env())
        if setup is None:
            await JSONResponse(
                {
                    "error": "not_found",
                    "error_description": "MCP authorization is not configured on this "
                    "deployment; /mcp takes the API's bearer token.",
                },
                status_code=404,
            )(scope, receive, send)
            return
        await _endpoints(setup)[self.path](scope, receive, send)


def oauth_routes() -> list[Route]:
    """The authorization server's routes, for the root of the API's route table."""
    methods = {
        AUTHORIZATION_SERVER_PATH: ["GET", "OPTIONS"],
        PROTECTED_RESOURCE_PATH: ["GET", "OPTIONS"],
        "/authorize": ["GET", "POST"],
        "/token": ["POST", "OPTIONS"],
        "/register": ["POST", "OPTIONS"],
        "/revoke": ["POST", "OPTIONS"],
    }
    return [Route(path, endpoint=_OAuthEndpoint(path), methods=m) for path, m in methods.items()]


def _redirect_host(uri: str) -> str:
    """What the consent screen names as where the grant goes: the host and port, never ``netloc``.

    ``netloc`` carries any user-info, which is exactly the part that can make one host read as
    another. Registration refuses user-info already; this is the second half of that, for a
    screen whose whole job is to be believed.
    """
    parts = urlsplit(uri)
    if not parts.hostname:
        return uri
    return f"{parts.hostname}:{parts.port}" if parts.port else parts.hostname


def complete_authorization(
    conn: psycopg.Connection[Any], config: Settings, *, state: str, code: str
) -> McpAuthorizationResponse:
    """Finish the Google half of an MCP authorization and mint the client's code.

    ``motet_api.main.complete_login``'s order, for the same reasons: the identity is
    verified completely before the allowlist is consulted, and the state is consumed exactly
    once. What differs is the result — not a session for this browser, but a code for the
    client that asked, returned to the SPA to deliver only if the person approves.
    """
    if not state.startswith(MCP_STATE_PREFIX):
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST, "That callback did not come from an MCP authorization."
        )
    if oauth_setup(config) is None:
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            "MCP authorization is not configured on this deployment.",
        )
    pending = phase2.consume_oauth_state(conn, state)
    if pending is None or pending["provider"] != GOOGLE_PROVIDER or not pending.get("mcp_request"):
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            "This authorization is unknown, already used, or expired. Start again from the "
            "MCP client.",
        )

    try:
        identity = build_identity_provider().complete(
            code=code,
            redirect_uri=pending["redirect_uri"],
            code_verifier=pending["code_verifier"],
            nonce=pending["nonce"] or "",
        )
    except (IdentityConfigError, IdentityUnavailableError) as exc:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, str(exc)) from exc
    except IdentityError as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc)) from exc

    if not is_allowed(identity.email, config.allowed_emails):
        logger.warning(
            "refused an MCP authorization for %s: not on %s", identity.email, ALLOWED_EMAILS_ENV
        )
        raise HTTPException(
            status.HTTP_403_FORBIDDEN, "That Google account is not allowed to use this Motet."
        )

    request: dict[str, Any] = pending["mcp_request"]
    client = mcp_oauth.get_client(conn, request["client_id"])
    if client is None:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            "The MCP client that asked is no longer registered. Start again from the client.",
        )

    if not redirect_uri_allowed(request["redirect_uri"]):
        # A client registered before registration checked this. Nothing is minted for it.
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            "The MCP client that asked registered a redirect Motet will not send a browser to.",
        )

    authorization_code = auth_repo.new_session_token()
    mcp_oauth.create_code(
        conn,
        code=authorization_code,
        client_id=request["client_id"],
        user_id=repo.OWNER_USER_ID,
        email=identity.email,
        scopes=list(request["scopes"]),
        code_challenge=request["code_challenge"],
        redirect_uri=request["redirect_uri"],
        redirect_uri_provided_explicitly=bool(request["redirect_uri_provided_explicitly"]),
        resource=request["resource"],
    )
    redirect_uri = request["redirect_uri"]
    return McpAuthorizationResponse(
        client_name=str(client.get("client_name") or request["client_id"]),
        redirect_host=_redirect_host(redirect_uri),
        email=identity.email,
        redirect_url=construct_redirect_uri(
            redirect_uri, code=authorization_code, state=request["state"]
        ),
        deny_url=construct_redirect_uri(
            redirect_uri,
            error="access_denied",
            error_description="The person declined.",
            state=request["state"],
        ),
    )
