"""The service's two routes, its two doors, and what health is actually for."""

from __future__ import annotations

from typing import Any

import pytest
from fastapi.testclient import TestClient
from motet_enrich.app import HEALTH_PATH, PLATFORM_RESERVED_PATHS, create_app, publishable_revision
from motet_enrich.config import EnrichSettings, load_settings
from motet_enrich.contract import EnrichRequest, EnrichResult, RunCaps
from motet_enrich.runner import FakeRunner


def settings(**overrides: Any) -> EnrichSettings:
    env = {"MOTET_INFERENCE_MODE": "fake", **overrides}
    return load_settings(env)


def request_body(**overrides: Any) -> dict[str, Any]:
    body = {
        "item_id": "si_1",
        "title": "The Information: today",
        "preview_text": "Two paragraphs and a link.",
        "candidate_urls": ["https://url1.example.com/ls/click?upn=abc"],
        "site": {"domain": "example.com", "username": "owner@example.com"},
        "caps": {"max_usd": 0.5, "max_tool_calls": 40, "timeout_seconds": 600},
    }
    body.update(overrides)
    return body


@pytest.fixture
def client() -> TestClient:
    return TestClient(create_app(settings(), FakeRunner()))


class TestHealth:
    def test_it_reports_the_service_and_the_toolchain(self, client: TestClient) -> None:
        body = client.get(HEALTH_PATH).json()
        assert body["status"] == "ok"
        assert body["service"] == "motet-enrich"
        assert body["inference_mode"] == "fake"
        # No toolchain on a laptop or in CI, and that is the honest answer rather than a
        # crash: the route has to answer for a container whose npm half failed to install.
        assert body["toolchain_ready"] is False
        assert "missing" in body["toolchain_detail"]

    def test_it_reports_the_caps_a_caller_will_actually_get(self, client: TestClient) -> None:
        body = client.get(HEALTH_PATH).json()
        assert body["max_usd_per_item"] == 0.5
        assert body["max_tool_calls"] == 40
        assert body["timeout_seconds"] == 600

    def test_an_unauthenticated_deployment_says_so(self, client: TestClient) -> None:
        assert client.get(HEALTH_PATH).json()["authenticated"] is False

    def test_the_platforms_reserved_paths_are_not_ours(self, client: TestClient) -> None:
        """Cloud Run answers these before the request reaches the container (motet#16)."""
        for reserved in PLATFORM_RESERVED_PATHS:
            assert client.get(reserved).status_code == 404

    def test_a_revision_that_is_not_a_commit_sha_is_refused(self) -> None:
        assert publishable_revision("16d2b85") == "16d2b85"
        assert publishable_revision("bootstrap") == "bootstrap"
        assert publishable_revision("europe-west1-docker.pkg.dev/p/r/i:tag") is None
        assert publishable_revision(None) is None


class TestTheInnerDoor:
    def test_a_configured_deployment_refuses_a_caller_without_the_bearer(self) -> None:
        app = create_app(settings(MOTET_ENRICH_SERVICE_TOKEN="s3cret"), FakeRunner())
        with TestClient(app) as client:
            assert client.post("/v1/enrich", json=request_body()).status_code == 401
            answer = client.post(
                "/v1/enrich", json=request_body(), headers={"Authorization": "Bearer s3cret"}
            )
            assert answer.status_code == 200
            assert answer.json()["status"] == "ok"

    def test_a_wrong_bearer_is_refused(self) -> None:
        app = create_app(settings(MOTET_ENRICH_SERVICE_TOKEN="s3cret"), FakeRunner())
        with TestClient(app) as client:
            answer = client.post(
                "/v1/enrich", json=request_body(), headers={"Authorization": "Bearer other"}
            )
            assert answer.status_code == 401

    def test_health_is_never_behind_the_door(self) -> None:
        """The platform's own startup probe carries no bearer."""
        app = create_app(settings(MOTET_ENRICH_SERVICE_TOKEN="s3cret"), FakeRunner())
        with TestClient(app) as client:
            assert client.get(HEALTH_PATH).status_code == 200


