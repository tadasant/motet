"""The drain trigger: the invoke itself, and the five routes that arm it.

Two halves, and they are on trial for different things.

The first half drives the **real** :class:`CloudRunJobTrigger` over
``httpx.MockTransport``, so what is asserted is the bytes this code puts on a socket — the
method, the regional host, the ``:run`` path, the bearer header, and above all the
**absence of a request body**. A fake trigger could not make that claim, and the body is
the one detail that has already cost four applies to bisect once (see the module
docstring): Cloud Run reported the rejection only to GCP Cloud Logging, which invariant 11
means nothing here can read.

The second half goes through ``TestClient`` against a real Postgres, and asserts that
enqueuing arms exactly one invoke, that it happens **after** the transaction commits, and
that every way the invoke can fail still leaves the user with their 201.

Nothing here reaches Google. ``MOTET_DRAIN_TRIGGER`` is off in CI, so the shipped default
is the inert trigger, and the enabled cases inject their own transport and token.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import httpx
import psycopg
import pytest
from fastapi.testclient import TestClient
from motet_api import app
from motet_api import deps as api_deps
from motet_api import drain as api_drain
from motet_api.config import ConfigError
from motet_api.deps import drain_trigger, reset_drain_trigger, reset_store
from motet_api.drain import (
    ENABLED_ENV,
    JOB_NAME_ENV,
    PROJECT_ENV,
    REGION_ENV,
    CloudRunJobTrigger,
    DrainNudge,
    DrainReason,
    DrainTrigger,
    NullDrainTrigger,
    WorkerJob,
    build_trigger,
    resolve_job,
)
from motet_api.main import HEALTH_PATH
from motet_db import SourceKind, phase2, repo
from motet_sources import FakeMailClient
from motet_workers import Queue, drain

TOKEN = "test-api-token"
AUTH = {"Authorization": f"Bearer {TOKEN}"}

#: A made-up project and a region the estate does not use, on purpose: the real ones are
#: topology, and this repo is public.
ENV = {ENABLED_ENV: "true", PROJECT_ENV: "motet-smoke", REGION_ENV: "europe-west4"}
JOB = WorkerJob(project="motet-smoke", region="europe-west4", name="motet-worker")
RUN_URL = (
    "https://europe-west4-run.googleapis.com/v2/"
    "projects/motet-smoke/locations/europe-west4/jobs/motet-worker:run"
)

#: What Cloud Run answers a `:run` with: a long-running Operation. Nothing reads it — the
#: trigger only cares that the request was accepted — but answering with the real shape
#: keeps the test honest about what the transport is standing in for.
OPERATION = {"name": "projects/motet-smoke/locations/europe-west4/operations/abc123"}


class Recorder:
    """A trigger that remembers what it was asked for. For the route-level cases."""

    def __init__(self, *, enabled: bool = True, boom: Exception | None = None) -> None:
        self.fired: list[DrainReason] = []
        self._enabled = enabled
        self._boom = boom

    @property
    def enabled(self) -> bool:
        return self._enabled

    def fire(self, reason: DrainReason) -> None:
        self.fired.append(reason)
        if self._boom is not None:
            raise self._boom


class MetricSpy:
    """Stands in for ``motet.api.drain_triggers``: every trigger records through it."""

    def __init__(self) -> None:
        self.adds: list[dict[str, str]] = []

    def add(self, amount: int, attributes: dict[str, str] | None = None) -> None:
        self.adds.append(dict(attributes or {}))

    @property
    def outcomes(self) -> list[str]:
        return [a["outcome"] for a in self.adds]


@pytest.fixture
def metric(monkeypatch: pytest.MonkeyPatch) -> MetricSpy:
    spy = MetricSpy()
    monkeypatch.setattr(api_drain, "_drain_triggers", spy)
    return spy


def recording_transport(
    seen: list[httpx.Request], *, status_code: int = 200, body: Any = OPERATION
) -> httpx.MockTransport:
    def handle(request: httpx.Request) -> httpx.Response:
        # Read the body here: `request.content` is what actually went out.
        seen.append(request)
        return httpx.Response(status_code, json=body)

    return httpx.MockTransport(handle)


def enabled_trigger(
    seen: list[httpx.Request], *, status_code: int = 200, body: Any = OPERATION
) -> CloudRunJobTrigger:
    return CloudRunJobTrigger(
        JOB,
        token=lambda: "ya29.fake-token",
        transport=recording_transport(seen, status_code=status_code, body=body),
    )


class TestWhereTheJobIs:
    def test_resolves_from_the_environment(self) -> None:
        """Project from `GOOGLE_CLOUD_PROJECT`, region from its own variable, name defaulted."""
        assert resolve_job(ENV) == JOB

    def test_the_job_name_can_be_overridden(self) -> None:
        job = resolve_job({**ENV, JOB_NAME_ENV: "motet-worker-canary"})
        assert job is not None and job.name == "motet-worker-canary"

    def test_builds_the_regional_run_endpoint(self) -> None:
        """The regional host, not the global one — the shape gcloud has always used."""
        assert JOB.run_url == RUN_URL

    @pytest.mark.parametrize(
        "env",
        [
            pytest.param({ENABLED_ENV: "maybe"}, id="switch-not-a-boolean"),
            pytest.param({ENABLED_ENV: "true", REGION_ENV: "r"}, id="no-project"),
            pytest.param({ENABLED_ENV: "true", PROJECT_ENV: "p"}, id="no-region"),
            pytest.param({**ENV, REGION_ENV: "r/../x"}, id="region-splices-a-path"),
            pytest.param({**ENV, PROJECT_ENV: "p q"}, id="project-with-a-space"),
            pytest.param({**ENV, JOB_NAME_ENV: "jobs/x"}, id="job-name-splices-a-path"),
            pytest.param({**ENV, REGION_ENV: "a:b"}, id="region-with-a-colon"),
            pytest.param({**ENV, REGION_ENV: "x.y"}, id="region-with-a-dot"),
            pytest.param({**ENV, JOB_NAME_ENV: "Motet_Worker"}, id="job-name-not-a-cloud-run-name"),
        ],
    )
    def test_refuses_anything_it_cannot_put_in_a_url(self, env: dict[str, str]) -> None:
        """Strict, because every failure downstream of here is swallowed.

        A half-resolved location would build a URL that 404s inside a call that never
        raises, so the mistake would be invisible for as long as nobody read the metric.
        """
        with pytest.raises(ConfigError):
            resolve_job(env)


class TestTheInvoke:
    """The real adapter over a mock transport: what goes on the wire."""

    def test_posts_to_the_regional_run_endpoint(self) -> None:
        seen: list[httpx.Request] = []
        enabled_trigger(seen).fire(DrainReason.PASTE)
        assert len(seen) == 1
        assert seen[0].method == "POST"
        assert str(seen[0].url) == RUN_URL

    def test_sends_no_request_body(self) -> None:
        """**The expensive one.** An `overrides` body is what Cloud Run silently rejected.

        The worker job declares `args = ["all"]` itself, so an unmodified execution
        already drains every queue and there is nothing a body could usefully carry. A
        `Content-Type` would be the first step back toward one, so it is asserted absent
        too.
        """
        seen: list[httpx.Request] = []
        enabled_trigger(seen).fire(DrainReason.EPISODE)
        assert seen[0].content == b""
        assert seen[0].headers.get("content-type") is None
        assert seen[0].headers.get("content-length") in (None, "0")

    def test_carries_the_ambient_bearer_token(self) -> None:
        seen: list[httpx.Request] = []
        enabled_trigger(seen).fire(DrainReason.SOURCE_POLL)
        assert seen[0].headers["authorization"] == "Bearer ya29.fake-token"

    def test_a_403_reads_as_not_enabled_here_rather_than_an_error(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        """What production answers until its grant is flipped on: expected, so not alarming.

        A WARNING rather than an ERROR, because ERROR is what becomes a GlitchTip event and
        a page per paste for a decision somebody made on purpose is noise. Google's own
        sentence still survives into the line, so a grant that *should* be there is findable.
        """
        seen: list[httpx.Request] = []
        trigger = enabled_trigger(
            seen,
            status_code=403,
            body={"error": {"message": "Permission 'run.jobs.run' denied"}},
        )
        with caplog.at_level(logging.DEBUG, logger="motet.api"):
            trigger.fire(DrainReason.PASTE)
        assert len(seen) == 1
        warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
        assert len(warnings) == 1
        assert "403" in warnings[0].getMessage()
        assert "run.jobs.run" in warnings[0].getMessage(), "the reason has to survive"
        assert not [r for r in caplog.records if r.levelno >= logging.ERROR]

    def test_any_other_rejection_is_an_error(self, caplog: pytest.LogCaptureFixture) -> None:
        """A 500 is not a decision anybody made, so it does page — and still never raises."""
        seen: list[httpx.Request] = []
        trigger = enabled_trigger(
            seen, status_code=500, body={"error": {"message": "backend unavailable"}}
        )
        with caplog.at_level(logging.ERROR, logger="motet.api"):
            trigger.fire(DrainReason.PASTE)
        assert "500" in caplog.text
        assert "backend unavailable" in caplog.text

    def test_a_transport_failure_is_logged_and_swallowed(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        def explode(request: httpx.Request) -> httpx.Response:
            raise httpx.ConnectTimeout("no route to the admin API")

        trigger = CloudRunJobTrigger(
            JOB,
            token=lambda: "ya29.fake-token",
            transport=httpx.MockTransport(explode),
        )
        with caplog.at_level(logging.ERROR, logger="motet.api"):
            trigger.fire(DrainReason.PASTE)
        assert "no route to the admin API" in caplog.text

    def test_a_credential_failure_is_logged_and_swallowed(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        """The metadata server refusing is the realistic shape of a missing grant chain."""
        seen: list[httpx.Request] = []

        def no_token() -> str:
            raise RuntimeError("ambient credentials produced no access token")

        trigger = CloudRunJobTrigger(JOB, token=no_token, transport=recording_transport(seen))
        with caplog.at_level(logging.ERROR, logger="motet.api"):
            trigger.fire(DrainReason.PASTE)
        assert seen == [], "no request should be attempted without a token"
        assert "ambient credentials" in caplog.text


class TestTheMetric:
    """``motet.api.drain_triggers{outcome}`` is how anyone learns whether asks succeed."""

    def test_an_accepted_ask_is_fired(self, metric: MetricSpy) -> None:
        enabled_trigger([]).fire(DrainReason.PASTE)
        assert metric.adds == [{"reason": "paste", "outcome": "fired"}]

    def test_a_403_is_denied(self, metric: MetricSpy) -> None:
        enabled_trigger([], status_code=403, body={"error": {}}).fire(DrainReason.EPISODE)
        assert metric.adds == [{"reason": "episode", "outcome": "denied"}]

    def test_any_other_rejection_is_failed(self, metric: MetricSpy) -> None:
        enabled_trigger([], status_code=500, body={"error": {}}).fire(DrainReason.SOURCE_POLL)
        assert metric.adds == [{"reason": "source_poll", "outcome": "failed"}]

    def test_a_transport_failure_is_failed(self, metric: MetricSpy) -> None:
        def explode(request: httpx.Request) -> httpx.Response:
            raise httpx.ConnectTimeout("no route")

        trigger = CloudRunJobTrigger(
            JOB, token=lambda: "ya29.fake-token", transport=httpx.MockTransport(explode)
        )
        trigger.fire(DrainReason.PASTE)
        assert metric.outcomes == ["failed"]

    def test_the_off_switch_is_counted_as_disabled(self, metric: MetricSpy) -> None:
        """So "nothing enqueued" and "nothing nudging" are different series."""
        NullDrainTrigger().fire(DrainReason.SMART_EPISODE)
        assert metric.adds == [{"reason": "smart_episode", "outcome": "disabled"}]

    def test_a_trigger_that_raises_is_counted_as_failed(self, metric: MetricSpy) -> None:
        nudge = DrainNudge(Recorder(boom=RuntimeError("broken")))
        nudge.arm(DrainReason.PASTE)
        nudge.fire()
        assert metric.adds == [{"reason": "paste", "outcome": "failed"}]


class TestTheGate:
    """Inert until an environment opts in — requirement two of the issue."""

    def test_unset_means_off(self) -> None:
        assert build_trigger({}).enabled is False
        assert isinstance(build_trigger({}), NullDrainTrigger)

    @pytest.mark.parametrize("value", ["", "   ", "false", "0", "off", "No"])
    def test_anything_falsy_means_off(self, value: str) -> None:
        """Unset and empty are the same thing in a Cloud Run service definition."""
        assert build_trigger({**ENV, ENABLED_ENV: value}).enabled is False

    def test_a_project_alone_opts_nothing_in(self) -> None:
        """Every deployment has `GOOGLE_CLOUD_PROJECT`; production must still stay off."""
        env = {PROJECT_ENV: "motet-smoke", REGION_ENV: "europe-west4"}
        assert build_trigger(env).enabled is False

    def test_opting_in_turns_it_on(self) -> None:
        trigger = build_trigger(ENV)
        assert trigger.enabled is True
        assert isinstance(trigger, CloudRunJobTrigger)
        assert trigger.job.run_url == RUN_URL

    def test_opting_in_without_a_region_is_loud_and_still_off(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        """Off, but never quietly: a deployment that meant to nudge is not a laptop."""
        with caplog.at_level(logging.ERROR, logger="motet.api"):
            trigger = build_trigger({ENABLED_ENV: "true", PROJECT_ENV: "motet-smoke"})
        assert trigger.enabled is False
        assert REGION_ENV in caplog.text

    def test_the_off_trigger_does_nothing_and_does_not_raise(self) -> None:
        NullDrainTrigger().fire(DrainReason.PASTE)


class TestTheNudge:
    """Arming and firing are separate, because commit sits between them."""

    def test_an_unarmed_nudge_fires_nothing(self) -> None:
        recorder = Recorder()
        DrainNudge(recorder).fire()
        assert recorder.fired == []

    def test_one_arm_is_one_invoke_however_often_it_fires(self) -> None:
        recorder = Recorder()
        nudge = DrainNudge(recorder)
        nudge.arm(DrainReason.PASTE)
        nudge.fire()
        nudge.fire()
        assert recorder.fired == [DrainReason.PASTE]


@pytest.fixture
def recorder() -> Iterator[Recorder]:
    """Swap the process trigger for one that remembers, for the duration of a test."""
    recording = Recorder()
    app.dependency_overrides[drain_trigger] = lambda: recording
    try:
        yield recording
    finally:
        app.dependency_overrides.pop(drain_trigger, None)


@pytest.fixture
def api(
    db: psycopg.Connection[Any],
    _migrated: str,
    object_store: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> Iterator[TestClient]:
    monkeypatch.setenv("MOTET_API_TOKEN", TOKEN)
    monkeypatch.setenv("DATABASE_URL", _migrated)
    reset_store()
    reset_drain_trigger()
    with TestClient(app) as started:
        yield started
    reset_store()
    reset_drain_trigger()


def paste(client: TestClient) -> Any:
    return client.post(
        "/v1/sources/paste",
        json={"title": "Acme raises $20M", "text": "Acme raises $20M Series A."},
        headers=AUTH,
    )


class TestTheRoutesThatArmIt:
    """Every enqueue in the API is a user action, and every one of them nudges."""

    def test_paste_nudges(self, api: TestClient, recorder: Recorder) -> None:
        assert paste(api).status_code == 201
        assert recorder.fired == [DrainReason.PASTE]

    def test_creating_an_episode_nudges(self, api: TestClient, recorder: Recorder) -> None:
        response = api.post(
            "/v1/episodes", json={"title": "Morning", "max_duration_ms": 600_000}, headers=AUTH
        )
        assert response.status_code == 201
        assert recorder.fired == [DrainReason.EPISODE]

    def test_creating_a_smart_episode_nudges(self, api: TestClient, recorder: Recorder) -> None:
        response = api.post(
            "/v1/episodes/smart",
            json={
                "title": "Today",
                "max_duration_ms": 600_000,
                "rule": {"unread_only": True, "order": "oldest_first"},
            },
            headers=AUTH,
        )
        assert response.status_code == 201
        assert recorder.fired == [DrainReason.SMART_EPISODE]

    def test_asking_a_source_to_poll_nudges(
        self, api: TestClient, recorder: Recorder, db: psycopg.Connection[Any]
    ) -> None:
        source = phase2.create_source(
            db, user_id=repo.OWNER_USER_ID, kind=SourceKind.GMAIL, name="Inbox"
        )
        phase2.set_source_active(db, source.id, active=True)
        db.commit()

        response = api.post(f"/v1/sources/{source.id}/poll", headers=AUTH)
        assert response.status_code == 200
        assert recorder.fired == [DrainReason.SOURCE_POLL]

    def test_completing_mailbox_consent_nudges(
        self, api: TestClient, recorder: Recorder, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The fifth enqueue site: the OAuth callback queues the mailbox's first poll.

        Walked end to end against the fake provider, so the one nudge is the callback's —
        starting consent writes a state row and enqueues nothing.
        """
        monkeypatch.setenv("MOTET_VAULT_BACKEND", "local")
        started = api.post(
            "/v1/sources/connect",
            json={
                "provider": "gmail",
                "name": "Gmail",
                "redirect_uri": "https://app.example.invalid/oauth/callback",
            },
            headers=AUTH,
        )
        assert started.status_code == 201, started.text
        assert recorder.fired == [], "starting consent enqueues nothing"

        done = api.post(
            "/v1/sources/callback",
            json={"state": started.json()["state"], "code": "fake-auth-code"},
            headers=AUTH,
        )
        assert done.status_code == 200, done.text
        assert recorder.fired == [DrainReason.SOURCE_POLL]

    def test_reading_the_backlog_nudges_nothing(self, api: TestClient, recorder: Recorder) -> None:
        """The nudge is armed by an enqueue, never by a request."""
        assert api.get("/v1/news-items", headers=AUTH).status_code == 200
        assert api.get("/v1/episodes", headers=AUTH).status_code == 200
        assert recorder.fired == []

    def test_a_refused_poll_nudges_nothing(
        self, api: TestClient, recorder: Recorder, db: psycopg.Connection[Any]
    ) -> None:
        """A route refused before it enqueues has nothing to nudge for.

        An inactive source is a 409 before `enqueue_source_poll` runs, so `arm` is never
        reached. The harder case — armed, *then* failed — is
        `TestItNeverFailsTheRequest::test_a_request_that_fails_after_arming_nudges_nothing`.
        """
        source = phase2.create_source(
            db, user_id=repo.OWNER_USER_ID, kind=SourceKind.GMAIL, name="Inbox"
        )
        phase2.set_source_active(db, source.id, active=False)
        db.commit()
        assert api.post(f"/v1/sources/{source.id}/poll", headers=AUTH).status_code == 409
        assert recorder.fired == []


