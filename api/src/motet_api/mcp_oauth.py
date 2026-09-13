"""PROTOTYPE — OAuth 2.1 for a remote MCP server, as the MCP specification lays it out.

The MCP authorization spec (2025-06-18) is a composition of five RFCs, and this module
is that composition and nothing else:

1. **RFC 9728, protected resource metadata.** An unauthenticated request to the MCP URL
   answers 401 with ``WWW-Authenticate: Bearer resource_metadata="…"``; that document
   names the canonical ``resource`` and the authorization server(s).
2. **RFC 8414, authorization server metadata.** ``/.well-known/oauth-authorization-server``
   on the issuer (path-suffixed first when the issuer has a path, per the spec's
   ordering), which is where the endpoints come from. Nothing is hardcoded.
3. **RFC 7591, dynamic client registration.** If the server advertises a
   ``registration_endpoint`` a client id is minted on the spot. If it does not, the flow
   stops — a pre-registered client id is a one-time human-owned step (invariant 9) — and
   :class:`RegistrationUnsupportedError` says so in words the UI can show.
4. **RFC 7636, PKCE**, S256 only, and **RFC 8707, resource indicators**: ``resource`` is
   sent on the authorization request and the token request, so a token is bound to
   *this* server and cannot be replayed at another.
5. **RFC 9207**: an authorization response may carry ``iss``, and when it does the
   callback checks it against the issuer discovery produced.

**What was verified against a real server (the owner's email MCP server, 2026-09-12):** the 401
pointer, both metadata documents, open registration answering 201 with a public client
(``token_endpoint_auth_method: none``, no secret), S256, and a canonical resource with the
``?servers=`` query stripped. The token exchange is the one leg that needs a human on a
consent screen and is covered here by :class:`FakeMcpOAuthClient` and by a
``MockTransport`` test that asserts the bytes.

The real client and the fake share a Protocol, and :func:`build_mcp_oauth_client` reads
``MOTET_INFERENCE_MODE`` through the one parser, exactly as every other vendor seam does.
"""

from __future__ import annotations

import json
import logging
import re
import secrets
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, Final, Protocol
from urllib.parse import urlencode, urlsplit, urlunsplit

import httpx
from motet_inference.mode import current_mode

logger = logging.getLogger("motet.api.mcp_oauth")

#: Marks a ``state`` as belonging to an MCP connector authorization. The third flow on the
#: SPA's one callback path; the dot is safe for `LOGIN_STATE_PREFIX`'s reason. Keep in
#: step with `web/src/oauth.ts`.
CONNECTOR_STATE_PREFIX: Final = "connector."

DEFAULT_TIMEOUT_SECONDS: Final = 10.0
CLIENT_NAME: Final = "Motet"
PROVIDER: Final = "mcp"

_WELL_KNOWN_PRM: Final = "/.well-known/oauth-protected-resource"
_WELL_KNOWN_AS: Final = "/.well-known/oauth-authorization-server"
_WELL_KNOWN_OIDC: Final = "/.well-known/openid-configuration"


class McpOAuthError(RuntimeError):
    """The server did not do what the MCP authorization spec says it should."""


class RegistrationUnsupportedError(McpOAuthError):
    """No ``registration_endpoint``: a client id has to be provisioned by a human."""


@dataclass(frozen=True)
class AuthorizationServer:
    """What discovery produced. None of it is secret; all of it is recorded on the row."""

    issuer: str
    authorization_endpoint: str
    token_endpoint: str
    registration_endpoint: str | None
    #: The canonical resource identifier (RFC 8707) the token will be bound to.
    resource: str
    scopes: tuple[str, ...]
    iss_parameter_supported: bool


