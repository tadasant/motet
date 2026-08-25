"""The wiring that only ever fails once the thing is actually deployed.

Every case here passed on a laptop and would have broken in Cloud Run, because a laptop
serves the SPA and the API from one origin and a deployment serves them from two, and
because a laptop needs no telemetry credential and a deployment does.

None of it needs a database.
"""

from __future__ import annotations

import logging
import tomllib
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from motet_api.config import APP_BASE_URL_ENV, ConfigError, Settings
from motet_api.main import UnhandledErrorMiddleware, configure_cors
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
from motet_vault import kms_sdk_installed

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


class TestDockerfileCoversTheWorkspace:
    """The Dockerfile's COPY list must match `[tool.uv.workspace] members`.

    `uv sync --frozen` plans the whole workspace: `uv.lock` records each member as a path
    source, so a member missing from the build context fails the sync outright rather than
    being quietly skipped. The image does not have to *import* a member for this to matter
    — `voice` is a separate deployable that neither image imports, and omitting it still
    broke the build.

    This is a check rather than the comment that was there before, because a comment
    saying "keep this list in step" is exactly what was in place when the list went out of
    step. It went out of step in the way CI could not see: the Dockerfile was written on a
    branch whose workspace had five members while a sixth was added on main in parallel,
    so both branches were green and only their merge was broken. A test that reads both
    lists out of the two files catches that on the merge commit, where the mismatch first
    exists — and it needs no Docker daemon, so it belongs in `bin/ci` rather than in the
    images job that cannot run on a laptop.
    """

    @staticmethod
    def _repo_root() -> Path:
        # api/tests/test_deploy_wiring.py -> api/tests -> api -> repo root
        return Path(__file__).resolve().parents[2]

    def test_every_workspace_member_is_copied(self) -> None:
        root = self._repo_root()
        members = set(
            tomllib.loads((root / "pyproject.toml").read_text())["tool"]["uv"]["workspace"][
                "members"
            ]
        )
        dockerfile = (root / "Dockerfile").read_text()

        # Two lists, and both have to be complete: the metadata copy feeds the cached
        # dependency layer and the source copy feeds the install. Missing from either one
        # fails the build, at a different line.
        metadata = {
            member
            for member in members
            if f"COPY {member}/pyproject.toml {member}/pyproject.toml" in dockerfile
        }
        sources = {member for member in members if f"COPY {member} {member}\n" in dockerfile}

        assert members - metadata == set(), (
            "Dockerfile is missing `COPY <member>/pyproject.toml` for these workspace "
            "members, so `uv sync --frozen` cannot plan: "
            f"{sorted(members - metadata)}"
        )
        assert members - sources == set(), (
            "Dockerfile is missing `COPY <member> <member>` for these workspace members, "
            f"so `uv sync --frozen` cannot install them: {sorted(members - sources)}"
        )


class TestUnhandledErrorsReachTheBrowser:
    """A 500 the SPA can read, instead of `TypeError: Failed to fetch`.

    This is the class that would have made the Gmail-connect bug a five-minute diagnosis.
    Starlette answers an escaped exception from `ServerErrorMiddleware`, which sits
    *outside* everything `add_middleware` installs — so the 500 never passes the CORS
    layer, carries no `Access-Control-Allow-Origin`, and a browser refuses to hand it to
    the caller at all. `fetch` rejects with a `TypeError` naming no status and no body,
    which is exactly what the user saw and reported.

    Both cases below are run through the real `configure_cors` and the real
    `UnhandledErrorMiddleware`, in the real order, for the same reason the class above
    does: a test that rebuilt the stack by hand would stay green after somebody changed
    it.
    """

    @staticmethod
    def _client(*, guarded: bool) -> TestClient:
        app = FastAPI()

        @app.get("/v1/boom")
        def _boom() -> None:
            raise RuntimeError("a vendor exception nobody caught")

        # Same order as `main`: the guard is added first and CORS second, so
        # `add_middleware`'s prepending leaves CORS on the outside.
        if guarded:
            app.add_middleware(UnhandledErrorMiddleware)
        configure_cors(app, _settings(APP_ORIGIN))
        return TestClient(app, raise_server_exceptions=False)

    def test_the_five_hundred_carries_the_allow_origin_header(self) -> None:
        response = self._client(guarded=True).get("/v1/boom", headers={"Origin": APP_ORIGIN})
        assert response.status_code == 500
        assert response.headers["access-control-allow-origin"] == APP_ORIGIN
        # Readable by the SPA, and saying nothing about the exception: this response
        # crosses an origin, and a vendor message can quote a KMS key path.
        assert "detail" in response.json()
        assert "vendor exception" not in response.text

    def test_the_exception_is_logged_with_its_traceback(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        """The log line is what keeps GlitchTip getting the error, not a nicety.

        Converting the exception into a response stops it reaching the outermost ASGI
        layer, which is where the Sentry SDK otherwise captures it. The SDK's logging
        integration turns this `logger.exception` into the same event, carrying the same
        exception — so deleting the call would silently stop errors arriving.
        """
        with caplog.at_level(logging.ERROR, logger="motet.api"):
            self._client(guarded=True).get("/v1/boom", headers={"Origin": APP_ORIGIN})
        recorded = [r for r in caplog.records if r.levelno >= logging.ERROR]
        assert recorded, "an unhandled error must be logged"
        assert recorded[-1].exc_info is not None, "without exc_info there is no traceback"
        assert "/v1/boom" in recorded[-1].getMessage()

    def test_without_the_guard_the_browser_would_see_nothing(self) -> None:
        """The bug, pinned. Delete the middleware and this is what comes back."""
        response = self._client(guarded=False).get("/v1/boom", headers={"Origin": APP_ORIGIN})
        assert response.status_code == 500
        assert "access-control-allow-origin" not in response.headers


class TestTheKmsSdkShipsInTheImage:
    """`motet-vault[kms]`, not `motet-vault` — the whole of the Gmail-connect fix.

    The SDK is imported lazily inside `CloudKmsKeyManager` so that a laptop and CI never
    pull in a cloud dependency they cannot use. That is right, and it is *not* a reason
    for the extra to be absent from the images: `uv sync --no-dev` installs default
    extras only, so nothing asked for `[kms]` and nothing had it. The first line of code
    to notice was the import, inside a request, after Google had already issued a refresh
    token — an unhandled 500 with no CORS headers on it.

    Two claims, and the second is the one that catches a regression in the lock file
    rather than in the manifests.
    """

    @staticmethod
    def _requires_kms(member: str) -> bool:
        root = TestDockerfileCoversTheWorkspace._repo_root()
        manifest = tomllib.loads((root / member / "pyproject.toml").read_text())
        return "motet-vault[kms]" in manifest["project"]["dependencies"]

    @pytest.mark.parametrize("member", ["api", "workers"])
    def test_both_deployables_ask_for_the_extra(self, member: str) -> None:
        assert self._requires_kms(member), (
            f"{member}/pyproject.toml depends on bare `motet-vault`, so the image it "
            "builds has no google-cloud-kms and sealing a credential fails at the last "
            "step of the consent flow."
        )

    def test_the_sdk_is_importable_here(self) -> None:
        """Resolved, not just declared — and no call is made, so CI stays offline."""
        assert kms_sdk_installed()
