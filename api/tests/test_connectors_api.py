"""PROTOTYPE — the Credentials screen's API, and the MCP OAuth adapter on the wire.

Two halves, on trial for different things, exactly as ``test_drain.py`` splits them.

The first drives the **real** :class:`HttpMcpOAuthClient` over ``httpx.MockTransport``
scripted to answer the way the owner's email MCP server answered on 2026-09-12 — the 401 pointer,
both metadata documents, open registration — and asserts the bytes: which URLs discovery
asks, what registration sends, and that the token request carries the PKCE verifier and
the canonical ``resource`` with the ``?servers=`` query stripped. A server without a
``registration_endpoint`` is the one branch the UI renders differently, so it is pinned
too.

The second goes through ``TestClient`` against a real Postgres and the fake client, and
asserts the contract: no response carries a secret, a passwordless site login is a 201,
domains normalize, the state prefix routes the callback, and the token set is sealed under
the vault (the fake vault, whose contract is the real one's).
"""

from __future__ import annotations

import base64
import hashlib
import json
from typing import Any
from urllib.parse import parse_qs, urlsplit

import httpx
import psycopg
import pytest
from fastapi.testclient import TestClient
from motet_api import app
from motet_api.deps import reset_store
from motet_api.mcp_oauth import (
    CONNECTOR_STATE_PREFIX,
    HttpMcpOAuthClient,
    McpOAuthError,
    RegistrationUnsupportedError,
    authorization_url,
    canonical_resource,
    reset_fake,
)
from motet_db import connectors as connector_repo
from motet_db import repo
from motet_vault import LocalKeyManager, build_key_manager

TOKEN = "test-api-token"
AUTH = {"Authorization": f"Bearer {TOKEN}"}
REDIRECT = "http://localhost:5173/oauth/callback"
MCP_URL = "https://strad.example/mcp?servers=gmail-ro"
ORIGIN = "https://strad.example"

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


