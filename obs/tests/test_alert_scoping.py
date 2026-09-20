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
from motet_obs import runtime

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

#: The batch processor behind the log exporter, whose "Queue full, dropping %s." fires on
#: the *emitting* thread — so a record it produced, exported, would produce another one.
#: Shared with the span processor, which is why it cannot be let through by signal.
LOG_PROCESSOR_LOGGER = "opentelemetry.sdk._shared_internal"
LOG_PROCESSOR_MESSAGE = "Queue full, dropping log."

#: Attribute cleaning, which logs from inside `LoggingHandler.emit` — re-entering the root
#: handlers while the outer `emit` is still on the stack.
ATTRIBUTES_LOGGER = "opentelemetry.attributes"
ATTRIBUTES_MESSAGE = "Invalid type dict for attribute 'motet.queue' value"

#: Motet's own failure, which is what the error channel is *for*. Present so that a green
#: run cannot be a run where error reporting was switched off wholesale. Emitted **last**,
#: so that the three third-party records above are already on the breadcrumb trail when it
#: becomes an event — see `test_the_dropped_records_survive_as_breadcrumbs`.
MOTET_LOGGER = "motet.workers.runner"
MOTET_MESSAGE = "worker could not claim a job"

#: A credential held in a local while the frame that holds it raises — the exact shape of
#: ``main.oauth_callback``, whose ``grant`` holds a *user's* Gmail refresh token, and of
#: ``main.seed_gmail_source``. Both log with ``exception()`` on a vault failure by design,
#: because a traceback is what tells a bug apart from a KMS refusal — so if the SDK attaches
#: frame locals, one unreachable keyring posts a live refresh token to GlitchTip.
#:
#: **Held on an object named ``grant``, and that is the point of the test rather than an
#: incidental choice.** `sentry_sdk`'s own `EventScrubber` runs by default and filters a
#: variable *named* something on its denylist — ``secret``, ``token``, ``password``. A test
#: whose local was called ``secret`` passes with locals switched on and proves nothing. The
#: name the API actually uses is ``grant``, which is on no denylist, and its ``repr``
#: carries the token.
SECRET_LOCAL = "1//0e-a-refresh-token-shaped-string"  # noqa: S105 — a literal, not a secret
SEAL_FAILURE_LOGGER = "motet.api"
SEAL_FAILURE_MESSAGE = "could not seal the credential"

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
logging.getLogger({LOG_PROCESSOR_LOGGER!r}).error({LOG_PROCESSOR_MESSAGE!r})
logging.getLogger({ATTRIBUTES_LOGGER!r}).error({ATTRIBUTES_MESSAGE!r})
logging.getLogger({MOTET_LOGGER!r}).error({MOTET_MESSAGE!r})

class Grant:
    # Named as `motet_sources.TokenGrant` is, so the scrubber's name denylist does not
    # accidentally do this test's work for it.
    def __init__(self, refresh_token):
        self.refresh_token = refresh_token
    def __repr__(self):
        return "TokenGrant(refresh_token=" + repr(self.refresh_token) + ")"

def seal_and_fail(grant):
    # The frame `sentry_sdk` would attach locals from, holding the credential.
    try:
        raise RuntimeError("the vault refused")
    except RuntimeError:
        logging.getLogger({SEAL_FAILURE_LOGGER!r}).exception({SEAL_FAILURE_MESSAGE!r})

seal_and_fail(Grant({SECRET_LOCAL!r}))

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


def test_the_rest_of_the_log_export_path_is_on_both_guards(
    emitted: dict[str, Any], otlp_collector: OtlpCollector
) -> None:
    """A log export produces records from more than the log exporter, and all of it loops.

    The batch processor's "queue full" warning and the attribute cleaner's complaint are
    both emitted *synchronously inside* `LoggingHandler.emit`, so exporting either one
    re-enters the handler that produced it. Neither is reachable from a metric or trace
    export in a way this filter could tell apart, so both stay out of the pipeline.
    """
    for message in (LOG_PROCESSOR_MESSAGE, ATTRIBUTES_MESSAGE):
        assert message not in otlp_collector.log_bodies()
    for logger in (LOG_PROCESSOR_LOGGER, ATTRIBUTES_LOGGER):
        assert logger not in _event_loggers(otlp_collector)


