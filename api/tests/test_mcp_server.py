"""`/mcp` on motet-api (motet#111): who gets in, what a connection sees, a tool is its route.

Driven over HTTP against the real app, with a `Host` that is not localhost: the SDK's
DNS-rebinding protection answers such a request with `421 Misdirected Request` unless it is
switched off, and a test on `localhost` could never tell. The SDK's own client is used where
the claim is about what a real MCP client experiences; a raw JSON-RPC POST where the claim is
about the HTTP answer before the transport ever runs.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Awaitable, Callable, Iterator
from contextlib import AsyncExitStack, asynccontextmanager
from typing import Any

import httpx2
import psycopg
import pytest
from fastapi.testclient import TestClient
from mcp import Client
from mcp.client.streamable_http import streamable_http_client
from mcp.shared.inbound import CLIENT_CAPABILITIES_META_KEY, PROTOCOL_VERSION_META_KEY
from motet_api import app, deps
from motet_api.auth import ADMIN_EMAILS_ENV, ALLOWED_EMAILS_ENV
from motet_api.deps import reset_drain_trigger, reset_store
from motet_api.drain import DrainReason
from motet_api.mcp.registry import select_tools
from motet_api.mcp.server import mount
from motet_db import auth as auth_repo
from motet_db import repo
from starlette.applications import Starlette
from starlette.routing import BaseRoute
from starlette.types import ASGIApp

TOKEN = "test-api-token"
BEARER = {"Authorization": f"Bearer {TOKEN}"}
ADMIN_EMAIL = "operator@motet.test"
#: Not localhost, on purpose: see the module docstring.
HOST = "api.motet.test"
PROTOCOL = "2026-07-28"


@pytest.fixture
def env(
    db: psycopg.Connection[Any], _migrated: str, object_store: Any, monkeypatch: pytest.MonkeyPatch
) -> Iterator[None]:
    monkeypatch.setenv("MOTET_API_TOKEN", TOKEN)
    monkeypatch.setenv("DATABASE_URL", _migrated)
    monkeypatch.setenv(ALLOWED_EMAILS_ENV, ADMIN_EMAIL)
    monkeypatch.setenv(ADMIN_EMAILS_ENV, ADMIN_EMAIL)
    monkeypatch.delenv("MOTET_PUBLIC_BASE_URL", raising=False)
    monkeypatch.delenv("MOTET_APP_BASE_URL", raising=False)
    reset_store()
    yield
    reset_store()
    reset_drain_trigger()


@pytest.fixture
def api(env: None) -> Iterator[TestClient]:
    with TestClient(app, base_url=f"http://{HOST}") as started:
        yield started


def rpc(
    api: TestClient,
    method: str,
    params: dict[str, Any] | None = None,
    *,
    headers: dict[str, str] = BEARER,
    query: str = "",
) -> Any:
    """One self-contained 2026-07-28 JSON-RPC request: no initialize, no session."""
    meta = {PROTOCOL_VERSION_META_KEY: PROTOCOL, CLIENT_CAPABILITIES_META_KEY: {}}
    body = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": method,
        "params": {**(params or {}), "_meta": meta},
    }
    return api.post(
        f"/mcp{query}",
        json=body,
        headers={
            **headers,
            "Accept": "application/json, text/event-stream",
            "MCP-Protocol-Version": PROTOCOL,
            # 2026-07-28 routes on headers, and refuses a request whose headers and body differ.
            "Mcp-Method": method,
        },
    )


def session_for(db: psycopg.Connection[Any], email: str) -> dict[str, str]:
    token = auth_repo.new_session_token()
    auth_repo.create_session(db, user_id=repo.OWNER_USER_ID, email=email, token=token)
    db.commit()
    return {"Authorization": f"Bearer {token}"}


@asynccontextmanager
async def connected(
    target: ASGIApp, headers: dict[str, str], query: str = "", *, transport: Any = None
) -> AsyncIterator[Client]:
    http = httpx2.AsyncClient(
        transport=transport or httpx2.ASGITransport(app=target),
        base_url=f"http://{HOST}",
        headers=headers,
    )
    async with Client(streamable_http_client(f"http://{HOST}/mcp{query}", http_client=http)) as c:
        yield c


def with_client(
    headers: dict[str, str], query: str, work: Callable[[Client], Awaitable[Any]]
) -> Any:
    """Run ``work`` against the real app with its lifespan up, as a real MCP client would."""

    async def go() -> Any:
        async with app.router.lifespan_context(app), connected(app, headers, query) as client:
            return await work(client)

    return asyncio.run(go())


def text(result: Any) -> str:
    return " ".join(getattr(part, "text", "") for part in result.content)


# --- who gets in -------------------------------------------------------------------------


class TestWhoGetsIn:
    """The check in front of the transport is `require_caller`, and nothing else."""

    def test_no_credential_is_a_401_and_never_a_421(self, api: TestClient) -> None:
        response = rpc(api, "tools/list", headers={})
        assert response.status_code == 401
        assert response.headers["www-authenticate"].startswith("Bearer")
        assert response.json()["error"] == "invalid_token"

    def test_a_wrong_bearer_is_a_401_that_says_the_token_is_invalid(self, api: TestClient) -> None:
        response = rpc(api, "tools/list", headers={"Authorization": "Bearer not-it"})
        assert response.status_code == 401
        assert 'error="invalid_token"' in response.headers["www-authenticate"]

    def test_the_feed_token_is_refused_as_a_bearer(
        self, api: TestClient, db: psycopg.Connection[Any]
    ) -> None:
        feed_token = repo.ensure_feed_token(db, repo.OWNER_USER_ID)
        db.commit()
        response = rpc(api, "tools/list", headers={"Authorization": f"Bearer {feed_token}"})
        assert response.status_code == 401

    def test_the_feed_token_is_refused_in_the_query_string(
        self, api: TestClient, db: psycopg.Connection[Any]
    ) -> None:
        feed_token = repo.ensure_feed_token(db, repo.OWNER_USER_ID)
        db.commit()
        response = rpc(api, "tools/list", headers={}, query=f"?token={feed_token}")
        assert response.status_code == 401

    def test_the_shared_token_and_a_session_are_let_in(
        self, api: TestClient, db: psycopg.Connection[Any]
    ) -> None:
        assert rpc(api, "tools/list").status_code == 200
        assert rpc(api, "tools/list", headers=session_for(db, ADMIN_EMAIL)).status_code == 200

    def test_a_session_whose_address_left_the_allowlist_is_refused(
        self, api: TestClient, db: psycopg.Connection[Any]
    ) -> None:
        response = rpc(api, "tools/list", headers=session_for(db, "gone@motet.test"))
        assert response.status_code == 401

    def test_the_401_points_at_the_resource_metadata_once_oauth_is_configured(
        self, env: None, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("MOTET_PUBLIC_BASE_URL", f"https://{HOST}")
        monkeypatch.setenv("MOTET_APP_BASE_URL", "http://app.motet.test")
        with TestClient(app, base_url=f"http://{HOST}") as api:
            challenge = rpc(api, "tools/list", headers={}).headers["www-authenticate"]
        expected = f"https://{HOST}/.well-known/oauth-protected-resource/mcp"
        assert f'resource_metadata="{expected}"' in challenge

    def test_an_unknown_tool_group_is_a_400_before_anything_else(self, api: TestClient) -> None:
        response = rpc(api, "tools/list", headers={}, query="?tool_groups=nope")
        assert response.status_code == 400
        assert "Unknown tool group" in response.json()["error_description"]


# --- what a connection sees -----------------------------------------------------------------


class TestWhatAConnectionSees:
    def test_tools_list_over_http_is_the_default_surface(self, env: None) -> None:
        async def work(client: Client) -> set[str]:
            return {tool.name for tool in (await client.list_tools()).tools}

        assert with_client(BEARER, "", work) == select_tools(None)

    def test_a_readonly_connection_neither_lists_nor_calls_a_write_tool(self, env: None) -> None:
        async def work(client: Client) -> tuple[set[str], Any]:
            names = {tool.name for tool in (await client.list_tools()).tools}
            return names, await client.call_tool("paste_text", {"title": "t", "text": "x"})

        names, refused = with_client(BEARER, "?tool_groups=ingestion_readonly", work)
        assert names == {"get_ingestion_status", "list_held_source_items", "get_source_item"}
        assert refused.is_error and "Unknown tool" in text(refused)

    def test_admin_is_absent_by_default_and_answers_only_an_admin_when_named(
        self, env: None, db: psycopg.Connection[Any]
    ) -> None:
        async def names(client: Client) -> set[str]:
            return {tool.name for tool in (await client.list_tools()).tools}

        async def overview(client: Client) -> Any:
            return await client.call_tool("get_admin_overview", {})

        assert "get_admin_overview" not in with_client(BEARER, "", names)
        as_admin = with_client(session_for(db, ADMIN_EMAIL), "?tool_groups=admin", overview)
        assert not as_admin.is_error, text(as_admin)
        assert as_admin.structured_content["users"]
        as_token = with_client(BEARER, "?tool_groups=admin", overview)
        assert as_token.is_error and "403: " in text(as_token)

    def test_a_write_tool_is_its_route_including_the_drain_nudge(
        self, env: None, db: psycopg.Connection[Any], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        class Spy:
            enabled = True

            def __init__(self) -> None:
                self.fired: list[DrainReason] = []

            def fire(self, reason: DrainReason) -> None:
                self.fired.append(reason)

        spy = Spy()
        monkeypatch.setattr(deps, "_trigger", spy)

        async def work(client: Client) -> tuple[Any, Any]:
            pasted = await client.call_tool(
                "paste_text", {"title": "An MCP paste", "text": "Something happened today."}
            )
            return pasted, await client.call_tool("get_ingestion_status", {})

        pasted, status = with_client(BEARER, "", work)
        assert not pasted.is_error, text(pasted)
        item = pasted.structured_content
        assert item["state"] == "pending" and item["title"] == "An MCP paste"
        stored = db.execute(
            "SELECT title FROM source_items WHERE id = %s", (item["id"],)
        ).fetchone()
        assert stored is not None
        assert item["id"] in {row["id"] for row in status.structured_content["result"]}
        # Fired after the tool call's own commit, by `deps.connection`, exactly as for a POST.
        assert spy.fired == [DrainReason.PASTE]

    def test_a_route_refusal_is_a_tool_error_in_the_routes_own_words(self, env: None) -> None:
        async def work(client: Client) -> Any:
            return await client.call_tool("get_episode", {"episode_id": "ep_nope"})

        result = with_client(BEARER, "", work)
        assert result.is_error
        assert text(result).endswith("404: No such episode.")

    def test_arguments_the_request_model_refuses_are_a_422(self, env: None) -> None:
        async def work(client: Client) -> Any:
            return await client.call_tool("paste_text", {"title": "", "text": "x"})

        result = with_client(BEARER, "", work)
        assert result.is_error and "422: " in text(result)

    def test_whoami_says_how_the_connection_authenticated(
        self, env: None, db: psycopg.Connection[Any]
    ) -> None:
        async def work(client: Client) -> Any:
            return (await client.call_tool("whoami", {})).structured_content

        assert with_client(BEARER, "", work)["how"] == "token"
        as_session = with_client(session_for(db, ADMIN_EMAIL), "", work)
        assert as_session["how"] == "session" and as_session["email"] == ADMIN_EMAIL


# --- stateless --------------------------------------------------------------------------------


class Alternating(httpx2.AsyncBaseTransport):
    """Send each request to the next app in turn, as a load balancer with no affinity would."""

    def __init__(self, *apps: ASGIApp) -> None:
        self.transports = [httpx2.ASGITransport(app=target) for target in apps]
        self.served = [0 for _ in apps]
        self.turn = 0

    async def handle_async_request(self, request: httpx2.Request) -> httpx2.Response:
        index = self.turn % len(self.transports)
        self.turn += 1
        self.served[index] += 1
        return await self.transports[index].handle_async_request(request)


def fresh_instance() -> tuple[Starlette, Any]:
    """A second, independent `/mcp`: its own server, transport and session manager."""
    routes: list[BaseRoute] = []
    instance = mount(routes)

    @asynccontextmanager
    async def lifespan(_: Starlette) -> AsyncIterator[None]:
        async with instance.running():
            yield

    return Starlette(routes=routes, lifespan=lifespan), instance


def test_no_request_depends_on_the_instance_that_served_the_one_before(env: None) -> None:
    """Two freshly built apps, and every request alternates between them. Nothing breaks.

    With sessions, the second request would carry an `Mcp-Session-Id` the other instance had
    never issued and be refused; that is the affinity a Cloud Run service with several
    instances cannot give, and the reason the transport is stateless.
    """
    first, _ = fresh_instance()
    second, _ = fresh_instance()
    balancer = Alternating(first, second)

    async def go() -> tuple[set[str], Any]:
        async with AsyncExitStack() as stack:
            await stack.enter_async_context(first.router.lifespan_context(first))
            await stack.enter_async_context(second.router.lifespan_context(second))
            client = await stack.enter_async_context(connected(first, BEARER, transport=balancer))
            names = {tool.name for tool in (await client.list_tools()).tools}
            return names, await client.call_tool("whoami", {})

    names, who = asyncio.run(go())
    assert names == select_tools(None)
    assert not who.is_error and who.structured_content["how"] == "token"
    assert all(count >= 1 for count in balancer.served), balancer.served
