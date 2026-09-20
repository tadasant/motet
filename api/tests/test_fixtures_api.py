"""The staging test harness: the flag, the boot refusal, and the loop closing.

Three things are pinned here, and they are different kinds of claim.

**The flag and the refusal** are pure configuration and are asserted directly: off means
every route is a 503 that reads nothing, and on-in-production means the process does not
start. The second is asserted through a real ``TestClient`` lifespan rather than by calling
the guard, because "a raise in the lifespan stops the app" is the whole mechanism and
calling the function proves only that the function raises.

**The seal** is asserted by opening it. A route that wrote a row which merely looked
encrypted would pass a test that checked the columns were non-null, so the credential is
unsealed with a real :class:`~motet_vault.KeyManager` — the worker's half — and compared to
the token the environment held. That also pins the AAD, since a wrong one fails to
authenticate rather than returning the wrong bytes.

**The loop** is the deliverable: seed, trigger, drain, assert items appeared, reset, assert
the baseline is clean. It runs against a real Postgres and the real worker, over the fake
mailbox — which serves complete RFC 822 messages, so what the pipeline meets here is the
shape it will meet in staging rather than a convenient stand-in (invariant 9 keeps the
vendor itself out of CI).
"""

from __future__ import annotations

from typing import Any

import psycopg
import pytest
from fastapi.testclient import TestClient
from motet_api import app
from motet_api.deps import reset_store
from motet_api.fixtures import (
    GMAIL_ADDRESS_ENV,
    GMAIL_REFRESH_TOKEN_ENV,
    TEST_FIXTURES_ENV,
    FixturesRefused,
    check_startup,
    fixtures_enabled,
)
from motet_db import CredentialPurpose, phase2, repo
from motet_db import fixtures as fixtures_repo
from motet_sources import LABEL_SYNC_SCOPES
from motet_vault import BACKEND_ENV
from motet_workers import Queue, drain
from motet_workers.queues import PIPELINE

TOKEN = "test-api-token"
AUTH = {"Authorization": f"Bearer {TOKEN}"}
REFRESH_TOKEN = "fixture-refresh-token-for-the-staging-inbox"
MAILBOX = "tadasanttesting@gmail.com"
#: What the fake mailbox says it is. The loop below records *this* rather than
#: :data:`MAILBOX`, because the account check below is real: recording an address the
#: grant does not reach disconnects the source on the first poll, which is the whole
#: point of recording one and is asserted on its own.
FAKE_MAILBOX = "owner@example.invalid"

#: Every harness operation, as ``(method, path, body)``. One list so that the flag-off and
#: the unauthenticated cases are asserted over the whole surface rather than over whichever
#: route somebody remembered — a fifth route added without an entry here is a route those
#: two properties were never claimed for.
HARNESS_CALLS: tuple[tuple[str, str, dict[str, Any] | None], ...] = (
    ("POST", "/v1/testing/gmail-source", {}),
    ("POST", "/v1/testing/reset", None),
    ("POST", "/v1/testing/jobs", {"kind": "gmail_poll"}),
    ("GET", "/v1/testing/jobs/1", None),
)


def _call(client: TestClient, method: str, path: str, body: dict[str, Any] | None) -> Any:
    kwargs: dict[str, Any] = {"headers": AUTH}
    if body is not None:
        kwargs["json"] = body
    return client.request(method, path, **kwargs)


