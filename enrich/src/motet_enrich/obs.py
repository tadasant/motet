"""This service's telemetry: :mod:`motet_obs` bound to this service's name, and two metrics.

**Invariant 11.** Metrics, logs and errors go to the self-hosted obs stack, never GCP
Cloud Logging — there is deliberately no GCP MCP server, so the obs stack is the only place
an agent can see how a deployed service is behaving. The wiring itself is ``motet-obs``,
the package the API, the worker and the voice service share; what is this service's own is
the name it reports as and the two instruments below.

**The worker records the enrichment metrics too, and the split is deliberate rather than
duplication.** It records what it *did with* a run — the outcome that reached a source
item, keyed by domain — and it can, because it has the user and the row. This side records
what the run *cost here*, which is the only place the agent's own token spend is visible at
all: this process holds no database, so there is no ledger row it could write (motet#92's
``llm_usage`` is the worker's, and the voice service makes the same trade for the same
reason). A run whose answer never reached the worker — a dropped connection, a worker that
died — appears on this side and nowhere else, which is exactly the case worth being able
to see.
"""

from __future__ import annotations

from collections.abc import Mapping

import motet_obs
from motet_obs import ObsStatus
from opentelemetry import metrics

from .contract import EnrichResult

__all__ = [
    "SERVICE_NAME",
    "ObsStatus",
    "configure",
    "instrument",
    "logger",
    "record_run",
    "shutdown",
    "status",
]

SERVICE_NAME = "motet-enrich"

logger = motet_obs.logger

# Against OpenTelemetry's proxy meter at import, the same shape as
# `motet_inference.accounting`: a no-op until `configure` installs a provider.
_meter = metrics.get_meter("motet.enrich")

_runs = _meter.create_counter(
    "motet.enrich.service_runs",
    unit="{run}",
    description=(
        "Agent runs this service finished, by outcome and by site domain. Named apart "
        "from the worker's `motet.enrich.runs` because they count different populations: "
        "this one includes a run whose answer never reached the worker, and the worker's "
        "includes an item the service was never asked about."
    ),
)
_cost = _meter.create_counter(
    "motet.enrich.service_cost_usd",
    unit="USD",
    description=(
        "What the agent's own completions cost, by outcome and domain. This service has no "
        "database, so there is no `llm_usage` row for an enrichment run — this counter is "
        "the only place the agent's token spend is visible, and `capped` rising on it is "
        "the per-item cap doing its job rather than a fault."
    ),
)
_tool_calls = _meter.create_counter(
    "motet.enrich.service_tool_calls",
    unit="{call}",
    description=(
        "Tool calls the agent made, by outcome and domain. Read beside the cost: a domain "
        "whose calls climb while its `ok` runs do not is a site whose shape the prompt no "
        "longer fits."
    ),
)


def record_run(result: EnrichResult, *, domain: str) -> None:
    """Count one finished run. Added to on **every** outcome, including zero cost.

    A series that exists only when something is wrong cannot tell "no enrichment ran" from
    "the exporter never started" — invariant 11's own trap, which is why the zero is added
    rather than skipped.
    """
    attributes = {"outcome": result.status, "domain": domain}
    _runs.add(1, attributes)
    _cost.add(result.cost_usd, attributes)
    _tool_calls.add(result.tool_calls, attributes)


def status(env: Mapping[str, str] | None = None) -> ObsStatus:
    """What is wired, and what this process actually installed."""
    return motet_obs.status(env, default_service_name=SERVICE_NAME)


def configure() -> ObsStatus:
    """Install the exporters. Called from the lifespan, before anything else runs."""
    return motet_obs.configure(SERVICE_NAME)


def shutdown() -> None:
    """Flush and stop the exporters, at the end of the lifespan.

    This service scales to zero between walks, so without the flush the last batch of
    spans and log records — the ones covering the run somebody is asking about — is lost.
    """
    motet_obs.shutdown()


def instrument(app: object) -> None:
    """Add request spans and HTTP server metrics.

    Before the lifespan runs, because instrumenting adds ASGI middleware and Starlette
    refuses that once the middleware stack is built.
    """
    motet_obs.instrument_fastapi(app)
