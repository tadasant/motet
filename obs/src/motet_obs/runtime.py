"""Installing the exporters — the half of telemetry that actually sends bytes.

:mod:`motet_obs.settings` reads the environment; this module acts on it. Everything here
is a no-op when the environment is unset, which is what lets a laptop and CI run with no
obs stack at all — and which is precisely the trap :func:`status` exists to close, because
a silent no-op is indistinguishable from a healthy, quiet service.
"""

from __future__ import annotations

import logging
import os
from collections.abc import Mapping
from typing import Any

from .settings import (
    OTLP_ENDPOINT_ENV,
    OTLP_HEADERS_ENV,
    OTLP_PROTOCOL_ENV,
    OTLP_TOKEN_ENV,
    SUPPORTED_PROTOCOL,
    ObsStatus,
    parse_headers,
    resolve_deployment_environment,
    resolve_error_dsn,
    resolve_otlp_headers,
    resolve_service_version,
    sdk_disabled,
)
from .settings import (
    status as _env_status,
)

logger = logging.getLogger("motet.obs")

#: How often metrics are pushed. Shorter than the SDK's 60s default because the worker is
#: a Cloud Run *job*: a run that finishes in twenty seconds and exits would otherwise
#: export exactly one point, at shutdown, or none at all if the flush were missed.
METRIC_EXPORT_INTERVAL_MS = 15_000

#: Loggers whose records must never be exported through the OTLP log pipeline.
#:
#: This is a feedback-loop guard, not tidiness. The log exporter is an HTTP client; when
#: the obs stack is unreachable it logs the failure, and a handler that exported *that*
#: record would produce another export, another failure, and another record. The loop is
#: fast enough to saturate a container.
_NO_EXPORT_LOGGERS = ("opentelemetry", "urllib3", "sentry_sdk")

#: What :func:`configure` actually installed, which is a different question from what the
#: environment asked for. Read back by :func:`status` so that the health route can answer "is
#: anything being exported" rather than only "was a variable set".
_installed: tuple[str, ...] = ()

_shutdown_hooks: list[Any] = []


class _ExporterLoopFilter(logging.Filter):
    """Drop the exporters' own records, so exporting cannot cause exporting."""

    def filter(self, record: logging.LogRecord) -> bool:
        return not record.name.startswith(_NO_EXPORT_LOGGERS)


def status(
    env: Mapping[str, str] | None = None, *, default_service_name: str = "motet"
) -> ObsStatus:
    """The environment's wiring, plus what this process actually installed."""
    return _env_status(env, default_service_name=default_service_name, exporters=_installed)


def _resource(service_name: str) -> Any:
    from opentelemetry.sdk.resources import SERVICE_NAME, Resource

    # `Resource.create` already merges `OTEL_RESOURCE_ATTRIBUTES`; the explicit service
    # name is passed because it has been resolved once, in `settings.status`, and one
    # resolution is the point — the name the health route reports and the name the spans
    # carry must be the same string.
    return Resource.create({SERVICE_NAME: service_name})


def _check_protocol(env: Mapping[str, str]) -> None:
    """Say so, loudly, if the deploy asked for a wire protocol these images cannot speak.

    Only ``http/protobuf`` is installed. Rather than silently exporting over HTTP to a
    gRPC endpoint and leaving somebody to diagnose it from the obs side, name the
    mismatch here: this line is the one an operator greps for when the stack is empty.
    """
    configured = env.get(OTLP_PROTOCOL_ENV, "").strip()
    if configured and configured != SUPPORTED_PROTOCOL:
        logger.error(
            "obs: %s=%r but these images only ship the %r exporter. Exports will be sent "
            "as %s regardless and will fail if the collector does not accept it — add "
            "the matching exporter package rather than changing this variable.",
            OTLP_PROTOCOL_ENV,
            configured,
            SUPPORTED_PROTOCOL,
            SUPPORTED_PROTOCOL,
        )


