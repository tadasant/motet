"""The MCP OAuth 2.1 client on the wire, and the guards on every URL a server hands it.

Drives the **real** :class:`HttpMcpOAuthClient` over ``httpx.MockTransport`` scripted the
way a real MCP server answered the prototype on 2026-09-12 — the 401 pointer, both
metadata documents, open registration — and asserts the bytes: which URLs discovery asks
for, what registration sends, and that the token request carries the PKCE verifier and
the canonical ``resource`` with the query stripped. ``test_drain.py``'s argument: this is a
claim about a socket, so a fake's bookkeeping cannot make it.

The guard tests are the half a real server would never exercise: a metadata document that
points at a private address, a non-https endpoint, and a ``javascript:`` authorization
endpoint that the SPA would otherwise hand to ``window.location``.
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest
from motet_sources.mcp_oauth import (
    FakeMcpOAuthClient,
    HttpMcpOAuthClient,
    McpOAuthError,
    RegistrationUnsupportedError,
    TokenSet,
    UnsafeUrlError,
    authorization_url,
    build_mcp_oauth_client,
    canonical_resource,
)

MCP_URL = "https://mcp.example/mcp?servers=mail-ro"
ORIGIN = "https://mcp.example"
REDIRECT = "http://localhost:5173/oauth/callback"

PRM = {
    "resource": f"{ORIGIN}/mcp",
    "authorization_servers": [ORIGIN],
    "scopes_supported": ["mcp"],
    "bearer_methods_supported": ["header"],
}
AS_METADATA = {
    "issuer": ORIGIN,
    "authorization_endpoint": f"{ORIGIN}/oauth/authorize",
    "token_endpoint": f"{ORIGIN}/oauth/token",
    "registration_endpoint": f"{ORIGIN}/oauth/register",
    "response_types_supported": ["code"],
    "grant_types_supported": ["authorization_code", "refresh_token"],
    "code_challenge_methods_supported": ["S256"],
    "token_endpoint_auth_methods_supported": ["none"],
    "authorization_response_iss_parameter_supported": True,
}


def public(_host: str) -> Sequence[str]:
    """Every host is on the public internet — the resolver a test that is not about it uses."""
    return ["93.184.216.34"]


def server(
    seen: list[httpx.Request], *, metadata: dict[str, object] | None = None
) -> httpx.MockTransport:
    """A transport that answers the way a real MCP server did, recording every request."""
    meta = AS_METADATA if metadata is None else metadata

    def handle(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        path = request.url.path
        if path == "/mcp":
            return httpx.Response(
                401,
                headers={
                    "WWW-Authenticate": (
                        'Bearer error="invalid_token", '
                        f'resource_metadata="{ORIGIN}/.well-known/oauth-protected-resource/mcp"'
                    )
                },
                json={"error": "invalid_token"},
            )
        if path == "/.well-known/oauth-protected-resource/mcp":
            return httpx.Response(200, json=PRM)
        if path == "/.well-known/oauth-authorization-server":
            return httpx.Response(200, json=meta)
        if path == "/oauth/register":
            body = json.loads(request.content)
            return httpx.Response(
                201,
                json={
                    "client_id": "registered-client",
                    "client_name": body["client_name"],
                    "redirect_uris": body["redirect_uris"],
                    "token_endpoint_auth_method": "none",
                },
            )
        if path == "/oauth/token":
            form = parse_qs(request.content.decode())
            if form["grant_type"] == ["refresh_token"]:
                return httpx.Response(
                    200, json={"access_token": "at-2", "token_type": "Bearer", "expires_in": 3600}
                )
            return httpx.Response(
                200,
                json={
                    "access_token": "at-1",
                    "refresh_token": "rt-1",
                    "token_type": "Bearer",
                    "expires_in": 3600,
                    "scope": "mcp",
                },
            )
        return httpx.Response(404)

    return httpx.MockTransport(handle)


def client(seen: list[httpx.Request], **kwargs: object) -> HttpMcpOAuthClient:
    return HttpMcpOAuthClient(transport=server(seen, **kwargs), resolve=public)  # type: ignore[arg-type]


class TestOnTheWire:
    def test_discovery_follows_the_401_pointer_to_both_documents(self) -> None:
        seen: list[httpx.Request] = []
        found = client(seen).discover(MCP_URL)
        assert [str(r.url) for r in seen] == [
            MCP_URL,
            f"{ORIGIN}/.well-known/oauth-protected-resource/mcp",
            f"{ORIGIN}/.well-known/oauth-authorization-server",
        ]
        # The probe of the MCP URL itself carries no credential.
        assert "authorization" not in seen[0].headers
        assert found.issuer == ORIGIN
        assert found.token_endpoint == f"{ORIGIN}/oauth/token"
        assert found.registration_endpoint == f"{ORIGIN}/oauth/register"
        assert found.resource == f"{ORIGIN}/mcp"
        assert found.scopes == ("mcp",)
        assert found.iss_parameter_supported is True

    def test_registration_sends_a_public_client_with_our_redirect(self) -> None:
        seen: list[httpx.Request] = []
        oauth = client(seen)
        client_id = oauth.register(oauth.discover(MCP_URL), redirect_uri=REDIRECT)
        assert client_id == "registered-client"
        request = seen[-1]
        assert request.method == "POST" and request.url.path == "/oauth/register"
        assert json.loads(request.content) == {
            "client_name": "Motet",
            "redirect_uris": [REDIRECT],
            "grant_types": ["authorization_code", "refresh_token"],
            "response_types": ["code"],
            "token_endpoint_auth_method": "none",
            "scope": "mcp",
        }

    def test_a_server_without_registration_stops_with_its_own_error(self) -> None:
        seen: list[httpx.Request] = []
        metadata = {k: v for k, v in AS_METADATA.items() if k != "registration_endpoint"}
        oauth = client(seen, metadata=metadata)
        found = oauth.discover(MCP_URL)
        assert found.registration_endpoint is None
        with pytest.raises(RegistrationUnsupportedError, match="registered by hand"):
            oauth.register(found, redirect_uri=REDIRECT)
        assert not any(r.url.path == "/oauth/register" for r in seen)

    def test_the_consent_url_carries_pkce_and_the_canonical_resource(self) -> None:
        found = client([]).discover(MCP_URL)
        url = authorization_url(
            found, client_id="cid", redirect_uri=REDIRECT, state="connector.s", code_challenge="ch"
        )
        parts = urlsplit(url)
        assert f"{parts.scheme}://{parts.netloc}{parts.path}" == f"{ORIGIN}/oauth/authorize"
        assert parse_qs(parts.query) == {
            "response_type": ["code"],
            "client_id": ["cid"],
            "redirect_uri": [REDIRECT],
            "state": ["connector.s"],
            "code_challenge": ["ch"],
            "code_challenge_method": ["S256"],
            "resource": [f"{ORIGIN}/mcp"],
            "scope": ["mcp"],
        }

    def test_the_token_request_is_a_form_with_the_verifier_and_resource(self) -> None:
        seen: list[httpx.Request] = []
        oauth = client(seen)
        tokens = oauth.exchange_code(
            oauth.discover(MCP_URL),
            client_id="cid",
            code="the-code",
            redirect_uri=REDIRECT,
            code_verifier="ver",
        )
        request = seen[-1]
        assert request.method == "POST" and request.url.path == "/oauth/token"
        assert request.headers["content-type"] == "application/x-www-form-urlencoded"
        assert "authorization" not in request.headers  # a public client: no secret to send
        assert parse_qs(request.content.decode()) == {
            "grant_type": ["authorization_code"],
            "code": ["the-code"],
            "redirect_uri": [REDIRECT],
            "client_id": ["cid"],
            "code_verifier": ["ver"],
            "resource": [f"{ORIGIN}/mcp"],
        }
        assert tokens.access_token == "at-1" and tokens.refresh_token == "rt-1"
        assert tokens.expires_at is not None
        assert "at-1" not in repr(tokens) and "rt-1" not in repr(tokens)

    def test_refresh_spends_the_refresh_token_against_the_recorded_endpoint(self) -> None:
        seen: list[httpx.Request] = []
        tokens = client(seen).refresh(
            token_endpoint=f"{ORIGIN}/oauth/token",
            client_id="cid",
            refresh_token="rt-1",
            resource=f"{ORIGIN}/mcp",
        )
        assert parse_qs(seen[-1].content.decode()) == {
            "grant_type": ["refresh_token"],
            "refresh_token": ["rt-1"],
            "client_id": ["cid"],
            "resource": [f"{ORIGIN}/mcp"],
        }
        assert tokens.access_token == "at-2" and tokens.refresh_token is None

    def test_metadata_on_another_origin_is_refused(self) -> None:
        def handle(request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                401,
                headers={
                    "WWW-Authenticate": 'Bearer resource_metadata="https://evil.example/.well-known/x"'
                },
            )

        oauth = HttpMcpOAuthClient(transport=httpx.MockTransport(handle), resolve=public)
        with pytest.raises(McpOAuthError, match="another origin"):
            oauth.discover(MCP_URL)

    def test_canonical_resource_strips_the_selection_hint(self) -> None:
        assert canonical_resource("https://MCP.Example/mcp/?servers=a,b#x") == f"{ORIGIN}/mcp"

    def test_a_confidential_client_is_refused(self) -> None:
        def handle(request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                201,
                json={
                    "client_id": "c",
                    "client_secret": "s",
                    "token_endpoint_auth_method": "client_secret_basic",
                },
            )

        oauth = HttpMcpOAuthClient(transport=httpx.MockTransport(handle), resolve=public)
        found = FakeMcpOAuthClient().discover(MCP_URL)
        with pytest.raises(McpOAuthError, match="confidential"):
            oauth.register(found, redirect_uri=REDIRECT)

    def test_a_client_registered_for_another_redirect_is_refused(self) -> None:
        def handle(request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                201, json={"client_id": "c", "redirect_uris": ["https://evil.example/cb"]}
            )

        oauth = HttpMcpOAuthClient(transport=httpx.MockTransport(handle), resolve=public)
        with pytest.raises(McpOAuthError, match="different redirect URI"):
            oauth.register(FakeMcpOAuthClient().discover(MCP_URL), redirect_uri=REDIRECT)

    def test_a_non_json_page_at_one_location_falls_through_to_the_next(self) -> None:
        seen: list[httpx.Request] = []
        inner = server(seen)

        def handle(request: httpx.Request) -> httpx.Response:
            if request.url.path == "/mcp":
                # No 401 pointer, so discovery walks the well-known locations.
                seen.append(request)
                return httpx.Response(405)
            if request.url.path == "/.well-known/oauth-protected-resource/mcp":
                seen.append(request)
                return httpx.Response(200, text="<html>not here</html>")
            if request.url.path == "/.well-known/oauth-protected-resource":
                seen.append(request)
                return httpx.Response(200, json=PRM)
            return inner.handle_request(request)

        found = HttpMcpOAuthClient(transport=httpx.MockTransport(handle), resolve=public).discover(
            MCP_URL
        )
        assert found.issuer == ORIGIN
        assert f"{ORIGIN}/.well-known/oauth-protected-resource" in [str(r.url) for r in seen]

    def test_scopes_that_are_not_a_list_are_refused(self) -> None:
        def handle(request: httpx.Request) -> httpx.Response:
            if request.url.path == "/.well-known/oauth-protected-resource/mcp":
                return httpx.Response(200, json={**PRM, "scopes_supported": "mcp"})
            return server([]).handle_request(request)

        oauth = HttpMcpOAuthClient(transport=httpx.MockTransport(handle), resolve=public)
        with pytest.raises(McpOAuthError, match="not a list"):
            oauth.discover(MCP_URL)

    def test_the_consent_url_never_lands_after_a_fragment(self) -> None:
        found = FakeMcpOAuthClient().discover(MCP_URL)
        odd = type(found)(
            **{**found.__dict__, "authorization_endpoint": f"{ORIGIN}/authorize?a=1#x"}
        )
        url = authorization_url(
            odd, client_id="cid", redirect_uri=REDIRECT, state="connector.s", code_challenge="ch"
        )
        parts = urlsplit(url)
        assert parts.fragment == ""
        assert parse_qs(parts.query)["a"] == ["1"] and parse_qs(parts.query)["state"] == [
            "connector.s"
        ]


class TestTheGuards:
    def test_a_server_on_a_private_address_is_never_asked(self) -> None:
        seen: list[httpx.Request] = []
        oauth = HttpMcpOAuthClient(transport=server(seen), resolve=lambda _host: ["10.0.0.7"])
        with pytest.raises(UnsafeUrlError, match="private or reserved"):
            oauth.discover(MCP_URL)
        assert seen == []

    @pytest.mark.parametrize(
        "address",
        [
            "127.0.0.1",
            "169.254.169.254",
            "::1",
            "fd00::1",
            # `ipaddress` calls these global, and both still reach inwards.
            "64:ff9b::a9fe:a9fe",
            "::127.0.0.1",
        ],
    )
    def test_every_inward_address_is_refused(self, address: str) -> None:
        oauth = HttpMcpOAuthClient(transport=server([]), resolve=lambda _host: [address])
        with pytest.raises(UnsafeUrlError):
            oauth.discover(MCP_URL)

    def test_a_token_endpoint_that_resolves_inwards_is_refused_at_the_request(self) -> None:
        seen: list[httpx.Request] = []

        def resolve(host: str) -> Sequence[str]:
            return ["10.1.2.3"] if host == "internal.example" else ["93.184.216.34"]

        oauth = HttpMcpOAuthClient(transport=server(seen), resolve=resolve)
        with pytest.raises(UnsafeUrlError):
            oauth.refresh(
                token_endpoint="https://internal.example/token",
                client_id="cid",
                refresh_token="rt",
                resource=f"{ORIGIN}/mcp",
            )
        assert seen == []

    def test_a_plain_http_server_is_refused(self) -> None:
        oauth = HttpMcpOAuthClient(transport=server([]), resolve=public)
        with pytest.raises(UnsafeUrlError, match="https"):
            oauth.discover("http://mcp.example/mcp")

    @pytest.mark.parametrize(
        "endpoint", ["javascript:alert(document.domain)", "http://mcp.example/oauth/authorize"]
    )
    def test_an_authorization_endpoint_the_browser_would_run_is_refused(
        self, endpoint: str
    ) -> None:
        metadata = {**AS_METADATA, "authorization_endpoint": endpoint}
        with pytest.raises(UnsafeUrlError, match="authorization endpoint"):
            client([], metadata=metadata).discover(MCP_URL)

    def test_an_unresolvable_host_is_an_error_not_a_pass(self) -> None:
        def fail(_host: str) -> Sequence[str]:
            raise OSError("no such host")

        oauth = HttpMcpOAuthClient(transport=server([]), resolve=fail)
        with pytest.raises(McpOAuthError, match="Could not resolve"):
            oauth.discover(MCP_URL)


class TestTheTokenSet:
    def test_it_round_trips_through_its_sealed_form(self) -> None:
        tokens = TokenSet(
            access_token="at",
            refresh_token="rt",
            expires_at=datetime(2026, 9, 13, 12, 0, tzinfo=UTC),
            token_type="Bearer",
            scope="mcp",
        )
        assert TokenSet.from_json(tokens.to_json()) == tokens

    def test_a_document_without_an_access_token_is_refused(self) -> None:
        with pytest.raises(McpOAuthError):
            TokenSet.from_json('{"refresh_token": "rt"}')


class TestTheFake:
    def test_it_refuses_the_bad_code_and_issues_an_unexpired_grant_otherwise(self) -> None:
        fake = FakeMcpOAuthClient()
        found = fake.discover(MCP_URL)
        assert found.resource == f"{ORIGIN}/mcp"
        with pytest.raises(McpOAuthError):
            fake.exchange_code(
                found, client_id="c", code="bad-code", redirect_uri=REDIRECT, code_verifier="v"
            )
        tokens = fake.exchange_code(
            found, client_id="c", code="ok", redirect_uri=REDIRECT, code_verifier="v"
        )
        assert tokens.expires_at is not None
        assert tokens.expires_at > datetime.now(UTC) + timedelta(minutes=30)

    def test_the_builder_follows_the_one_mode_variable(self) -> None:
        assert isinstance(build_mcp_oauth_client({}), FakeMcpOAuthClient)
        assert isinstance(
            build_mcp_oauth_client({"MOTET_INFERENCE_MODE": "fake"}), FakeMcpOAuthClient
        )
        assert isinstance(
            build_mcp_oauth_client({"MOTET_INFERENCE_MODE": "real"}), HttpMcpOAuthClient
        )
