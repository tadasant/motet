"""`GET /v1/source-items/held` and `POST /v1/source-items/integrate`.

A connected source polls, fetches and extracts on its own and stops before `integrate`,
the first stage that spends inference. What it leaves is a `pending` source item with no
integrate job — the held state, which no column records — and these two routes are how
the owner sees it and how the owner ends it.
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any

import psycopg
import pytest
from fastapi.testclient import TestClient
from motet_api import app
from motet_api.deps import drain_trigger, reset_drain_trigger, reset_store
from motet_api.drain import DrainReason
from motet_db import SourceItemState, SourceKind, phase2, repo
from motet_workers import Queue, jobs

TOKEN = "test-api-token"
AUTH = {"Authorization": f"Bearer {TOKEN}"}


class Recorder:
    """A drain trigger that remembers what it was asked for."""

    def __init__(self) -> None:
        self.fired: list[DrainReason] = []

    @property
    def enabled(self) -> bool:
        return True

    def fire(self, reason: DrainReason) -> None:
        self.fired.append(reason)


@pytest.fixture
def recorder() -> Iterator[Recorder]:
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


def held_item(db: psycopg.Connection[Any], source_id: str, title: str, text: str) -> str:
    """A source item the way `handle_extract` now leaves it: pending, and no job."""
    item_id = phase2.insert_polled_source_item(
        db,
        user_id=repo.OWNER_USER_ID,
        source_id_=source_id,
        external_id=f"msg-{title}",
        title=title,
        text=text,
    )
    assert item_id is not None
    return item_id


def integrate_jobs(db: psycopg.Connection[Any]) -> list[str]:
    with db.cursor() as cur:
        cur.execute(
            "SELECT payload ->> 'source_item_id' AS item FROM jobs WHERE queue = %s ORDER BY id",
            (Queue.INTEGRATE.value,),
        )
        return [row["item"] for row in cur.fetchall()]


@pytest.fixture
def gmail(db: psycopg.Connection[Any]) -> str:
    source = phase2.create_source(
        db, user_id=repo.OWNER_USER_ID, kind=SourceKind.GMAIL.value, name="Inbox"
    )
    return source.id


class TestHeld:
    def test_lists_pending_items_without_a_job_oldest_first(
        self, api: TestClient, db: psycopg.Connection[Any], gmail: str
    ) -> None:
        older = held_item(db, gmail, "Acme raises", "Acme   raises\n\n$20M Series A.  ")
        newer = held_item(db, gmail, "Northbridge", "x" * 500)
        with db.cursor() as cur:
            # Both rows share the transaction's `now()`; make the order unambiguous.
            cur.execute(
                "UPDATE source_items SET created_at = created_at - interval '1 hour' WHERE id = %s",
                (older,),
            )
        db.commit()

        body = api.get("/v1/source-items/held", headers=AUTH).json()
        assert [item["id"] for item in body] == [older, newer]

        first = body[0]
        assert first["title"] == "Acme raises"
        assert first["source_id"] == gmail
        assert first["source_kind"] == "gmail"
        assert first["source_name"] == "Inbox"
        assert first["received_at"]
        assert first["chars"] == len("Acme   raises\n\n$20M Series A.  ")
        assert first["preview"] == "Acme raises $20M Series A.", "whitespace collapsed"
        assert len(body[1]["preview"]) == 200, "and bounded"

    def test_an_item_with_any_integrate_job_is_not_held(
        self, api: TestClient, db: psycopg.Connection[Any], gmail: str
    ) -> None:
        """Queued, running, done or failed: a job row of any state means somebody asked."""
        queued = held_item(db, gmail, "Queued", "text")
        failed = held_item(db, gmail, "Failed", "text")
        still = held_item(db, gmail, "Still held", "text")
        jobs.enqueue(db, Queue.INTEGRATE, {"source_item_id": queued})
        job = jobs.enqueue(db, Queue.INTEGRATE, {"source_item_id": failed})
        with db.cursor() as cur:
            cur.execute("UPDATE jobs SET state = 'failed' WHERE id = %s", (job,))
        db.commit()

        body = api.get("/v1/source-items/held", headers=AUTH).json()
        assert [item["id"] for item in body] == [still]

    def test_a_paste_is_never_held(self, api: TestClient, db: psycopg.Connection[Any]) -> None:
        """Pasting is asking: the job is written in the same transaction as the row."""
        response = api.post(
            "/v1/sources/paste", json={"title": "Pasted", "text": "Pasted text."}, headers=AUTH
        )
        assert response.status_code == 201
        assert api.get("/v1/source-items/held", headers=AUTH).json() == []

    def test_requires_a_caller(self, api: TestClient) -> None:
        assert api.get("/v1/source-items/held").status_code == 401


class TestIntegrate:
    def test_queues_held_items_and_nudges(
        self, api: TestClient, db: psycopg.Connection[Any], gmail: str, recorder: Recorder
    ) -> None:
        one = held_item(db, gmail, "One", "text one")
        two = held_item(db, gmail, "Two", "text two")
        db.commit()

        response = api.post(
            "/v1/source-items/integrate", json={"ids": [one, two, "si_unknown"]}, headers=AUTH
        )
        assert response.status_code == 200, response.text
        assert response.json() == {"queued": 2, "skipped": 1}
        assert recorder.fired == [DrainReason.INTEGRATE]

        # Exactly a paste's job: same queue, same payload, same serialization key.
        with db.cursor() as cur:
            cur.execute(
                "SELECT payload, serialize_key FROM jobs WHERE queue = 'integrate' ORDER BY id"
            )
            rows = cur.fetchall()
        assert {row["payload"]["source_item_id"] for row in rows} == {one, two}
        assert {row["serialize_key"] for row in rows} == {repo.OWNER_USER_ID}

        # And they are no longer held.
        assert api.get("/v1/source-items/held", headers=AUTH).json() == []

    def test_asking_twice_skips_and_writes_no_second_job(
        self, api: TestClient, db: psycopg.Connection[Any], gmail: str, recorder: Recorder
    ) -> None:
        item = held_item(db, gmail, "Once", "text")
        db.commit()

        first = api.post("/v1/source-items/integrate", json={"ids": [item]}, headers=AUTH)
        second = api.post("/v1/source-items/integrate", json={"ids": [item, item]}, headers=AUTH)
        assert first.json() == {"queued": 1, "skipped": 0}
        assert second.json() == {"queued": 0, "skipped": 1}, "duplicates in one request count once"
        assert integrate_jobs(db) == [item]
        assert recorder.fired == [DrainReason.INTEGRATE], "nothing queued, so no nudge"

    def test_an_integrated_item_is_skipped(
        self, api: TestClient, db: psycopg.Connection[Any], gmail: str
    ) -> None:
        item = held_item(db, gmail, "Done", "text")
        with db.cursor() as cur:
            cur.execute("UPDATE source_items SET state = 'integrated' WHERE id = %s", (item,))
        db.commit()

        response = api.post("/v1/source-items/integrate", json={"ids": [item]}, headers=AUTH)
        assert response.json() == {"queued": 0, "skipped": 1}
        assert integrate_jobs(db) == []

    def test_an_empty_list_is_a_validation_error(self, api: TestClient) -> None:
        assert (
            api.post("/v1/source-items/integrate", json={"ids": []}, headers=AUTH).status_code
            == 422
        )


class TestDetail:
    """`GET /v1/source-items/{id}` — PROTOTYPE, the three-stage lifecycle view."""

    def test_a_held_item_has_stage_one_only(
        self, api: TestClient, db: psycopg.Connection[Any], gmail: str
    ) -> None:
        item = held_item(db, gmail, "Acme raises", "Acme raises $20M.")
        db.commit()

        body = api.get(f"/v1/source-items/{item}", headers=AUTH).json()
        assert body["id"] == item
        assert body["state"] == "pending"

        pulled = body["pulled"]
        assert pulled["source_kind"] == "gmail"
        assert pulled["source_name"] == "Inbox"
        assert pulled["external_id"] == "msg-Acme raises"
        assert pulled["text"] == "Acme raises $20M."
        assert pulled["chars"] == len("Acme raises $20M.")
        assert pulled["raw_stored"] is False, "only the extracted text is kept"

        processed = body["processed"]
        assert processed["status"] == "held"
        assert processed["job"] is None
        assert processed["outcome"] is None
        assert processed["title"] is None
        assert processed["decision_recorded"] is False
        assert processed["cost_recorded"] is False
        assert body["news_items"] == []

    def test_a_queued_item_reports_its_job(
        self, api: TestClient, db: psycopg.Connection[Any], gmail: str
    ) -> None:
        item = held_item(db, gmail, "Queued", "text")
        job = jobs.enqueue(db, Queue.INTEGRATE, {"source_item_id": item})
        db.commit()

        processed = api.get(f"/v1/source-items/{item}", headers=AUTH).json()["processed"]
        assert processed["status"] == "queued"
        assert processed["job"]["id"] == job
        assert processed["job"]["state"] == "ready"
        assert processed["job"]["attempts"] == 0
        assert processed["job"]["max_attempts"] == jobs.DEFAULT_MAX_ATTEMPTS

    def test_an_integrated_item_links_its_news_item(
        self, api: TestClient, db: psycopg.Connection[Any], gmail: str
    ) -> None:
        first = held_item(db, gmail, "First write-up", "Acme raised.")
        second = held_item(db, gmail, "Second write-up", "Acme raised, again.")
        news_item = repo.insert_news_item(
            db, user_id=repo.OWNER_USER_ID, title="Acme raises", summary="s", source_item_id_=first
        )
        repo.merge_source_into_news_item(
            db,
            news_item_id_=news_item,
            source_item_id_=second,
            title="Acme raises $20M",
            summary="s2",
        )
        for item in (first, second):
            job = jobs.enqueue(db, Queue.INTEGRATE, {"source_item_id": item})
            with db.cursor() as cur:
                cur.execute("UPDATE jobs SET state = 'done' WHERE id = %s", (job,))
            repo.mark_source_item(db, item, SourceItemState.INTEGRATED)
        db.commit()

        body = api.get(f"/v1/source-items/{second}", headers=AUTH).json()
        assert body["state"] == "integrated"
        assert body["processed"]["status"] == "done"
        assert body["processed"]["outcome"] == "merged"
        assert body["processed"]["title"] == "Acme raises $20M"
        assert body["processed"]["integrated_at"]
        assert body["news_items"] == [
            {
                "id": news_item,
                "title": "Acme raises $20M",
                "summary": "s2",
                "read": False,
                "source_count": 2,
                "position": 1,
            }
        ]
        assert api.get(f"/v1/source-items/{first}", headers=AUTH).json()["processed"][
            "outcome"
        ] == ("new")

        # And the backlog now names its sources, so a list can open them.
        [listed] = api.get("/v1/news-items", headers=AUTH).json()
        assert listed["sources"] == [
            {"id": first, "title": "First write-up"},
            {"id": second, "title": "Second write-up"},
        ]

    def test_unknown_or_someone_elses_item_is_404(self, api: TestClient) -> None:
        assert api.get("/v1/source-items/si_nope", headers=AUTH).status_code == 404