class TestOnlyAUsersRequestFiresIt:
    """The trigger lives in the API's request path, never in the shared enqueue helpers.

    `handle_poll` re-arms a poll from inside the worker. A trigger placed in
    `enqueue_source_poll` would therefore fire from worker code — an execution starting
    another — and "a Cloud Run execution exists because a user did something" would stop
    being true. These pin both halves: behaviourally, and structurally.
    """

    def test_a_worker_side_poll_re_arm_does_not_fire_it(
        self,
        api: TestClient,
        recorder: Recorder,
        db: psycopg.Connection[Any],
        monkeypatch: pytest.MonkeyPatch,
        metric: MetricSpy,
    ) -> None:
        """Watched two ways, because the override alone cannot see the regression.

        `dependency_overrides` only applies where FastAPI resolves a route's dependencies,
        and worker code never does — a trigger wired into `enqueue_source_poll` would call
        `drain_trigger()` directly. So the process-wide trigger *is* the recorder too, and
        the counter every real trigger records through is spied on: nothing the worker
        could reach goes unwatched.
        """
        monkeypatch.setattr(api_deps, "_trigger", recorder)
        monkeypatch.setenv("MOTET_VAULT_BACKEND", "local")
        started = api.post(
            "/v1/sources/connect",
            json={
                "provider": "gmail",
                "name": "Gmail",
                "redirect_uri": "https://app.example.invalid/oauth/callback",
            },
            headers=AUTH,
        )
        assert started.status_code == 201, started.text
        done = api.post(
            "/v1/sources/callback",
            json={"state": started.json()["state"], "code": "fake-auth-code"},
            headers=AUTH,
        )
        assert done.status_code == 200, done.text
        assert recorder.fired == [DrainReason.SOURCE_POLL], "the user's action fires once"

        # A cursor the fake mailbox will declare expired, so the worker takes the branch
        # that re-enqueues a poll on its own.
        phase2.set_source_sync_state(db, started.json()["source_id"], {"cursor": "999"})
        db.commit()
        monkeypatch.setattr(
            "motet_workers.ingest.build_mail_client",
            lambda token, env=None: FakeMailClient(expire_cursor=True),
        )

        assert drain(Queue.POLL, os.environ["DATABASE_URL"]) == 1
        with psycopg.connect(os.environ["DATABASE_URL"]) as other:
            polls = other.execute(
                "SELECT count(*), count(*) FILTER (WHERE state = 'ready') "
                "FROM jobs WHERE queue = 'poll'"
            ).fetchone()
        assert polls is not None and (polls[0], polls[1]) == (2, 1), "the worker re-armed"
        assert recorder.fired == [DrainReason.SOURCE_POLL], "and nothing more fired"
        assert metric.adds == [], "no trigger implementation recorded anything either"

    def test_the_worker_package_cannot_reach_the_trigger(self) -> None:
        """Structural half: `motet-workers` never imports `motet_api`, so it cannot fire it.

        Imports, not mentions — a comment naming the API is fine; a dependency on it would
        also be a cycle, since `motet-api` already depends on `motet-workers`.
        """
        importing = re.compile(r"^\s*(?:from|import)\s+motet_api\b", re.MULTILINE)
        workers_src = Path(__file__).resolve().parents[2] / "workers" / "src"
        offenders = [
            str(path.relative_to(workers_src))
            for path in workers_src.rglob("*.py")
            if importing.search(path.read_text())
        ]
        assert offenders == []


