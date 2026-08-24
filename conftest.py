"""Fixtures shared by every test package.

Three things live here because they are cross-cutting:

* **A migrated, empty database**, for the tests that exercise the pipeline for real. There
  is no in-memory substitute worth having — the queue is ``SELECT ... FOR UPDATE SKIP
  LOCKED``, read state is a partial index, and a claim's span is a ``CHECK`` constraint.
  A fake database would verify none of it.
* **``MOTET_INFERENCE_MODE=fake``, pinned for the whole session.** Invariant 7: no test in
  this repo may make a real vendor call. Set here rather than trusted from the environment,
  so a developer who exported ``real`` in their shell cannot spend money by running pytest.
* **An OTLP collector**, :class:`OtlpCollector`, because "telemetry actually leaves the
  process" has to be asserted from what arrived on a socket rather than from a flag, and
  both ``obs/tests`` and ``api/tests`` need to assert it.

Without ``DATABASE_URL`` the database-backed tests **skip** rather than fail, so a quick
``uv run pytest`` works with no Postgres. CI always has one, so that path is always covered
there — but a green local run without Postgres has not exercised it.
"""

from __future__ import annotations

import gzip
import os
import threading
from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

import psycopg
import pytest

#: Truncated before every test. Ordered for readability only — `CASCADE` handles the
#: dependencies.
#:
#: ``sources`` is deliberately **absent**: migration 0002 seeds ``src_paste``, and
#: truncating the table would take that row with it and break paste-in — Phase 1's only
#: ingestion route — in a way that looks like an application bug. Connected sources added
#: by a test are removed by :data:`_DELETE_NON_SEED_SOURCES` instead.
TABLES = (
    "jobs",
    "source_items",
    "news_items",
    "news_item_sources",
    "episodes",
    "episode_segments",
    "segment_claims",
    "feed_tokens",
    "source_credentials",
    "oauth_states",
    "highlights",
)

#: Sources a test connected, without disturbing the seeded paste source. Cascades to that
#: source's credentials and source items, which is what makes the ordering above harmless.
_DELETE_NON_SEED_SOURCES = "DELETE FROM sources WHERE id <> 'src_paste'"


@pytest.fixture(scope="session", autouse=True)
def _never_call_a_vendor() -> None:
    """Invariant 7, enforced rather than assumed."""
    os.environ["MOTET_INFERENCE_MODE"] = "fake"


@pytest.fixture(scope="session")
def database_url() -> str:
    url = os.environ.get("DATABASE_URL")
    if not url:
        pytest.skip("DATABASE_URL is not set; skipping the database-backed tests")
    return url


@pytest.fixture(scope="session")
def _migrated(database_url: str) -> str:
    from motet_db import migrate

    migrate(database_url)
    return database_url


@pytest.fixture
def db(_migrated: str) -> Iterator[psycopg.Connection[Any]]:
    """A connection to an empty database, truncated before each test.

    Truncated rather than rolled back: the pipeline tests run a worker, which opens its
    *own* connection, so work done inside an uncommitted transaction on this one would be
    invisible to it.
    """
    from motet_db import repo

    with repo.connect(_migrated) as conn:
        conn.execute(f"TRUNCATE {', '.join(TABLES)} RESTART IDENTITY CASCADE")
        conn.execute(_DELETE_NON_SEED_SOURCES)
        conn.commit()
        yield conn


