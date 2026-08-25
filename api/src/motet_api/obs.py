"""The API's telemetry, which is :mod:`motet_obs` bound to this service's name.

The wiring itself lives in the ``motet-obs`` workspace package, because the worker needs
the same wiring and cannot import from here: ``motet-api`` depends on ``motet-workers``,
so the arrow only goes one way. What is left in this module is the one thing that is
genuinely per-service — the name this process reports as, which is the label an operator
filters on and therefore must not be shared by two processes.

``OTEL_SERVICE_NAME`` still wins when it is set; :data:`SERVICE_NAME` is the fallback for
a laptop and for a deployment that forgot.
"""

from __future__ import annotations

from collections.abc import Mapping

import motet_obs
from motet_obs import (
    ERROR_DSN_ENV,
    GLITCHTIP_DSN_ENV,
    OTLP_ENDPOINT_ENV,
    OTLP_HEADERS_ENV,
    OTLP_TOKEN_ENV,
    ObsStatus,
    logger,
    record_exception,
    resolve_error_dsn,
    resolve_otlp_headers,
)

__all__ = [
    "ERROR_DSN_ENV",
    "GLITCHTIP_DSN_ENV",
    "OTLP_ENDPOINT_ENV",
    "OTLP_HEADERS_ENV",
    "OTLP_TOKEN_ENV",
    "SERVICE_NAME",
    "ObsStatus",
    "configure",
    "instrument",
    "logger",
    "record_exception",
    "resolve_error_dsn",
    "resolve_otlp_headers",
    "shutdown",
    "status",
]

SERVICE_NAME = "motet-api"


def status(env: Mapping[str, str] | None = None) -> ObsStatus:
    """What is wired, and what this process actually installed."""
    return motet_obs.status(env, default_service_name=SERVICE_NAME)


def configure() -> ObsStatus:
    """Install the exporters. Called from the lifespan, before anything else runs."""
    return motet_obs.configure(SERVICE_NAME)


def shutdown() -> None:
    """Flush and stop the exporters. Called at the end of the lifespan."""
    motet_obs.shutdown()


def instrument(app: object) -> None:
    """Add request spans and HTTP server metrics to the app.

    Separate from :func:`configure` and called at *import* rather than from the lifespan,
    because instrumenting adds ASGI middleware and Starlette refuses that once the
    middleware stack is built — which it is by the time a lifespan event arrives.
    """
    motet_obs.instrument_fastapi(app)
