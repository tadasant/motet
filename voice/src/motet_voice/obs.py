"""The voice service's telemetry, which is :mod:`motet_obs` bound to this service's name.

**Invariant 11.** Metrics, logs and errors go to the self-hosted obs stack, never GCP
Cloud Logging: there is deliberately no GCP MCP server, so the obs stack is the only place
an agent can see how a deployed service is behaving.

The wiring itself lives in the ``motet-obs`` workspace package — the same one the API and
the worker use — because a second hand-rolled copy is how two services end up disagreeing
about which variables mean what. ``motet-obs`` depends on no ``motet-*`` package, so taking
it does not weaken invariant 2: it reaches an OTLP endpoint and never a database.

What is left here is the part that is genuinely this service's: the name it reports as, and
the two numbers below.

**The counters exist because "advisory" has to mean visible.** Grounding is a hard gate on
the narration path and advisory on the conversational one (motet#10), and the entire
difference between *advisory* and *absent* is whether an operator can answer "how often
does Motet say something it cannot source out loud?" afterwards. These are that answer:

    sum(rate(motet_voice_conversational_replies_total{grounded="false"}[1h]))

is a Grafana query, not a log trawl. The per-reply detail — which number, which name — is
in the warning :mod:`motet_voice.session` logs alongside it, and reaches VictoriaLogs
through the same exporter.

Every exporter no-ops when its variable is unset, which is the trap :func:`status` exists
to close: a silent no-op is indistinguishable from a healthy, quiet service. Never infer
"no ungrounded replies" from "no data" — ask :func:`status`, which ``/internal/health``
reports.
"""

from __future__ import annotations

from collections.abc import Mapping

import motet_obs
from motet_obs import ObsStatus, logger
from opentelemetry import metrics

from .grounding import GroundingVerdict

__all__ = [
    "SERVICE_NAME",
    "ObsStatus",
    "configure",
    "instrument",
    "logger",
    "record_conversational_reply",
    "shutdown",
    "status",
]

SERVICE_NAME = "motet-voice"

# Created at import against OpenTelemetry's *proxy* meter, which resolves to the real one
# the moment `motet_obs.configure` installs a provider. That is what keeps telemetry
# entirely optional: with nothing configured these are no-ops, and no code path has to ask
# whether obs exists before recording.
_meter = metrics.get_meter("motet.voice")

_conversational_replies = _meter.create_counter(
    "motet.voice.conversational_replies",
    unit="{reply}",
    description=(
        "Conversational replies spoken, by whether the advisory grounding check could "
        "source every specific in them. Advisory: a false verdict did not block audio."
    ),
)
_unsupported_specifics = _meter.create_counter(
    "motet.voice.unsupported_specifics",
    unit="{specific}",
    description=(
        "Numbers, names and quotations in a conversational reply that the session's "
        "material does not contain, by kind."
    ),
)


def status(env: Mapping[str, str] | None = None) -> ObsStatus:
    """What is wired, and what this process actually installed."""
    return motet_obs.status(env, default_service_name=SERVICE_NAME)


def configure() -> ObsStatus:
    """Install the exporters. Called from the lifespan, before anything else runs."""
    return motet_obs.configure(SERVICE_NAME)


def shutdown() -> None:
    """Flush and stop the exporters. Called at the end of the lifespan.

    The flush matters more here than anywhere: a Cloud Run instance that scales to zero
    between two walks would otherwise drop the last batch of verdicts, which are precisely
    the ones nobody would think to go looking for.
    """
    motet_obs.shutdown()


def instrument(app: object) -> None:
    """Add request spans and HTTP server metrics to the app.

    Separate from :func:`configure` and called before the lifespan runs, because
    instrumenting adds ASGI middleware and Starlette refuses that once the middleware stack
    is built — which it is by the time a lifespan event arrives.
    """
    motet_obs.instrument_fastapi(app)


def record_conversational_reply(verdict: GroundingVerdict, *, arm: str) -> None:
    """Count one advisory verdict.

    ``arm`` is an attribute rather than a separate metric because the question an operator
    actually has is comparative — does the composed arm fabricate more than the realtime
    one — and that is a ``by (arm)`` on one series rather than a join across two.
    """
    _conversational_replies.add(
        1,
        {
            "grounded": "true" if verdict.grounded else "false",
            "checker": verdict.checker,
            "arm": arm,
        },
    )
    for item in verdict.unsupported:
        # The offending *text* is deliberately not an attribute: it is unbounded
        # user-shaped data, and putting it on a metric would mint a new time series per
        # fabricated number. It goes in the log line, which is where unbounded detail
        # belongs.
        _unsupported_specifics.add(1, {"kind": item.kind, "checker": verdict.checker, "arm": arm})