@pytest.fixture
def object_store(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Any:
    """A local object store rooted in the test's own temporary directory."""
    from motet_storage import LocalObjectStore

    monkeypatch.setenv("MOTET_STORAGE_BACKEND", "local")
    monkeypatch.setenv("MOTET_STORAGE_DIR", str(tmp_path / "objects"))
    return LocalObjectStore(root=tmp_path / "objects")


class OtlpCollector:
    """The smallest thing that can honestly be called an OTLP/HTTP endpoint.

    Here rather than in one test package because two need it: `obs/tests` proves the
    wiring exports at all, and `api/tests` proves the API process in particular does. It
    exists because "telemetry works" is the one claim a status flag cannot support — this
    repo reported `telemetry_configured: true` for months from a process with no exporter
    compiled into it, so the only convincing test is one that reads what arrived off the
    wire.

    The decoding lives here as methods rather than as module-level helpers because a test
    module cannot `from conftest import ...`: there is a second `conftest` in
    `voice/tests`, and whichever is imported first wins the name.
    """

    def __init__(self) -> None:
        self.received: list[tuple[str, dict[str, str], bytes]] = []
        collector = self

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self) -> None:  # noqa: N802 — BaseHTTPRequestHandler's spelling
                length = int(self.headers.get("Content-Length", "0"))
                body = self.rfile.read(length)
                if self.headers.get("Content-Encoding") == "gzip":
                    body = gzip.decompress(body)
                collector.received.append((self.path, dict(self.headers), body))
                self.send_response(200)
                self.send_header("Content-Length", "0")
                self.end_headers()

            def log_message(self, *_: Any) -> None:
                """Silence: the stdlib default writes every request to stderr."""

        self._server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)

    def __enter__(self) -> OtlpCollector:
        self._thread.start()
        return self

    def __exit__(self, *_: object) -> None:
        self._server.shutdown()
        self._server.server_close()
        self._thread.join(timeout=5)

    @property
    def endpoint(self) -> str:
        return f"http://127.0.0.1:{self._server.server_port}"

    def bodies(self, path: str) -> list[bytes]:
        return [body for received, _, body in self.received if received == path]

    def headers(self, path: str) -> list[dict[str, str]]:
        return [headers for received, headers, _ in self.received if received == path]

    def spans(self) -> list[Any]:
        """Every span in everything that arrived on `/v1/traces`."""
        from opentelemetry.proto.collector.trace.v1.trace_service_pb2 import (
            ExportTraceServiceRequest,
        )

        found = []
        for body in self.bodies("/v1/traces"):
            request = ExportTraceServiceRequest()
            request.ParseFromString(body)
            found += [
                span
                for resource_spans in request.resource_spans
                for scope_spans in resource_spans.scope_spans
                for span in scope_spans.spans
            ]
        return found

    def trace_resource(self) -> dict[str, str]:
        """The resource attributes the first trace export carried."""
        from opentelemetry.proto.collector.trace.v1.trace_service_pb2 import (
            ExportTraceServiceRequest,
        )

        request = ExportTraceServiceRequest()
        request.ParseFromString(self.bodies("/v1/traces")[0])
        return {
            attribute.key: attribute.value.string_value
            for attribute in request.resource_spans[0].resource.attributes
        }

    def log_bodies(self) -> list[str]:
        """Every log record's message, in arrival order."""
        from opentelemetry.proto.collector.logs.v1.logs_service_pb2 import (
            ExportLogsServiceRequest,
        )

        found = []
        for body in self.bodies("/v1/logs"):
            request = ExportLogsServiceRequest()
            request.ParseFromString(body)
            found += [
                str(record.body.string_value)
                for resource_logs in request.resource_logs
                for scope_logs in resource_logs.scope_logs
                for record in scope_logs.log_records
            ]
        return found

    def metric_names(self) -> list[str]:
        from opentelemetry.proto.collector.metrics.v1.metrics_service_pb2 import (
            ExportMetricsServiceRequest,
        )

        found = []
        for body in self.bodies("/v1/metrics"):
            request = ExportMetricsServiceRequest()
            request.ParseFromString(body)
            found += [
                metric.name
                for resource_metrics in request.resource_metrics
                for scope_metrics in resource_metrics.scope_metrics
                for metric in scope_metrics.metrics
            ]
        return found


@pytest.fixture(scope="module")
def otlp_collector() -> Iterator[OtlpCollector]:
    """A collector for the module, so one subprocess run can serve several assertions."""
    with OtlpCollector() as collector:
        yield collector
