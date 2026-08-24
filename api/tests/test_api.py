"""The API surface: health, authentication, and the routes the SPA and the feed use.

The routes that touch data run against a real Postgres and skip without ``DATABASE_URL``;
everything else runs anywhere. Requests go through ``TestClient``, so the dependency graph,
the response models, and the generated contract are all exercised as a client would meet
them.
"""

from __future__ import annotations

import io
from typing import Any

import feedparser
import podcastparser
import psycopg
import pytest
from fastapi.testclient import TestClient
from motet_api import app
from motet_api.deps import reset_store
from motet_api.obs import ERROR_DSN_ENV, OTLP_ENDPOINT_ENV
from motet_inference.llm import LlmConfigError
from motet_workers import Queue, drain

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
        response = client.get("/healthz")
        assert response.status_code == 200
        assert response.json()["status"] == "ok"

    def test_reports_unconfigured_telemetry_as_unconfigured(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Guards the "no data is not no errors" trap in obs.py."""
        monkeypatch.delenv(OTLP_ENDPOINT_ENV, raising=False)
        monkeypatch.delenv(ERROR_DSN_ENV, raising=False)
        body = client.get("/healthz").json()
        assert body["telemetry_configured"] is False
        assert body["errors_configured"] is False

    def test_reports_configured_telemetry(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv(OTLP_ENDPOINT_ENV, "https://obs.example.invalid/otel/v1")
        monkeypatch.setenv(ERROR_DSN_ENV, "https://public@glitchtip.example.invalid/1")
        body = client.get("/healthz").json()
        assert body["telemetry_configured"] is True
        assert body["errors_configured"] is True

    def test_reports_whether_the_deployment_is_authenticated(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """An open deployment looks exactly like a working one until the bill arrives.

        The same reasoning as the telemetry flags: something silently absent needs a way
        to be asked about.
        """
        monkeypatch.delenv("MOTET_API_TOKEN", raising=False)
        assert client.get("/healthz").json()["authenticated"] is False
        monkeypatch.setenv("MOTET_API_TOKEN", TOKEN)
        assert client.get("/healthz").json()["authenticated"] is True

    def test_an_empty_token_variable_counts_as_unset(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Unset and empty are the same thing in a Cloud Run service definition.

        Treating an empty string as a *token* would mean an empty `Authorization: Bearer`
        header authenticated successfully.
        """
        monkeypatch.setenv("MOTET_API_TOKEN", "   ")
        assert client.get("/healthz").json()["authenticated"] is False


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
            assert started.get("/healthz").status_code == 200

    def test_the_app_does_not_require_the_vendor_key(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """The public service validates LLM config but never holds the key.

        Phase 1 runs every model call in a worker, so requiring ``OPENROUTER_API_KEY`` here
        would mean mounting the one vendor secret in the system into the process most
        exposed to untrusted input, for no functional gain.
        """
        monkeypatch.setenv("MOTET_INFERENCE_MODE", "real")
        monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
        with TestClient(app) as started:
            assert started.get("/healthz").status_code == 200

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