def strad_like(seen: list[httpx.Request], *, registration: bool = True) -> httpx.MockTransport:
    """A transport that answers the way strad did, recording every request."""

    def handle(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        path = request.url.path
        if path == "/mcp":
            return httpx.Response(
                401,
                headers={
                    "WWW-Authenticate": (
                        'Bearer realm="strad", error="invalid_token", '
                        f'resource_metadata="{ORIGIN}/.well-known/oauth-protected-resource/mcp"'
                    )
                },
                json={"error": "invalid_token"},
            )
        if path == "/.well-known/oauth-protected-resource/mcp":
            return httpx.Response(200, json=PRM)
        if path == "/.well-known/oauth-authorization-server":
            meta = dict(AS_METADATA)
            if not registration:
                del meta["registration_endpoint"]
            return httpx.Response(200, json=meta)
        if path == "/oauth/register":
            body = json.loads(request.content)
            return httpx.Response(
                201,
                json={
                    "client_id": "eyJ.registered",
                    "client_id_issued_at": 1,
                    "client_name": body["client_name"],
                    "redirect_uris": body["redirect_uris"],
                    "token_endpoint_auth_method": "none",
                },
            )
        if path == "/oauth/token":
            form = parse_qs(request.content.decode())
            if form["grant_type"] == ["refresh_token"]:
                return httpx.Response(
                    200,
                    json={"access_token": "at-2", "token_type": "Bearer", "expires_in": 3600},
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


class TestTheAdapterOnTheWire:
    def test_discovery_follows_the_401_pointer_to_both_documents(self) -> None:
        seen: list[httpx.Request] = []
        server = HttpMcpOAuthClient(transport=strad_like(seen)).discover(MCP_URL)
        assert [str(r.url) for r in seen] == [
            MCP_URL,
            f"{ORIGIN}/.well-known/oauth-protected-resource/mcp",
            f"{ORIGIN}/.well-known/oauth-authorization-server",
        ]
        # The probe of the MCP URL itself carries no credential and no body.
        assert "authorization" not in seen[0].headers
        assert server.issuer == ORIGIN
        assert server.token_endpoint == f"{ORIGIN}/oauth/token"
        assert server.registration_endpoint == f"{ORIGIN}/oauth/register"
        # The canonical resource: `?servers=gmail-ro` stripped, as the PRM states it.
        assert server.resource == f"{ORIGIN}/mcp"
        assert server.scopes == ("mcp",)
        assert server.iss_parameter_supported is True

    def test_registration_sends_a_public_client_with_our_redirect(self) -> None:
        seen: list[httpx.Request] = []
        client = HttpMcpOAuthClient(transport=strad_like(seen))
        server = client.discover(MCP_URL)
        client_id = client.register(server, redirect_uri=REDIRECT)
        assert client_id == "eyJ.registered"
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
        client = HttpMcpOAuthClient(transport=strad_like(seen, registration=False))
        server = client.discover(MCP_URL)
        assert server.registration_endpoint is None
        with pytest.raises(RegistrationUnsupportedError, match="registered by hand"):
            client.register(server, redirect_uri=REDIRECT)
        assert not any(r.url.path == "/oauth/register" for r in seen)

    def test_the_consent_url_carries_pkce_and_the_canonical_resource(self) -> None:
        server = HttpMcpOAuthClient(transport=strad_like([])).discover(MCP_URL)
        url = authorization_url(
            server, client_id="cid", redirect_uri=REDIRECT, state="connector.s", code_challenge="ch"
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
        client = HttpMcpOAuthClient(transport=strad_like(seen))
        server = client.discover(MCP_URL)
        tokens = client.exchange_code(
            server, client_id="cid", code="the-code", redirect_uri=REDIRECT, code_verifier="ver"
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
        assert "at-1" not in repr(tokens)

    def test_refresh_spends_the_refresh_token_against_the_recorded_endpoint(self) -> None:
        seen: list[httpx.Request] = []
        tokens = HttpMcpOAuthClient(transport=strad_like(seen)).refresh(
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
        assert tokens.access_token == "at-2"

    def test_metadata_on_another_origin_is_refused(self) -> None:
        def handle(request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                401,
                headers={
                    "WWW-Authenticate": 'Bearer resource_metadata="https://evil.example/.well-known/x"'
                },
            )

        with pytest.raises(McpOAuthError, match="another origin"):
            HttpMcpOAuthClient(transport=httpx.MockTransport(handle)).discover(MCP_URL)

    def test_canonical_resource_strips_the_selection_hint(self) -> None:
        assert canonical_resource("https://Strad.Example/mcp/?servers=a,b#x") == f"{ORIGIN}/mcp"


@pytest.fixture
def api(
    db: psycopg.Connection[Any],
    _migrated: str,
    object_store: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> Any:
    monkeypatch.setenv("MOTET_API_TOKEN", TOKEN)
    monkeypatch.setenv("DATABASE_URL", _migrated)
    monkeypatch.setenv("MOTET_INFERENCE_MODE", "fake")
    monkeypatch.setenv("MOTET_VAULT_BACKEND", "local")
    reset_store()
    reset_fake()
    with TestClient(app) as started:
        yield started
    reset_store()
    reset_fake()


def key_manager() -> LocalKeyManager:
    """The same local KEK the API's wrapper resolves to, so tests can play the worker."""
    manager = build_key_manager({"MOTET_VAULT_BACKEND": "local", "MOTET_INFERENCE_MODE": "fake"})
    assert isinstance(manager, LocalKeyManager)
    return manager


SECRET_KEYS = {"password", "secret", "ciphertext", "nonce", "wrapped_dek", "access_token"}


class TestSiteConnectors:
    def test_a_passwordless_login_is_stored_and_never_echoed(
        self, api: TestClient, db: psycopg.Connection[Any]
    ) -> None:
        created = api.post(
            "/v1/connectors",
            json={
                "kind": "site",
                "label": "The Information",
                "domain": "https://www.TheInformation.com/",
                "username": "reader@example.com",
                "password": "",
            },
            headers=AUTH,
        )
        assert created.status_code == 201, created.text
        body = created.json()
        assert body["domain"] == "theinformation.com"
        assert body["username"] == "reader@example.com"
        assert body["has_secret"] is False
        assert body["status"] == "ready"
        assert not SECRET_KEYS & body.keys()

        listed = api.get("/v1/connectors", headers=AUTH).json()
        assert [c["id"] for c in listed] == [body["id"]]
        assert not SECRET_KEYS & listed[0].keys()

    def test_a_password_is_sealed_and_the_worker_can_open_it(
        self, api: TestClient, db: psycopg.Connection[Any]
    ) -> None:
        created = api.post(
            "/v1/connectors",
            json={
                "kind": "site",
                "label": "x",
                "domain": "x.example",
                "username": "u",
                "password": "hunter2",
            },
            headers=AUTH,
        ).json()
        assert created["has_secret"] is True
        assert "hunter2" not in json.dumps(created)
        assert "hunter2" not in json.dumps(api.get("/v1/connectors", headers=AUTH).json())
        row = db.execute(
            "SELECT ciphertext FROM connectors WHERE id = %s", (created["id"],)
        ).fetchone()
        assert row is not None and b"hunter2" not in bytes(row["ciphertext"])
        assert (
            connector_repo.load_connector_secret(db, key_manager(), connector_id=created["id"])
            == "hunter2"
        )

    def test_the_same_domain_twice_is_a_conflict(self, api: TestClient) -> None:
        body = {"kind": "site", "label": "x", "domain": "x.example", "username": "u"}
        assert api.post("/v1/connectors", json=body, headers=AUTH).status_code == 201
        again = api.post("/v1/connectors", json={**body, "domain": "WWW.X.EXAMPLE"}, headers=AUTH)
        assert again.status_code == 409

    @pytest.mark.parametrize(
        "body",
        [
            {"kind": "site", "label": "x", "domain": "", "username": "u"},
            {"kind": "site", "label": "x", "domain": "nodot", "username": "u"},
            {"kind": "site", "label": "x", "domain": "x.example", "username": " "},
            {"kind": "mcp", "label": "x", "url": "http://insecure.example/mcp"},
            {"kind": "other", "label": "x"},
        ],
    )
    def test_incomplete_connectors_are_refused(self, api: TestClient, body: dict[str, Any]) -> None:
        assert api.post("/v1/connectors", json=body, headers=AUTH).status_code == 400

    def test_delete(self, api: TestClient) -> None:
        created = api.post(
            "/v1/connectors",
            json={"kind": "site", "label": "x", "domain": "x.example", "username": "u"},
            headers=AUTH,
        ).json()
        assert api.delete(f"/v1/connectors/{created['id']}", headers=AUTH).status_code == 204
        assert api.get("/v1/connectors", headers=AUTH).json() == []
        assert api.delete(f"/v1/connectors/{created['id']}", headers=AUTH).status_code == 404

    def test_the_routes_need_a_token(self, api: TestClient) -> None:
        assert api.get("/v1/connectors").status_code == 401


def add_mcp(api: TestClient) -> dict[str, Any]:
    response = api.post(
        "/v1/connectors",
        json={
            "kind": "mcp",
            "label": "Email (gmail-ro)",
            "url": MCP_URL,
            "domains": ["Mail.Example"],
        },
        headers=AUTH,
    )
    assert response.status_code == 201, response.text
    return dict(response.json())


class TestMcpConnectors:
    def test_a_new_server_needs_auth_and_carries_its_domains(self, api: TestClient) -> None:
        body = add_mcp(api)
        assert body["status"] == "needs_auth"
        assert body["has_secret"] is False
        assert body["oauth_registered"] is False
        assert body["domains"] == ["mail.example"]
        assert body["url"] == MCP_URL

    def test_authorize_registers_and_returns_a_consent_url(
        self, api: TestClient, db: psycopg.Connection[Any]
    ) -> None:
        connector = add_mcp(api)
        started = api.post(
            f"/v1/connectors/{connector['id']}/authorize",
            json={"redirect_uri": REDIRECT},
            headers=AUTH,
        )
        assert started.status_code == 200, started.text
        state = started.json()["state"]
        assert state.startswith(CONNECTOR_STATE_PREFIX)
        query = parse_qs(urlsplit(started.json()["authorization_url"]).query)
        assert query["state"] == [state]
        assert query["code_challenge_method"] == ["S256"]
        assert query["resource"] == [f"{ORIGIN}/mcp"]
        assert query["redirect_uri"] == [REDIRECT]

        listed = api.get("/v1/connectors", headers=AUTH).json()[0]
        assert listed["oauth_registered"] is True
        assert listed["oauth_issuer"] == ORIGIN
        pending = db.execute(
            "SELECT provider, connector_id, code_verifier FROM oauth_states WHERE state = %s",
            (state,),
        ).fetchone()
        assert pending is not None
        assert pending["provider"] == "mcp" and pending["connector_id"] == connector["id"]
        digest = hashlib.sha256(pending["code_verifier"].encode()).digest()
        assert query["code_challenge"] == [base64.urlsafe_b64encode(digest).decode().rstrip("=")]

    def test_the_callback_seals_the_token_set(
        self, api: TestClient, db: psycopg.Connection[Any]
    ) -> None:
        connector = add_mcp(api)
        state = api.post(
            f"/v1/connectors/{connector['id']}/authorize",
            json={"redirect_uri": REDIRECT},
            headers=AUTH,
        ).json()["state"]
        finished = api.post(
            "/v1/connectors/oauth/callback",
            json={"state": state, "code": "the-code", "iss": ORIGIN},
            headers=AUTH,
        )
        assert finished.status_code == 200, finished.text
        body = finished.json()
        assert body["status"] == "ready" and body["has_secret"] is True
        assert body["secret_expires_at"] is not None
        assert "fake-access" not in finished.text
        opened = connector_repo.load_connector_secret(
            db, key_manager(), connector_id=connector["id"]
        )
        assert opened is not None
        assert json.loads(opened)["refresh_token"] == "fake-refresh-the-code"
        # Single use.
        replay = api.post(
            "/v1/connectors/oauth/callback", json={"state": state, "code": "the-code"}, headers=AUTH
        )
        assert replay.status_code == 400

    def test_a_refused_exchange_leaves_the_row_needing_auth_with_the_reason(
        self, api: TestClient
    ) -> None:
        connector = add_mcp(api)
        state = api.post(
            f"/v1/connectors/{connector['id']}/authorize",
            json={"redirect_uri": REDIRECT},
            headers=AUTH,
        ).json()["state"]
        refused = api.post(
            "/v1/connectors/oauth/callback", json={"state": state, "code": "bad-code"}, headers=AUTH
        )
        assert refused.status_code == 400
        listed = api.get("/v1/connectors", headers=AUTH).json()[0]
        assert listed["status"] == "needs_auth" and "invalid_grant" in listed["last_error"]

    def test_an_issuer_mismatch_is_refused(self, api: TestClient) -> None:
        connector = add_mcp(api)
        state = api.post(
            f"/v1/connectors/{connector['id']}/authorize",
            json={"redirect_uri": REDIRECT},
            headers=AUTH,
        ).json()["state"]
        mixed = api.post(
            "/v1/connectors/oauth/callback",
            json={"state": state, "code": "the-code", "iss": "https://other.example"},
            headers=AUTH,
        )
        assert mixed.status_code == 400
        assert "issuer" in mixed.json()["detail"]

    def test_the_gmail_callback_refuses_a_connector_state(self, api: TestClient) -> None:
        refused = api.post(
            "/v1/sources/callback", json={"state": "connector.abc", "code": "c"}, headers=AUTH
        )
        assert refused.status_code == 400 and "connector" in refused.json()["detail"]

    def test_the_connector_callback_refuses_the_other_flows(self, api: TestClient) -> None:
        for state in ("login.abc", "plain-gmail-state"):
            refused = api.post(
                "/v1/connectors/oauth/callback", json={"state": state, "code": "c"}, headers=AUTH
            )
            assert refused.status_code == 400

    def test_authorizing_a_site_connector_is_refused(self, api: TestClient) -> None:
        created = api.post(
            "/v1/connectors",
            json={"kind": "site", "label": "x", "domain": "x.example", "username": "u"},
            headers=AUTH,
        ).json()
        refused = api.post(
            f"/v1/connectors/{created['id']}/authorize",
            json={"redirect_uri": REDIRECT},
            headers=AUTH,
        )
        assert refused.status_code == 400

    def test_a_server_without_registration_is_reported_on_the_row(
        self, api: TestClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from motet_api import connectors as routes
        from motet_api.mcp_oauth import FakeMcpOAuthClient

        monkeypatch.setattr(
            routes, "build_mcp_oauth_client", lambda: FakeMcpOAuthClient(registration=False)
        )
        connector = add_mcp(api)
        stopped = api.post(
            f"/v1/connectors/{connector['id']}/authorize",
            json={"redirect_uri": REDIRECT},
            headers=AUTH,
        )
        assert stopped.status_code == 409
        assert "registered by hand" in stopped.json()["detail"]
        listed = api.get("/v1/connectors", headers=AUTH).json()[0]
        assert listed["status"] == "needs_auth"
        assert listed["last_error"] and "registered by hand" in listed["last_error"]
        assert listed["oauth_registered"] is False


def test_the_owner_is_the_only_user(db: psycopg.Connection[Any]) -> None:
    """Connectors hang off the one account, like everything else (see the sign-in section)."""
    assert repo.OWNER_USER_ID == "motet-owner"
