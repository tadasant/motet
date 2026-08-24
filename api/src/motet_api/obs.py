"""Telemetry wiring — to the self-hosted obs stack, never GCP Cloud Logging.

**Invariant 11.** Metrics, logs, and errors go to the self-hosted Grafana /
VictoriaMetrics / VictoriaLogs / GlitchTip stack. That is load-bearing rather than a
preference: there is deliberately no GCP MCP server, so the obs stack is the only place an
agent can see how production is behaving. Telemetry in Cloud Logging is telemetry nobody
can debug from.

Every exporter here **no-ops when its variable is unset**, so tests and laptops need no obs
stack. That creates the trap :func:`status` exists to close: a silent no-op looks exactly
like a healthy, quiet service. Never infer "no errors" from "no data" — ask
:func:`status` instead, which the API exposes at ``/internal/health``.

Endpoint values and tokens live in the private infrastructure repo. This module only ever
reads variable *names*.

**Two of those names have a second spelling, and it is not cosmetic.** Secret Manager
holds one value per secret, and the CI identity that applies the infrastructure cannot
read a secret back — it holds Secret Manager admin *minus* ``versions.access``. So a
service definition can inject a secret under its own name and nothing more: it cannot
read ``OTEL_INGEST_TOKEN`` in order to compose the ``Authorization=Bearer <token>`` string
that ``OTEL_EXPORTER_OTLP_HEADERS`` wants. Composing it is therefore *this* process's job,
from the raw token it is handed. ``GLITCHTIP_DSN`` is the same story without the
formatting: it is the name the secret was placed under. Accepting both spellings here is
what keeps telemetry from being wired up perfectly and still exporting nothing.
"""

from __future__ import annotations

import logging
import os
from collections.abc import Mapping
from dataclasses import dataclass

logger = logging.getLogger("motet")

OTLP_ENDPOINT_ENV = "OTEL_EXPORTER_OTLP_ENDPOINT"
OTLP_HEADERS_ENV = "OTEL_EXPORTER_OTLP_HEADERS"
SERVICE_NAME_ENV = "OTEL_SERVICE_NAME"
ERROR_DSN_ENV = "SENTRY_DSN_BACKEND"

#: The raw obs ingest bearer, injected from Secret Manager under this name. Composed into
#: :data:`OTLP_HEADERS_ENV` by :func:`resolve_otlp_headers` when that is not already set.
OTLP_TOKEN_ENV = "OTEL_INGEST_TOKEN"

#: What the GlitchTip DSN secret is actually called in Secret Manager.
GLITCHTIP_DSN_ENV = "GLITCHTIP_DSN"

DEFAULT_SERVICE_NAME = "motet-api"


@dataclass(frozen=True)
class ObsStatus:
    """Which exporters are actually configured in this process."""

    service_name: str
    otlp_configured: bool
    errors_configured: bool

    @property
    def fully_configured(self) -> bool:
        return self.otlp_configured and self.errors_configured


def resolve_otlp_headers(env: Mapping[str, str] | None = None) -> str | None:
    """The OTLP header string, composed from the raw ingest token when necessary.

    An explicit ``OTEL_EXPORTER_OTLP_HEADERS`` always wins — it is the standard variable
    and somebody who set it meant it. Falling back to ``OTEL_INGEST_TOKEN`` is what lets
    a Cloud Run service inject the secret under the only name it has and still export.
    """
    environ = os.environ if env is None else env
    explicit = environ.get(OTLP_HEADERS_ENV, "").strip()
    if explicit:
        return explicit
    token = environ.get(OTLP_TOKEN_ENV, "").strip()
    if token:
        return f"Authorization=Bearer {token}"
    return None


def resolve_error_dsn(env: Mapping[str, str] | None = None) -> str | None:
    """The GlitchTip DSN, under either of the two names it travels as."""
    environ = os.environ if env is None else env
    for name in (ERROR_DSN_ENV, GLITCHTIP_DSN_ENV):
        value = environ.get(name, "").strip()
        if value:
            return value
    return None


def status(env: Mapping[str, str] | None = None) -> ObsStatus:
    """Report the wiring, so 'no data' can be told apart from 'no errors'.

    Telemetry needs an endpoint *and* a credential: obs rejects an unauthenticated
    export, so an endpoint on its own buys a 401 per export rather than data — which
    reads as an obs fault and is the most expensive way to discover a missing token.
    ``otlp_configured`` therefore means both are present, not just the endpoint.
    """
    environ = os.environ if env is None else env
    endpoint = environ.get(OTLP_ENDPOINT_ENV, "").strip()
    return ObsStatus(
        service_name=environ.get(SERVICE_NAME_ENV, DEFAULT_SERVICE_NAME),
        otlp_configured=bool(endpoint) and resolve_otlp_headers(environ) is not None,
        errors_configured=resolve_error_dsn(environ) is not None,
    )


def configure() -> ObsStatus:
    """Set up logging and report what telemetry is wired.

    Called from the API's lifespan — a ``configure()`` nobody calls is exactly the silent
    no-op this module warns about, and until this was wired it was one.

    **The worker does not call this, and that is a known gap rather than a decision.**
    ``motet_api`` depends on ``motet_workers``, so the worker cannot import back without
    a cycle; giving it telemetry means moving this module somewhere both can reach. The
    gap matters because the worker is the process that makes every vendor call and has no
    ``/internal/health`` to ask, so "ask the app instead" currently has no answer for it. Tracked
    rather than half-fixed here.

    Scaffold: the OTLP and GlitchTip exporters themselves are not installed yet, so this
    configures stdlib logging and records the intent. Wiring the exporters means filling
    this function in — not calling a vendor SDK from somewhere else.
    """
    current = status()
    # `.upper()` because `logging.basicConfig(level="debug")` raises `ValueError: Unknown
    # level`. This is the first statement in the API's lifespan, so a lowercase LOG_LEVEL
    # would be a failed revision whose traceback never mentions LOG_LEVEL.
    logging.basicConfig(level=os.environ.get("LOG_LEVEL", "INFO").strip().upper() or "INFO")
    logger.info(
        "obs: service=%s otlp=%s errors=%s",
        current.service_name,
        "configured" if current.otlp_configured else "unset (no-op)",
        "configured" if current.errors_configured else "unset (no-op)",
    )
    # Louder than the line above, because this is the shape the trap actually takes: an
    # endpoint is set, so somebody believes telemetry is on, and every export 401s.
    if os.environ.get(OTLP_ENDPOINT_ENV, "").strip() and not current.otlp_configured:
        logger.warning(
            "obs: %s is set but no ingest credential is: set %s (or %s). Exports would "
            "be rejected, and rejected exports look exactly like a quiet service.",
            OTLP_ENDPOINT_ENV,
            OTLP_TOKEN_ENV,
            OTLP_HEADERS_ENV,
        )
    return current
