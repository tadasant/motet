"""The Credentials screen's API (motet#102), through ``TestClient`` against a real Postgres.

The fake MCP OAuth client stands in for the server (invariant 7); the client's own wire
behaviour is pinned in ``sources/tests/test_mcp_oauth.py``. What is asserted here is the
contract: no answer carries a secret; a site needs only a domain; an MCP server cannot be
added without the owner acknowledging its risk; the ``connector.`` state prefix routes the
callback and the other two flows refuse it; and the token set is sealed under the vault so
that only a worker's key manager opens it.
"""

from __future__ import annotations

import base64
import hashlib
import json
from typing import Any
from urllib.parse import parse_qs, urlsplit

import psycopg
import pytest
from fastapi.testclient import TestClient
from motet_api import app
from motet_api.connectors import CONNECTOR_STATE_PREFIX, MCP_RISK
from motet_api.deps import reset_store
from motet_db import connectors as connector_repo
from motet_sources.mcp_oauth import FakeMcpOAuthClient
from motet_vault import LocalKeyManager, build_key_manager

TOKEN = "test-api-token"
AUTH = {"Authorization": f"Bearer {TOKEN}"}
REDIRECT = "http://localhost:5173/oauth/callback"
MCP_URL = "https://mcp.example/mcp?servers=mail-ro"
ORIGIN = "https://mcp.example"

#: Every name that would mean a secret, or its envelope, leaked into an answer.
SECRET_KEYS = {"password", "secret", "ciphertext", "nonce", "wrapped_dek", "access_token"}


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
    with TestClient(app) as started:
        yield started
    reset_store()


def key_manager() -> LocalKeyManager:
    """The same local KEK the API's wrapper resolves to, so a test can play the worker."""
    manager = build_key_manager({"MOTET_VAULT_BACKEND": "local", "MOTET_INFERENCE_MODE": "fake"})
    assert isinstance(manager, LocalKeyManager)
    return manager


def add_site(api: TestClient, **body: Any) -> Any:
    return api.post("/v1/connectors", json={"kind": "site", **body}, headers=AUTH)


def add_mcp(api: TestClient, **overrides: Any) -> dict[str, Any]:
    response = api.post(
        "/v1/connectors",
        json={
            "kind": "mcp",
            "label": "Mail (read-only)",
            "url": MCP_URL,
            "domains": ["Example.com"],
            "acknowledge_risk": True,
            **overrides,
        },
        headers=AUTH,
    )
    assert response.status_code == 201, response.text
    return dict(response.json())


def authorize(api: TestClient, connector_id: str) -> str:
    started = api.post(
        f"/v1/connectors/{connector_id}/authorize", json={"redirect_uri": REDIRECT}, headers=AUTH
    )
    assert started.status_code == 200, started.text
    return str(started.json()["state"])


class TestSites:
    def test_a_domain_alone_is_a_site_and_the_opt_in(self, api: TestClient) -> None:
        created = add_site(api, domain="https://www.Example.com/newsletters")
        assert created.status_code == 201, created.text
        body = created.json()
        assert body["domain"] == "example.com" and body["label"] == "example.com"
        assert body["username"] is None and body["has_secret"] is False
        assert body["status"] == "ready" and body["risk_acknowledged_at"] is None
        assert not SECRET_KEYS & body.keys()

    def test_a_passwordless_login_is_stored_and_never_echoed(self, api: TestClient) -> None:
        body = add_site(
            api, label="Example", domain="example.com", username="reader@example.net", password=""
        ).json()
        assert body["username"] == "reader@example.net" and body["has_secret"] is False
        listed = api.get("/v1/connectors", headers=AUTH).json()
        assert [c["id"] for c in listed] == [body["id"]]
        assert not SECRET_KEYS & listed[0].keys()

    def test_a_password_is_sealed_and_only_a_worker_can_open_it(
        self, api: TestClient, db: psycopg.Connection[Any]
    ) -> None:
        created = add_site(api, domain="x.example", username="u", password="hunter2-long").json()
        assert created["has_secret"] is True
        assert "hunter2-long" not in json.dumps(created)
        assert "hunter2-long" not in api.get("/v1/connectors", headers=AUTH).text
        row = db.execute(
            "SELECT ciphertext FROM connectors WHERE id = %s", (created["id"],)
        ).fetchone()
        assert row is not None and b"hunter2-long" not in bytes(row["ciphertext"])
        opened = connector_repo.load_connector_secret(db, key_manager(), connector_id=created["id"])
        assert opened == "hunter2-long"

    def test_the_same_domain_twice_is_a_conflict(self, api: TestClient) -> None:
        assert add_site(api, domain="x.example").status_code == 201
        again = add_site(api, domain="WWW.X.EXAMPLE", username="someone")
        assert again.status_code == 409
        assert "x.example" in again.json()["detail"]

    @pytest.mark.parametrize(
        "body",
        [
            {"domain": ""},
            {"domain": "nodot"},
            {"domain": "10.0.0.1"},
            {"domain": "x.example", "password": "pw"},
        ],
    )
    def test_a_site_that_cannot_be_one_is_refused(
        self, api: TestClient, body: dict[str, Any]
    ) -> None:
        assert add_site(api, **body).status_code == 400

    def test_delete(self, api: TestClient) -> None:
        created = add_site(api, domain="x.example").json()
        assert api.delete(f"/v1/connectors/{created['id']}", headers=AUTH).status_code == 204
        assert api.get("/v1/connectors", headers=AUTH).json() == []
        assert api.delete(f"/v1/connectors/{created['id']}", headers=AUTH).status_code == 404

    def test_the_routes_need_a_caller(self, api: TestClient) -> None:
        assert api.get("/v1/connectors").status_code == 401
        assert (
            api.post("/v1/connectors", json={"kind": "site", "domain": "x.example"}).status_code
            == 401
        )

    def test_an_unknown_kind_is_not_a_connector(self, api: TestClient) -> None:
        refused = api.post("/v1/connectors", json={"kind": "other"}, headers=AUTH)
        assert refused.status_code == 422


