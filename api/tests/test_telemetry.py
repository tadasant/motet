"""The API's own telemetry, proved the only way it can be: off the wire.

``obs/tests/test_export.py`` proves the shared wiring exports. This proves the thing an
operator actually asks for — that *this* service, under its own name, produces a span for
a request it served and admits on its health route that it is exporting.

**In a subprocess, deliberately.** OpenTelemetry's providers are process-global and can be
set once; configuring inside the test runner would work exactly once and poison every
later test in the session.
"""

from __future__ import annotations

import json
import subprocess
import sys
from typing import TYPE_CHECKING, Any

import pytest
from motet_api.main import HEALTH_PATH

if TYPE_CHECKING:  # pragma: no cover - typing only
    from conftest import OtlpCollector

TOKEN = "not-a-real-token"  # noqa: S105 — a literal for a throwaway local collector

#: A whole API process: import the app (which instruments it), enter the lifespan (which
#: installs the exporters), serve one request, leave the lifespan (which flushes).
#: `TestClient` rather than uvicorn because the claim under test is about the ASGI app and
#: its middleware stack, and a real socket would add a port, a poll loop and a signal.
#:
#: **Nothing calls `motet_obs.shutdown()` here, deliberately.** The flush has to be the
#: lifespan's own doing: Cloud Run stops a revision with SIGTERM, and measured locally a
#: terminate straight after a request exported *nothing at all* before the lifespan took
#: responsibility for flushing. If somebody removes that, every assertion below goes red.
SERVE_ONE_REQUEST = """
import json
from fastapi.testclient import TestClient
from motet_api.main import HEALTH_PATH, app

with TestClient(app) as client:
    health = client.get(HEALTH_PATH).json()

print(json.dumps(health))
"""


@pytest.fixture(scope="module")
def health(otlp_collector: OtlpCollector) -> dict[str, Any]:
    """Run the API for one request against the collector; return what it said about itself."""
    completed = subprocess.run(
        [sys.executable, "-c", SERVE_ONE_REQUEST],
        capture_output=True,
        text=True,
        timeout=120,
        env={
            "PATH": "/usr/bin:/bin",
            "MOTET_INFERENCE_MODE": "fake",
            "OTEL_EXPORTER_OTLP_ENDPOINT": otlp_collector.endpoint,
            "OTEL_INGEST_TOKEN": TOKEN,
        },
    )
    assert completed.returncode == 0, completed.stderr
    return dict(json.loads(completed.stdout))


def test_the_health_route_admits_that_it_is_exporting(health: dict[str, Any]) -> None:
    """The flag the issue was about.

    ``telemetry_configured`` was true for months from a process with no exporter in it.
    ``telemetry_exporting`` is the answer to the question that was actually being asked.
    """
    assert health["telemetry_configured"] is True
    assert health["telemetry_exporting"] is True


def test_a_request_produces_a_span(health: dict[str, Any], otlp_collector: OtlpCollector) -> None:
    """Automatic, from the ASGI instrumentation installed at import.

    It has to be installed at import rather than in the lifespan: instrumenting adds
    middleware and Starlette refuses that once the stack is built. If somebody moves the
    call next to `obs.configure()`, this is what goes red.
    """
    names = [span.name for span in otlp_collector.spans()]
    assert any(HEALTH_PATH in name for name in names), names


def test_the_spans_are_labelled_motet_api(
    health: dict[str, Any], otlp_collector: OtlpCollector
) -> None:
    """`motet-api`, distinct from `motet-worker`. It is what an operator filters on."""
    assert otlp_collector.trace_resource()["service.name"] == "motet-api"
    assert health["service"] == "motet-api"


def test_the_startup_log_reaches_obs_too(
    health: dict[str, Any], otlp_collector: OtlpCollector
) -> None:
    """Not only spans. The startup lines are what tell an operator which build is running."""
    bodies = otlp_collector.log_bodies()
    assert any(line.startswith("llm:") for line in bodies), bodies
