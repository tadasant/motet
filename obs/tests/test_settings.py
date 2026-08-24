"""What the environment says, read without an SDK anywhere near it.

These are the cases that only ever fail once the thing is deployed: a secret that could
only be injected under its own name, a variable that interpolated to the empty string, a
bearer token whose base64 padding got eaten by a careless split.
"""

from __future__ import annotations

from motet_obs import (
    ERROR_DSN_ENV,
    GLITCHTIP_DSN_ENV,
    OTLP_ENDPOINT_ENV,
    OTLP_HEADERS_ENV,
    OTLP_TOKEN_ENV,
    RESOURCE_ATTRIBUTES_ENV,
    SDK_DISABLED_ENV,
    SERVICE_NAME_ENV,
    parse_headers,
    resolve_deployment_environment,
    resolve_error_dsn,
    resolve_otlp_headers,
    resolve_service_version,
    sdk_disabled,
    status,
)

ENDPOINT = "https://obs.example.invalid/otel"


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


class TestHeaderParsing:
    def test_a_single_pair(self) -> None:
        assert parse_headers("Authorization=Bearer tok") == {"Authorization": "Bearer tok"}

    def test_several_pairs(self) -> None:
        assert parse_headers("a=1,b=2") == {"a": "1", "b": "2"}

    def test_base64_padding_survives(self) -> None:
        """The motivating case: splitting on every `=` truncates the credential.

        A token that is silently wrong is far worse than one that is visibly absent — the
        export 401s, which reads as an obs fault rather than as a parsing bug here.
        """
        assert parse_headers("Authorization=Bearer YWJjZA==") == {
            "Authorization": "Bearer YWJjZA=="
        }

    def test_a_pair_with_no_separator_is_skipped(self) -> None:
        assert parse_headers("nonsense,a=1") == {"a": "1"}


class TestErrorDsnResolution:
    def test_either_name_resolves(self) -> None:
        dsn = "https://public@glitchtip.example.invalid/1"
        assert resolve_error_dsn({ERROR_DSN_ENV: dsn}) == dsn
        assert resolve_error_dsn({GLITCHTIP_DSN_ENV: dsn}) == dsn

    def test_the_documented_name_wins_when_both_are_set(self) -> None:
        assert resolve_error_dsn({ERROR_DSN_ENV: "a", GLITCHTIP_DSN_ENV: "b"}) == "a"

    def test_nothing_set_resolves_to_nothing(self) -> None:
        assert resolve_error_dsn({}) is None


class TestResourceAttributes:
    """Error reports carry the same environment and version label the metrics do."""

    def test_the_current_spelling(self) -> None:
        env = {RESOURCE_ATTRIBUTES_ENV: "deployment.environment.name=staging,service.version=abc"}
        assert resolve_deployment_environment(env) == "staging"
        assert resolve_service_version(env) == "abc"

    def test_the_older_spelling(self) -> None:
        env = {RESOURCE_ATTRIBUTES_ENV: "deployment.environment=prod"}
        assert resolve_deployment_environment(env) == "prod"

    def test_absent_is_none_rather_than_empty(self) -> None:
        """`sentry_sdk.init(environment="")` would label every event with a blank."""
        assert resolve_deployment_environment({}) is None
        assert resolve_service_version({}) is None


class TestKillSwitch:
    def test_the_standard_variable_turns_it_off(self) -> None:
        assert sdk_disabled({SDK_DISABLED_ENV: "true"}) is True
        assert sdk_disabled({SDK_DISABLED_ENV: "TRUE"}) is True

    def test_anything_else_leaves_it_on(self) -> None:
        assert sdk_disabled({}) is False
        assert sdk_disabled({SDK_DISABLED_ENV: "false"}) is False
        assert sdk_disabled({SDK_DISABLED_ENV: "1"}) is False


class TestStatus:
    """`status()` answers "is telemetry actually on", not "did somebody set a variable"."""

    def test_an_endpoint_alone_is_not_configured(self) -> None:
        assert status({OTLP_ENDPOINT_ENV: ENDPOINT}).otlp_configured is False

    def test_a_credential_alone_is_not_configured(self) -> None:
        assert status({OTLP_TOKEN_ENV: "tok"}).otlp_configured is False

    def test_both_together_are_configured(self) -> None:
        assert status({OTLP_ENDPOINT_ENV: ENDPOINT, OTLP_TOKEN_ENV: "tok"}).otlp_configured is True

    def test_fully_configured_needs_errors_too(self) -> None:
        env = {OTLP_ENDPOINT_ENV: ENDPOINT, OTLP_TOKEN_ENV: "tok"}
        assert status(env).fully_configured is False
        assert status({**env, GLITCHTIP_DSN_ENV: "dsn"}).fully_configured is True

    def test_configured_is_not_exporting(self) -> None:
        """The distinction the whole package exists for.

        Every variable can be set and nothing be installed — which is precisely the state
        this repo shipped in for months, reporting `telemetry_configured: true` from a
        process that had no exporter compiled into it at all.
        """
        current = status({OTLP_ENDPOINT_ENV: ENDPOINT, OTLP_TOKEN_ENV: "tok"})
        assert current.otlp_configured is True
        assert current.exporting is False

    def test_the_service_name_falls_back_and_is_overridable(self) -> None:
        assert status({}, default_service_name="motet-worker").service_name == "motet-worker"
        assert (
            status(
                {SERVICE_NAME_ENV: "motet-api"}, default_service_name="motet-worker"
            ).service_name
            == "motet-api"
        )

    def test_an_empty_service_name_falls_back(self) -> None:
        """An unset Terraform variable interpolates to `""` rather than dropping the key."""
        assert status({SERVICE_NAME_ENV: " "}, default_service_name="motet-api").service_name == (
            "motet-api"
        )
