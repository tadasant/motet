"""`GET /v1/admin/overview`: the whole deployment at a glance, across every user."""

from __future__ import annotations

from typing import Any

import psycopg
import pytest
from fastapi.testclient import TestClient
from motet_api import app
from motet_api.deps import reset_store
from motet_db import repo
from motet_workers import Queue, jobs

TOKEN = "test-api-token"
AUTH = {"Authorization": f"Bearer {TOKEN}"}


@pytest.fixture
def api(
    db: psycopg.Connection[Any],
    _migrated: str,
    object_store: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> Any:
    monkeypatch.setenv("MOTET_API_TOKEN", TOKEN)
    monkeypatch.setenv("DATABASE_URL", _migrated)
    reset_store()
    with TestClient(app) as started:
        yield started
    reset_store()


class TestAdminOverview:
    def test_reports_users_queues_and_resolved_jobs(
        self, api: TestClient, db: psycopg.Connection[Any]
    ) -> None:
        item = repo.insert_source_item(
            db, user_id=repo.OWNER_USER_ID, title="Acme raises", text="Acme raises $20M."
        )
        job_id = jobs.enqueue(
            db, Queue.INTEGRATE, {"source_item_id": item.id}, serialize_key=repo.OWNER_USER_ID
        )
        repo.record_worker_heartbeat(db, Queue.INTEGRATE.value)
        db.commit()

        body = api.get("/v1/admin/overview", headers=AUTH).json()
        assert body["generated_at"]

        # Every user, every counter, zeros included.
        assert [user["user_id"] for user in body["users"]] == [repo.OWNER_USER_ID]
        owner = body["users"][0]
        assert owner["email"] is None
        assert owner["source_items"] == {"pending": 1, "integrated": 0, "failed": 0}
        assert owner["news_items"] == {"unread": 0, "read": 0}
        assert owner["episodes"] == {
            "pending": 0,
            "scripting": 0,
            "rendering": 0,
            "ready": 0,
            "failed": 0,
        }
        assert owner["jobs"] == {"ready": 1, "running": 0, "done": 0, "failed": 0}

        # Every pipeline queue, in order, even the empty ones.
        assert [queue["queue"] for queue in body["queues"]] == [
            "poll",
            "extract",
            "integrate",
            "assemble",
            "script",
            "tts",
        ]
        by_queue = {queue["queue"]: queue for queue in body["queues"]}
        integrate = by_queue["integrate"]
        assert (
            integrate["ready"],
            integrate["running"],
            integrate["done"],
            integrate["failed"],
        ) == (
            1,
            0,
            0,
            0,
        )
        assert integrate["oldest_ready_age_s"] is not None and integrate["oldest_ready_age_s"] >= 0
        assert integrate["last_heartbeat_at"] is not None
        assert by_queue["tts"]["oldest_ready_age_s"] is None
        assert by_queue["tts"]["last_heartbeat_at"] is None

        # The job is resolved to its user and its subject through the payload.
        assert len(body["jobs"]) == 1
        job = body["jobs"][0]
        assert job["id"] == job_id
        assert (job["queue"], job["state"], job["attempts"]) == ("integrate", "ready", 0)
        assert job["user_id"] == repo.OWNER_USER_ID
        assert job["subject"] == item.id
        assert job["last_error"] is None
        assert job["locked_at"] is None
        assert job["run_at"] and job["created_at"] and job["updated_at"]

    def test_user_filter_narrows_jobs_but_not_aggregates(
        self, api: TestClient, db: psycopg.Connection[Any]
    ) -> None:
        jobs.enqueue(db, Queue.TTS, {"episode_id": "ep_missing"})
        db.commit()

        everyone = api.get("/v1/admin/overview", headers=AUTH).json()
        assert [job["user_id"] for job in everyone["jobs"]] == [None]
        assert everyone["jobs"][0]["subject"] == "ep_missing"

        filtered = api.get("/v1/admin/overview", params={"user_id": "nobody"}, headers=AUTH).json()
        assert filtered["jobs"] == []
        # Aggregates are still for everyone (ages tick between the two calls, so compare counts).
        assert [q["ready"] for q in filtered["queues"]] == [q["ready"] for q in everyone["queues"]]
        assert filtered["users"] == everyone["users"]

    def test_is_behind_the_same_lock_as_everything_else(self, api: TestClient) -> None:
        assert api.get("/v1/admin/overview").status_code == 401
