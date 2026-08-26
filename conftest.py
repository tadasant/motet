"""Fixtures shared by every test package.

Four things live here because they are cross-cutting:

* **A database of this run's own**, created before collection and dropped at the end. See
  :func:`pytest_configure` — the reason it is a hook rather than a fixture is that the
  variable it rewrites is read at import time.
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

import contextlib
import gzip
import os
import secrets
import threading
import time
from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit, urlunsplit

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
    "auth_sessions",
    # Not user data: one row per queue saying a worker was there. Truncated all the same,
    # so a test that drains a queue cannot make a later test think one is running.
    "worker_heartbeats",
)

#: Sources a test connected, without disturbing the seeded paste source. Cascades to that
#: source's credentials and source items, which is what makes the ordering above harmless.
_DELETE_NON_SEED_SOURCES = "DELETE FROM sources WHERE id <> 'src_paste'"

#: The statement :data:`TABLES` exists for. Named so that ``db/tests/test_isolation.py``
#: can run *this* statement rather than a copy of it that could drift out of step.
TRUNCATE_SQL = f"TRUNCATE {', '.join(TABLES)} RESTART IDENTITY CASCADE"

# --- one database per run ---------------------------------------------------------------
#
# `bin/ci` defaults DATABASE_URL to one fixed name, so every run on a machine used to meet
# in the same tables: two agent sessions, two terminals, a local run beside a CI job. The
# `db` fixture's TRUNCATE is not a private act — it takes AccessExclusiveLock on twelve
# tables and deletes whatever the other run had written. Postgres reports the collision as
# a deadlock against the other run's INSERT and picks one victim (motet#15); the survivor
# then fails somewhere else entirely, as a row that was written and is not there. Which
# tests fail depends on the interleaving, so it reads as a flaky suite rather than as two
# runs sharing a database.
#
# The fix is ownership rather than politeness: a run that owns its database can truncate
# whatever it likes. Retrying the truncate, or taking weaker locks with DELETE, would have
# left both runs deleting each other's rows — quietly, which is worse.

#: Marks a database this file created, and separates the configured name from the parts
#: that make it unique. The trailing component is a unix timestamp, which is what makes
#: the sweep below safe: age alone decides, so it can never take a live run's database.
_RUN_INFIX = "_run"

#: How old a leftover run database must be before a later run drops it. Longer than any
#: run of this suite by orders of magnitude — a leftover only exists because a run was
#: killed between creating its database and dropping it.
_STALE_AFTER_SECONDS = 6 * 60 * 60

#: Postgres's identifier limit. Over it, a name is **truncated rather than refused**, and
#: what is lost is the tail — the timestamp. See :func:`_stem`.
_MAX_IDENTIFIER_BYTES = 63

#: Below this, a trailing number is not a timestamp this code wrote (2001-09-09), so the
#: name it came from is not swept. Guards the sweep against reading a truncated or
#: differently-shaped name as ancient.
_PLAUSIBLE_TIMESTAMP = 1_000_000_000

#: How long to wait for the configured database before giving up and saying so. Short
#: because this runs at configure time on every invocation, including ones that want no
#: database at all.
_CONNECT_TIMEOUT_SECONDS = 5

#: Set this to keep the run's database for a post-mortem instead of dropping it. The name
#: is reported in the header line, so ``psql`` on it is a copy and paste.
KEEP_DATABASE_ENV = "MOTET_TEST_KEEP_DATABASE"

#: What ``DATABASE_URL`` said before this file rewrote it, and what it rewrote it to.
#: Module state because a conftest is imported once per process, and because
#: ``pytest_unconfigure`` has to find the database again to drop it.
_base_database_url: str | None = None
_run_database_url: str | None = None


def _database_name(url: str) -> str:
    return urlsplit(url).path.lstrip("/")


def _with_database(url: str, name: str) -> str:
    return urlunsplit(urlsplit(url)._replace(path=f"/{name}"))


def _stem(base_url: str, suffix_length: int) -> str:
    """The configured database's name, shortened to leave room for a run's suffix.

    **Postgres truncates an identifier over 63 bytes rather than refusing it**, and the
    part that would be lost is the end — which is where the timestamp the sweep below
    reads lives. A name too long by one character therefore reads as created in 1970, and
    the next run drops it as stale while it is in use.
    """
    sanitized = "".join(c for c in _database_name(base_url) if c.isalnum() or c == "_")
    return sanitized[: _MAX_IDENTIFIER_BYTES - suffix_length]


def _connect_admin(base_url: str) -> psycopg.Connection[Any]:
    """Connect to the configured database to create or drop a run's database.

    Connected *to* it, rather than to ``postgres``: the URL a developer set is the one
    connection this code knows works, and a managed Postgres may not offer a maintenance
    database at all.

    The timeout matters because this runs at configure time, on **every** invocation —
    including ``pytest storage/tests``, which needs no database at all. Somebody with
    ``DATABASE_URL`` exported and Postgres stopped should wait a couple of seconds and get
    the warning, not the operating system's TCP timeout.
    """
    return psycopg.connect(base_url, autocommit=True, connect_timeout=_CONNECT_TIMEOUT_SECONDS)


def _create_database(base_url: str) -> str:
    """Create a database for one run of this suite, and return its URL."""
    suffix = f"{_RUN_INFIX}{os.getpid()}_{secrets.token_hex(3)}_{int(time.time())}"
    stem = _stem(base_url, len(suffix))
    name = f"{stem}{suffix}"
    with _connect_admin(base_url) as admin:
        _drop_stale_databases(admin, stem)
        # Not parameterisable — CREATE DATABASE takes an identifier, not a value. Every
        # part of `name` is built above out of the configured name, digits and `_`.
        admin.execute(f'CREATE DATABASE "{name}"')
    return _with_database(base_url, name)


def _drop_stale_databases(admin: psycopg.Connection[Any], stem: str) -> None:
    """Drop run databases left behind by a run that was killed before it could tidy up.

    Age is the only criterion, and it is deliberately generous. A run that is still going
    holds connections to its database, but "has connections" is not a safe test — there is
    a window between ``CREATE DATABASE`` and the first connection where a live database
    looks abandoned.

    A name whose tail is not a plausible timestamp is left alone rather than assumed
    ancient, so a database this code did not name — or one it named before some future
    change to the format — is never dropped on a misreading.
    """
    cutoff = time.time() - _STALE_AFTER_SECONDS
    rows = admin.execute(
        "SELECT datname FROM pg_database WHERE datname LIKE %s", (f"{stem}{_RUN_INFIX}%",)
    ).fetchall()
    for (datname,) in rows:
        _, _, stamp = str(datname).rpartition("_")
        if not stamp.isdigit() or not _PLAUSIBLE_TIMESTAMP <= int(stamp) <= cutoff:
            continue
        # Another run sweeping the same leftover at the same moment is fine and expected.
        with contextlib.suppress(psycopg.Error):
            admin.execute(f'DROP DATABASE "{datname}" WITH (FORCE)')


def _drop_database(base_url: str, url: str) -> None:
    with _connect_admin(base_url) as admin:
        # FORCE rather than waiting: a connection this run leaked would otherwise turn
        # tidying up into a hang at the very end of a green suite.
        admin.execute(f'DROP DATABASE IF EXISTS "{_database_name(url)}" WITH (FORCE)')


def pytest_configure(config: pytest.Config) -> None:
    """Point ``DATABASE_URL`` at a database this run owns.

    A hook rather than a fixture because the variable is read at *import* time —
    ``db/tests/test_migrate.py`` builds a skip marker out of it — and imports happen
    during collection, which is after this and before any fixture. Rewriting the
    environment rather than only the fixture also covers the code that reads it directly:
    ``Settings.from_env``, the worker entry point, and anything a test starts.

    Failing to create the database is not fatal. A developer whose ``DATABASE_URL`` points
    somewhere they cannot create databases keeps the previous behaviour — one shared
    database — and gets told that concurrent runs will collide there.
    """
    global _base_database_url, _run_database_url

    base_url = os.environ.get("DATABASE_URL")
    if not base_url:
        return
    # `--help` and `--collect-only` reach this hook and run no test, so creating a
    # database for them would mean `pytest --help` needing a Postgres to be quick.
    if getattr(config.option, "help", False) or getattr(config.option, "collectonly", False):
        return
    if not urlsplit(base_url).scheme.startswith("postgres"):
        config.issue_config_time_warning(
            pytest.PytestConfigWarning(
                "DATABASE_URL is not a postgres:// URL, so this run cannot be given a "
                "database of its own. Two runs against one database delete each other's "
                "rows — see motet#15."
            ),
            stacklevel=2,
        )
        return

    try:
        _run_database_url = _create_database(base_url)
    except psycopg.Error as exc:
        config.issue_config_time_warning(
            pytest.PytestConfigWarning(
                f"could not create a database for this run ({exc.__class__.__name__}), so "
                f"it will share {_database_name(base_url)!r}. A second run against the "
                "same database deletes this one's rows — see motet#15."
            ),
            stacklevel=2,
        )
        return

    _base_database_url = base_url
    os.environ["DATABASE_URL"] = _run_database_url


def pytest_report_header() -> list[str]:
    """Say which database this is, because the answer is now different every run."""
    if _run_database_url is None:
        return []
    return [f"database: {_database_name(_run_database_url)}"]


def pytest_unconfigure() -> None:
    global _base_database_url, _run_database_url

    if _base_database_url is None or _run_database_url is None:
        return
    if not os.environ.get(KEEP_DATABASE_ENV):
        _drop_database(_base_database_url, _run_database_url)
    os.environ["DATABASE_URL"] = _base_database_url
    _base_database_url = _run_database_url = None


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
def base_database_url() -> str:
    """What ``DATABASE_URL`` said before this run was given a database of its own.

    Only ``db/tests/test_isolation.py`` wants this, and only to prove the two are not the
    same database. Nothing else should reach for it: writing to the configured database is
    exactly what this file stopped doing.
    """
    if _base_database_url is None:
        pytest.skip(
            "this run shares the configured database — see the warning above; there is no "
            "second database to compare against"
        )
    return _base_database_url


@pytest.fixture(scope="session")
def truncate_statement() -> str:
    """The statement the ``db`` fixture runs, so a test can assert on *it* and not a copy."""
    return TRUNCATE_SQL


@pytest.fixture
def blank_database(base_database_url: str) -> Iterator[str]:
    """A created but **unmigrated** database, for the tests that migrate one."""
    url = _create_database(base_database_url)
    try:
        yield url
    finally:
        _drop_database(base_database_url, url)


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

    Truncating is safe because the database belongs to this run and nothing else is in it
    — see :func:`pytest_configure`. It was not safe while every run shared one database,
    and that is motet#15.
    """
    from motet_db import repo

    with repo.connect(_migrated) as conn:
        conn.execute(TRUNCATE_SQL)
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