def _install_otlp(service_name: str, headers: dict[str, str]) -> list[str]:
    """Traces, metrics and logs out of this process, over OTLP/HTTP.

    Endpoints come from the standard variables, read by the exporters themselves — each
    signal's path is appended to ``OTEL_EXPORTER_OTLP_ENDPOINT`` per the OTel spec, and a
    per-signal override still wins. Only the headers are passed explicitly, because they
    are the one thing the environment may not have been able to compose.
    """
    from opentelemetry import metrics, trace
    from opentelemetry._logs import set_logger_provider
    from opentelemetry.exporter.otlp.proto.http._log_exporter import OTLPLogExporter
    from opentelemetry.exporter.otlp.proto.http.metric_exporter import OTLPMetricExporter
    from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
    from opentelemetry.sdk._logs import LoggerProvider, LoggingHandler
    from opentelemetry.sdk._logs.export import BatchLogRecordProcessor
    from opentelemetry.sdk.metrics import MeterProvider
    from opentelemetry.sdk.metrics.export import PeriodicExportingMetricReader
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import BatchSpanProcessor

    resource = _resource(service_name)

    tracer_provider = TracerProvider(resource=resource)
    tracer_provider.add_span_processor(BatchSpanProcessor(OTLPSpanExporter(headers=headers)))
    trace.set_tracer_provider(tracer_provider)

    meter_provider = MeterProvider(
        resource=resource,
        metric_readers=[
            PeriodicExportingMetricReader(
                OTLPMetricExporter(headers=headers),
                export_interval_millis=METRIC_EXPORT_INTERVAL_MS,
            )
        ],
    )
    metrics.set_meter_provider(meter_provider)

    logger_provider = LoggerProvider(resource=resource)
    logger_provider.add_log_record_processor(
        BatchLogRecordProcessor(OTLPLogExporter(headers=headers))
    )
    set_logger_provider(logger_provider)

    # On the ROOT logger, so that uvicorn's and the libraries' records reach obs too and
    # not only `motet.*`. stdout keeps its own handler: the Cloud Run execution log is
    # what is left when the obs stack itself is the thing that is broken.
    handler = LoggingHandler(level=logging.NOTSET, logger_provider=logger_provider)
    handler.addFilter(_ExporterLoopFilter())
    logging.getLogger().addHandler(handler)

    _shutdown_hooks.extend(
        [tracer_provider.shutdown, meter_provider.shutdown, logger_provider.shutdown]
    )
    return ["traces", "metrics", "logs"]


def _install_errors(service_name: str, dsn: str, env: Mapping[str, str]) -> list[str]:
    """Errors to GlitchTip, which speaks the Sentry protocol.

    ``logger.exception`` and ``logger.error`` become events through the SDK's logging
    integration, which is why the worker reports failures by logging them rather than by
    importing a vendor SDK into the queue runner.
    """
    import sentry_sdk

    sentry_sdk.init(
        dsn=dsn,
        environment=resolve_deployment_environment(env),
        release=resolve_service_version(env),
        # Tracing is OpenTelemetry's job here, and paying for both would mean two
        # sampling decisions and two trace ids for the same request.
        traces_sample_rate=0.0,
        # A briefing is built out of the user's own mail. Never let the error reporter be
        # the thing that copies it somewhere else.
        send_default_pii=False,
    )
    sentry_sdk.set_tag("service", service_name)
    _shutdown_hooks.append(lambda: sentry_sdk.flush(timeout=5.0))
    return ["errors"]