class TestItNeverFailsTheRequest:
    """Requirement one: the enqueue is committed, the invoke is a nudge on top of it."""

    def test_a_raising_trigger_still_returns_201(
        self, api: TestClient, caplog: pytest.LogCaptureFixture
    ) -> None:
        """Belt and braces around `fire`'s own swallow.

        `CloudRunJobTrigger.fire` catches everything it can go wrong with, so this stands
        in for the case it cannot: a bug in the trigger itself. The paste is committed
        before anything is asked to drain, so the user's answer must not change — and the
        paste must still be there afterwards.
        """
        exploding = Recorder(boom=RuntimeError("the trigger itself is broken"))
        app.dependency_overrides[drain_trigger] = lambda: exploding
        try:
            with caplog.at_level(logging.ERROR, logger="motet.api"):
                response = paste(api)
        finally:
            app.dependency_overrides.pop(drain_trigger, None)
        assert response.status_code == 201
        assert exploding.fired == [DrainReason.PASTE]
        assert "the trigger itself is broken" in caplog.text
        pending = api.get("/v1/ingestion", headers=AUTH).json()
        assert [item["id"] for item in pending] == [response.json()["id"]]

    def test_a_request_that_fails_after_arming_nudges_nothing(
        self,
        api: TestClient,
        recorder: Recorder,
        monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """Armed, then failed: the rollback path re-raises, so the fire never happens.

        `create_episode` arms the nudge beside its enqueue and then reads the episode back;
        failing that read is a request that got past the enqueue and still did not commit.
        Asking a worker to drain on its behalf would be a nudge for a job row that does not
        exist — and the row is checked on a separate connection to prove it does not.
        """

        def unreadable(*_: Any, **__: Any) -> None:
            raise RuntimeError("failed after the enqueue")

        monkeypatch.setattr(repo, "get_episode", unreadable)
        with caplog.at_level(logging.ERROR, logger="motet.api"):
            response = api.post(
                "/v1/episodes", json={"title": "Morning", "max_duration_ms": 600_000}, headers=AUTH
            )
        assert response.status_code == 500
        assert recorder.fired == []
        with psycopg.connect(os.environ["DATABASE_URL"]) as other:
            row = other.execute("SELECT count(*) FROM jobs WHERE queue = 'assemble'").fetchone()
        assert row is not None and row[0] == 0, "the enqueue was rolled back"

    def test_the_nudge_fires_before_the_response_starts(self, api: TestClient) -> None:
        """The ordering the whole placement argument rests on, pinned on a raw ASGI `send`.

        FastAPI tears a default-scoped `yield` dependency down *after* the response is
        sent, which on Cloud Run is the CPU-throttled window a background task would also
        have landed in. `connection` is `scope="function"` so the commit and the nudge run
        before `http.response.start`. `TestClient` cannot see this — it hands back a
        response only once the whole exchange is over — so this drives the app directly.
        """
        events: list[str] = []

        class Ordering:
            enabled = True

            def fire(self, reason: DrainReason) -> None:
                events.append("fire")

        body = json.dumps({"title": "Acme raises $20M", "text": "Acme raises $20M."}).encode()
        scope: dict[str, Any] = {
            "type": "http",
            "asgi": {"version": "3.0"},
            "http_version": "1.1",
            "method": "POST",
            "scheme": "http",
            "path": "/v1/sources/paste",
            "raw_path": b"/v1/sources/paste",
            "query_string": b"",
            "root_path": "",
            "headers": [
                (b"host", b"testserver"),
                (b"authorization", f"Bearer {TOKEN}".encode()),
                (b"content-type", b"application/json"),
                (b"content-length", str(len(body)).encode()),
            ],
            "client": ("testclient", 50000),
            "server": ("testserver", 80),
        }

        async def receive() -> dict[str, Any]:
            return {"type": "http.request", "body": body, "more_body": False}

        async def send(message: dict[str, Any]) -> None:
            if message["type"] == "http.response.start":
                events.append(f"start {message['status']}")

        app.dependency_overrides[drain_trigger] = Ordering
        try:
            asyncio.run(app(scope, receive, send))
        finally:
            app.dependency_overrides.pop(drain_trigger, None)
        assert events == ["fire", "start 201"]

    def test_a_403_from_cloud_run_still_returns_201_and_keeps_the_work(
        self, api: TestClient
    ) -> None:
        """The realistic failure: the run.invoker grant has not landed yet.

        End to end through the real trigger — a 403 on the wire, a 201 to the caller, and
        the source item still queued for the scheduled sweep to take.
        """
        seen: list[httpx.Request] = []
        app.dependency_overrides[drain_trigger] = lambda: enabled_trigger(
            seen, status_code=403, body={"error": {"message": "denied"}}
        )
        try:
            response = paste(api)
        finally:
            app.dependency_overrides.pop(drain_trigger, None)

        assert response.status_code == 201
        assert len(seen) == 1, "it really did try"
        pending = api.get("/v1/ingestion", headers=AUTH).json()
        assert [item["state"] for item in pending] == ["pending"]

    def test_the_work_is_committed_before_the_invoke(self, api: TestClient) -> None:
        """The ordering that makes the nudge mean anything.

        A worker that started before the transaction committed would sweep an empty queue
        and go home. The trigger reads the table on a **separate connection**, so what it
        sees is what a worker's own claim would see: if the source item is visible from
        outside the request, the commit has already happened.
        """
        visible: list[int] = []

        class CountingTrigger:
            enabled = True

            def fire(self, reason: DrainReason) -> None:
                with psycopg.connect(os.environ["DATABASE_URL"]) as other:
                    row = other.execute(
                        "SELECT count(*) FROM jobs WHERE queue = 'integrate'"
                    ).fetchone()
                    assert row is not None
                    visible.append(int(row[0]))

        app.dependency_overrides[drain_trigger] = CountingTrigger
        try:
            assert paste(api).status_code == 201
        finally:
            app.dependency_overrides.pop(drain_trigger, None)
        assert visible == [1], "the integrate job was already committed when the nudge fired"


class TestHealthReportsIt:
    def test_off_by_default(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv(ENABLED_ENV, raising=False)
        reset_drain_trigger()
        try:
            body = TestClient(app).get(HEALTH_PATH).json()
        finally:
            reset_drain_trigger()
        assert body["drain_trigger"] is False

    def test_on_when_the_environment_opts_in(self, monkeypatch: pytest.MonkeyPatch) -> None:
        for name, value in ENV.items():
            monkeypatch.setenv(name, value)
        reset_drain_trigger()
        try:
            body = TestClient(app).get(HEALTH_PATH).json()
        finally:
            reset_drain_trigger()
        assert body["drain_trigger"] is True

    def test_never_publishes_where_the_job_is(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """This route is unauthenticated. A project id and a region are topology.

        Same argument `revision` makes one field along: the boolean answers the question,
        and the location would answer a question nobody asked on a public URL.
        """
        for name, value in ENV.items():
            monkeypatch.setenv(name, value)
        reset_drain_trigger()
        try:
            body = TestClient(app).get(HEALTH_PATH).text
        finally:
            reset_drain_trigger()
        assert "motet-smoke" not in body
        assert "europe-west4" not in body
        assert json.loads(body)["drain_trigger"] is True


def test_the_protocol_is_satisfied_by_both_implementations() -> None:
    assert isinstance(NullDrainTrigger(), DrainTrigger)
    assert isinstance(enabled_trigger([]), DrainTrigger)
