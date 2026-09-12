"""Which records page, and which records only get logged — read off the wire.

`motet_obs` carries two guards over library log records and they are deliberately *not*
the same width, which is the whole of motet#73:

* the **loop guard** keeps the OTLP *log* pipeline from exporting its own failures, because
  that is a feedback loop that saturates a container;
* the **event guard** keeps a third-party exporter's diagnostic out of GlitchTip, because a
  new issue in the production project pages Slack `#alerts`.

Before the fix the first was the only one that existed and it was written at whole-namespace
width, so `Failed to export metrics batch due to timeout, max retries or shutdown.` — the
OTel SDK's own line, logged once from a CPU-throttled Cloud Run instance — reached
*neither* VictoriaLogs *nor* any non-paging surface, and paged.

Asserted the way `test_export.py` asserts everything: a real process, real SDKs, and the
bytes that arrived at a socket. Whether a record becomes a Sentry event is decided inside
`sentry_sdk`, so a test that stubbed it out would be testing the stub. The collector answers
200 to any POST, which lets one server be both the OTLP endpoint and the GlitchTip ingest.
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
DSN_KEY = "0123456789abcdef0123456789abcdef"  # noqa: S105 — likewise
SERVICE = "motet-api"

#: The logger the production page came from, and the message verbatim from
#: `opentelemetry/exporter/otlp/proto/http/metric_exporter/__init__.py`.
METRIC_EXPORTER_LOGGER = "opentelemetry.exporter.otlp.proto.http.metric_exporter"
METRIC_EXPORTER_MESSAGE = "Failed to export metrics batch due to timeout, max retries or shutdown."

#: The one exporter whose own records still must not reach the log pipeline: exporting them
#: is what produces them.
LOG_EXPORTER_LOGGER = "opentelemetry.exporter.otlp.proto.http._log_exporter"
LOG_EXPORTER_MESSAGE = "Failed to export logs batch code: 503, reason: obs is down"

#: Shared by every signal's HTTP, so a record here cannot be attributed to one of them and
#: has to be treated as the log pipeline's. Deliberately **not** `urllib3.connectionpool`,
#: which `sentry_sdk` ignores for events out of the box — an assertion about that one would
#: pass whatever this module did.
TRANSPORT_LOGGER = "urllib3.util.retry"
TRANSPORT_MESSAGE = "Incremented Retry for (url='/v1/logs')"

#: Motet's own failure, which is what the error channel is *for*. Present so that a green
#: run cannot be a run where error reporting was switched off wholesale.
MOTET_LOGGER = "motet.workers.runner"
MOTET_MESSAGE = "worker could not claim a job"

EMITTER = f"""
import json
import logging
import motet_obs

current = motet_obs.configure({SERVICE!r})
assert current.exporting, current
assert current.errors_configured, current

logging.getLogger({METRIC_EXPORTER_LOGGER!r}).error({METRIC_EXPORTER_MESSAGE!r})
logging.getLogger({LOG_EXPORTER_LOGGER!r}).error({LOG_EXPORTER_MESSAGE!r})
logging.getLogger({TRANSPORT_LOGGER!r}).error({TRANSPORT_MESSAGE!r})
logging.getLogger({MOTET_LOGGER!r}).error({MOTET_MESSAGE!r})

# Flushes both the OTLP batch processors and the Sentry transport, which is what puts the
# envelopes on the socket before this process exits.
motet_obs.shutdown()
print(json.dumps({{"exporters": list(current.exporters)}}))
"""


@pytest.fixture(scope="module")
def emitted(otlp_collector: OtlpCollector) -> dict[str, Any]:
    """One process, four log records, one socket carrying both OTLP and GlitchTip."""
    port = otlp_collector.endpoint.rsplit(":", 1)[1]
    completed = subprocess.run(
        [sys.executable, "-c", EMITTER],
        capture_output=True,
        text=True,
        timeout=120,
        env={
            "PATH": "/usr/bin:/bin",
            "OTEL_EXPORTER_OTLP_ENDPOINT": otlp_collector.endpoint,
            "OTEL_INGEST_TOKEN": TOKEN,
            # The name the DSN secret is actually placed under, so this exercises the
            # resolution the deploy uses rather than a second spelling.
            "GLITCHTIP_DSN": f"http://{DSN_KEY}@127.0.0.1:{port}/1",
        },
    )
    assert completed.returncode == 0, completed.stderr
    return dict(json.loads(completed.stdout))


def _event_loggers(collector: OtlpCollector) -> list[str]:
    return [str(event.get("logger")) for event in collector.sentry_events()]


def test_error_reporting_is_actually_installed(emitted: dict[str, Any]) -> None:
    """`errors` alongside the OTLP three: the guards below are about a live channel."""
    assert emitted["exporters"] == ["traces", "metrics", "logs", "errors"]


def test_motets_own_error_still_becomes_an_event(
    emitted: dict[str, Any], otlp_collector: OtlpCollector
) -> None:
    """The control. Scoping an alert must not be an off switch for error reporting."""
    assert MOTET_LOGGER in _event_loggers(otlp_collector)


def test_the_metric_exporters_diagnostic_does_not_page(
    emitted: dict[str, Any], otlp_collector: OtlpCollector
) -> None:
    """motet#73: this exact record created a new GlitchTip issue and paged `#alerts`."""
    assert METRIC_EXPORTER_LOGGER not in _event_loggers(otlp_collector)
    assert METRIC_EXPORTER_MESSAGE not in [
        str(event.get("logentry", {}).get("message")) for event in otlp_collector.sentry_events()
    ]


def test_the_metric_exporters_diagnostic_still_reaches_victorialogs(
    emitted: dict[str, Any], otlp_collector: OtlpCollector
) -> None:
    """Invariant 11: the obs stack is the only channel an agent can observe production
    through, so scoping the alert has to move the signal rather than delete it.

    This is the assertion the old whole-namespace loop guard would have failed — and the
    reason the fix is two guards of different widths instead of one wider one.
    """
    assert METRIC_EXPORTER_MESSAGE in otlp_collector.log_bodies()


def test_the_log_exporters_own_failure_is_still_not_exported(
    emitted: dict[str, Any], otlp_collector: OtlpCollector
) -> None:
    """The loop guard, which the narrowing must not have opened.

    Exporting *this* record is what produces it, and the loop is fast enough to saturate a
    container — so it stays out of the log pipeline, and out of GlitchTip as well.
    """
    assert LOG_EXPORTER_MESSAGE not in otlp_collector.log_bodies()
    assert LOG_EXPORTER_LOGGER not in _event_loggers(otlp_collector)


def test_the_shared_http_transport_is_on_both_guards(
    emitted: dict[str, Any], otlp_collector: OtlpCollector
) -> None:
    """`urllib3` carries every signal's requests, so a record cannot be attributed to one.

    It could be the log exporter's, which makes it a loop; it is never a Motet fault, which
    makes it not worth paging on. Stdout keeps it.
    """
    assert TRANSPORT_MESSAGE not in otlp_collector.log_bodies()
    assert TRANSPORT_LOGGER not in _event_loggers(otlp_collector)
