"""Telemetry for the voice service — the self-hosted obs stack, never GCP Cloud Logging.

**Invariant 11.** Same contract as :mod:`motet_api.obs`, same reasoning: there is
deliberately no GCP MCP server, so the self-hosted stack is the only place an agent can see
how a deployed service is behaving. This module differs from the API's only in its default
service name, and it is a separate module rather than an import because the two are separate
Cloud Run services with separate lifecycles.

Every exporter no-ops when its variable is unset, which is the trap :func:`status` exists to
close: a silent no-op is indistinguishable from a healthy, quiet service. Never infer "no
false barge-ins" from "no data" — ask.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from typing import Final

logger = logging.getLogger("motet.voice")

OTLP_ENDPOINT_ENV: Final = "OTEL_EXPORTER_OTLP_ENDPOINT"
SERVICE_NAME_ENV: Final = "OTEL_SERVICE_NAME"
ERROR_DSN_ENV: Final = "SENTRY_DSN_BACKEND"

DEFAULT_SERVICE_NAME: Final = "motet-voice"


@dataclass(frozen=True)
class ObsStatus:
    service_name: str
    otlp_configured: bool
    errors_configured: bool


def status() -> ObsStatus:
    return ObsStatus(
        service_name=os.environ.get(SERVICE_NAME_ENV, DEFAULT_SERVICE_NAME),
        otlp_configured=bool(os.environ.get(OTLP_ENDPOINT_ENV, "").strip()),
        errors_configured=bool(os.environ.get(ERROR_DSN_ENV, "").strip()),
    )


def configure() -> ObsStatus:
    """Set up logging and report what telemetry is actually wired."""
    current = status()
    logging.basicConfig(level=os.environ.get("LOG_LEVEL", "INFO"))
    logger.info(
        "obs: service=%s otlp=%s errors=%s",
        current.service_name,
        "configured" if current.otlp_configured else "unset (no-op)",
        "configured" if current.errors_configured else "unset (no-op)",
    )
    return current
