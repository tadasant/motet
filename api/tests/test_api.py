"""The API surface: health, authentication, and the routes the SPA and the feed use.

The routes that touch data run against a real Postgres and skip without ``DATABASE_URL``;
everything else runs anywhere. Requests go through ``TestClient``, so the dependency graph,
the response models, and the generated contract are all exercised as a client would meet
them.
"""

from __future__ import annotations

import io
import json
from typing import Any

import feedparser
import podcastparser
import psycopg
import pytest
from fastapi.testclient import TestClient
from motet_api import app
from motet_api.deps import reset_store
from motet_api.main import HEALTH_PATH
from motet_api.obs import (
    ERROR_DSN_ENV,
    GLITCHTIP_DSN_ENV,
    OTLP_ENDPOINT_ENV,
    OTLP_HEADERS_ENV,
    OTLP_TOKEN_ENV,
    RESOURCE_ATTRIBUTES_ENV,
)
from motet_db import SourceKind, phase2, repo
from motet_inference.llm import LlmConfigError
from motet_vault import BACKEND_ENV, KMS_KEY_ENV
from motet_workers import DEFAULT_MAX_ATTEMPTS, Queue, drain, jobs
from motet_workers.handlers import source_item_failed

TOKEN = "test-api-token"
AUTH = {"Authorization": f"Bearer {TOKEN}"}

NEWSLETTER = (
    "Acme raises $20M Series A",
    "Acme raises $20M Series A. Acme announced the round on Tuesday, led by Northwind "
    "Ventures, bringing total funding to $31M.",
)
SECOND = (
    "Regulator opens inquiry",
    "Regulator opens inquiry. The agency confirmed an inquiry into data retention.",
)

client = TestClient(app)


