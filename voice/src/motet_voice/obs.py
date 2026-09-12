"""The voice service's telemetry, which is :mod:`motet_obs` bound to this service's name.

**Invariant 11.** Metrics, logs and errors go to the self-hosted obs stack, never GCP
Cloud Logging: there is deliberately no GCP MCP server, so the obs stack is the only place
an agent can see how a deployed service is behaving.

The wiring itself lives in the ``motet-obs`` workspace package — the same one the API and
the worker use — because a second hand-rolled copy is how two services end up disagreeing
about which variables mean what. ``motet-obs`` depends on no ``motet-*`` package, so taking
it does not weaken invariant 2: it reaches an OTLP endpoint and never a database.

What is left here is the part that is genuinely this service's: the name it reports as.

Every exporter no-ops when its variable is unset, which is the trap :func:`status` exists
to close: a silent no-op is indistinguishable from a healthy, quiet service. Never infer
"no errors" from "no data" — ask :func:`status`, which ``/internal/health`` reports.
"""

from __future__ import annotations

from collections.abc import Mapping

import motet_obs
from motet_obs import ObsStatus

__all__ = [
    "SERVICE_NAME",
    "ObsStatus",
    "configure",
    "instrument",
    "shutdown",
    "status",
]

SERVICE_NAME = "motet-voice"


def status(env: Mapping[str, str] | None = None) -> ObsStatus:
    """What is wired, and what this process actually installed."""
    return motet_obs.status(env, default_service_name=SERVICE_NAME)


def configure() -> ObsStatus:
    """Install the exporters. Called from the lifespan, before anything else runs."""
    return motet_obs.configure(SERVICE_NAME)


def shutdown() -> None:
    """Flush and stop the exporters. Called at the end of the lifespan.

    The flush matters more here than anywhere: a Cloud Run instance that scales to zero
    between two walks would otherwise drop the last batch of spans and log records, which
    are precisely the ones nobody would think to go looking for.
    """
    motet_obs.shutdown()


def instrument(app: object) -> None:
    """Add request spans and HTTP server metrics to the app.

    Separate from :func:`configure` and called before the lifespan runs, because
    instrumenting adds ASGI middleware and Starlette refuses that once the middleware stack
    is built — which it is by the time a lifespan event arrives.
    """
    motet_obs.instrument_fastapi(app)