class TestMcpServers:
    def test_a_server_is_refused_until_its_risk_is_acknowledged(self, api: TestClient) -> None:
        refused = api.post("/v1/connectors", json={"kind": "mcp", "url": MCP_URL}, headers=AUTH)
        assert refused.status_code == 400
        assert refused.json()["detail"] == MCP_RISK
        assert api.get("/v1/connectors", headers=AUTH).json() == []

    def test_a_plain_http_server_is_refused(self, api: TestClient) -> None:
        refused = api.post(
            "/v1/connectors",
            json={"kind": "mcp", "url": "http://insecure.example/mcp", "acknowledge_risk": True},
            headers=AUTH,
        )
        assert refused.status_code == 400

    def test_a_new_server_needs_auth_and_carries_its_domains_and_the_acknowledgement(
        self, api: TestClient
    ) -> None:
        body = add_mcp(api, domains=["Example.com", "www.example.com", "other.example"])
        assert body["status"] == "needs_auth" and body["has_secret"] is False
        assert body["oauth_registered"] is False
        assert body["domains"] == ["example.com", "other.example"]
        assert body["url"] == MCP_URL
        assert body["risk_acknowledged_at"] is not None

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

        # Nothing on the connector moves until consent completes: the client rides the state.
        listed = api.get("/v1/connectors", headers=AUTH).json()[0]
        assert listed["oauth_registered"] is False and listed["oauth_issuer"] is None
        pending = db.execute(
            "SELECT provider, connector_id, source_id, code_verifier, oauth_client "
            "FROM oauth_states WHERE state = %s",
            (state,),
        ).fetchone()
        assert pending is not None
        assert pending["provider"] == "mcp" and pending["connector_id"] == connector["id"]
        assert pending["source_id"] is None
        assert pending["oauth_client"] == {
            "issuer": ORIGIN,
            "client_id": "fake-client",
            "token_endpoint": f"{ORIGIN}/oauth/token",
            "resource": f"{ORIGIN}/mcp",
            "iss_parameter_supported": True,
        }
        digest = hashlib.sha256(pending["code_verifier"].encode()).digest()
        assert query["code_challenge"] == [base64.urlsafe_b64encode(digest).decode().rstrip("=")]

    def test_the_callback_seals_the_token_set_once(
        self, api: TestClient, db: psycopg.Connection[Any]
    ) -> None:
        connector = add_mcp(api)
        state = authorize(api, connector["id"])
        finished = api.post(
            "/v1/connectors/oauth/callback",
            json={"state": state, "code": "the-code", "iss": ORIGIN},
            headers=AUTH,
        )
        assert finished.status_code == 200, finished.text
        body = finished.json()
        assert body["status"] == "ready" and body["has_secret"] is True
        assert body["secret_expires_at"] is not None
        assert body["oauth_registered"] is True and body["oauth_issuer"] == ORIGIN
        assert "fake-access" not in finished.text and "fake-refresh" not in finished.text
        opened = connector_repo.load_connector_secret(
            db, key_manager(), connector_id=connector["id"]
        )
        assert opened is not None
        assert json.loads(opened)["refresh_token"] == "fake-refresh-the-code"

        replay = api.post(
            "/v1/connectors/oauth/callback", json={"state": state, "code": "the-code"}, headers=AUTH
        )
        assert replay.status_code == 400

    def test_a_refused_exchange_leaves_the_row_needing_auth_with_the_reason(
        self, api: TestClient
    ) -> None:
        connector = add_mcp(api)
        state = authorize(api, connector["id"])
        refused = api.post(
            "/v1/connectors/oauth/callback",
            json={"state": state, "code": "bad-code", "iss": ORIGIN},
            headers=AUTH,
        )
        assert refused.status_code == 400
        listed = api.get("/v1/connectors", headers=AUTH).json()[0]
        assert listed["status"] == "needs_auth" and "invalid_grant" in (listed["last_error"] or "")

    def test_an_issuer_mismatch_is_refused(self, api: TestClient) -> None:
        connector = add_mcp(api)
        state = authorize(api, connector["id"])
        mixed = api.post(
            "/v1/connectors/oauth/callback",
            json={"state": state, "code": "the-code", "iss": "https://other.example"},
            headers=AUTH,
        )
        assert mixed.status_code == 400
        assert "issuer" in mixed.json()["detail"]

    def test_a_missing_iss_from_a_server_that_promised_one_is_refused(
        self, api: TestClient
    ) -> None:
        # The fake advertises RFC 9207 support, so a response without `iss` is a stripped one.
        connector = add_mcp(api)
        state = authorize(api, connector["id"])
        stripped = api.post(
            "/v1/connectors/oauth/callback", json={"state": state, "code": "the-code"}, headers=AUTH
        )
        assert stripped.status_code == 400
        assert "names itself" in stripped.json()["detail"]

    def test_a_server_url_carrying_credentials_is_refused(self, api: TestClient) -> None:
        refused = api.post(
            "/v1/connectors",
            json={
                "kind": "mcp",
                "url": "https://user:pw@mcp.example/mcp",
                "acknowledge_risk": True,
            },
            headers=AUTH,
        )
        assert refused.status_code == 400

    def test_a_server_that_resolves_inwards_is_a_400_on_the_row(
        self, api: TestClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from motet_api import main
        from motet_sources.mcp_oauth import UnsafeUrlError

        class Inward(FakeMcpOAuthClient):
            def discover(self, mcp_url: str) -> Any:
                raise UnsafeUrlError("mcp.example resolves to a private or reserved address")

        monkeypatch.setattr(main, "build_mcp_oauth_client", Inward)
        connector = add_mcp(api)
        refused = api.post(
            f"/v1/connectors/{connector['id']}/authorize",
            json={"redirect_uri": REDIRECT},
            headers=AUTH,
        )
        assert refused.status_code == 400
        listed = api.get("/v1/connectors", headers=AUTH).json()[0]
        assert listed["status"] == "error" and "private" in (listed["last_error"] or "")


class TestReauthorizing:
    """A server that already works must survive every way a second authorization can go."""

    @pytest.fixture
    def registrations(self, monkeypatch: pytest.MonkeyPatch) -> list[str]:
        from motet_api import main

        seen: list[str] = []

        class Counting(FakeMcpOAuthClient):
            def register(self, server: Any, *, redirect_uri: str) -> str:
                seen.append(redirect_uri)
                return f"client-{len(seen)}"

        monkeypatch.setattr(main, "build_mcp_oauth_client", Counting)
        return seen

    def connected(self, api: TestClient) -> dict[str, Any]:
        connector = add_mcp(api)
        state = authorize(api, connector["id"])
        finished = api.post(
            "/v1/connectors/oauth/callback",
            json={"state": state, "code": "first", "iss": ORIGIN},
            headers=AUTH,
        )
        assert finished.status_code == 200, finished.text
        return dict(finished.json())

    def test_the_registered_client_is_reused_while_nothing_moved(
        self, api: TestClient, registrations: list[str]
    ) -> None:
        connector = self.connected(api)
        authorize(api, connector["id"])
        assert registrations == [REDIRECT]

    def test_a_new_redirect_uri_gets_a_new_client(
        self, api: TestClient, registrations: list[str]
    ) -> None:
        connector = self.connected(api)
        other = "http://127.0.0.1:5173/oauth/callback"
        api.post(
            f"/v1/connectors/{connector['id']}/authorize",
            json={"redirect_uri": other},
            headers=AUTH,
        ).raise_for_status()
        assert registrations == [REDIRECT, other]

    def test_an_abandoned_reauthorize_leaves_the_working_client_alone(
        self, api: TestClient, db: psycopg.Connection[Any], registrations: list[str]
    ) -> None:
        connector = self.connected(api)
        before = db.execute(
            "SELECT oauth_client_id, oauth_redirect_uri FROM connectors WHERE id = %s",
            (connector["id"],),
        ).fetchone()
        api.post(
            f"/v1/connectors/{connector['id']}/authorize",
            json={"redirect_uri": "http://127.0.0.1:5173/oauth/callback"},
            headers=AUTH,
        ).raise_for_status()
        after = db.execute(
            "SELECT oauth_client_id, oauth_redirect_uri FROM connectors WHERE id = %s",
            (connector["id"],),
        ).fetchone()
        assert before == after == {"oauth_client_id": "client-1", "oauth_redirect_uri": REDIRECT}

    def test_a_failed_reauthorize_keeps_a_working_server_ready(
        self, api: TestClient, monkeypatch: pytest.MonkeyPatch, registrations: list[str]
    ) -> None:
        from motet_api import main
        from motet_sources.mcp_oauth import McpOAuthError

        connector = self.connected(api)

        class Unreachable(FakeMcpOAuthClient):
            def discover(self, mcp_url: str) -> Any:
                raise McpOAuthError("Could not reach the server")

        monkeypatch.setattr(main, "build_mcp_oauth_client", Unreachable)
        failed = api.post(
            f"/v1/connectors/{connector['id']}/authorize",
            json={"redirect_uri": REDIRECT},
            headers=AUTH,
        )
        assert failed.status_code == 502
        listed = api.get("/v1/connectors", headers=AUTH).json()[0]
        assert listed["status"] == "ready" and listed["has_secret"] is True
        assert "Could not reach" in (listed["last_error"] or "")

    def test_a_refused_code_on_reauthorize_keeps_a_working_server_ready(
        self, api: TestClient, registrations: list[str]
    ) -> None:
        connector = self.connected(api)
        state = authorize(api, connector["id"])
        refused = api.post(
            "/v1/connectors/oauth/callback",
            json={"state": state, "code": "bad-code", "iss": ORIGIN},
            headers=AUTH,
        )
        assert refused.status_code == 400
        listed = api.get("/v1/connectors", headers=AUTH).json()[0]
        assert listed["status"] == "ready" and "invalid_grant" in (listed["last_error"] or "")

    def test_the_mailbox_callback_refuses_a_connector_state(self, api: TestClient) -> None:
        refused = api.post(
            "/v1/sources/callback", json={"state": "connector.abc", "code": "c"}, headers=AUTH
        )
        assert refused.status_code == 400 and "connector" in refused.json()["detail"]

    def test_the_connector_callback_refuses_the_other_flows(self, api: TestClient) -> None:
        for state in ("login.abc", "plain-mailbox-state"):
            refused = api.post(
                "/v1/connectors/oauth/callback", json={"state": state, "code": "c"}, headers=AUTH
            )
            assert refused.status_code == 400

    def test_a_mailbox_state_is_not_spent_by_the_connector_callback(
        self, api: TestClient, db: psycopg.Connection[Any]
    ) -> None:
        started = api.post(
            "/v1/sources/connect",
            json={"provider": "gmail", "name": "Mail", "redirect_uri": REDIRECT},
            headers=AUTH,
        )
        assert started.status_code == 201, started.text
        state = started.json()["state"]
        api.post("/v1/connectors/oauth/callback", json={"state": state, "code": "c"}, headers=AUTH)
        still = db.execute("SELECT 1 FROM oauth_states WHERE state = %s", (state,)).fetchone()
        assert still is not None

    def test_a_redirect_uri_that_is_not_this_deployments_callback_is_refused(
        self, api: TestClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # A dynamically registered client accepts whatever it was registered with, so this
        # route has to be the check the provider is for Google.
        monkeypatch.setenv("MOTET_APP_BASE_URL", "https://app.example")
        connector = add_mcp(api)
        refused = api.post(
            f"/v1/connectors/{connector['id']}/authorize",
            json={"redirect_uri": "https://evil.example/oauth/callback"},
            headers=AUTH,
        )
        assert refused.status_code == 400
        allowed = api.post(
            f"/v1/connectors/{connector['id']}/authorize",
            json={"redirect_uri": "https://app.example/oauth/callback"},
            headers=AUTH,
        )
        assert allowed.status_code == 200, allowed.text

    def test_authorizing_a_site_is_refused(self, api: TestClient) -> None:
        created = add_site(api, domain="x.example").json()
        refused = api.post(
            f"/v1/connectors/{created['id']}/authorize",
            json={"redirect_uri": REDIRECT},
            headers=AUTH,
        )
        assert refused.status_code == 400

    def test_a_server_without_registration_is_reported_on_the_row(
        self, api: TestClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from motet_api import main

        monkeypatch.setattr(
            main, "build_mcp_oauth_client", lambda: FakeMcpOAuthClient(registration=False)
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
        assert "registered by hand" in (listed["last_error"] or "")
        assert listed["oauth_registered"] is False
