"""The wiring that only ever fails once the thing is actually deployed.

Every case here passed on a laptop and would have broken in Cloud Run, because a laptop
serves the SPA and the API from one origin and a deployment serves them from two, and
because a laptop needs no telemetry credential and a deployment does.

None of it needs a database.
"""

from __future__ import annotations

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from motet_api.config import APP_BASE_URL_ENV, ConfigError, Settings
from motet_api.main import configure_cors
from motet_api.obs import (
    ERROR_DSN_ENV,
    GLITCHTIP_DSN_ENV,
    OTLP_ENDPOINT_ENV,
    OTLP_HEADERS_ENV,
    OTLP_TOKEN_ENV,
    resolve_error_dsn,
    resolve_otlp_headers,
    status,
)

APP_ORIGIN = "https://app.example.invalid"


def _settings(app_base_url: str | None) -> Settings:
    """Settings carrying just the one field the CORS policy is built from."""
    return Settings(
        database_url=None,
        inference_mode="fake",
        api_token=None,
        public_base_url=None,
        app_base_url=app_base_url,
        feed_title="Motet",
        feed_description="",
        feed_author="Motet",
    )


class TestCorsOrigins:
    """`Settings.cors_origins` is what the browser policy is built from."""

    def test_unset_grants_no_origin(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Same-origin dev is the only case this is right for, and it is the default."""
        monkeypatch.delenv(APP_BASE_URL_ENV, raising=False)
        assert Settings.from_env().cors_origins == []

    def test_the_app_base_url_becomes_the_allowed_origin(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv(APP_BASE_URL_ENV, APP_ORIGIN)
        assert Settings.from_env().cors_origins == [APP_ORIGIN]

    @pytest.mark.parametrize(
        "configured",
        [
            f"{APP_ORIGIN}/",
            f"{APP_ORIGIN}/backlog",
            f"{APP_ORIGIN}/?x=1",
        ],
    )
    def test_a_path_or_query_is_reduced_to_the_origin(
        self, monkeypatch: pytest.MonkeyPatch, configured: str
    ) -> None:
        """A browser sends `scheme://host[:port]` and nothing else.

        A configured value carrying a trailing slash or a path would never match the
        `Origin` header, so every request would be refused for a reason that is invisible
        in both the request and the configuration.
        """
        monkeypatch.setenv(APP_BASE_URL_ENV, configured)
        assert Settings.from_env().cors_origins == [APP_ORIGIN]

    def test_a_port_is_part_of_the_origin(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv(APP_BASE_URL_ENV, "http://localhost:5173/")
        assert Settings.from_env().cors_origins == ["http://localhost:5173"]

    def test_it_is_never_a_wildcard(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """One named origin, so a stranger's page cannot drive this API."""
        monkeypatch.setenv(APP_BASE_URL_ENV, APP_ORIGIN)
        assert "*" not in Settings.from_env().cors_origins

    def test_the_host_is_lowercased(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """A browser always sends a lowercase host; the comparison is exact.

        `urlsplit` lowercases the scheme but not the netloc, so building from `netloc`
        would yield an origin no `Origin` header could ever equal.
        """
        monkeypatch.setenv(APP_BASE_URL_ENV, "HTTPS://App.Example.INVALID")
        assert Settings.from_env().cors_origins == ["https://app.example.invalid"]

    def test_userinfo_is_dropped(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """It never appears in an `Origin` header, and it would reach the startup log."""
        monkeypatch.setenv(APP_BASE_URL_ENV, "https://user:pw@app.example.invalid")
        assert Settings.from_env().cors_origins == ["https://app.example.invalid"]

    def test_an_ipv6_literal_keeps_its_brackets(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv(APP_BASE_URL_ENV, "http://[::1]:8080")
        assert Settings.from_env().cors_origins == ["http://[::1]:8080"]

    def test_a_bare_hostname_is_assumed_https(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv(APP_BASE_URL_ENV, "app.example.invalid")
        assert Settings.from_env().cors_origins == ["https://app.example.invalid"]

    @pytest.mark.parametrize(
        "configured",
        [
            # The motivating case: one missing slash. The lenient reading of this is the
            # netloc `https:`, giving `https://https:` — a policy that installs cleanly
            # and refuses every request, while the "no origin configured" warning stays
            # silent because an origin *was* configured.
            "https:/app.example.invalid",
            "ftp://app.example.invalid",
            "https://",
            "https://app.example.invalid:notaport",
        ],
    )
    def test_an_unusable_value_raises_rather_than_never_matching(
        self, monkeypatch: pytest.MonkeyPatch, configured: str
    ) -> None:
        """Fail at startup, like an unknown model slug — not invisibly at request time."""
        monkeypatch.setenv(APP_BASE_URL_ENV, configured)
        with pytest.raises(ConfigError):
            _ = Settings.from_env().cors_origins


class TestCorsMiddleware:
    """The policy as a browser meets it: a preflight and an actual request.

    Applied to a throwaway app rather than the real one, because the real app configures
    itself at import and a test needing a different origin would have to reimport the
    module. What matters is that it goes through **``main.configure_cors``** — the same
    function ``main`` calls. Retyping the middleware arguments here would leave these
    tests passing after somebody changed the real policy, which is the one failure a CORS
    test exists to catch.
    """

    @staticmethod
    def _client(app_base_url: str | None) -> TestClient:
        app = FastAPI()
        configure_cors(app, _settings(app_base_url))

        @app.get("/v1/news-items")
        def _news_items() -> list[str]:
            return []

        return TestClient(app)

    def test_a_preflight_from_the_app_origin_is_allowed(self) -> None:
        """Without this the SPA fails every call before it is ever sent."""
        response = self._client(APP_ORIGIN).options(
            "/v1/news-items",
            headers={
                "Origin": APP_ORIGIN,
                "Access-Control-Request-Method": "GET",
                "Access-Control-Request-Headers": "authorization",
            },
        )
        assert response.status_code == 200
        assert response.headers["access-control-allow-origin"] == APP_ORIGIN
        assert "authorization" in response.headers["access-control-allow-headers"].lower()

    def test_the_actual_request_carries_the_allow_origin_header(self) -> None:
        response = self._client(APP_ORIGIN).get("/v1/news-items", headers={"Origin": APP_ORIGIN})
        assert response.status_code == 200
        assert response.headers["access-control-allow-origin"] == APP_ORIGIN

    def test_another_origin_is_not_granted_access(self) -> None:
        """The point of naming one origin rather than allowing every one."""
        response = self._client(APP_ORIGIN).get(
            "/v1/news-items", headers={"Origin": "https://evil.example.invalid"}
        )
        assert "access-control-allow-origin" not in response.headers

    def test_no_configured_origin_means_no_cors_headers(self) -> None:
        response = self._client(None).get("/v1/news-items", headers={"Origin": APP_ORIGIN})
        assert "access-control-allow-origin" not in response.headers


class TestOtlpHeaderResolution:
    """Secret Manager injects a secret under its own name and cannot compose a string."""

    def test_nothing_set_resolves_to_nothing(self) -> None:
        assert resolve_otlp_headers({}) is None

    def test_the_raw_token_becomes_a_bearer_header(self) -> None:
        assert resolve_otlp_headers({OTLP_TOKEN_ENV: "tok"}) == "Authorization=Bearer tok"

    def test_an_explicit_header_string_wins(self) -> None:
        """Somebody who set the standard variable meant it."""
        resolved = resolve_otlp_headers(
            {OTLP_HEADERS_ENV: "Authorization=Bearer explicit", OTLP_TOKEN_ENV: "tok"}
        )
        assert resolved == "Authorization=Bearer explicit"

    def test_whitespace_only_counts_as_unset(self) -> None:
        """Unset and empty are the same thing in a Cloud Run service definition."""
        assert resolve_otlp_headers({OTLP_HEADERS_ENV: "  ", OTLP_TOKEN_ENV: "  "}) is None


class TestErrorDsnResolution:
    def test_either_name_resolves(self) -> None:
        dsn = "https://public@glitchtip.example.invalid/1"
        assert resolve_error_dsn({ERROR_DSN_ENV: dsn}) == dsn
        assert resolve_error_dsn({GLITCHTIP_DSN_ENV: dsn}) == dsn

    def test_the_documented_name_wins_when_both_are_set(self) -> None:
        assert resolve_error_dsn({ERROR_DSN_ENV: "a", GLITCHTIP_DSN_ENV: "b"}) == "a"

    def test_nothing_set_resolves_to_nothing(self) -> None:
        assert resolve_error_dsn({}) is None


class TestObsStatus:
    """`status()` answers "is telemetry actually on", not "did somebody set a variable"."""

    def test_an_endpoint_alone_is_not_configured(self) -> None:
        assert status({OTLP_ENDPOINT_ENV: "https://obs.example.invalid"}).otlp_configured is False

    def test_a_credential_alone_is_not_configured(self) -> None:
        assert status({OTLP_TOKEN_ENV: "tok"}).otlp_configured is False

    def test_both_together_are_configured(self) -> None:
        current = status({OTLP_ENDPOINT_ENV: "https://obs.example.invalid", OTLP_TOKEN_ENV: "tok"})
        assert current.otlp_configured is True

    def test_fully_configured_needs_errors_too(self) -> None:
        env = {OTLP_ENDPOINT_ENV: "https://obs.example.invalid", OTLP_TOKEN_ENV: "tok"}
        assert status(env).fully_configured is False
        assert status({**env, GLITCHTIP_DSN_ENV: "dsn"}).fully_configured is True
