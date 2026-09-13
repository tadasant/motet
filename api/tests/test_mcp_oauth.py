"""An MCP client authorizing itself against Motet, end to end (motet#111, pick C2).

Discovery, dynamic registration, `/authorize` through the fake Google provider, the SPA's
callback, the code exchange, a tool call with the token, refresh rotation, revocation, and
the allowlist cutting a grant off. Every step is the real handler; only Google is the fake,
exactly as it is for signing in (`test_auth.py`).
"""

from __future__ import annotations

import base64
import hashlib
import secrets
from collections.abc import Iterator
from typing import Any
from urllib.parse import parse_qs, urlsplit

import psycopg
import pytest
from fastapi.testclient import TestClient
from mcp.shared.inbound import CLIENT_CAPABILITIES_META_KEY, PROTOCOL_VERSION_META_KEY
from motet_api import app
from motet_api.auth import ADMIN_EMAILS_ENV, ALLOWED_EMAILS_ENV, FAKE_EMAIL
from motet_api.deps import reset_store
from motet_db import mcp_oauth

TOKEN = "test-api-token"
HOST = "api.motet.test"
ISSUER = f"https://{HOST}"
RESOURCE = f"{ISSUER}/mcp"
APP_ORIGIN = "http://app.motet.test"
CLIENT_REDIRECT = "http://localhost:33418/callback"
PROTOCOL = "2026-07-28"


@pytest.fixture
def api(
    db: psycopg.Connection[Any], _migrated: str, object_store: Any, monkeypatch: pytest.MonkeyPatch
) -> Iterator[TestClient]:
    monkeypatch.setenv("MOTET_API_TOKEN", TOKEN)
    monkeypatch.setenv("DATABASE_URL", _migrated)
    monkeypatch.setenv(ALLOWED_EMAILS_ENV, FAKE_EMAIL)
    monkeypatch.setenv("MOTET_PUBLIC_BASE_URL", ISSUER)
    monkeypatch.setenv("MOTET_APP_BASE_URL", APP_ORIGIN)
    reset_store()
    with TestClient(app, base_url=ISSUER) as started:
        yield started
    reset_store()


def pkce() -> tuple[str, str]:
    verifier = secrets.token_urlsafe(48)
    digest = hashlib.sha256(verifier.encode()).digest()
    return verifier, base64.urlsafe_b64encode(digest).decode().rstrip("=")


def register(api: TestClient, name: str = "Test agent") -> str:
    response = api.post(
        "/register",
        json={
            "client_name": name,
            "redirect_uris": [CLIENT_REDIRECT],
            "token_endpoint_auth_method": "none",
            "grant_types": ["authorization_code", "refresh_token"],
            "response_types": ["code"],
        },
    )
    assert response.status_code == 201, response.text
    client_id: str = response.json()["client_id"]
    return client_id


def authorize(api: TestClient, client_id: str, **extra: str) -> tuple[str, str, str]:
    """`/authorize`, through Google's (fake) consent, back to the SPA: code, state, verifier."""
    verifier, challenge = pkce()
    response = api.get(
        "/authorize",
        params={
            "response_type": "code",
            "client_id": client_id,
            "redirect_uri": CLIENT_REDIRECT,
            "code_challenge": challenge,
            "code_challenge_method": "S256",
            "state": "client-state",
            "resource": RESOURCE,
            **extra,
        },
        follow_redirects=False,
    )
    assert response.status_code == 302, response.text
    landing = urlsplit(response.headers["location"])
    # Google hands the person back to the SPA's one registered callback, never to the client.
    assert f"{landing.scheme}://{landing.netloc}{landing.path}" == f"{APP_ORIGIN}/oauth/callback"
    query = parse_qs(landing.query)
    return query["code"][0], query["state"][0], verifier


def approve(api: TestClient, spa_code: str, state: str) -> dict[str, Any]:
    response = api.post("/v1/auth/mcp/callback", json={"state": state, "code": spa_code})
    assert response.status_code == 200, response.text
    body: dict[str, Any] = response.json()
    return body


def exchange(api: TestClient, client_id: str, code: str, verifier: str) -> Any:
    return api.post(
        "/token",
        data={
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": CLIENT_REDIRECT,
            "client_id": client_id,
            "code_verifier": verifier,
            "resource": RESOURCE,
        },
    )