def test_the_loop_guards_module_paths_match_the_installed_sdk(
    emitted: dict[str, Any],
) -> None:
    """Every `opentelemetry` entry in the guard is an underscore-private module path.

    Upstream has already moved one of them once — `BatchProcessor` used to live under
    `sdk._logs._internal.export` — and a stale literal here reopens an *unbounded* loop
    without anything going red. So the literals are checked against the classes they name,
    and a dependency bump that renames one fails this test instead of saturating a
    container in production.
    """
    from opentelemetry.attributes import BoundedAttributes
    from opentelemetry.exporter.otlp.proto.common import _internal as encoder
    from opentelemetry.exporter.otlp.proto.http._log_exporter import OTLPLogExporter
    from opentelemetry.sdk._logs import LoggingHandler
    from opentelemetry.sdk._logs.export import BatchLogRecordProcessor
    from opentelemetry.sdk._shared_internal import BatchProcessor

    on_the_log_export_path = [
        OTLPLogExporter.__module__,
        BatchLogRecordProcessor.__module__,
        BatchProcessor.__module__,
        LoggingHandler.__module__,
        BoundedAttributes.__module__,
        encoder.__name__,
    ]
    for module in on_the_log_export_path:
        assert module.startswith(runtime._NO_EXPORT_LOGGERS), (
            f"{module} logs from inside a log export but is not behind the loop guard"
        )


def test_the_shared_http_transport_is_on_both_guards(
    emitted: dict[str, Any], otlp_collector: OtlpCollector
) -> None:
    """`urllib3` carries every signal's requests, so a record cannot be attributed to one.

    It could be the log exporter's, which makes it a loop; it is never a Motet fault, which
    makes it not worth paging on. Stdout keeps it.
    """
    assert TRANSPORT_MESSAGE not in otlp_collector.log_bodies()
    assert TRANSPORT_LOGGER not in _event_loggers(otlp_collector)


def test_the_dropped_records_survive_as_breadcrumbs(
    emitted: dict[str, Any], otlp_collector: OtlpCollector
) -> None:
    """The reason the event guard is `before_send` and not `ignore_logger`.

    `ignore_logger`'s list drops breadcrumbs as well as events, so a real Motet error would
    arrive at GlitchTip with no trace of the exporter trouble that preceded it — which is
    often the context that explains it. Pinned rather than asserted in prose, because a
    later switch to `ignore_logger` would pass every other test in this module.
    """
    events = [
        event for event in otlp_collector.sentry_events() if event.get("logger") == MOTET_LOGGER
    ]
    assert events, "the control event is missing; the rest of this assertion means nothing"
    trail = [crumb.get("message") for crumb in events[0].get("breadcrumbs", {}).get("values", [])]
    assert METRIC_EXPORTER_MESSAGE in trail
    assert TRANSPORT_MESSAGE in trail


def test_a_credential_in_a_frame_local_does_not_reach_glitchtip(
    emitted: dict[str, Any], otlp_collector: OtlpCollector
) -> None:
    """The error channel must not be the thing that copies a mailbox token out.

    ``send_default_pii=False`` does **not** cover this — it governs request bodies, headers
    and user identity, while local variables are attached on their own switch that this SDK
    defaults to *on*. Two places in the API hold a refresh token in a local inside a ``try``
    and log with ``exception()`` there on purpose, because a traceback is what tells a bug
    apart from a KMS refusal; with locals attached, one unreachable keyring puts a live
    credential into a searchable issue.

    Asserted over the raw envelope rather than over a flag, because the question is what
    left the process — and over a local named ``grant`` rather than ``secret``, because
    `sentry_sdk`'s own scrubber filters the second by name and would make this test green
    against a leak it never prevented.
    """
    events = otlp_collector.sentry_events()
    reported = [
        event
        for event in events
        if str(event.get("logentry", {}).get("message")) == SEAL_FAILURE_MESSAGE
    ]
    assert reported, "the control: a Motet failure with a traceback still becomes an event"
    assert SECRET_LOCAL not in json.dumps(events), (
        "a credential held in a frame local reached the error reporter"
    )
