"""OAuth 2.1 for a remote MCP server, as the MCP specification lays it out (motet#102).

Here rather than in the API because both halves of a connector's life need it: the API
runs discovery, registration and the code exchange when the owner authorizes a server, and
a worker refreshes the token set before it hands the server to the enrichment agent. The
worker cannot import the API — the dependency arrow only goes one way — and ``motet-sources``
is already the package both reach for an OAuth client.

The MCP authorization spec (2025-06-18) is a composition of five RFCs, and this module is
that composition and nothing else:

1. **RFC 9728, protected resource metadata.** An unauthenticated request to the MCP URL
   answers 401 with ``WWW-Authenticate: Bearer resource_metadata="…"``; that document names
   the canonical ``resource`` and the authorization server.
2. **RFC 8414, authorization server metadata**, at the issuer's ``.well-known`` location,
   which is where every endpoint comes from. Nothing is hardcoded.
3. **RFC 7591, dynamic client registration.** A server that advertises a
   ``registration_endpoint`` mints a public client on the spot. One that does not stops the
   flow — a pre-registered client id is a one-time human-owned step (invariant 9) — with
   :class:`RegistrationUnsupportedError` saying so in words the screen can show.
4. **RFC 7636, PKCE**, S256 only, and **RFC 8707, resource indicators**: ``resource`` rides
   on the authorization and token requests, so a token is bound to *this* server.
5. **RFC 9207**: an authorization response may carry ``iss``, and the callback checks it
   against the issuer discovery produced.

**Every URL this client is handed by a server is untrusted, and two guards say so.** The
endpoints come out of documents a third party serves, so each request goes through
:meth:`HttpMcpOAuthClient._guard` — ``https`` only, and a host that resolves to a public
address, so a metadata document cannot point the API at the metadata server or at anything
else inside the network it runs in. And the authorization endpoint, which the SPA hands to
``window.location``, must itself be ``https``: a ``javascript:`` URL there would run in
Motet's own origin.

The real client and the fake share a Protocol, and :func:`build_mcp_oauth_client` reads
``MOTET_INFERENCE_MODE`` through the one parser, exactly as every other vendor seam does.
"""

from __future__ import annotations

import ipaddress
import json
import os
import re
import socket
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any, Final, Protocol
from urllib.parse import urlencode, urlsplit, urlunsplit

from motet_inference.mode import current_mode

if TYPE_CHECKING:
    import httpx

DEFAULT_TIMEOUT_SECONDS: Final = 10.0
CLIENT_NAME: Final = "Motet"
#: The ``oauth_states.provider`` value an MCP authorization is recorded under.
PROVIDER: Final = "mcp"

_WELL_KNOWN_PRM: Final = "/.well-known/oauth-protected-resource"
_WELL_KNOWN_AS: Final = "/.well-known/oauth-authorization-server"
_WELL_KNOWN_OIDC: Final = "/.well-known/openid-configuration"

#: Resolves a host name to the addresses a connection to it would use.
Resolver = Callable[[str], Sequence[str]]

#: Addresses `ipaddress` calls global that still reach inwards: NAT64 prefixes, which a
#: gateway maps onto IPv4 (169.254.169.254 among it), and the deprecated IPv4-compatible
#: block, where `::127.0.0.1` is loopback.
_INWARD_NETWORKS: Final = (
    ipaddress.ip_network("64:ff9b::/96"),
    ipaddress.ip_network("64:ff9b:1::/48"),
    ipaddress.ip_network("::/96"),
)


class McpOAuthError(RuntimeError):
    """The server did not do what the MCP authorization spec says it should."""


class RegistrationUnsupportedError(McpOAuthError):
    """No ``registration_endpoint``: a client id has to be provisioned by a human."""