@dataclass(frozen=True)
class TokenSet:
    """One grant's tokens. **This is the secret** the connector row seals."""

    access_token: str
    refresh_token: str | None
    expires_at: datetime | None
    token_type: str
    scope: str

    def to_json(self) -> str:
        return json.dumps(
            {
                "access_token": self.access_token,
                "refresh_token": self.refresh_token,
                "expires_at": self.expires_at.isoformat() if self.expires_at else None,
                "token_type": self.token_type,
                "scope": self.scope,
            }
        )

    def __repr__(self) -> str:
        return f"TokenSet(expires_at={self.expires_at!r}, scope={self.scope!r}, tokens=<redacted>)"


class McpOAuthClient(Protocol):
    def discover(self, mcp_url: str) -> AuthorizationServer: ...

    def register(self, server: AuthorizationServer, *, redirect_uri: str) -> str: ...

    def exchange_code(
        self,
        server: AuthorizationServer,
        *,
        client_id: str,
        code: str,
        redirect_uri: str,
        code_verifier: str,
    ) -> TokenSet: ...

    def refresh(
        self, *, token_endpoint: str, client_id: str, refresh_token: str, resource: str
    ) -> TokenSet: ...


def authorization_url(
    server: AuthorizationServer,
    *,
    client_id: str,
    redirect_uri: str,
    state: str,
    code_challenge: str,
) -> str:
    """The consent URL. Pure — the same for the real client and the fake.

    ``scope`` is whatever the resource advertised, or omitted: the spec says a server
    that ignores scope substitutes its own, and asking for one it did not list is how a
    connector fails over a word that never mattered.
    """
    params: dict[str, str] = {
        "response_type": "code",
        "client_id": client_id,
        "redirect_uri": redirect_uri,
        "state": state,
        "code_challenge": code_challenge,
        "code_challenge_method": "S256",
        "resource": server.resource,
    }
    if server.scopes:
        params["scope"] = " ".join(server.scopes)
    joiner = "&" if "?" in server.authorization_endpoint else "?"
    return f"{server.authorization_endpoint}{joiner}{urlencode(params)}"


def canonical_resource(mcp_url: str) -> str:
    """``https://host/mcp?servers=x`` → ``https://host/mcp``: origin plus path, no query,
    no fragment, no trailing slash — RFC 8707's canonical form as the MCP spec reads it."""
    parts = urlsplit(mcp_url)
    path = parts.path.rstrip("/")
    return urlunsplit((parts.scheme.lower(), parts.netloc.lower(), path, "", ""))