def grant(api: TestClient) -> tuple[str, dict[str, Any]]:
    """The whole flow, returning the client id and the token response."""
    client_id = register(api)
    spa_code, state, verifier = authorize(api, client_id)
    approval = approve(api, spa_code, state)
    code = parse_qs(urlsplit(approval["redirect_url"]).query)["code"][0]
    response = exchange(api, client_id, code, verifier)
    assert response.status_code == 200, response.text
    tokens: dict[str, Any] = response.json()
    return client_id, tokens


def whoami(api: TestClient, access_token: str) -> Any:
    meta = {PROTOCOL_VERSION_META_KEY: PROTOCOL, CLIENT_CAPABILITIES_META_KEY: {}}
    return api.post(
        "/mcp",
        json={
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {"name": "whoami", "arguments": {}, "_meta": meta},
        },
        headers={
            "Authorization": f"Bearer {access_token}",
            "Accept": "application/json, text/event-stream",
            "MCP-Protocol-Version": PROTOCOL,
            "Mcp-Method": "tools/call",
            "Mcp-Name": "whoami",
        },
    )


def refresh(api: TestClient, client_id: str, refresh_token: str) -> Any:
    return api.post(
        "/token",
        data={
            "grant_type": "refresh_token",
            "refresh_token": refresh_token,
            "client_id": client_id,
        },
    )