@pytest.fixture
def harness_env(
    db: psycopg.Connection[Any],
    _migrated: str,
    object_store: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> str:
    """A deployment with the harness on, a vault it can seal with, and a test token."""
    monkeypatch.setenv("MOTET_API_TOKEN", TOKEN)
    monkeypatch.setenv("DATABASE_URL", _migrated)
    monkeypatch.setenv("MOTET_INFERENCE_MODE", "fake")
    monkeypatch.setenv(BACKEND_ENV, "local")
    monkeypatch.setenv(TEST_FIXTURES_ENV, "1")
    monkeypatch.setenv(GMAIL_REFRESH_TOKEN_ENV, REFRESH_TOKEN)
    # Both deliberately cleared rather than left: a value in the developer's own shell
    # would otherwise decide which mailbox a seed records, and which environment the
    # boot guard thinks this is.
    monkeypatch.delenv(GMAIL_ADDRESS_ENV, raising=False)
    monkeypatch.delenv("OTEL_RESOURCE_ATTRIBUTES", raising=False)
    reset_store()
    return _migrated


@pytest.fixture
def api(harness_env: str) -> Any:
    with TestClient(app) as started:
        yield started
    reset_store()


# --- the flag ---------------------------------------------------------------------------


def test_the_flag_is_exactly_one_and_nothing_else() -> None:
    for value in ("1", " 1 ", "1\n"):
        assert fixtures_enabled({TEST_FIXTURES_ENV: value}), value
    for value in ("", "0", "true", "yes", "on", "enabled", "2"):
        assert not fixtures_enabled({TEST_FIXTURES_ENV: value}), value
    assert not fixtures_enabled({})


@pytest.mark.parametrize(
    "environment",
    ["production", "prod", "PRODUCTION", " Production ", "prod-eu", "motet-production"],
)
def test_the_harness_refuses_to_start_in_production(
    environment: str, caplog: pytest.LogCaptureFixture
) -> None:
    """Refuses — and says so at ERROR *before* raising, which is what the lifespan's flush
    ships. A refusal that only raised would leave the obs stack with no record of why the
    revision failed; uvicorn's own traceback lands after the log pipeline has closed."""
    with (
        caplog.at_level("ERROR", logger="motet.api.fixtures"),
        pytest.raises(FixturesRefused) as refusal,
    ):
        check_startup(
            {
                TEST_FIXTURES_ENV: "1",
                "OTEL_RESOURCE_ATTRIBUTES": f"deployment.environment={environment}",
            }
        )
    assert TEST_FIXTURES_ENV in str(refusal.value)
    assert any(
        record.levelname == "ERROR" and TEST_FIXTURES_ENV in record.getMessage()
        for record in caplog.records
    ), "the refusal must be logged at ERROR so the flush has something to ship"


def test_a_production_deployment_with_the_flag_off_starts_normally() -> None:
    check_startup(
        {"OTEL_RESOURCE_ATTRIBUTES": "deployment.environment=production"},
    )


@pytest.mark.parametrize(
    "attributes",
    [
        "deployment.environment=staging",
        "deployment.environment.name=staging",
        "service.name=motet-api",  # no environment attribute at all: a laptop, or CI
        "",
    ],
)
def test_everywhere_that_is_not_production_may_run_the_harness(attributes: str) -> None:
    check_startup({TEST_FIXTURES_ENV: "1", "OTEL_RESOURCE_ATTRIBUTES": attributes})


def test_the_app_will_not_boot_with_the_harness_on_in_production(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The refusal through the real lifespan, which is where it has to bite.

    Calling ``check_startup`` proves the function raises. This proves the process does not
    serve — which is the claim the production safety requirement actually makes, and the
    difference between a failed Cloud Run revision and a running one that says no per
    request.
    """
    monkeypatch.setenv(TEST_FIXTURES_ENV, "1")
    monkeypatch.setenv("OTEL_RESOURCE_ATTRIBUTES", "deployment.environment=production")
    with pytest.raises(FixturesRefused), TestClient(app):
        pass  # pragma: no cover - the context manager raises on entry


# --- what the flag gates ------------------------------------------------------------------


@pytest.mark.parametrize(("method", "path", "body"), HARNESS_CALLS)
def test_every_route_is_off_without_the_flag(
    db: psycopg.Connection[Any],
    _migrated: str,
    monkeypatch: pytest.MonkeyPatch,
    method: str,
    path: str,
    body: dict[str, Any] | None,
) -> None:
    monkeypatch.setenv("MOTET_API_TOKEN", TOKEN)
    monkeypatch.setenv("DATABASE_URL", _migrated)
    monkeypatch.delenv(TEST_FIXTURES_ENV, raising=False)
    reset_store()
    with TestClient(app) as client:
        answer = _call(client, method, path, body)
        assert client.get("/internal/health").json()["test_fixtures"] is False
    assert answer.status_code == 503
    assert TEST_FIXTURES_ENV in answer.json()["detail"]


def test_health_reports_the_harness_is_on(api: TestClient) -> None:
    """For ``vault_ready``'s reason: a deployment with the harness on and one without look
    identical from outside, and an agent should be able to ask before it seeds."""
    assert api.get("/internal/health").json()["test_fixtures"] is True


@pytest.mark.parametrize(("method", "path", "body"), HARNESS_CALLS)
def test_every_route_needs_a_bearer_even_with_the_flag_on(
    api: TestClient, method: str, path: str, body: dict[str, Any] | None
) -> None:
    kwargs: dict[str, Any] = {}
    if body is not None:
        kwargs["json"] = body
    assert api.request(method, path, **kwargs).status_code == 401


def _testing_operations() -> set[tuple[str, str]]:
    """Every ``(METHOD, path)`` the app serves under ``/v1/testing``."""
    from fastapi.routing import APIRoute

    return {
        (method, route.path)
        for route in app.routes
        if isinstance(route, APIRoute) and route.path.startswith("/v1/testing")
        for method in route.methods or ()
        if method != "HEAD"
    }


def test_every_testing_route_is_one_the_checks_above_cover() -> None:
    """A fifth route cannot dodge the flag-off and unauthenticated assertions.

    :data:`HARNESS_CALLS` is hand-written, so this is what stops it going stale — the two
    parametrized tests above claim their properties *of the whole surface*, and that is only
    true while the list is the surface. The same shape as ``test_mcp_parity``: a route added
    without a decision about it is a red run.
    """
    listed = {(method, path) for method, path, _ in HARNESS_CALLS}
    declared = _testing_operations()
    # `/v1/testing/jobs/1` stands for the templated path, which is what the app declares.
    listed = {(method, path.replace("/1", "/{job_id}")) for method, path in listed}
    assert declared == listed, (
        f"HARNESS_CALLS and the app disagree: only in app {sorted(declared - listed)}, "
        f"only in the list {sorted(listed - declared)}"
    )


def test_every_testing_route_takes_the_fixtures_dependency() -> None:
    """The walk that stops a fifth route escaping the flag.

    ``/v1/admin`` has the same walk for the same reason: a guard applied per route is a
    guard somebody can forget on the next one, and there is no router-level dependency to
    reach for here (see the note on ``/v1/admin`` in ``main.py``).

    **Matched by name rather than by identity**, which is not fussiness: ``test_deploy_wiring``
    reloads ``motet_api.main`` to assert the middleware stack, and a reload rebinds every
    function in that module while ``app`` keeps routes whose dependants hold the *old*
    objects. An identity check therefore passes alone and fails in a full run — which is
    exactly the shape of flake that gets a real assertion deleted. The behavioural claim is
    the one above: with the flag off, every route in ``HARNESS_CALLS`` is a 503.
    """
    from fastapi.routing import APIRoute

    unguarded = [
        route.path
        for route in app.routes
        if isinstance(route, APIRoute)
        and route.path.startswith("/v1/testing")
        and "require_fixtures"
        not in {
            getattr(dependency.call, "__name__", "") for dependency in route.dependant.dependencies
        }
    ]
    assert not unguarded, f"routes under /v1/testing without the fixtures guard: {unguarded}"


# --- seeding a credential -----------------------------------------------------------------


def test_the_seed_writes_a_credential_the_worker_can_open(
    api: TestClient, db: psycopg.Connection[Any], harness_env: str
) -> None:
    """The row is sealed, and it is sealed the way the OAuth callback seals one.

    Unsealed with a real key manager rather than checked for non-null columns: the AAD is
    bound to ``user_id:source_id:provider``, so a seed that got any of the three wrong
    would write a row that fails to authenticate here rather than one that opens to
    something else.
    """
    answer = api.post("/v1/testing/gmail-source", headers=AUTH, json={"mailbox": MAILBOX})
    assert answer.status_code == 201, answer.text
    body = answer.json()
    assert body["created"] is True
    assert body["mailbox"] == MAILBOX
    assert body["scopes"] == ["https://www.googleapis.com/auth/gmail.readonly"]
    assert body["poll_job"]["queue"] == Queue.POLL.value
    assert body["poll_job"]["state"] == "ready"

    opened = phase2.load_source_credential(
        db,
        _key_manager(),
        source_id_=body["source_id"],
        purpose=CredentialPurpose.REFRESH.value,
    )
    assert opened == REFRESH_TOKEN

    source = phase2.get_source(db, body["source_id"], user_id=repo.OWNER_USER_ID)
    assert source is not None and source.active
    assert source.sync_state["mailbox_address"] == MAILBOX


def _key_manager() -> Any:
    """The worker's half of the vault, for a test that has to open what the API sealed."""
    from motet_vault import build_key_manager

    return build_key_manager()


def test_re_seeding_replaces_the_grant_rather_than_adding_a_mailbox(
    api: TestClient, db: psycopg.Connection[Any]
) -> None:
    first = api.post("/v1/testing/gmail-source", headers=AUTH, json={}).json()
    second = api.post("/v1/testing/gmail-source", headers=AUTH, json={}).json()
    assert second["source_id"] == first["source_id"]
    assert second["created"] is False
    assert len(phase2.list_sources(db, repo.OWNER_USER_ID)) == 2  # the paste source, and this


def test_a_deployment_with_no_refresh_token_says_it_is_not_provisioned(
    api: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Unset is a setup step outstanding, not a seeding fault, and the 503 has to say which.

    Cloud Run will not create a revision whose secret has no enabled version, so the mount
    stays off until a human places the token — which means "unset" is the *expected* state
    of a freshly deployed staging until then. A generic "could not seed" there sends
    somebody to read this code instead of to place the value.
    """
    monkeypatch.delenv(GMAIL_REFRESH_TOKEN_ENV, raising=False)
    answer = api.post("/v1/testing/gmail-source", headers=AUTH, json={})
    assert answer.status_code == 503
    detail = answer.json()["detail"]
    assert GMAIL_REFRESH_TOKEN_ENV in detail
    assert "not provisioned yet" in detail


def test_the_mailbox_defaults_to_the_one_the_deployment_names(
    api: TestClient, db: psycopg.Connection[Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    """A non-secret variable beside the token, so the seed needs no profile call.

    The API talks to no vendor, so it cannot ask Gmail which account the token reaches —
    and recording nothing would give up the account check that makes a wrong token loud.
    """
    monkeypatch.setenv(GMAIL_ADDRESS_ENV, MAILBOX)
    seeded = api.post("/v1/testing/gmail-source", headers=AUTH, json={}).json()
    assert seeded["mailbox"] == MAILBOX
    source = phase2.get_source(db, seeded["source_id"], user_id=repo.OWNER_USER_ID)
    assert source is not None and source.sync_state["mailbox_address"] == MAILBOX


def test_a_request_may_override_the_configured_mailbox(
    api: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(GMAIL_ADDRESS_ENV, MAILBOX)
    seeded = api.post(
        "/v1/testing/gmail-source", headers=AUTH, json={"mailbox": FAKE_MAILBOX}
    ).json()
    assert seeded["mailbox"] == FAKE_MAILBOX


# --- triggering a job, and telling slow from broken ---------------------------------------


def test_a_trigger_returns_a_job_a_caller_can_watch(
    api: TestClient, harness_env: str, db: psycopg.Connection[Any]
) -> None:
    seeded = api.post("/v1/testing/gmail-source", headers=AUTH, json={}).json()
    triggered = api.post("/v1/testing/jobs", headers=AUTH, json={"kind": "gmail_poll"})
    assert triggered.status_code == 201, triggered.text
    job = triggered.json()
    assert job["queue"] == Queue.POLL.value
    assert job["subject_id"] == seeded["source_id"]
    assert job["max_attempts"] == 5

    # No worker has run, and the answer says so rather than leaving it to be inferred from
    # how long the job has been sitting there — which is the whole point of the field.
    watched = api.get(f"/v1/testing/jobs/{job['job_id']}", headers=AUTH).json()
    assert watched["state"] == "ready"
    assert watched["worker_last_seen_at"] is None
    assert watched["worker_fresh"] is False

    drain(Queue.POLL, harness_env)
    after = api.get(f"/v1/testing/jobs/{job['job_id']}", headers=AUTH).json()
    assert after["state"] == "done"
    assert after["worker_fresh"] is True


def test_an_episode_trigger_returns_its_assemble_job(api: TestClient) -> None:
    api.post("/v1/paste", headers=AUTH, json={"title": "Acme", "text": "Acme raised $20M."})
    triggered = api.post(
        "/v1/testing/jobs", headers=AUTH, json={"kind": "episode", "title": "Harness"}
    )
    assert triggered.status_code == 201, triggered.text
    job = triggered.json()
    assert job["queue"] == Queue.ASSEMBLE.value
    assert job["subject_id"] is not None
    assert api.get(f"/v1/testing/jobs/{job['job_id']}", headers=AUTH).status_code == 200


def test_a_poll_trigger_with_no_mailbox_is_refused_rather_than_guessed_at(
    api: TestClient,
) -> None:
    answer = api.post("/v1/testing/jobs", headers=AUTH, json={"kind": "gmail_poll"})
    assert answer.status_code == 422
    assert "0 Gmail sources that would poll" in answer.json()["detail"]


def test_a_poll_trigger_naming_a_source_that_is_not_connected_is_a_404(api: TestClient) -> None:
    answer = api.post(
        "/v1/testing/jobs", headers=AUTH, json={"kind": "gmail_poll", "source_id": "src_paste"}
    )
    assert answer.status_code == 404


def test_a_job_belonging_to_nobody_visible_is_not_readable(
    api: TestClient, db: psycopg.Connection[Any]
) -> None:
    """A job whose subject is gone resolves to no user, and must not be anyone's to read."""
    from motet_workers import enqueue

    job_id = enqueue(db, Queue.ASSEMBLE, {"episode_id": "ep_does_not_exist"})
    db.commit()
    assert api.get(f"/v1/testing/jobs/{job_id}", headers=AUTH).status_code == 404
    assert api.get("/v1/testing/jobs/99999999", headers=AUTH).status_code == 404


# --- the loop ------------------------------------------------------------------------------


def test_the_whole_loop_closes(
    api: TestClient, harness_env: str, db: psycopg.Connection[Any]
) -> None:
    """Seed, poll, ingest, build an episode, reset, and be back at a defined baseline.

    This is the deliverable rather than a smoke test: each of the three capabilities is
    worth little alone, and what the staging loop needs is that they compose — that a reset
    leaves a state the next seed can start from, and that nothing it deleted is left behind
    on a queue to fail later.
    """
    seeded = api.post(
        "/v1/testing/gmail-source", headers=AUTH, json={"mailbox": FAKE_MAILBOX}
    ).json()
    source_id = seeded["source_id"]

    # The poll the seed enqueued, then the chain it produces: poll → extract → a held item.
    for _ in range(3):
        for queue in PIPELINE:
            drain(queue, harness_env)

    poll_job = api.get(f"/v1/testing/jobs/{seeded['poll_job']['job_id']}", headers=AUTH).json()
    assert poll_job["state"] == "done", poll_job["last_error"]

    held = api.get("/v1/source-items/held", headers=AUTH).json()
    assert held, "the fake mailbox's newsletters should be waiting for a person"
    ingested = api.post(
        "/v1/source-items/integrate",
        headers=AUTH,
        json={"ids": [item["id"] for item in held]},
    )
    assert ingested.status_code == 200, ingested.text
    for queue in PIPELINE:
        drain(queue, harness_env)

    news_items = api.get("/v1/news-items", headers=AUTH).json()
    assert news_items, "ingesting the held items should have produced news items"

    episode_job = api.post(
        "/v1/testing/jobs", headers=AUTH, json={"kind": "episode", "title": "Harness"}
    ).json()
    for _ in range(3):
        for queue in PIPELINE:
            drain(queue, harness_env)
    assert api.get(f"/v1/testing/jobs/{episode_job['job_id']}", headers=AUTH).json()["state"] == (
        "done"
    )
    episodes = api.get("/v1/episodes", headers=AUTH).json()
    assert episodes and episodes[0]["state"] == "ready", episodes

    # --- and back to a defined baseline ---
    reset = api.post("/v1/testing/reset", headers=AUTH)
    assert reset.status_code == 200, reset.text
    deleted = reset.json()["deleted"]
    assert deleted["sources"] == 1
    assert deleted["source_items"] == len(held)
    assert deleted["news_items"] == len(news_items)
    assert deleted["episodes"] == 1
    assert deleted["episode_segments"] > 0
    assert deleted["jobs"] > 0

    assert api.get("/v1/news-items", headers=AUTH).json() == []
    assert api.get("/v1/episodes", headers=AUTH).json() == []
    assert api.get("/v1/source-items/held", headers=AUTH).json() == []
    # The paste source survives — deleting the row migration 0002 seeds would take
    # paste-in down in a way that reads as an application bug rather than as a reset.
    assert [source["id"] for source in api.get("/v1/sources", headers=AUTH).json()] == [
        repo.PASTE_SOURCE_ID
    ]
    assert (
        phase2.get_source_credential(
            db, source_id_=source_id, purpose=CredentialPurpose.REFRESH.value
        )
        is None
    )

    # Nothing of this user's is left on a queue to fail on a row that no longer exists.
    assert db.execute("SELECT count(*) AS n FROM jobs").fetchone()["n"] == 0

    # And the baseline is one the next run can start from.
    again = api.post("/v1/testing/gmail-source", headers=AUTH, json={"mailbox": MAILBOX})
    assert again.status_code == 201 and again.json()["created"] is True


def test_a_token_for_the_wrong_inbox_disconnects_rather_than_ingesting_it(
    api: TestClient, harness_env: str, db: psycopg.Connection[Any]
) -> None:
    """Why ``mailbox`` is worth recording, asserted rather than asserted about.

    A refresh token in Secret Manager is a value nobody can read back, so "is it for the
    inbox we think" is exactly the question a seeded source cannot answer for itself. It
    does not have to: the worker asks Gmail which account a grant reaches before reading
    with it (motet#96), and recording the expected address is what turns a wrong token from
    "a different inbox quietly ingested under this source" into a disconnected source with
    both addresses in ``last_error``.
    """
    seeded = api.post("/v1/testing/gmail-source", headers=AUTH, json={"mailbox": MAILBOX}).json()
    drain(Queue.POLL, harness_env)

    source = phase2.get_source(db, seeded["source_id"], user_id=repo.OWNER_USER_ID)
    assert source is not None
    assert not source.active
    assert MAILBOX in (source.last_error or "") and FAKE_MAILBOX in (source.last_error or "")
    assert (
        phase2.get_source_credential(
            db, source_id_=seeded["source_id"], purpose=CredentialPurpose.REFRESH.value
        )
        is None
    ), "the other account's credential must be deleted, not kept"


def test_a_paused_source_is_refused_rather_than_reported_done(
    api: TestClient, harness_env: str, db: psycopg.Connection[Any]
) -> None:
    """The false *green* this surface exists to prevent, asserted.

    ``ingest`` pauses a source on a permanently refused refresh and **keeps** the credential
    — which is the state an OAuth client in "Testing" publishing status produces weekly. A
    poll enqueued against it short-circuits in ``handle_poll`` and the job finishes ``done``
    with no error, so a caller reading state alone is told a mailbox synced that nothing
    opened.
    """
    seeded = api.post("/v1/testing/gmail-source", headers=AUTH, json={}).json()
    phase2.set_source_active(db, seeded["source_id"], active=False)
    phase2.set_source_error(db, seeded["source_id"], "Google rejected the token request (400)")
    db.commit()

    # Named explicitly: a 409 that says why, not a job that would report success.
    explicit = api.post(
        "/v1/testing/jobs",
        headers=AUTH,
        json={"kind": "gmail_poll", "source_id": seeded["source_id"]},
    )
    assert explicit.status_code == 409
    assert "paused" in explicit.json()["detail"]
    assert "Google rejected" in explicit.json()["detail"]

    # And it is not silently picked as "the one connected mailbox" either.
    implicit = api.post("/v1/testing/jobs", headers=AUTH, json={"kind": "gmail_poll"})
    assert implicit.status_code == 422
    assert "1 more hold a credential but are paused" in implicit.json()["detail"]

    # Re-seeding is the repair, and it puts the loop back on its feet.
    assert api.post("/v1/testing/gmail-source", headers=AUTH, json={}).status_code == 201
    assert api.post("/v1/testing/jobs", headers=AUTH, json={"kind": "gmail_poll"}).status_code == (
        201
    )


def test_a_scope_motet_never_asks_google_for_is_refused(api: TestClient) -> None:
    """The recorded scopes are what the worker decides "may this grant write" from."""
    answer = api.post(
        "/v1/testing/gmail-source",
        headers=AUTH,
        json={"scopes": ["https://www.googleapis.com/auth/drive"]},
    )
    assert answer.status_code == 422
    granted = api.post(
        "/v1/testing/gmail-source",
        headers=AUTH,
        json={"scopes": list(LABEL_SYNC_SCOPES)},
    )
    assert granted.status_code == 201
    assert granted.json()["scopes"] == list(LABEL_SYNC_SCOPES)


def test_a_personal_access_token_drives_the_harness_and_survives_a_reset(
    api: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The credential the staging agent actually holds (motet#142), through the whole loop.

    The harness takes ``User`` like every other ``/v1`` route, so a personal access token
    reaches it with nothing added here — that is the integration point, and this is the
    proof it works rather than the assumption. The reset keeps ``api_tokens`` for
    ``auth_sessions``' reason: the agent is *holding* one, and a reset that revoked it
    would answer its own next request with a 401.
    """
    from urllib.parse import parse_qs, urlsplit

    from motet_api.auth import ALLOWED_EMAILS_ENV, FAKE_EMAIL
    from motet_api.config import CALLBACK_PATH

    origin = "https://app.motet.test"
    monkeypatch.setenv("MOTET_APP_BASE_URL", origin)
    monkeypatch.setenv(ALLOWED_EMAILS_ENV, FAKE_EMAIL)
    # A token is minted by a signed-in person, so sign in the way a browser does first.
    started = api.post("/v1/auth/google/start", json={"redirect_uri": f"{origin}{CALLBACK_PATH}"})
    assert started.status_code == 200, started.text
    query = parse_qs(urlsplit(started.json()["authorization_url"]).query)
    signed_in = api.post(
        "/v1/auth/google/callback", json={"state": query["state"][0], "code": query["code"][0]}
    )
    assert signed_in.status_code == 200, signed_in.text
    session = {"Authorization": f"Bearer {signed_in.json()['token']}"}
    minted = api.post("/v1/auth/tokens", json={"label": "staging agent"}, headers=session)
    assert minted.status_code == 201, minted.text
    pat = {"Authorization": f"Bearer {minted.json()['token']}"}

    seeded = api.post("/v1/testing/gmail-source", headers=pat, json={})
    assert seeded.status_code == 201, seeded.text
    triggered = api.post("/v1/testing/jobs", headers=pat, json={"kind": "gmail_poll"})
    assert triggered.status_code == 201, triggered.text
    assert api.get(f"/v1/testing/jobs/{triggered.json()['job_id']}", headers=pat).status_code == 200

    reset = api.post("/v1/testing/reset", headers=pat)
    assert reset.status_code == 200, reset.text
    assert "api_tokens" in reset.json()["kept"]
    # Still authenticated after the reset it just ran — the loop can go round again.
    assert api.get("/v1/auth/session", headers=pat).status_code == 200
    assert api.post("/v1/testing/gmail-source", headers=pat, json={}).status_code == 201


def test_a_reset_keeps_the_session_the_caller_is_holding(
    api: TestClient, db: psycopg.Connection[Any]
) -> None:
    """The harness must not log its own caller out halfway through a run."""
    assert "auth_sessions" in fixtures_repo.RESET_KEEPS
    api.post("/v1/paste", headers=AUTH, json={"title": "Acme", "text": "Acme raised $20M."})
    before = db.execute("SELECT count(*) AS n FROM feed_tokens").fetchone()["n"]
    api.get("/v1/feed", headers=AUTH)
    api.post("/v1/testing/reset", headers=AUTH)
    after = db.execute("SELECT count(*) AS n FROM feed_tokens").fetchone()["n"]
    assert after >= before and after > 0, "a reset must not rotate the feed a client is on"