@pytest.fixture
def api(
    db: psycopg.Connection[Any],
    _migrated: str,
    object_store: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> Any:
    """An authenticated client against an empty database and a temp object store."""
    monkeypatch.setenv("MOTET_API_TOKEN", TOKEN)
    monkeypatch.setenv("DATABASE_URL", _migrated)
    reset_store()
    with TestClient(app) as started:
        yield started
    reset_store()


def run_pipeline(url: str) -> None:
    for queue in (Queue.INTEGRATE, Queue.ASSEMBLE, Queue.SCRIPT, Queue.TTS):
        drain(queue, url)


class TestHealth:
    def test_is_ok(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv(OTLP_ENDPOINT_ENV, raising=False)
        monkeypatch.delenv(ERROR_DSN_ENV, raising=False)
        response = client.get(HEALTH_PATH)
        assert response.status_code == 200
        assert response.json()["status"] == "ok"

    def test_reports_which_build_is_serving(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """The question a deploy leaves behind: is the pin bump actually live?

        The deploy already puts the commit SHA in `service.version`, so this reads back
        the one value the deploy set rather than inventing a second way to know. The two
        workarounds it replaces both fail on the commit that matters most — a bugfix pin
        bump changes no route, so diffing the served OpenAPI document cannot see it, and
        `motet-api` does no per-request logging, so the obs stack has nothing recent to
        read the label off.
        """
        monkeypatch.setenv(
            RESOURCE_ATTRIBUTES_ENV,
            "service.name=motet-api,service.version=d1177570148f58d30657015ab972d63892700519",
        )
        body = client.get(HEALTH_PATH).json()
        assert body["revision"] == "d1177570148f58d30657015ab972d63892700519"

    def test_an_unnamed_build_reports_null_rather_than_a_guess(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A laptop and a bare `docker run` set no resource attributes.

        Null is the honest answer and the field has to keep it available: a placeholder
        string would be a build label that matches no commit, which is worse than no
        label at all because it looks like one.
        """
        monkeypatch.delenv(RESOURCE_ATTRIBUTES_ENV, raising=False)
        body = client.get(HEALTH_PATH).json()
        assert "revision" in body
        assert body["revision"] is None

    def test_a_build_label_that_is_not_a_commit_sha_is_refused(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The disclosure argument, made structural instead of asserted.

        Nothing in this repo can see what the private infrastructure repo puts in
        `service.version`, and this route is unauthenticated and this repo is public. The
        realistic accident is the full image reference — which carries a project id and a
        registry host — so the route repeats a build label only when it has the shape of
        one. Same reasoning as `vault_ready`'s `detail`, one field along.
        """
        monkeypatch.setenv(
            RESOURCE_ATTRIBUTES_ENV,
            "service.version=europe-west1-docker.pkg.dev/a-project/motet/api:abc123",
        )
        body = client.get(HEALTH_PATH).json()
        assert body["revision"] is None
        assert "a-project" not in json.dumps(body)

    @pytest.mark.parametrize(
        "value",
        [
            "d1177570148f58d30657015ab972d63892700519",  # the deploy's usual value
            "bootstrap",  # the deploy's own sentinel, before any image was built
            "abc1234",  # a short SHA
        ],
    )
    def test_a_build_label_shaped_like_one_is_published(
        self, value: str, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The refusal above must not be so tight that it eats the real values."""
        monkeypatch.setenv(RESOURCE_ATTRIBUTES_ENV, f"service.version={value}")
        assert client.get(HEALTH_PATH).json()["revision"] == value

    def test_the_build_is_reported_even_when_telemetry_is_not_wired(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A revision whose telemetry is broken is exactly when the build label matters.

        It is read out of the resource attributes, not off the exporter, so a missing
        ingest token cannot also take away the answer to "which build am I asking?".
        """
        for name in (OTLP_ENDPOINT_ENV, OTLP_HEADERS_ENV, OTLP_TOKEN_ENV):
            monkeypatch.delenv(name, raising=False)
        monkeypatch.setenv(RESOURCE_ATTRIBUTES_ENV, "service.version=abc123")
        body = client.get(HEALTH_PATH).json()
        assert body["telemetry_configured"] is False
        assert body["revision"] == "abc123"

    def test_reports_unconfigured_telemetry_as_unconfigured(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Guards the "no data is not no errors" trap in obs.py."""
        for name in (OTLP_ENDPOINT_ENV, OTLP_HEADERS_ENV, OTLP_TOKEN_ENV):
            monkeypatch.delenv(name, raising=False)
        monkeypatch.delenv(ERROR_DSN_ENV, raising=False)
        monkeypatch.delenv(GLITCHTIP_DSN_ENV, raising=False)
        body = client.get(HEALTH_PATH).json()
        assert body["telemetry_configured"] is False
        assert body["errors_configured"] is False
        assert body["telemetry_exporting"] is False

    def test_reports_configured_telemetry(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv(OTLP_ENDPOINT_ENV, "https://obs.example.invalid/otel/v1")
        monkeypatch.setenv(OTLP_HEADERS_ENV, "Authorization=Bearer not-a-real-token")
        monkeypatch.setenv(ERROR_DSN_ENV, "https://public@glitchtip.example.invalid/1")
        body = client.get(HEALTH_PATH).json()
        assert body["telemetry_configured"] is True
        assert body["errors_configured"] is True
        # Configured is not exporting: this process never called `obs.configure()` with
        # those variables set, and saying otherwise is the exact confusion issue #9 was
        # about. `api/tests/test_telemetry.py` is where a process that *did* is checked.
        assert body["telemetry_exporting"] is False

    def test_an_endpoint_without_a_credential_is_not_configured(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The most expensive way to be wrong about telemetry.

        obs rejects an unauthenticated export, so an endpoint on its own buys a 401 per
        export rather than data — and a health check that called that "configured" would
        confirm the belief that made it happen.
        """
        monkeypatch.setenv(OTLP_ENDPOINT_ENV, "https://obs.example.invalid/otel/v1")
        monkeypatch.delenv(OTLP_HEADERS_ENV, raising=False)
        monkeypatch.delenv(OTLP_TOKEN_ENV, raising=False)
        assert client.get(HEALTH_PATH).json()["telemetry_configured"] is False

    def test_the_raw_ingest_token_configures_telemetry(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Secret Manager can only inject the secret under its own name.

        The CI identity that applies the infrastructure cannot read a secret value back,
        so it cannot compose the `Authorization=Bearer <token>` string itself. Accepting
        the raw token is what makes the deployed wiring work at all.
        """
        monkeypatch.setenv(OTLP_ENDPOINT_ENV, "https://obs.example.invalid/otel/v1")
        monkeypatch.delenv(OTLP_HEADERS_ENV, raising=False)
        monkeypatch.setenv(OTLP_TOKEN_ENV, "not-a-real-token")
        assert client.get(HEALTH_PATH).json()["telemetry_configured"] is True

    def test_the_glitchtip_dsn_name_configures_errors(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """`GLITCHTIP_DSN` is the name the secret was actually placed under."""
        monkeypatch.delenv(ERROR_DSN_ENV, raising=False)
        monkeypatch.setenv(GLITCHTIP_DSN_ENV, "https://public@glitchtip.example.invalid/1")
        assert client.get(HEALTH_PATH).json()["errors_configured"] is True

    def test_reports_whether_the_deployment_is_authenticated(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """An open deployment looks exactly like a working one until the bill arrives.

        The same reasoning as the telemetry flags: something silently absent needs a way
        to be asked about.
        """
        monkeypatch.delenv("MOTET_API_TOKEN", raising=False)
        assert client.get(HEALTH_PATH).json()["authenticated"] is False
        monkeypatch.setenv("MOTET_API_TOKEN", TOKEN)
        assert client.get(HEALTH_PATH).json()["authenticated"] is True

    def test_an_empty_token_variable_counts_as_unset(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Unset and empty are the same thing in a Cloud Run service definition.

        Treating an empty string as a *token* would mean an empty `Authorization: Bearer`
        header authenticated successfully.
        """
        monkeypatch.setenv("MOTET_API_TOKEN", "   ")
        assert client.get(HEALTH_PATH).json()["authenticated"] is False

    def test_reports_whether_a_mailbox_credential_could_be_sealed(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The flag that would have made the Gmail-connect bug a five-minute diagnosis.

        The vault is exercised exactly once per mailbox — by a human, at the end of a
        consent flow — so a deployment that cannot seal and one nobody has asked to seal
        for look identical from outside. Same reasoning as `authenticated` and
        `login_configured`.
        """
        monkeypatch.delenv(BACKEND_ENV, raising=False)
        body = client.get(HEALTH_PATH).json()
        assert (body["vault_backend"], body["vault_ready"]) == ("local", True)

    def test_the_local_vault_is_not_ready_in_real_mode(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A deployed environment holding the KEK in its own memory satisfies none of
        invariant 8, so the vault refuses — and health has to say so rather than leaving
        it to be discovered by the one person who clicks Connect."""
        monkeypatch.delenv(BACKEND_ENV, raising=False)
        monkeypatch.setenv("MOTET_INFERENCE_MODE", "real")
        body = client.get(HEALTH_PATH).json()
        assert (body["vault_backend"], body["vault_ready"]) == ("local", False)

    def test_the_kms_vault_needs_a_key_to_be_ready(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv(BACKEND_ENV, "kms")
        monkeypatch.setenv("MOTET_INFERENCE_MODE", "real")
        monkeypatch.delenv(KMS_KEY_ENV, raising=False)
        assert client.get(HEALTH_PATH).json()["vault_ready"] is False
        monkeypatch.setenv(KMS_KEY_ENV, "projects/x/locations/y/keyRings/z/cryptoKeys/k")
        body = client.get(HEALTH_PATH).json()
        assert (body["vault_backend"], body["vault_ready"]) == ("kms", True)

    def test_the_key_path_is_never_in_the_response(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """This route is unauthenticated and this repo is public: a KMS key path is
        infrastructure topology and must not leak through either."""
        monkeypatch.setenv(BACKEND_ENV, "kms")
        monkeypatch.setenv(KMS_KEY_ENV, "projects/secret-proj/locations/y/keyRings/z/cryptoKeys/k")
        assert "secret-proj" not in client.get(HEALTH_PATH).text


class TestAuthentication:
    @pytest.mark.parametrize(
        ("method", "path"),
        [
            ("post", "/v1/sources/paste"),
            ("get", "/v1/news-items"),
            ("get", "/v1/episodes"),
            ("post", "/v1/episodes"),
            ("get", "/v1/feed"),
            ("post", "/v1/feed/rotate"),
        ],
    )
    def test_every_v1_route_refuses_an_unauthenticated_request(
        self, api: TestClient, method: str, path: str
    ) -> None:
        kwargs = {"json": {}} if method == "post" else {}
        assert getattr(api, method)(path, **kwargs).status_code == 401

    def test_a_wrong_token_is_refused(self, api: TestClient) -> None:
        response = api.get("/v1/news-items", headers={"Authorization": "Bearer nope"})
        assert response.status_code == 401

    def test_a_malformed_authorization_header_is_refused(self, api: TestClient) -> None:
        assert api.get("/v1/news-items", headers={"Authorization": TOKEN}).status_code == 401

    def test_the_feed_refuses_an_unknown_token(self, api: TestClient) -> None:
        assert api.get("/feed.xml", params={"token": "nope"}).status_code == 401
        assert api.get("/feed.xml").status_code == 401


class TestValidation:
    def test_request_models_are_enforced(self, api: TestClient) -> None:
        assert (
            api.post("/v1/sources/paste", json={"title": "", "text": ""}, headers=AUTH).status_code
            == 422
        )
        assert (
            api.post(
                "/v1/episodes", json={"title": "t", "max_duration_ms": 0}, headers=AUTH
            ).status_code
            == 422
        )


class TestEndToEnd:
    def test_paste_backlog_episode_feed_audio(
        self, api: TestClient, db: psycopg.Connection[Any], _migrated: str
    ) -> None:
        """The whole Phase 1 path, as a client meets it."""
        for title, text in (NEWSLETTER, SECOND):
            created = api.post(
                "/v1/sources/paste", json={"title": title, "text": text}, headers=AUTH
            )
            assert created.status_code == 201
            assert created.json()["state"] == "pending"

        # Nothing appears until a worker runs: ingestion is serialized per user
        # (invariant 6) and never happens on the request thread.
        assert api.get("/v1/news-items", headers=AUTH).json() == []
        run_pipeline(_migrated)

        items = api.get("/v1/news-items", headers=AUTH).json()
        assert len(items) == 2
        assert all(item["read"] is False for item in items)

        created = api.post(
            "/v1/episodes",
            json={"title": "Morning briefing", "max_duration_ms": 1_200_000},
            headers=AUTH,
        )
        assert created.status_code == 201
        episode_id = created.json()["id"]
        assert created.json()["state"] == "pending"

        run_pipeline(_migrated)
        episode = api.get(f"/v1/episodes/{episode_id}", headers=AUTH).json()
        assert episode["state"] == "ready"
        assert episode["duration_ms"] > 0
        assert episode["audio_bytes"] > 0

        # Invariant 3, as the episode screen renders it: every claim next to the source
        # text it is answerable to.
        assert episode["segments"]
        for segment in episode["segments"]:
            assert segment["claims"]
            for claim in segment["claims"]:
                assert claim["source_excerpt"]
                assert claim["span"]["end"] > claim["span"]["start"]
                assert claim["source_title"]

        feed_info = api.get("/v1/feed", headers=AUTH).json()
        assert feed_info["token"] in feed_info["url"]

        feed = api.get("/feed.xml", params={"token": feed_info["token"]})
        assert feed.status_code == 200
        assert feed.headers["content-type"].startswith("application/rss+xml")

        parsed = podcastparser.parse("https://motet.example/feed.xml", io.StringIO(feed.text))
        (entry,) = parsed["episodes"]
        assert entry["guid"] == episode_id
        (enclosure,) = entry["enclosures"]
        assert enclosure["file_size"] == episode["audio_bytes"]

        assert feedparser.parse(feed.text).bozo is False

        # And the enclosure URL a client would follow actually returns the bytes.
        audio = api.get(enclosure["url"].replace("https://motet.example", ""))
        assert audio.status_code == 200
        assert len(audio.content) == episode["audio_bytes"]

        # Listening marks every story in the episode read — the same column the backlog
        # toggle writes (invariant 5).
        listened = api.post(f"/v1/episodes/{episode_id}/listened", headers=AUTH).json()
        assert listened["news_items_marked_read"] == len(episode["segments"])
        assert all(item["read"] for item in api.get("/v1/news-items", headers=AUTH).json())

    def test_a_paste_is_visible_while_it_is_pending_and_after_it_lands(
        self, api: TestClient, _migrated: str
    ) -> None:
        """The gap this route closes: between accepting a paste and showing a news item.

        Before it, that gap was invisible from every surface the SPA has — the backlog
        lists news items, and an item that fails ingestion never becomes one. So a paste
        that failed was accepted, retried, abandoned, and then existed only as rows nobody
        could see.
        """
        created = api.post(
            "/v1/sources/paste",
            json={"title": NEWSLETTER[0], "text": NEWSLETTER[1]},
            headers=AUTH,
        ).json()

        (pending,) = api.get("/v1/ingestion", headers=AUTH).json()
        assert pending["id"] == created["id"]
        assert pending["state"] == "pending"
        assert pending["attempts"] == 0
        assert pending["last_error"] is None
        # Reported rather than restated: "attempt 3 of 5" is only useful if the 5 is the
        # number the queue is actually counting to.
        assert pending["max_attempts"] == DEFAULT_MAX_ATTEMPTS

        drain(Queue.INTEGRATE, _migrated)

        (done,) = api.get("/v1/ingestion", headers=AUTH).json()
        assert done["state"] == "integrated"
        assert len(api.get("/v1/news-items", headers=AUTH).json()) == 1

    def test_an_item_the_pipeline_gave_up_on_reports_why(
        self, api: TestClient, db: psycopg.Connection[Any], _migrated: str
    ) -> None:
        """A failure has to arrive with its reason attached.

        "Failed" on its own sends the reader to an agent or to the person who built this;
        the vendor's own words let them decide whether to wait, re-paste, or report it.
        """
        api.post(
            "/v1/sources/paste",
            json={"title": NEWSLETTER[0], "text": NEWSLETTER[1]},
            headers=AUTH,
        )
        # The pair the runner writes when a stage stops being retried — the real path,
        # not a shape invented for the test. See `motet_workers.loop._run_job`.
        error = "ReasoningNotAppliedError: dedup returned no reasoning evidence"
        job = jobs.claim(db, Queue.INTEGRATE)
        assert job is not None
        assert jobs.fail(db, job, error, max_attempts=0) is False
        source_item_failed(db, job.payload, error)
        db.commit()

        (failed,) = api.get("/v1/ingestion", headers=AUTH).json()
        assert failed["state"] == "failed"
        assert "ReasoningNotAppliedError" in failed["last_error"]
        assert failed["attempts"] == 1
        assert failed["next_attempt_at"] is None
        # And it is still nowhere in the backlog, which is the reason it needed somewhere
        # else to be.
        assert api.get("/v1/news-items", headers=AUTH).json() == []

    def test_a_mailbox_message_that_never_extracted_is_reported_too(
        self, api: TestClient, db: psycopg.Connection[Any], _migrated: str
    ) -> None:
        """motet#35: for part of its life a polled message has no ``source_items`` row.

        Extraction is what writes that row, so a message that could not be fetched or
        parsed has only its job row — and the poll cursor moved past it in the same
        transaction that queued the fetch, so nothing will look at it again. Reported from
        source items alone it was invisible on every surface the user has, exactly as a
        failed paste was before this route existed.
        """
        source = phase2.create_source(
            db, user_id=repo.OWNER_USER_ID, kind=SourceKind.GMAIL.value, name="Gmail"
        )
        error = "PermanentFailure: source needs reconnecting: invalid_grant"
        db.execute(
            """
            INSERT INTO jobs (queue, payload, attempts, state, last_error)
            VALUES ('extract', %s::jsonb, 5, 'failed', %s)
            """,
            (json.dumps({"source_id": source.id, "message_id": "18f2a3b4c5"}), error),
        )
        db.commit()

        (failed,) = api.get("/v1/ingestion", headers=AUTH).json()
        assert failed["state"] == "failed"
        assert failed["attempts"] == 5
        assert failed["last_error"] == error
        assert failed["next_attempt_at"] is None
        # Named by the message id, because reading the subject line is what failed.
        assert failed["title"] == "Gmail message 18f2a3b4c5"
        # And by the route it came in on, which is what says whether re-pasting is a
        # repair the reader can perform. For a mailbox message it is not.
        assert failed["source_kind"] == "gmail"

    def test_processing_reports_whether_anything_is_draining_the_queue(
        self, api: TestClient, _migrated: str
    ) -> None:
        """motet#38's missing fact, and the reason it is a route rather than a guess.

        `/v1/ingestion` says what is waiting. Nothing said whether anything was coming for
        it, so the SPA told the user "a worker takes it off the queue within a few
        seconds" against a queue no worker had ever touched. Null and a timestamp are two
        different sentences on screen, which is why null is not flattened to an epoch.
        """
        before = api.get("/v1/processing", headers=AUTH).json()
        assert before["worker_last_seen_at"] is None
        assert before["queues"] == []
        # The server's own clock, so the client never ages a database timestamp against a
        # browser one. Present even when nothing has ever run.
        assert before["now"]

        drain(Queue.INTEGRATE, _migrated)

        after = api.get("/v1/processing", headers=AUTH).json()
        assert after["worker_last_seen_at"] is not None
        assert [entry["queue"] for entry in after["queues"]] == ["integrate"]
        assert after["now"] >= after["worker_last_seen_at"]

    def test_processing_reports_the_per_queue_scaling_signal(
        self, api: TestClient, db: psycopg.Connection[Any], _migrated: str
    ) -> None:
        """motet#78's other half: how much is due, and how many workers could take it.

        Depth alone is the wrong number for a serialized queue — three `integrate` jobs for
        one user can employ one worker, not three — so the route carries both. It is on
        this route because it is the only surface that can answer for a queue **no worker
        is running**, which is exactly the queue a scaler has to hear about; the worker's
        gauges go quiet in that case, being emitted by the worker that is not there.
        """
        empty = api.get("/v1/processing", headers=AUTH).json()
        assert [entry["queue"] for entry in empty["readiness"]] == [
            "poll",
            "extract",
            "integrate",
            "assemble",
            "script",
            "tts",
        ]
        assert all(
            (entry["ready"], entry["ready_keys"], entry["blocked_keys"]) == (0, 0, 0)
            for entry in empty["readiness"]
        )

        for _ in range(3):
            jobs.enqueue(
                db, Queue.INTEGRATE, {"source_item_id": "si_x"}, serialize_key=repo.OWNER_USER_ID
            )
        jobs.enqueue(db, Queue.TTS, {"episode_id": "ep_x"})
        db.commit()

        readiness = {
            entry["queue"]: entry
            for entry in api.get("/v1/processing", headers=AUTH).json()["readiness"]
        }
        assert (readiness["integrate"]["ready"], readiness["integrate"]["ready_keys"]) == (3, 1)
        assert (readiness["tts"]["ready"], readiness["tts"]["ready_keys"]) == (1, 1)
        # Nothing holds a key, so nothing is waiting on a worker that already has it.
        assert all(entry["blocked_keys"] == 0 for entry in readiness.values())

    def test_processing_is_behind_the_same_lock_as_everything_else(
        self, api: TestClient, _migrated: str
    ) -> None:
        assert api.get("/v1/processing").status_code == 401

    def test_read_state_round_trips(self, api: TestClient, _migrated: str) -> None:
        api.post(
            "/v1/sources/paste",
            json={"title": NEWSLETTER[0], "text": NEWSLETTER[1]},
            headers=AUTH,
        )
        drain(Queue.INTEGRATE, _migrated)
        item_id = api.get("/v1/news-items", headers=AUTH).json()[0]["id"]

        marked = api.post(f"/v1/news-items/{item_id}/read", json={"read": True}, headers=AUTH)
        assert marked.status_code == 200
        assert marked.json()["read"] is True

        unmarked = api.post(f"/v1/news-items/{item_id}/read", json={"read": False}, headers=AUTH)
        assert unmarked.json()["read"] is False

    def test_unknown_ids_are_404_not_500(self, api: TestClient) -> None:
        assert api.get("/v1/episodes/ep_nope", headers=AUTH).status_code == 404
        assert (
            api.post("/v1/news-items/ni_nope/read", json={"read": True}, headers=AUTH).status_code
            == 404
        )
        assert api.post("/v1/episodes/ep_nope/listened", headers=AUTH).status_code == 404

    def test_audio_is_404_until_the_episode_is_rendered(
        self, api: TestClient, _migrated: str
    ) -> None:
        api.post(
            "/v1/sources/paste",
            json={"title": NEWSLETTER[0], "text": NEWSLETTER[1]},
            headers=AUTH,
        )
        drain(Queue.INTEGRATE, _migrated)
        episode_id = api.post(
            "/v1/episodes", json={"title": "E", "max_duration_ms": 600_000}, headers=AUTH
        ).json()["id"]
        token = api.get("/v1/feed", headers=AUTH).json()["token"]

        response = api.get(f"/v1/episodes/{episode_id}/audio", params={"token": token})
        assert response.status_code == 404

    def test_an_unrendered_episode_is_not_in_the_feed(
        self, api: TestClient, _migrated: str
    ) -> None:
        """A client must never see an enclosure it cannot download."""
        api.post(
            "/v1/sources/paste",
            json={"title": NEWSLETTER[0], "text": NEWSLETTER[1]},
            headers=AUTH,
        )
        drain(Queue.INTEGRATE, _migrated)
        api.post("/v1/episodes", json={"title": "E", "max_duration_ms": 600_000}, headers=AUTH)
        token = api.get("/v1/feed", headers=AUTH).json()["token"]

        feed = api.get("/feed.xml", params={"token": token})
        assert feedparser.parse(feed.text).entries == []


class TestFeedTokens:
    def test_the_url_is_stable_across_requests(self, api: TestClient) -> None:
        """A feed URL is copied to a new device months later; it cannot be a one-shot."""
        first = api.get("/v1/feed", headers=AUTH).json()
        second = api.get("/v1/feed", headers=AUTH).json()
        assert first == second

    def test_rotation_invalidates_the_old_url(self, api: TestClient) -> None:
        old = api.get("/v1/feed", headers=AUTH).json()["token"]
        new = api.post("/v1/feed/rotate", headers=AUTH).json()["token"]

        assert new != old
        assert api.get("/feed.xml", params={"token": old}).status_code == 401
        assert api.get("/feed.xml", params={"token": new}).status_code == 200


class TestStartup:
    def test_the_app_starts_when_inference_is_faked(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Lifespan validation must not need a vendor key in CI or on a laptop."""
        monkeypatch.delenv("MOTET_INFERENCE_MODE", raising=False)
        monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
        with TestClient(app) as started:
            assert started.get(HEALTH_PATH).status_code == 200

    def test_the_app_does_not_require_the_vendor_key(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """The public service validates LLM config but never holds the key.

        Phase 1 runs every model call in a worker, so requiring ``OPENROUTER_API_KEY`` here
        would mean mounting the one vendor secret in the system into the process most
        exposed to untrusted input, for no functional gain.
        """
        monkeypatch.setenv("MOTET_INFERENCE_MODE", "real")
        monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
        with TestClient(app) as started:
            assert started.get(HEALTH_PATH).status_code == 200

    def test_the_app_refuses_to_start_with_an_unknown_model(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("MOTET_INFERENCE_MODE", "real")
        monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or-test")
        monkeypatch.setenv("MOTET_LLM_MODEL", "anthropic/claude-sonnet-99")
        with pytest.raises(LlmConfigError, match="not in the catalog"), TestClient(app):
            pass  # pragma: no cover - startup raises before the body runs

    def test_without_a_database_data_routes_say_so_rather_than_crashing(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv("DATABASE_URL", raising=False)
        monkeypatch.setenv("MOTET_API_TOKEN", TOKEN)
        with TestClient(app) as started:
            assert started.get("/v1/news-items", headers=AUTH).status_code == 503