class TestEnrich:
    def test_it_answers_with_the_article_and_the_cookies(self, client: TestClient) -> None:
        body = client.post("/v1/enrich", json=request_body()).json()
        assert body["status"] == "ok"
        assert body["article_markdown"].startswith("# The Information: today")
        assert body["browser_state"]
        assert body["transcript"]

    def test_a_request_may_ask_for_less_than_the_deployment_allows(self) -> None:
        seen: list[RunCaps] = []

        class Recording:
            def run(self, request: EnrichRequest, caps: RunCaps) -> EnrichResult:
                seen.append(caps)
                return EnrichResult(status="blocked")

        app = create_app(settings(), Recording())
        with TestClient(app) as client:
            client.post(
                "/v1/enrich",
                json=request_body(
                    caps={"max_usd": 0.1, "max_tool_calls": 5, "timeout_seconds": 30}
                ),
            )
        assert seen[0].max_usd == 0.1
        assert seen[0].max_tool_calls == 5
        assert seen[0].timeout_seconds == 30

    def test_a_request_may_not_ask_for_more(self) -> None:
        """A worker rolled out ahead of this service must not widen its own bound."""
        seen: list[RunCaps] = []

        class Recording:
            def run(self, request: EnrichRequest, caps: RunCaps) -> EnrichResult:
                seen.append(caps)
                return EnrichResult(status="blocked")

        app = create_app(settings(MOTET_ENRICH_MAX_USD_PER_ITEM="0.25"), Recording())
        with TestClient(app) as client:
            client.post(
                "/v1/enrich",
                json=request_body(
                    caps={"max_usd": 99.0, "max_tool_calls": 9999, "timeout_seconds": 99999}
                ),
            )
        assert seen[0].max_usd == 0.25
        assert seen[0].max_tool_calls == 40
        assert seen[0].timeout_seconds == 600

    def test_a_run_that_went_badly_is_a_200_with_a_status(self) -> None:
        """Never a 5xx: the caller's answer is the same for every one of them, and a 5xx
        would put it on the worker's retry ladder to meet the same wall again."""
        app = create_app(settings(), FakeRunner(blocked_domains=frozenset({"example.com"})))
        with TestClient(app) as client:
            answer = client.post("/v1/enrich", json=request_body())
        assert answer.status_code == 200
        assert answer.json()["status"] == "blocked"

    def test_a_request_with_no_candidate_url_is_refused(self, client: TestClient) -> None:
        """There is nothing to open, so this is a caller bug rather than a run that failed."""
        assert client.post("/v1/enrich", json=request_body(candidate_urls=[])).status_code == 422


class TestToolchainResolution:
    def test_a_complete_toolchain_reads_as_ready(self, tmp_path: Any) -> None:
        (tmp_path / "node_modules" / ".bin").mkdir(parents=True)
        for name in ("pi-mcp-adapter", "playwright-stealth-mcp-server"):
            (tmp_path / "node_modules" / name).mkdir()
        pi = tmp_path / "node_modules" / ".bin" / "pi"
        pi.write_text("#!/bin/sh\n")
        pi.chmod(0o755)
        (tmp_path / "harness").mkdir()
        (tmp_path / "harness" / "browser-mcp.mjs").write_text("// harness\n")
        browsers = tmp_path / "browsers"
        (browsers / "chromium-1234").mkdir(parents=True)

        resolved = load_settings(
            {
                "MOTET_INFERENCE_MODE": "real",
                "OPENROUTER_API_KEY": "sk-test",
                "MOTET_ENRICH_TOOLCHAIN_DIR": str(tmp_path),
                "PLAYWRIGHT_BROWSERS_PATH": str(browsers),
            }
        ).toolchain
        assert resolved.ready, resolved.detail

    def test_a_missing_piece_is_named_and_nothing_else_is(self, tmp_path: Any) -> None:
        """The *name*, never the path: this route is unauthenticated and public."""
        detail = load_settings(
            {"MOTET_INFERENCE_MODE": "fake", "MOTET_ENRICH_TOOLCHAIN_DIR": str(tmp_path)}
        ).toolchain.detail
        assert detail is not None
        assert "pi-mcp-adapter" in detail
        assert str(tmp_path) not in detail


class TestDormancy:
    def test_fake_mode_is_never_dormant(self) -> None:
        assert settings().dormant_reason is None

    def test_real_mode_without_a_vendor_key_says_which_one(self) -> None:
        reason = load_settings({"MOTET_INFERENCE_MODE": "real"}).dormant_reason
        assert reason == "OPENROUTER_API_KEY is unset"
