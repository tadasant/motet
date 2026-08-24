"""Proof that bytes leave the process — the one thing a status flag cannot tell you.

Everything else about telemetry can be asserted from the environment, and this repo did
assert it, and it was wrong: the health route said `telemetry_configured: true` for months
from a process with no exporter compiled into it. So these tests stand up an OTLP collector
on localhost, run a *real* process against it, and read what arrived off the wire.

**In a subprocess, deliberately.** OpenTelemetry's providers are process-global and can be
set exactly once — a second `set_tracer_provider` is ignored with a warning. Configuring
inside the test runner would therefore work once, poison every later test in the session,
and quietly stop being a test the moment somebody added a second case.
"""

from __future__ import annotations

import json
import subprocess
import sys
from typing import TYPE_CHECKING, Any

import pytest

if TYPE_CHECKING:  # pragma: no cover - typing only
    from conftest import OtlpCollector

TOKEN = "s3cret-with-padding=="  # noqa: S105 — a literal for a throwaway local collector
SERVICE = "motet-worker"

#: Emitted by the subprocess below, and looked for in what the collector captured.
SPAN_NAME = "job integrate"
LOG_MESSAGE = "worker drained a queue"
COUNTER_NAME = "motet.jobs.processed"

#: A whole process, because that is the claim under test: a service that starts, wires
#: itself from nothing but environment variables, emits one of each signal, and flushes on
#: the way out the way a Cloud Run job has to.
EMITTER = f"""
import json
import logging
import motet_obs
from opentelemetry import metrics, trace

current = motet_obs.configure({SERVICE!r})
assert current.service_name == {SERVICE!r}, current
assert current.exporting, current

with trace.get_tracer("motet.worker").start_as_current_span({SPAN_NAME!r}) as span:
    span.set_attribute("motet.queue", "integrate")
    logging.getLogger("motet.worker").info({LOG_MESSAGE!r})

metrics.get_meter("motet.worker").create_counter({COUNTER_NAME!r}).add(
    1, {{"motet.queue": "integrate", "motet.job.outcome": "completed"}}
)

# What a Cloud Run job must do and what nothing else would make it do: a batch processor
# holding the end of the run loses it if the process just exits.
motet_obs.shutdown()
print(json.dumps({{"exporters": list(current.exporters)}}))
"""


@pytest.fixture(scope="module")
def emitted(otlp_collector: OtlpCollector) -> dict[str, Any]:
    """Run one process against the collector and keep what it reported on the way out."""
    completed = subprocess.run(
        [sys.executable, "-c", EMITTER],
        capture_output=True,
        text=True,
        timeout=120,
        env={
            "PATH": "/usr/bin:/bin",
            "OTEL_EXPORTER_OTLP_ENDPOINT": otlp_collector.endpoint,
            # The raw token, NOT the composed header — the case the deploy is in, because
            # the CI identity that applies it cannot read a secret back to build the
            # string itself.
            "OTEL_INGEST_TOKEN": TOKEN,
            "OTEL_RESOURCE_ATTRIBUTES": (
                "deployment.environment.name=test,service.version=0.0.0-test"
            ),
        },
    )
    assert completed.returncode == 0, completed.stderr
    return dict(json.loads(completed.stdout))


def test_the_process_reports_what_it_installed(emitted: dict[str, Any]) -> None:
    """`exporters` is the answer to "is anything being exported", asked of the app."""
    assert emitted["exporters"] == ["traces", "metrics", "logs"]


def test_a_span_arrives(emitted: dict[str, Any], otlp_collector: OtlpCollector) -> None:
    assert [span.name for span in otlp_collector.spans()] == [SPAN_NAME]


def test_the_span_carries_the_service_name_an_operator_filters_on(
    emitted: dict[str, Any], otlp_collector: OtlpCollector
) -> None:
    """`service.name` is the label; the wrong one is as good as no telemetry."""
    assert otlp_collector.trace_resource()["service.name"] == SERVICE


def test_the_deploys_resource_attributes_ride_along(
    emitted: dict[str, Any], otlp_collector: OtlpCollector
) -> None:
    """`OTEL_RESOURCE_ATTRIBUTES` is set by the deploy and must not be overwritten here."""
    resource = otlp_collector.trace_resource()
    assert resource["deployment.environment.name"] == "test"
    assert resource["service.version"] == "0.0.0-test"