class UnsafeUrlError(McpOAuthError):
    """A URL that is not ``https``, or whose host is not on the public internet."""


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
    """One grant's tokens. **This is the secret** a connector row seals."""

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

    @classmethod
    def from_json(cls, raw: str) -> TokenSet:
        doc = json.loads(raw)
        if not isinstance(doc, dict) or not doc.get("access_token"):
            raise McpOAuthError("the sealed token set carries no access_token")
        expires = doc.get("expires_at")
        return cls(
            access_token=str(doc["access_token"]),
            refresh_token=str(doc["refresh_token"]) if doc.get("refresh_token") else None,
            expires_at=datetime.fromisoformat(expires) if expires else None,
            token_type=str(doc.get("token_type") or "Bearer"),
            scope=str(doc.get("scope") or ""),
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

    ``scope`` is whatever the resource advertised, or omitted: the spec lets a server that
    ignores scope substitute its own, and asking for one it did not list is how a connector
    fails over a word that never mattered.
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
    parts = urlsplit(server.authorization_endpoint)
    query = f"{parts.query}&{urlencode(params)}" if parts.query else urlencode(params)
    return urlunsplit((parts.scheme, parts.netloc, parts.path, query, ""))


def canonical_resource(mcp_url: str) -> str:
    """``https://host/mcp?servers=x`` → ``https://host/mcp``: origin plus path, no query, no
    fragment, no trailing slash — RFC 8707's canonical form as the MCP spec reads it."""
    parts = urlsplit(mcp_url)
    return urlunsplit((parts.scheme.lower(), parts.netloc.lower(), parts.path.rstrip("/"), "", ""))


def system_resolver(host: str) -> Sequence[str]:
    """Every address ``host`` resolves to, as the socket layer would connect to it."""
    return [str(info[4][0]) for info in socket.getaddrinfo(host, None)]


class HttpMcpOAuthClient:
    """The real thing, over httpx.

    ``transport`` and ``resolve`` are for tests; nothing else sets them. The guard is a
    check before the request rather than a pinned connection, so a host whose DNS answer
    changes between the check and the connect is not covered — it narrows the SSRF surface
    to that race rather than closing it, and says so here rather than implying more.
    """

    def __init__(
        self,
        *,
        timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
        transport: httpx.BaseTransport | None = None,
        resolve: Resolver = system_resolver,
    ) -> None:
        import httpx  # noqa: PLC0415 — fake mode never pulls in an HTTP client

        self._resolve = resolve
        self._client = httpx.Client(
            timeout=timeout_seconds,
            transport=transport,
            follow_redirects=False,
            # No ambient proxy and no .netrc: through a proxy the connection goes somewhere
            # other than the host `_guard` resolved, and the guard would be checking nothing.
            trust_env=False,
            headers={"User-Agent": "motet-connectors/1"},
        )

    # --- discovery --------------------------------------------------------------------

    def discover(self, mcp_url: str) -> AuthorizationServer:
        self._guard(mcp_url)
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
        authorize = str(meta["authorization_endpoint"])
        if urlsplit(authorize).scheme != "https":
            # The SPA hands this URL to `window.location`. Anything but https — and a
            # `javascript:` URL above all — would run in Motet's origin, not the server's.
            raise UnsafeUrlError("The authorization endpoint is not an https URL.")
        raw_scopes = prm.get("scopes_supported") or []
        if not isinstance(raw_scopes, list):
            raise McpOAuthError("The resource metadata's scopes_supported is not a list.")
        scopes = tuple(str(s) for s in raw_scopes)
        return AuthorizationServer(
            issuer=str(meta["issuer"]).rstrip("/"),
            authorization_endpoint=authorize,
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

        A stream rather than a plain GET: a streamable-HTTP endpoint that *does* answer a
        bare GET answers with an event stream that never ends, and only the status and the
        headers are wanted here.
        """
        import httpx  # noqa: PLC0415

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
                    "The server pointed at resource metadata on another origin, which is "
                    "not allowed."
                )
            return pointed
        origin = _origin(mcp_url)
        path = urlsplit(mcp_url).path.rstrip("/")
        for candidate in (f"{origin}{_WELL_KNOWN_PRM}{path}", f"{origin}{_WELL_KNOWN_PRM}"):
            if self._json_if_present(candidate, "protected resource metadata") is not None:
                return candidate
        raise McpOAuthError(
            "The server did not point at any OAuth protected resource metadata, so it does "
            "not support the MCP authorization flow."
        )

    def _authorization_server_metadata(self, issuer: str) -> dict[str, Any]:
        origin = _origin(issuer)
        path = urlsplit(issuer).path.rstrip("/")
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

    # --- registration -----------------------------------------------------------------

    def register(self, server: AuthorizationServer, *, redirect_uri: str) -> str:
        if not server.registration_endpoint:
            raise RegistrationUnsupportedError(_no_registration(server.issuer))
        body: dict[str, Any] = {
            "client_name": CLIENT_NAME,
            "redirect_uris": [redirect_uri],
            "grant_types": ["authorization_code", "refresh_token"],
            "response_types": ["code"],
            "token_endpoint_auth_method": "none",
        }
        if server.scopes:
            body["scope"] = " ".join(server.scopes)
        response = self._send(
            "POST", server.registration_endpoint, "client registration", json=body
        )
        if response.status_code not in (200, 201):
            raise McpOAuthError(
                f"Client registration was refused ({response.status_code}): {_error_text(response)}"
            )
        registered = _document(response, server.registration_endpoint, "registration response")
        client_id = registered.get("client_id")
        if not client_id:
            raise McpOAuthError("Client registration answered without a client_id.")
        echoed = registered.get("redirect_uris")
        if isinstance(echoed, list) and redirect_uri not in echoed:
            raise McpOAuthError(
                "The server registered a client for a different redirect URI than Motet's."
            )
        if (
            registered.get("client_secret")
            and registered.get("token_endpoint_auth_method") != "none"
        ):
            # A confidential client would need its secret stored and sent; nothing verified
            # needs one, so it is refused rather than half-supported.
            raise McpOAuthError(
                "The server registered a confidential client (with a secret); only public "
                "clients are supported."
            )
        return str(client_id)

    # --- tokens -----------------------------------------------------------------------

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
        response = self._send(
            "POST",
            token_endpoint,
            "token request",
            data=dict(form),
            headers={"Accept": "application/json"},
        )
        if response.status_code != 200:
            raise McpOAuthError(
                f"The token request was refused ({response.status_code}): {_error_text(response)}"
            )
        return parse_token_response(
            _document(response, token_endpoint, "token response"), now=datetime.now(UTC)
        )

    # --- plumbing ---------------------------------------------------------------------

    def _guard(self, url: str) -> None:
        parts = urlsplit(url)
        host = parts.hostname or ""
        if parts.scheme != "https" or not host:
            raise UnsafeUrlError(f"{url} is not an https URL.")
        try:
            addresses = self._resolve(host)
        except OSError as exc:
            raise McpOAuthError(f"Could not resolve {host}: {exc}") from exc
        if not addresses:
            raise McpOAuthError(f"{host} resolves to no address.")
        for address in addresses:
            try:
                ip = ipaddress.ip_address(address.split("%", 1)[0])
            except ValueError as exc:
                raise UnsafeUrlError(f"{host} resolves to {address!r}, not an address.") from exc
            if not ip.is_global or any(ip in net for net in _INWARD_NETWORKS):
                raise UnsafeUrlError(
                    f"{host} resolves to a private or reserved address, which a connector "
                    "may not reach."
                )

    def _send(self, method: str, url: str, what: str, **kwargs: Any) -> httpx.Response:
        import httpx  # noqa: PLC0415

        self._guard(url)
        try:
            return self._client.request(method, url, **kwargs)
        except httpx.HTTPError as exc:
            raise McpOAuthError(f"The {what} to {url} failed: {exc}") from exc

    def _json_if_present(self, url: str, what: str) -> dict[str, Any] | None:
        """One request: ``None`` when absent (try the next location), else the document."""
        try:
            response = self._send("GET", url, what, headers={"Accept": "application/json"})
        except UnsafeUrlError:
            raise
        except McpOAuthError:
            return None
        if response.status_code != 200:
            return None
        try:
            return _document(response, url, what)
        except McpOAuthError:
            # A catch-all HTML page at one well-known location is not the end of discovery.
            return None

    def _json(self, url: str, what: str) -> dict[str, Any]:
        response = self._send("GET", url, what, headers={"Accept": "application/json"})
        if response.status_code != 200:
            raise McpOAuthError(f"The {what} at {url} answered {response.status_code}.")
        return _document(response, url, what)


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
    """Deterministic: discovers a server on the URL's own origin, registers, exchanges.

    ``registration=False`` models a server without dynamic client registration, which is
    the one branch the screen renders differently. The code ``bad-code`` is refused, so the
    failed-exchange path has something to drive it.
    """

    def __init__(self, *, registration: bool = True) -> None:
        self.registration = registration

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
            raise RegistrationUnsupportedError(_no_registration(server.issuer))
        return "fake-client"

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
        return TokenSet(
            access_token=f"fake-access-{code}",
            refresh_token=f"fake-refresh-{code}",
            expires_at=datetime.now(UTC) + timedelta(hours=1),
            token_type="Bearer",
            scope="mcp",
        )

    def refresh(
        self, *, token_endpoint: str, client_id: str, refresh_token: str, resource: str
    ) -> TokenSet:
        return TokenSet(
            access_token="fake-access-refreshed",
            refresh_token=refresh_token,
            expires_at=datetime.now(UTC) + timedelta(hours=1),
            token_type="Bearer",
            scope="mcp",
        )


def build_mcp_oauth_client(env: Mapping[str, str] | None = None) -> McpOAuthClient:
    """Real in real mode, the fake otherwise — the same rule as every vendor seam."""
    environ = dict(os.environ) if env is None else dict(env)
    if current_mode(environ) == "real":
        return HttpMcpOAuthClient()
    return FakeMcpOAuthClient()


def _no_registration(issuer: str) -> str:
    return (
        f"{issuer} does not offer dynamic client registration. Authorizing it needs a client "
        "id registered by hand with that server, which Motet does not support."
    )


def _origin(url: str) -> str:
    parts = urlsplit(url)
    return f"{parts.scheme.lower()}://{parts.netloc.lower()}"


def _document(response: httpx.Response, url: str, what: str) -> dict[str, Any]:
    try:
        document = response.json()
    except ValueError as exc:
        raise McpOAuthError(f"The {what} at {url} is not JSON.") from exc
    if not isinstance(document, dict):
        raise McpOAuthError(f"The {what} at {url} is not a JSON object.")
    return document


def _error_text(response: httpx.Response) -> str:
    try:
        body = response.json()
    except ValueError:
        return response.text[:200]
    if isinstance(body, dict):
        return str(body.get("error_description") or body.get("error") or body)[:200]
    return str(body)[:200]


__all__ = [
    "PROVIDER",
    "AuthorizationServer",
    "FakeMcpOAuthClient",
    "HttpMcpOAuthClient",
    "McpOAuthClient",
    "McpOAuthError",
    "RegistrationUnsupportedError",
    "Resolver",
    "TokenSet",
    "UnsafeUrlError",
    "authorization_url",
    "build_mcp_oauth_client",
    "canonical_resource",
    "parse_token_response",
    "system_resolver",
]
