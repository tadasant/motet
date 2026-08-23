"""Telemetry wiring — to the self-hosted obs stack, never GCP Cloud Logging.

**Invariant 11.** Metrics, logs, and errors go to the self-hosted Grafana /
VictoriaMetrics / VictoriaLogs / GlitchTip stack. That is load-bearing rather than a
preference: there is deliberately no GCP MCP server, so the obs stack is the only place an
agent can see how production is behaving. Telemetry in Cloud Logging is telemetry nobody
can debug from.

Every exporter here **no-ops when its variable is unset**, so tests and laptops need no obs
stack. That creates the trap :func:`status` exists to close: a silent no-op looks exactly
like a healthy, quiet service. Never infer "no errors" from "no data" — ask
:func:`status` instead, which the API exposes at ``/healthz``.

Endpoint values and tokens live in the private infrastructure repo. This module only ever
reads variable *names*.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass

logger = logging.getLogger("motet")

OTLP_ENDPOINT_ENV = "OTEL_EXPORTER_OTLP_ENDPOINT"
OTLP_HEADERS_ENV = "OTEL_EXPORTER_OTLP_HEADERS"
SERVICE_NAME_ENV = "OTEL_SERVICE_NAME"
ERROR_DSN_ENV = "SENTRY_DSN_BACKEND"

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


def status() -> ObsStatus:
    """Report the wiring, so 'no data' can be told apart from 'no errors'."""
    return ObsStatus(
        service_name=os.environ.get(SERVICE_NAME_ENV, DEFAULT_SERVICE_NAME),
        otlp_configured=bool(os.environ.get(OTLP_ENDPOINT_ENV, "").strip()),
        errors_configured=bool(os.environ.get(ERROR_DSN_ENV, "").strip()),
    )


def configure() -> ObsStatus:
    """Set up logging and report what telemetry is wired.

    Scaffold: the OTLP and GlitchTip exporters themselves are not installed yet, so this
    configures stdlib logging and records the intent. Wiring the exporters means filling
    this function in — not calling a vendor SDK from somewhere else.
    """
    current = status()
    logging.basicConfig(level=os.environ.get("LOG_LEVEL", "INFO"))
    logger.info(
        "obs: service=%s otlp=%s errors=%s",
        current.service_name,
        "configured" if current.otlp_configured else "unset (no-op)",
        "configured" if current.errors_configured else "unset (no-op)",
    )
    return current
