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

Nothing here reaches Google. ``MOTET_WORKER_JOB`` is unset in CI, so the shipped default
is the inert trigger, and the enabled cases inject their own transport and token.
"""

from __future__ import annotations

import json
import logging
import os
from collections.abc import Iterator
from typing import Any

import httpx
import psycopg
import pytest
from fastapi.testclient import TestClient
from motet_api import app
from motet_api.config import ConfigError
from motet_api.deps import drain_trigger, reset_drain_trigger, reset_store
from motet_api.drain import (
    WORKER_JOB_ENV,
    CloudRunJobTrigger,
    DrainNudge,
    DrainReason,
    DrainTrigger,
    NullDrainTrigger,
    build_trigger,
    parse_job,
)
from motet_api.main import HEALTH_PATH
from motet_db import SourceKind, phase2, repo

TOKEN = "test-api-token"
AUTH = {"Authorization": f"Bearer {TOKEN}"}

JOB = "projects/motet-smoke/locations/europe-west4/jobs/motet-worker"
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
        parse_job(JOB),
        token=lambda: "ya29.fake-token",
        transport=recording_transport(seen, status_code=status_code, body=body),
    )


class TestResourceNames:
    def test_parses_a_cloud_run_v2_job_name(self) -> None:
        job = parse_job(JOB)
        assert (job.project, job.region, job.name) == (
            "motet-smoke",
            "europe-west4",
            "motet-worker",
        )
        assert job.resource == JOB

    def test_builds_the_regional_run_endpoint(self) -> None:
        """The regional host, not the global one — the shape gcloud has always used."""
        assert parse_job(JOB).run_url == RUN_URL

    @pytest.mark.parametrize(
        "value",
        [
            "motet-worker",
            "projects/p/jobs/motet-worker",
            "projects/p/locations/r/services/motet-worker",
            "projects/p/locations//jobs/motet-worker",
            "projects/p/locations/r/jobs/motet-worker/executions/x",
        ],
    )
    def test_refuses_anything_that_is_not_one(self, value: str) -> None:
        """Strict, because every failure downstream of here is swallowed.

        A half-parsed name would build a URL that 404s inside a call that never raises,
        so the mistake would be invisible for as long as nobody read the metric.
        """
        with pytest.raises(ConfigError):
            parse_job(value)


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

    def test_a_rejection_is_logged_and_swallowed(self, caplog: pytest.LogCaptureFixture) -> None:
        """A 403 is what an environment without the invoker grant gets. It must not raise."""
        seen: list[httpx.Request] = []
        trigger = enabled_trigger(
            seen,
            status_code=403,
            body={"error": {"message": "Permission 'run.jobs.run' denied"}},
        )
        with caplog.at_level(logging.ERROR, logger="motet.api"):
            trigger.fire(DrainReason.PASTE)
        assert len(seen) == 1
        assert "403" in caplog.text
        assert "run.jobs.run" in caplog.text, "the reason has to survive into the log line"

    def test_a_transport_failure_is_logged_and_swallowed(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        def explode(request: httpx.Request) -> httpx.Response:
            raise httpx.ConnectTimeout("no route to the admin API")

        trigger = CloudRunJobTrigger(
            parse_job(JOB),
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

        trigger = CloudRunJobTrigger(
            parse_job(JOB), token=no_token, transport=recording_transport(seen)
        )
        with caplog.at_level(logging.ERROR, logger="motet.api"):
            trigger.fire(DrainReason.PASTE)
        assert seen == [], "no request should be attempted without a token"
        assert "ambient credentials" in caplog.text


class TestTheGate:
    """Inert until an environment opts in — requirement two of the issue."""

    def test_unset_means_off(self) -> None:
        assert build_trigger({}).enabled is False
        assert isinstance(build_trigger({}), NullDrainTrigger)

    def test_blank_means_off(self) -> None:
        """Unset and empty are the same thing in a Cloud Run service definition."""
        assert build_trigger({WORKER_JOB_ENV: "   "}).enabled is False

    def test_a_named_job_turns_it_on(self) -> None:
        trigger = build_trigger({WORKER_JOB_ENV: JOB})
        assert trigger.enabled is True
        assert isinstance(trigger, CloudRunJobTrigger)
        assert trigger.job.run_url == RUN_URL

    def test_an_unusable_value_is_loud_and_still_off(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        """Off, but never quietly: a deployment that meant to nudge is not a laptop."""
        with caplog.at_level(logging.ERROR, logger="motet.api"):
            trigger = build_trigger({WORKER_JOB_ENV: "motet-worker"})
        assert trigger.enabled is False
        assert WORKER_JOB_ENV in caplog.text

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
        """A route that raises after arming rolls back, so it must not nudge either.

        An inactive source is refused with a 409 *before* the enqueue, which is the same
        end state by a shorter road: nothing was queued, so nothing is asked to drain.
        """
        source = phase2.create_source(
            db, user_id=repo.OWNER_USER_ID, kind=SourceKind.GMAIL, name="Inbox"
        )
        phase2.set_source_active(db, source.id, active=False)
        db.commit()
        assert api.post(f"/v1/sources/{source.id}/poll", headers=AUTH).status_code == 409
        assert recorder.fired == []


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
        monkeypatch.delenv(WORKER_JOB_ENV, raising=False)
        reset_drain_trigger()
        try:
            body = TestClient(app).get(HEALTH_PATH).json()
        finally:
            reset_drain_trigger()
        assert body["drain_trigger"] is False

    def test_on_when_a_job_is_named(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv(WORKER_JOB_ENV, JOB)
        reset_drain_trigger()
        try:
            body = TestClient(app).get(HEALTH_PATH).json()
        finally:
            reset_drain_trigger()
        assert body["drain_trigger"] is True

    def test_never_publishes_the_job_resource_name(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """This route is unauthenticated. A project id and a region are topology.

        Same argument `revision` makes one field along: the boolean answers the question,
        and the string would answer a question nobody asked on a public URL.
        """
        monkeypatch.setenv(WORKER_JOB_ENV, JOB)
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
