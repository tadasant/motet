"""Telemetry wiring — to the self-hosted obs stack, never GCP Cloud Logging.

**Invariant 11.** Metrics, logs, and errors go to the self-hosted Grafana /
VictoriaMetrics / VictoriaLogs / GlitchTip stack. That is load-bearing rather than a
preference: there is deliberately no GCP MCP server, so the obs stack is the only place an
agent can see how production is behaving. Telemetry in Cloud Logging is telemetry nobody
can debug from.

**This is a workspace package rather than a module inside `api/` because the worker is
the process that matters most and could not reach it there.** ``motet-api`` depends on
``motet-workers``, so a worker importing ``motet_api.obs`` would be a cycle — and the
worker is the half that makes every vendor call, has no health route to ask, and until now
produced no signal of any kind. Nothing here may ever depend on a ``motet-*`` package;
that is what keeps every deployable able to import it.

Every exporter **no-ops when its variable is unset**, so tests and laptops need no obs
stack. That creates the trap :func:`status` exists to close: a silent no-op looks exactly
like a healthy, quiet service. Never infer "no errors" from "no data" — ask :func:`status`
instead, which the API exposes on its health route.

Two questions, and they are different ones:

``otlp_configured``
    Somebody set an endpoint and a credential. A statement about the *environment*.
``exporting`` / ``exporters``
    This process built a provider and is batching data out of it. A statement about the
    *process*. It was false for months while the first was true, which is exactly how a
    service can look monitored and emit nothing.

Endpoint values and tokens live in the private infrastructure repo. This package only ever
reads variable *names*.

**Two of those names have a second spelling, and it is not cosmetic.** Secret Manager
holds one value per secret, and the CI identity that applies the infrastructure cannot
read a secret back. So a service definition can inject a secret under its own name and
nothing more: it cannot read ``OTEL_INGEST_TOKEN`` in order to compose the
``Authorization=Bearer <token>`` string that ``OTEL_EXPORTER_OTLP_HEADERS`` wants.
Composing it is therefore *this* process's job. ``GLITCHTIP_DSN`` is the same story
without the formatting: it is the name the secret was placed under.
"""

from .runtime import configure, instrument_fastapi, logger, shutdown, status
from .settings import (
    ERROR_DSN_ENV,
    GLITCHTIP_DSN_ENV,
    OTLP_ENDPOINT_ENV,
    OTLP_HEADERS_ENV,
    OTLP_PROTOCOL_ENV,
    OTLP_TOKEN_ENV,
    RESOURCE_ATTRIBUTES_ENV,
    SDK_DISABLED_ENV,
    SERVICE_NAME_ENV,
    SUPPORTED_PROTOCOL,
    ObsStatus,
    parse_headers,
    resolve_deployment_environment,
    resolve_error_dsn,
    resolve_otlp_headers,
    resolve_service_version,
    sdk_disabled,
)

__all__ = [
    "ERROR_DSN_ENV",
    "GLITCHTIP_DSN_ENV",
    "OTLP_ENDPOINT_ENV",
    "OTLP_HEADERS_ENV",
    "OTLP_PROTOCOL_ENV",
    "OTLP_TOKEN_ENV",
    "RESOURCE_ATTRIBUTES_ENV",
    "SDK_DISABLED_ENV",
    "SERVICE_NAME_ENV",
    "SUPPORTED_PROTOCOL",
    "ObsStatus",
    "configure",
    "instrument_fastapi",
    "logger",
    "parse_headers",
    "resolve_deployment_environment",
    "resolve_error_dsn",
    "resolve_otlp_headers",
    "resolve_service_version",
    "sdk_disabled",
    "shutdown",
    "status",
]