def test_the_ingest_token_is_composed_into_a_bearer_header(
    emitted: dict[str, Any], otlp_collector: OtlpCollector
) -> None:
    """The header Terraform could not build, built by the process, on the actual request.

    The `==` padding is the point: a split on every `=` would truncate the credential into
    one that 401s, which reads as an obs fault rather than as a parsing bug here.
    """
    authorization = [
        headers.get("Authorization") for headers in otlp_collector.headers("/v1/traces")
    ]
    assert authorization == [f"Bearer {TOKEN}"]


def test_a_log_record_arrives(emitted: dict[str, Any], otlp_collector: OtlpCollector) -> None:
    """Logs, not just spans — VictoriaLogs is where an operator actually starts."""
    assert LOG_MESSAGE in otlp_collector.log_bodies()


def test_the_exporters_own_logs_are_not_exported(
    emitted: dict[str, Any], otlp_collector: OtlpCollector
) -> None:
    """Otherwise an unreachable collector is a feedback loop rather than an outage."""
    assert not [line for line in otlp_collector.log_bodies() if "Failed to export" in line]


def test_a_metric_arrives(emitted: dict[str, Any], otlp_collector: OtlpCollector) -> None:
    assert COUNTER_NAME in otlp_collector.metric_names()


def test_nothing_is_sent_when_the_environment_is_empty(otlp_collector: OtlpCollector) -> None:
    """The other half of the contract: a laptop and CI need no obs stack at all.

    Its own process for the same reason as the rest — and with a collector up but
    deliberately not named in the environment, so "nothing arrived" means the process
    declined to export rather than that there was nowhere to export to.
    """
    before = len(otlp_collector.received)
    completed = subprocess.run(
        [
            sys.executable,
            "-c",
            (
                "import json, motet_obs\n"
                "current = motet_obs.configure('motet-worker')\n"
                "print(json.dumps({'exporting': current.exporting, "
                "'configured': current.otlp_configured}))\n"
            ),
        ],
        capture_output=True,
        text=True,
        timeout=120,
        env={"PATH": "/usr/bin:/bin"},
    )
    assert completed.returncode == 0, completed.stderr
    assert json.loads(completed.stdout) == {"exporting": False, "configured": False}
    assert len(otlp_collector.received) == before


def test_configuring_again_after_shutdown_refuses_rather_than_lying(
    otlp_collector: OtlpCollector,
) -> None:
    """The subtlest way this package could fail, guarded.

    OpenTelemetry's providers are process-global and may be set once. A second
    `configure` after a `shutdown` therefore builds providers that can never be installed,
    and a naive implementation would go on reporting `exporting` from a process whose
    spans all go to a provider that has already been flushed and stopped — a service that
    looks monitored and emits nothing, which is the whole thing this package exists to
    prevent. It has to refuse, and say so.
    """
    script = (
        "import json, motet_obs\n"
        "from opentelemetry import trace\n"
        "first = motet_obs.configure('motet-worker')\n"
        "motet_obs.shutdown()\n"
        "second = motet_obs.configure('motet-worker')\n"
        "with trace.get_tracer('motet.worker').start_as_current_span('after shutdown'):\n"
        "    pass\n"
        "print(json.dumps({'first': list(first.exporters), 'second': list(second.exporters)}))\n"
    )
    completed = subprocess.run(
        [sys.executable, "-c", script],
        capture_output=True,
        text=True,
        timeout=120,
        env={
            "PATH": "/usr/bin:/bin",
            "OTEL_EXPORTER_OTLP_ENDPOINT": otlp_collector.endpoint,
            "OTEL_INGEST_TOKEN": TOKEN,
        },
    )
    assert completed.returncode == 0, completed.stderr
    reported = json.loads(completed.stdout)
    assert reported["first"] == ["traces", "metrics", "logs"]
    # Not exporting, and honest about it, rather than claiming a provider it cannot use.
    assert reported["second"] == []
    assert "cannot be replaced" in completed.stderr
    # And the span emitted afterwards genuinely goes nowhere, which is what the flag says.
    # (The first `configure`'s own startup log still arrives — that export is the shutdown
    # flush doing its job, and it is the *trace* emitted after the refusal that must not.)
    assert "after shutdown" not in [span.name for span in otlp_collector.spans()]