def configure(service_name: str, env: Mapping[str, str] | None = None) -> ObsStatus:
    """Install every exporter the environment asks for, and report what was installed.

    Called once per process — from the API's lifespan and from the worker's entry point.
    Idempotent: a second call reports the first call's result rather than stacking a
    second set of providers on the global ones.

    ``service_name`` is the *fallback*; ``OTEL_SERVICE_NAME`` wins when it is set. Each
    deployable passes its own (``motet-api``, ``motet-worker``) because that label is what
    an operator filters on, and a shared default would collapse two processes into one.

    Nothing here raises. A misconfigured exporter must not stop a process from serving —
    but it must not be silent either, so a failure is logged and left out of
    :attr:`ObsStatus.exporters`, where the health route will report it as not exporting.
    """
    global _installed

    environ = os.environ if env is None else env
    # `.upper()` because `logging.basicConfig(level="debug")` raises `ValueError: Unknown
    # level`. This is the first statement in the API's lifespan, so a lowercase LOG_LEVEL
    # would be a failed revision whose traceback never mentions LOG_LEVEL.
    logging.basicConfig(level=environ.get("LOG_LEVEL", "INFO").strip().upper() or "INFO")

    current = _env_status(environ, default_service_name=service_name, exporters=_installed)
    if _installed:
        logger.debug("obs: already configured (%s)", ", ".join(_installed))
        return current

    installed: list[str] = []
    if sdk_disabled(environ):
        logger.warning(
            "obs: telemetry is off because OTEL_SDK_DISABLED is true. Nothing will be "
            "exported by %s, including errors.",
            current.service_name,
        )
    else:
        if current.otlp_configured:
            _check_protocol(environ)
            headers = parse_headers(resolve_otlp_headers(environ) or "")
            try:
                installed += _install_otlp(current.service_name, headers)
            except Exception:  # noqa: BLE001 — a broken exporter must not stop the process
                logger.exception("obs: OTLP exporters failed to start; nothing will be exported")
        dsn = resolve_error_dsn(environ)
        if dsn:
            try:
                installed += _install_errors(current.service_name, dsn, environ)
            except Exception:  # noqa: BLE001 — same reason
                logger.exception("obs: error reporting failed to start")

    _installed = tuple(installed)
    current = _env_status(environ, default_service_name=service_name, exporters=_installed)
    logger.info(
        "obs: service=%s otlp=%s errors=%s exporting=%s",
        current.service_name,
        "configured" if current.otlp_configured else "unset (no-op)",
        "configured" if current.errors_configured else "unset (no-op)",
        ", ".join(_installed) or "nothing",
    )
    # Louder than the line above, because this is the shape the trap actually takes: an
    # endpoint is set, so somebody believes telemetry is on, and every export 401s.
    if environ.get(OTLP_ENDPOINT_ENV, "").strip() and not current.otlp_configured:
        logger.warning(
            "obs: %s is set but no ingest credential is: set %s (or %s). Exports would "
            "be rejected, and rejected exports look exactly like a quiet service.",
            OTLP_ENDPOINT_ENV,
            OTLP_TOKEN_ENV,
            OTLP_HEADERS_ENV,
        )
    return current


def instrument_fastapi(app: Any) -> None:
    """Give an ASGI app request spans and HTTP server metrics.

    **Called at import, not from the lifespan.** Instrumenting adds middleware, and
    Starlette refuses that once the middleware stack is built — which it is by the time a
    lifespan event is delivered. Doing it before any provider exists is fine and is what
    the OTel API's proxy providers are for: the middleware holds a proxy tracer that
    resolves the moment :func:`configure` sets the real one.
    """
    try:
        from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor

        FastAPIInstrumentor.instrument_app(app)
    except Exception:  # noqa: BLE001 — an uninstrumented app still serves
        logger.exception("obs: FastAPI instrumentation failed; requests will not be traced")


def shutdown() -> None:
    """Flush and stop every provider this process installed.

    **The worker is why this is public.** A Cloud Run job drains a queue and exits, and a
    batch processor that has not been flushed loses whatever it was holding — which is
    the most interesting part, because it is the end of the run. The SDK registers its own
    ``atexit`` hook, but a job that is SIGKILLed after SIGTERM may never reach it, so the
    runner calls this in a ``finally`` instead of trusting interpreter shutdown.
    """
    global _installed

    for hook in _shutdown_hooks:
        try:
            hook()
        except Exception:  # noqa: BLE001 — shutting down is not worth an exception
            logger.exception("obs: a telemetry provider failed to shut down")
    _shutdown_hooks.clear()
    _installed = ()