class HttpMcpOAuthClient:
    """The real thing, over httpx. ``transport`` is for tests; nothing else sets it."""

    def __init__(
        self,
        *,
        timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        self._client = httpx.Client(
            timeout=timeout_seconds,
            transport=transport,
            follow_redirects=False,
            headers={"User-Agent": "motet-connectors/0"},
        )

    # --- discovery ------------------------------------------------------------------

    def discover(self, mcp_url: str) -> AuthorizationServer:
        if urlsplit(mcp_url).scheme != "https" and not _is_loopback(mcp_url):
            raise McpOAuthError("An MCP server URL must be https.")
        prm_url = self._resource_metadata_url(mcp_url)
        prm = self._json(prm_url, "protected resource metadata")

        resource = str(prm.get("resource") or canonical_resource(mcp_url))
        if _origin(resource) != _origin(mcp_url):
            raise McpOAuthError(
                f"The resource metadata names {resource}, which is not the server you gave."
            )
        servers = prm.get("authorization_servers") or []
        if not servers:
            raise McpOAuthError("The resource metadata names no authorization server.")
        issuer = str(servers[0]).rstrip("/")
        meta = self._authorization_server_metadata(issuer)

        methods = meta.get("code_challenge_methods_supported")
        if methods is not None and "S256" not in methods:
            raise McpOAuthError("The authorization server does not support PKCE S256.")
        scopes = tuple(str(s) for s in (prm.get("scopes_supported") or ()))
        return AuthorizationServer(
            issuer=str(meta["issuer"]).rstrip("/"),
            authorization_endpoint=str(meta["authorization_endpoint"]),
            token_endpoint=str(meta["token_endpoint"]),
            registration_endpoint=(
                str(meta["registration_endpoint"]) if meta.get("registration_endpoint") else None
            ),
            resource=resource,
            scopes=scopes,
            iss_parameter_supported=bool(
                meta.get("authorization_response_iss_parameter_supported")
            ),
        )

    def _resource_metadata_url(self, mcp_url: str) -> str:
        """Ask the MCP endpoint itself first; fall back to the well-known locations.

        A stream rather than a plain GET: an MCP streamable-HTTP endpoint that *does*
        answer a bare GET answers with an SSE stream that never ends, and only the status
        and headers are wanted here.
        """
        try:
            with self._client.stream(
                "GET", mcp_url, headers={"Accept": "application/json, text/event-stream"}
            ) as response:
                challenge = response.headers.get("www-authenticate", "")
        except httpx.HTTPError as exc:
            raise McpOAuthError(f"Could not reach {mcp_url}: {exc}") from exc
        match = re.search(r'resource_metadata="([^"]+)"', challenge)
        if match:
            pointed = match.group(1)
            if _origin(pointed) != _origin(mcp_url):
                raise McpOAuthError(
                    "The server pointed at resource metadata on another origin, which "
                    "is not allowed."
                )
            return pointed
        parts = urlsplit(mcp_url)
        origin = _origin(mcp_url)
        path = parts.path.rstrip("/")
        for candidate in (f"{origin}{_WELL_KNOWN_PRM}{path}", f"{origin}{_WELL_KNOWN_PRM}"):
            if self._exists(candidate):
                return candidate
        raise McpOAuthError(
            "The server did not point at any OAuth protected resource metadata, so it "
            "does not support the MCP authorization flow (or does not need auth)."
        )

    def _authorization_server_metadata(self, issuer: str) -> dict[str, Any]:
        parts = urlsplit(issuer)
        origin = _origin(issuer)
        path = parts.path.rstrip("/")
        candidates = (
            [
                f"{origin}{_WELL_KNOWN_AS}{path}",
                f"{origin}{_WELL_KNOWN_OIDC}{path}",
                f"{issuer}{_WELL_KNOWN_OIDC}",
            ]
            if path
            else [f"{origin}{_WELL_KNOWN_AS}", f"{origin}{_WELL_KNOWN_OIDC}"]
        )
        for candidate in candidates:
            meta = self._json_if_present(candidate, "authorization server metadata")
            if meta is None:
                continue
            declared = str(meta.get("issuer", "")).rstrip("/")
            if declared != issuer:
                raise McpOAuthError(
                    f"Authorization server metadata at {candidate} declares issuer "
                    f"{declared!r}, not {issuer!r}."
                )
            for key in ("authorization_endpoint", "token_endpoint"):
                if not meta.get(key):
                    raise McpOAuthError(f"Authorization server metadata has no {key}.")
            return meta
        raise McpOAuthError(f"No authorization server metadata found for {issuer}.")

    # --- registration ---------------------------------------------------------------

    def register(self, server: AuthorizationServer, *, redirect_uri: str) -> str:
        if not server.registration_endpoint:
            raise RegistrationUnsupportedError(
                f"{server.issuer} does not offer dynamic client registration. Authorizing "
                "it needs a client id registered by hand with that server — a one-time "
                "human step this prototype does not automate."
            )
        body = {
            "client_name": CLIENT_NAME,
            "redirect_uris": [redirect_uri],
            "grant_types": ["authorization_code", "refresh_token"],
            "response_types": ["code"],
            "token_endpoint_auth_method": "none",
        }
        if server.scopes:
            body["scope"] = " ".join(server.scopes)
        try:
            response = self._client.post(server.registration_endpoint, json=body)
        except httpx.HTTPError as exc:
            raise McpOAuthError(f"Client registration failed: {exc}") from exc
        if response.status_code not in (200, 201):
            raise McpOAuthError(
                f"Client registration was refused ({response.status_code}): {_error_text(response)}"
            )
        registered = response.json()
        client_id = registered.get("client_id")
        if not client_id:
            raise McpOAuthError("Client registration answered without a client_id.")
        if (
            registered.get("client_secret")
            and registered.get("token_endpoint_auth_method") != "none"
        ):
            # A confidential client would need the secret stored and sent; nothing
            # verified needs it, so it is refused rather than half-supported.
            raise McpOAuthError(
                "The server registered a confidential client (with a secret); this "
                "prototype supports public clients only."
            )
        return str(client_id)

    # --- tokens ---------------------------------------------------------------------

    def exchange_code(
        self,
        server: AuthorizationServer,
        *,
        client_id: str,
        code: str,
        redirect_uri: str,
        code_verifier: str,
    ) -> TokenSet:
        return self._token(
            server.token_endpoint,
            {
                "grant_type": "authorization_code",
                "code": code,
                "redirect_uri": redirect_uri,
                "client_id": client_id,
                "code_verifier": code_verifier,
                "resource": server.resource,
            },
        )

    def refresh(
        self, *, token_endpoint: str, client_id: str, refresh_token: str, resource: str
    ) -> TokenSet:
        return self._token(
            token_endpoint,
            {
                "grant_type": "refresh_token",
                "refresh_token": refresh_token,
                "client_id": client_id,
                "resource": resource,
            },
        )

    def _token(self, token_endpoint: str, form: Mapping[str, str]) -> TokenSet:
        try:
            response = self._client.post(
                token_endpoint, data=dict(form), headers={"Accept": "application/json"}
            )
        except httpx.HTTPError as exc:
            raise McpOAuthError(f"The token request failed: {exc}") from exc
        if response.status_code != 200:
            raise McpOAuthError(
                f"The token request was refused ({response.status_code}): {_error_text(response)}"
            )
        return parse_token_response(response.json(), now=datetime.now(UTC))

    # --- plumbing -------------------------------------------------------------------

    def _exists(self, url: str) -> bool:
        try:
            return self._client.get(url, headers={"Accept": "application/json"}).status_code == 200
        except httpx.HTTPError:
            return False

    def _json_if_present(self, url: str, what: str) -> dict[str, Any] | None:
        """One request: ``None`` on 404 (try the next well-known location), else the doc."""
        try:
            response = self._client.get(url, headers={"Accept": "application/json"})
        except httpx.HTTPError:
            return None
        if response.status_code != 200:
            return None
        return self._document(response, url, what)

    def _json(self, url: str, what: str) -> dict[str, Any]:
        try:
            response = self._client.get(url, headers={"Accept": "application/json"})
        except httpx.HTTPError as exc:
            raise McpOAuthError(f"Could not fetch the {what} at {url}: {exc}") from exc
        if response.status_code != 200:
            raise McpOAuthError(f"The {what} at {url} answered {response.status_code}.")
        return self._document(response, url, what)

    @staticmethod
    def _document(response: httpx.Response, url: str, what: str) -> dict[str, Any]:
        try:
            document = response.json()
        except ValueError as exc:
            raise McpOAuthError(f"The {what} at {url} is not JSON.") from exc
        if not isinstance(document, dict):
            raise McpOAuthError(f"The {what} at {url} is not a JSON object.")
        return document


def parse_token_response(body: Mapping[str, Any], *, now: datetime) -> TokenSet:
    access = body.get("access_token")
    if not access:
        raise McpOAuthError("The token response carried no access_token.")
    expires_in = body.get("expires_in")
    expires_at = None
    if isinstance(expires_in, int | float) and expires_in > 0:
        expires_at = now + timedelta(seconds=int(expires_in))
    return TokenSet(
        access_token=str(access),
        refresh_token=str(body["refresh_token"]) if body.get("refresh_token") else None,
        expires_at=expires_at,
        token_type=str(body.get("token_type") or "Bearer"),
        scope=str(body.get("scope") or ""),
    )


class FakeMcpOAuthClient:
    """Deterministic: discovers a server shaped like strad's, registers, and exchanges.

    ``registration=False`` models a server without DCR, which is the one branch the UI
    has to render differently.
    """

    def __init__(self, *, registration: bool = True) -> None:
        self.registration = registration
        self.exchanged: list[dict[str, str]] = []

    def discover(self, mcp_url: str) -> AuthorizationServer:
        origin = _origin(mcp_url)
        return AuthorizationServer(
            issuer=origin,
            authorization_endpoint=f"{origin}/oauth/authorize",
            token_endpoint=f"{origin}/oauth/token",
            registration_endpoint=f"{origin}/oauth/register" if self.registration else None,
            resource=canonical_resource(mcp_url),
            scopes=("mcp",),
            iss_parameter_supported=True,
        )

    def register(self, server: AuthorizationServer, *, redirect_uri: str) -> str:
        if not server.registration_endpoint:
            raise RegistrationUnsupportedError(
                f"{server.issuer} does not offer dynamic client registration. Authorizing "
                "it needs a client id registered by hand with that server — a one-time "
                "human step this prototype does not automate."
            )
        return f"fake-client-{redirect_uri.count('/')}"

    def exchange_code(
        self,
        server: AuthorizationServer,
        *,
        client_id: str,
        code: str,
        redirect_uri: str,
        code_verifier: str,
    ) -> TokenSet:
        if code == "bad-code":
            raise McpOAuthError("The token request was refused (400): invalid_grant")
        self.exchanged.append(
            {
                "client_id": client_id,
                "code": code,
                "redirect_uri": redirect_uri,
                "verifier": code_verifier,
            }
        )
        return TokenSet(
            access_token=f"fake-access-{code}",
            refresh_token=f"fake-refresh-{code}",
            expires_at=datetime(2026, 9, 13, 1, 0, tzinfo=UTC),
            token_type="Bearer",
            scope="mcp",
        )

    def refresh(
        self, *, token_endpoint: str, client_id: str, refresh_token: str, resource: str
    ) -> TokenSet:
        return TokenSet(
            access_token="fake-access-refreshed",
            refresh_token=refresh_token,
            expires_at=datetime(2026, 9, 13, 2, 0, tzinfo=UTC),
            token_type="Bearer",
            scope="mcp",
        )


_fake: FakeMcpOAuthClient | None = None


def build_mcp_oauth_client(env: Mapping[str, str] | None = None) -> McpOAuthClient:
    """Real in real mode, the fake otherwise — the same rule as every vendor seam."""
    global _fake
    if current_mode(env) == "real":
        return HttpMcpOAuthClient()
    if _fake is None:
        _fake = FakeMcpOAuthClient()
    return _fake


def reset_fake() -> None:
    global _fake
    _fake = None


def new_connector_state() -> str:
    return f"{CONNECTOR_STATE_PREFIX}{secrets.token_urlsafe(32)}"


def is_connector_state(state: str) -> bool:
    return state.startswith(CONNECTOR_STATE_PREFIX)


def _origin(url: str) -> str:
    parts = urlsplit(url)
    return f"{parts.scheme.lower()}://{parts.netloc.lower()}"


def _is_loopback(url: str) -> bool:
    host = urlsplit(url).hostname or ""
    return host in ("localhost", "127.0.0.1", "::1")


def _error_text(response: httpx.Response) -> str:
    try:
        body = response.json()
    except ValueError:
        return response.text[:200]
    if isinstance(body, dict):
        return str(body.get("error_description") or body.get("error") or body)[:200]
    return str(body)[:200]


__all__: Sequence[str] = [
    "CONNECTOR_STATE_PREFIX",
    "PROVIDER",
    "AuthorizationServer",
    "FakeMcpOAuthClient",
    "HttpMcpOAuthClient",
    "McpOAuthClient",
    "McpOAuthError",
    "RegistrationUnsupportedError",
    "TokenSet",
    "authorization_url",
    "build_mcp_oauth_client",
    "canonical_resource",
    "is_connector_state",
    "new_connector_state",
    "parse_token_response",
    "reset_fake",
]
