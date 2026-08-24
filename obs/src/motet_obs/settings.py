"""What the environment says about telemetry — read here and nowhere else.

Split from :mod:`motet_obs.runtime` so that reading the wiring costs nothing: this module
imports no SDK, so a health route can ask it on every request, and a test can ask it
without an exporter existing. :mod:`motet_obs.runtime` is the half that acts on it.

Endpoint values and tokens live in the private infrastructure repo. This module only ever
reads variable *names*.
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass, field

#: The standard OTel SDK variables the deploy sets. Named here rather than typed as
#: literals at each use, because a typo in one of these is invisible — it reads as "the
#: operator did not configure telemetry" rather than as a bug.
OTLP_ENDPOINT_ENV = "OTEL_EXPORTER_OTLP_ENDPOINT"
OTLP_HEADERS_ENV = "OTEL_EXPORTER_OTLP_HEADERS"
OTLP_PROTOCOL_ENV = "OTEL_EXPORTER_OTLP_PROTOCOL"
SERVICE_NAME_ENV = "OTEL_SERVICE_NAME"
RESOURCE_ATTRIBUTES_ENV = "OTEL_RESOURCE_ATTRIBUTES"
ERROR_DSN_ENV = "SENTRY_DSN_BACKEND"

#: The SDK's own kill switch, and the reason there is no `MOTET_OBS_ENABLED`. Telemetry
#: already defaults to off by having no endpoint; this exists for the other case — an
#: environment that *is* wired but where somebody wants a process to stay quiet.
SDK_DISABLED_ENV = "OTEL_SDK_DISABLED"

#: The raw obs ingest bearer, injected from Secret Manager under this name. Composed into
#: :data:`OTLP_HEADERS_ENV` by :func:`resolve_otlp_headers` when that is not already set.
OTLP_TOKEN_ENV = "OTEL_INGEST_TOKEN"

#: What the GlitchTip DSN secret is actually called in Secret Manager.
GLITCHTIP_DSN_ENV = "GLITCHTIP_DSN"

#: The only OTLP wire protocol these images can speak — see the exporter choice in
#: `obs/pyproject.toml`. `http/json` is deliberately not accepted: the HTTP exporter in
#: `opentelemetry-exporter-otlp-proto-http` does not implement it.
SUPPORTED_PROTOCOL = "http/protobuf"


@dataclass(frozen=True)
class ObsStatus:
    """Which exporters are wired — and, once :func:`motet_obs.configure` has run, which
    are actually *installed*.

    The distinction is the whole point. ``otlp_configured`` is a statement about the
    environment: somebody set an endpoint and a credential. ``exporters`` is a statement
    about this process: a provider exists and something is batching spans out of it. The
    first was true for months while the second was false, which is how a service can look
    monitored and emit nothing.
    """

    service_name: str
    otlp_configured: bool
    errors_configured: bool
    #: Names of the signals actually being exported: any of ``traces``, ``metrics``,
    #: ``logs``, ``errors``. Empty until :func:`motet_obs.configure` installs them.
    exporters: tuple[str, ...] = field(default=())

    @property
    def fully_configured(self) -> bool:
        return self.otlp_configured and self.errors_configured

    @property
    def exporting(self) -> bool:
        """Whether this process has actually installed an exporter for anything."""
        return bool(self.exporters)


def _environ(env: Mapping[str, str] | None) -> Mapping[str, str]:
    return os.environ if env is None else env


def _get(env: Mapping[str, str], name: str) -> str:
    """Read a variable, treating whitespace as unset.

    Unset and empty are the same thing in a Cloud Run service definition — an unset
    Terraform variable interpolates to `""` rather than dropping the entry.
    """
    return env.get(name, "").strip()


def resolve_otlp_headers(env: Mapping[str, str] | None = None) -> str | None:
    """The OTLP header string, composed from the raw ingest token when necessary.

    An explicit ``OTEL_EXPORTER_OTLP_HEADERS`` always wins — it is the standard variable
    and somebody who set it meant it. Falling back to ``OTEL_INGEST_TOKEN`` is what lets
    a Cloud Run service inject the secret under the only name it has and still export:
    Secret Manager holds one value per secret, and the CI identity that applies the
    infrastructure cannot read a secret back, so it cannot compose
    ``Authorization=Bearer <token>`` itself. Composing it is this process's job.
    """
    environ = _environ(env)
    explicit = _get(environ, OTLP_HEADERS_ENV)
    if explicit:
        return explicit
    token = _get(environ, OTLP_TOKEN_ENV)
    if token:
        return f"Authorization=Bearer {token}"
    return None


def parse_headers(raw: str) -> dict[str, str]:
    """Split an OTLP header string into the mapping an exporter takes.

    The format is the W3C-baggage-shaped ``k1=v1,k2=v2`` the OTel spec defines. Splitting
    on the *first* ``=`` is load-bearing rather than lazy: a bearer token is frequently
    base64 and ends in ``=`` padding, and splitting on every one would truncate it into a
    credential that is silently wrong rather than visibly absent.
    """
    headers: dict[str, str] = {}
    for pair in raw.split(","):
        name, separator, value = pair.partition("=")
        if not separator:
            continue
        key = name.strip()
        if key:
            headers[key] = value.strip()
    return headers


def resolve_error_dsn(env: Mapping[str, str] | None = None) -> str | None:
    """The GlitchTip DSN, under either of the two names it travels as."""
    environ = _environ(env)
    for name in (ERROR_DSN_ENV, GLITCHTIP_DSN_ENV):
        value = _get(environ, name)
        if value:
            return value
    return None


def resource_attributes(env: Mapping[str, str] | None = None) -> dict[str, str]:
    """``OTEL_RESOURCE_ATTRIBUTES``, parsed.

    The OTel SDK reads this variable itself when it builds a resource, so parsing it here
    is not about the resource — it is about the *other* consumer. Error reports want the
    same environment and version labels the metrics carry, and inventing a second pair of
    variables for facts the deploy already sets is how two labels for one thing start
    disagreeing.
    """
    raw = _get(_environ(env), RESOURCE_ATTRIBUTES_ENV)
    return parse_headers(raw) if raw else {}


def resolve_deployment_environment(env: Mapping[str, str] | None = None) -> str | None:
    """Which environment this is, out of the resource attributes.

    Both the current (``deployment.environment.name``) and the older
    (``deployment.environment``) spellings are read, because which one a given collector
    config uses is not this repo's business.
    """
    attributes = resource_attributes(env)
    for key in ("deployment.environment.name", "deployment.environment"):
        value = attributes.get(key, "").strip()
        if value:
            return value
    return None


def resolve_service_version(env: Mapping[str, str] | None = None) -> str | None:
    """The build this process is, out of the resource attributes.

    Worth carrying into error reports specifically: "which revision started failing" is
    the first question asked of a stack trace, and the deploy is the only thing that knows
    the answer.
    """
    return resource_attributes(env).get("service.version", "").strip() or None


def sdk_disabled(env: Mapping[str, str] | None = None) -> bool:
    """Whether the standard OTel kill switch is on."""
    return _get(_environ(env), SDK_DISABLED_ENV).lower() == "true"


def status(
    env: Mapping[str, str] | None = None,
    *,
    default_service_name: str = "motet",
    exporters: tuple[str, ...] = (),
) -> ObsStatus:
    """Report the wiring, so 'no data' can be told apart from 'no errors'.

    Telemetry needs an endpoint *and* a credential: obs rejects an unauthenticated
    export, so an endpoint on its own buys a 401 per export rather than data — which
    reads as an obs fault and is the most expensive way to discover a missing token.
    ``otlp_configured`` therefore means both are present, not just the endpoint.
    """
    environ = _environ(env)
    endpoint = _get(environ, OTLP_ENDPOINT_ENV)
    return ObsStatus(
        service_name=_get(environ, SERVICE_NAME_ENV) or default_service_name,
        otlp_configured=bool(endpoint) and resolve_otlp_headers(environ) is not None,
        errors_configured=resolve_error_dsn(environ) is not None,
        exporters=exporters,
    )