class TestDiscovery:
    def test_the_resource_names_motet_as_its_authorization_server(self, api: TestClient) -> None:
        resource = api.get("/.well-known/oauth-protected-resource/mcp").json()
        assert resource["resource"] == RESOURCE
        assert [server.rstrip("/") for server in resource["authorization_servers"]] == [ISSUER]

    def test_the_authorization_server_advertises_registration_pkce_and_revocation(
        self, api: TestClient
    ) -> None:
        metadata = api.get("/.well-known/oauth-authorization-server").json()
        assert metadata["issuer"].rstrip("/") == ISSUER
        assert metadata["authorization_endpoint"] == f"{ISSUER}/authorize"
        assert metadata["registration_endpoint"] == f"{ISSUER}/register"
        assert metadata["revocation_endpoint"] == f"{ISSUER}/revoke"
        assert metadata["code_challenge_methods_supported"] == ["S256"]

    def test_unconfigured_there_is_no_authorization_server_at_all(
        self, api: TestClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv("MOTET_PUBLIC_BASE_URL")
        assert api.get("/.well-known/oauth-authorization-server").status_code == 404
        assert api.get("/.well-known/oauth-protected-resource/mcp").status_code == 404
        refused = api.post("/v1/auth/mcp/callback", json={"state": "mcp.x", "code": "c"})
        assert refused.status_code == 503


class TestTheFlow:
    def test_a_client_gets_a_token_that_acts_as_the_person_who_approved_it(
        self, api: TestClient
    ) -> None:
        client_id = register(api)
        spa_code, state, verifier = authorize(api, client_id)
        assert state.startswith("mcp.")

        approval = approve(api, spa_code, state)
        assert approval["client_name"] == "Test agent"
        assert approval["redirect_host"] == "localhost:33418"
        assert approval["email"] == FAKE_EMAIL
        allow = parse_qs(urlsplit(approval["redirect_url"]).query)
        assert allow["state"] == ["client-state"]
        deny = parse_qs(urlsplit(approval["deny_url"]).query)
        assert deny["error"] == ["access_denied"] and deny["state"] == ["client-state"]

        tokens = exchange(api, client_id, allow["code"][0], verifier).json()
        assert tokens["token_type"].lower() == "bearer" and tokens["expires_in"] == 3600

        answered = whoami(api, tokens["access_token"])
        assert answered.status_code == 200, answered.text
        who = answered.json()["result"]["structuredContent"]
        assert who == {**who, "how": "session", "email": FAKE_EMAIL}
        # The same session is a `/v1` bearer too: one check decides who may call this API.
        headers = {"Authorization": f"Bearer {tokens['access_token']}"}
        assert api.get("/v1/auth/session", headers=headers).json()["email"] == FAKE_EMAIL

    def test_a_code_is_spent_exactly_once(self, api: TestClient) -> None:
        client_id = register(api)
        spa_code, state, verifier = authorize(api, client_id)
        code = parse_qs(urlsplit(approve(api, spa_code, state)["redirect_url"]).query)["code"][0]
        assert exchange(api, client_id, code, verifier).status_code == 200
        replay = exchange(api, client_id, code, verifier)
        assert replay.status_code == 400 and replay.json()["error"] == "invalid_grant"

    def test_the_spa_callback_is_spent_exactly_once(self, api: TestClient) -> None:
        spa_code, state, _ = authorize(api, register(api))
        approve(api, spa_code, state)
        again = api.post("/v1/auth/mcp/callback", json={"state": state, "code": spa_code})
        assert again.status_code == 400

    def test_a_wrong_code_verifier_is_refused(self, api: TestClient) -> None:
        client_id = register(api)
        spa_code, state, _ = authorize(api, client_id)
        code = parse_qs(urlsplit(approve(api, spa_code, state)["redirect_url"]).query)["code"][0]
        _, other_verifier = pkce()
        assert exchange(api, client_id, code, other_verifier).status_code == 400

    def test_a_token_for_another_resource_is_refused_at_authorize(self, api: TestClient) -> None:
        client_id = register(api)
        verifier, challenge = pkce()
        response = api.get(
            "/authorize",
            params={
                "response_type": "code",
                "client_id": client_id,
                "redirect_uri": CLIENT_REDIRECT,
                "code_challenge": challenge,
                "code_challenge_method": "S256",
                "state": "s",
                "resource": "https://elsewhere.example/mcp",
            },
            follow_redirects=False,
        )
        assert response.status_code == 302
        assert parse_qs(urlsplit(response.headers["location"]).query)["error"] == ["invalid_target"]

    def test_an_address_off_the_allowlist_is_refused_at_the_callback(
        self, api: TestClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        spa_code, state, _ = authorize(api, register(api))
        monkeypatch.setenv(ALLOWED_EMAILS_ENV, "someone-else@motet.test")
        refused = api.post("/v1/auth/mcp/callback", json={"state": state, "code": spa_code})
        assert refused.status_code == 403

    def test_the_two_callbacks_do_not_accept_each_others_state(self, api: TestClient) -> None:
        spa_code, state, _ = authorize(api, register(api))
        wrong = api.post("/v1/auth/google/callback", json={"state": state, "code": spa_code})
        assert wrong.status_code == 400
        login = api.post("/v1/auth/mcp/callback", json={"state": "login.abc", "code": "c"})
        assert login.status_code == 400


class TestTheGrantAfterwards:
    def test_refreshing_rotates_both_tokens(self, api: TestClient) -> None:
        client_id, tokens = grant(api)
        rotated = refresh(api, client_id, tokens["refresh_token"])
        assert rotated.status_code == 200, rotated.text
        new = rotated.json()
        assert whoami(api, new["access_token"]).status_code == 200
        assert whoami(api, tokens["access_token"]).status_code == 401
        replay = refresh(api, client_id, tokens["refresh_token"])
        assert replay.status_code == 400 and replay.json()["error"] == "invalid_grant"

    def test_revoking_the_access_token_ends_the_grant(self, api: TestClient) -> None:
        client_id, tokens = grant(api)
        revoked = api.post(
            "/revoke",
            # `client_secret` present and empty: the SDK's revocation model requires the key
            # even for a public client, whose authentication ignores it.
            data={"token": tokens["access_token"], "client_id": client_id, "client_secret": ""},
        )
        assert revoked.status_code == 200, revoked.text
        assert whoami(api, tokens["access_token"]).status_code == 401
        assert refresh(api, client_id, tokens["refresh_token"]).status_code == 400

    def test_leaving_the_allowlist_ends_both_the_token_and_the_refresh(
        self, api: TestClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        client_id, tokens = grant(api)
        monkeypatch.setenv(ALLOWED_EMAILS_ENV, "someone-else@motet.test")
        assert whoami(api, tokens["access_token"]).status_code == 401
        assert refresh(api, client_id, tokens["refresh_token"]).status_code == 400

    def test_logout_everywhere_revokes_mcp_grants_too(self, api: TestClient) -> None:
        client_id, tokens = grant(api)
        response = api.post("/v1/auth/logout-all", headers={"Authorization": f"Bearer {TOKEN}"})
        assert response.status_code == 200
        assert whoami(api, tokens["access_token"]).status_code == 401
        assert refresh(api, client_id, tokens["refresh_token"]).status_code == 400


class TestHostileClients:
    """Registration is unauthenticated, so every field of it is a stranger's input."""

    @pytest.mark.parametrize(
        "uri",
        [
            "javascript:alert(document.domain)//",
            "data:text/html,<script>alert(1)</script>",
            "vbscript:msgbox(1)",
            "http://attacker.example/callback",
        ],
    )
    def test_a_redirect_that_is_not_a_client_is_refused_at_registration(
        self, api: TestClient, uri: str
    ) -> None:
        response = api.post(
            "/register",
            json={
                "client_name": "Claude Desktop",
                "redirect_uris": [uri],
                "token_endpoint_auth_method": "none",
            },
        )
        assert response.status_code == 400, response.text
        assert response.json()["error"] == "invalid_redirect_uri"

    @pytest.mark.parametrize(
        "uri", ["https://client.example/cb", "http://127.0.0.1:9000/cb", "vscode://pub.ext/cb"]
    )
    def test_https_loopback_and_a_desktop_scheme_register(self, api: TestClient, uri: str) -> None:
        response = api.post(
            "/register", json={"redirect_uris": [uri], "token_endpoint_auth_method": "none"}
        )
        assert response.status_code == 201, response.text

    def test_registration_is_bounded(self, api: TestClient) -> None:
        too_many = [f"https://client.example/cb{i}" for i in range(11)]
        many = api.post(
            "/register", json={"redirect_uris": too_many, "token_endpoint_auth_method": "none"}
        )
        assert many.status_code == 400 and many.json()["error"] == "invalid_redirect_uri"
        huge = api.post(
            "/register",
            json={
                "client_name": "x" * 9000,
                "redirect_uris": [CLIENT_REDIRECT],
                "token_endpoint_auth_method": "none",
            },
        )
        assert huge.status_code == 400 and huge.json()["error"] == "invalid_client_metadata"

    def test_a_client_stored_before_the_check_gets_no_code(
        self, api: TestClient, db: psycopg.Connection[Any]
    ) -> None:
        hostile = "javascript:alert(document.domain)//"
        mcp_oauth.register_client(
            db,
            client_id="stored-before",
            client_info={
                "client_id": "stored-before",
                "redirect_uris": [hostile],
                "token_endpoint_auth_method": "none",
                "grant_types": ["authorization_code"],
                "response_types": ["code"],
            },
        )
        db.commit()
        spa_code, state, _ = authorize(api, "stored-before", redirect_uri=hostile)
        refused = api.post("/v1/auth/mcp/callback", json={"state": state, "code": spa_code})
        assert refused.status_code == 400
        assert db.execute("SELECT count(*) AS n FROM mcp_oauth_codes").fetchone()["n"] == 0

    def test_an_issuer_with_a_path_is_not_configured(
        self, api: TestClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("MOTET_PUBLIC_BASE_URL", f"{ISSUER}/api")
        assert api.get("/.well-known/oauth-authorization-server").status_code == 404


class TestWhatAGrantCanReach:
    def test_a_grant_is_never_an_operator_even_when_its_approver_is(
        self, api: TestClient, db: psycopg.Connection[Any], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv(ADMIN_EMAILS_ENV, FAKE_EMAIL)
        _, tokens = grant(api)
        as_grant = {"Authorization": f"Bearer {tokens['access_token']}"}
        refused = api.get("/v1/admin/overview", headers=as_grant)
        assert refused.status_code == 403
        assert "MCP client" in refused.json()["detail"]
        assert api.get("/v1/auth/session", headers=as_grant).json()["admin"] is False
        # The same person in a browser is still the operator.
        from motet_db import auth as auth_repo
        from motet_db import repo

        browser = auth_repo.new_session_token()
        auth_repo.create_session(db, user_id=repo.OWNER_USER_ID, email=FAKE_EMAIL, token=browser)
        db.commit()
        as_browser = {"Authorization": f"Bearer {browser}"}
        assert api.get("/v1/admin/overview", headers=as_browser).status_code == 200

    def test_logging_out_the_access_token_takes_its_refresh_token_too(
        self, api: TestClient
    ) -> None:
        client_id, tokens = grant(api)
        headers = {"Authorization": f"Bearer {tokens['access_token']}"}
        assert api.post("/v1/auth/logout", headers=headers).status_code == 204
        assert refresh(api, client_id, tokens["refresh_token"]).status_code == 400
