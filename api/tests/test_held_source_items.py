"""The source-item routes: held, integrate, dismiss, and the lifecycle view (motet#91).

A connected source polls, fetches and extracts on its own and stops before `integrate`,
the first stage that spends inference. What it leaves is a `pending` source item with no
integrate job — the held state, which no column records — and these routes are how the
owner sees it, ends it either way, and reads any item's life as three stages.

Every one of them acts on ids a caller names or returns mailbox text, so each has a test
that another user's item is refused or skipped.
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from typing import Any

import psycopg
import pytest
from fastapi.testclient import TestClient
from motet_api import app
from motet_api.deps import drain_trigger, reset_drain_trigger, reset_store
from motet_api.drain import DrainReason
from motet_api.schemas import SourceItemIdsRequest
from motet_db import SourceItemState, SourceKind, phase2, repo
from motet_workers import Queue, drain, jobs

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


def held_item(
    db: psycopg.Connection[Any],
    source_id: str,
    title: str,
    text: str,
    *,
    user_id: str = repo.OWNER_USER_ID,
    received_at: datetime | None = None,
) -> str:
    """A source item the way `handle_extract` now leaves it: pending, and no job."""
    item_id = phase2.insert_polled_source_item(
        db,
        user_id=user_id,
        source_id_=source_id,
        external_id=f"msg-{title}",
        title=title,
        text=text,
        received_at=received_at,
    )
    assert item_id is not None
    return item_id


def someone_else(db: psycopg.Connection[Any]) -> str:
    """A second user with a mailbox of their own. ``users`` survives the per-test truncate."""
    db.execute("INSERT INTO users (id, email) VALUES ('other', NULL) ON CONFLICT DO NOTHING")
    return phase2.create_source(db, user_id="other", kind=SourceKind.GMAIL.value, name="Theirs").id


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
    def test_lists_pending_items_without_a_job_oldest_message_first(
        self, api: TestClient, db: psycopg.Connection[Any], gmail: str
    ) -> None:
        now = datetime.now(UTC)
        # Stored first, sent second: the order is the message's, not the worker's.
        newer = held_item(db, gmail, "Northbridge", "x" * 500, received_at=now - timedelta(hours=1))
        older = held_item(
            db,
            gmail,
            "Acme raises",
            "Acme   raises\n\n$20M Series A.  ",
            received_at=now - timedelta(days=3),
        )
        db.commit()

        body = api.get("/v1/source-items/held", headers=AUTH).json()
        assert [item["id"] for item in body] == [older, newer]

        first = body[0]
        assert first["title"] == "Acme raises"
        assert first["source_id"] == gmail
        assert first["source_kind"] == "gmail"
        assert first["source_name"] == "Inbox"
        assert datetime.fromisoformat(first["received_at"]) == now - timedelta(days=3)
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

    def test_another_users_held_items_are_not_listed(
        self, api: TestClient, db: psycopg.Connection[Any], gmail: str
    ) -> None:
        held_item(db, someone_else(db), "Theirs", "text", user_id="other")
        mine = held_item(db, gmail, "Mine", "text")
        db.commit()

        assert [item["id"] for item in api.get("/v1/source-items/held", headers=AUTH).json()] == [
            mine
        ]

    def test_requires_a_caller(self, api: TestClient) -> None:
        assert api.get("/v1/source-items/held").status_code == 401
        assert api.post("/v1/source-items/integrate", json={"ids": ["x"]}).status_code == 401
        assert api.post("/v1/source-items/dismiss", json={"ids": ["x"]}).status_code == 401
        assert api.get("/v1/source-items/x").status_code == 401

    def test_one_request_can_name_everything_the_list_returns(self) -> None:
        """ "Select all, ingest" must fit in one request, so the two bounds are one number."""
        bound = SourceItemIdsRequest.model_fields["ids"].metadata
        assert any(getattr(item, "max_length", None) == repo.HELD_MAX_ITEMS for item in bound)


class TestHeldIsNotWork:
    """A held item is a decision still to be made, so no surface may call it in flight."""

    def test_it_is_not_on_the_ingestion_list(
        self, api: TestClient, db: psycopg.Connection[Any], gmail: str
    ) -> None:
        """Reported there it read as "on the way in", and the panel called it stalled."""
        held = held_item(db, gmail, "Held", "text")
        queued = held_item(db, gmail, "Queued", "text")
        jobs.enqueue(
            db, Queue.INTEGRATE, {"source_item_id": queued}, serialize_key=repo.OWNER_USER_ID
        )
        db.commit()

        ids = [item["id"] for item in api.get("/v1/ingestion", headers=AUTH).json()]
        assert ids == [queued]
        assert held not in ids

    def test_it_is_not_counted_as_ready_work_and_writes_no_heartbeat(
        self, api: TestClient, db: psycopg.Connection[Any], gmail: str
    ) -> None:
        held_item(db, gmail, "Held", "text")
        db.commit()

        body = api.get("/v1/processing", headers=AUTH).json()
        integrate = next(row for row in body["readiness"] if row["queue"] == "integrate")
        assert (integrate["ready"], integrate["ready_keys"]) == (0, 0)
        assert body["worker_last_seen_at"] is None, "nothing ran, and nothing says it did"

    def test_it_is_not_news(self, api: TestClient, db: psycopg.Connection[Any], gmail: str) -> None:
        """Read state is per news item (invariant 5); a held item has none to be unread."""
        held_item(db, gmail, "Held", "text")
        db.commit()

        assert api.get("/v1/news-items", headers=AUTH).json() == []


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

        # A paste's job — same queue, same serialization key — plus the flag that makes it
        # a deliberate ingest, which is what label sync keys on (motet#96).
        with db.cursor() as cur:
            cur.execute(
                "SELECT payload, serialize_key FROM jobs WHERE queue = 'integrate' ORDER BY id"
            )
            rows = cur.fetchall()
        assert {row["payload"]["source_item_id"] for row in rows} == {one, two}
        assert all(row["payload"]["deliberate"] is True for row in rows)
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

    def test_another_users_item_is_skipped_and_stays_held_for_them(
        self, api: TestClient, db: psycopg.Connection[Any], recorder: Recorder
    ) -> None:
        theirs = held_item(db, someone_else(db), "Theirs", "text", user_id="other")
        db.commit()

        response = api.post("/v1/source-items/integrate", json={"ids": [theirs]}, headers=AUTH)
        assert response.json() == {"queued": 0, "skipped": 1}
        assert integrate_jobs(db) == []
        assert recorder.fired == []
        assert [item.id for item in repo.list_held_source_items(db, "other")] == [theirs]

    def test_an_empty_or_oversized_list_is_a_validation_error(self, api: TestClient) -> None:
        for ids in ([], [f"si_{n}" for n in range(repo.HELD_MAX_ITEMS + 1)]):
            response = api.post("/v1/source-items/integrate", json={"ids": ids}, headers=AUTH)
            assert response.status_code == 422


class TestDismiss:
    def test_dismisses_held_items_and_spends_nothing(
        self, api: TestClient, db: psycopg.Connection[Any], gmail: str, recorder: Recorder
    ) -> None:
        unwanted = held_item(db, gmail, "Unwanted", "text")
        wanted = held_item(db, gmail, "Wanted", "text")
        db.commit()

        response = api.post(
            "/v1/source-items/dismiss", json={"ids": [unwanted, "si_unknown"]}, headers=AUTH
        )
        assert response.status_code == 200, response.text
        assert response.json() == {"dismissed": 1, "skipped": 1}
        assert integrate_jobs(db) == [], "no job"
        assert recorder.fired == [], "and no drain nudged for it"
        assert [item["id"] for item in api.get("/v1/source-items/held", headers=AUTH).json()] == [
            wanted
        ]

        # Gone from every surface, and not something "ingest now" can revive.
        assert api.get("/v1/ingestion", headers=AUTH).json() == []
        assert api.post(
            "/v1/source-items/integrate", json={"ids": [unwanted]}, headers=AUTH
        ).json() == {"queued": 0, "skipped": 1}
        detail = api.get(f"/v1/source-items/{unwanted}", headers=AUTH).json()
        assert (detail["state"], detail["status"], detail["processed"]) == (
            "dismissed",
            "dismissed",
            [],
        )

    def test_a_queued_or_another_users_item_is_skipped(
        self, api: TestClient, db: psycopg.Connection[Any], gmail: str
    ) -> None:
        queued = held_item(db, gmail, "Queued", "text")
        jobs.enqueue(db, Queue.INTEGRATE, {"source_item_id": queued})
        theirs = held_item(db, someone_else(db), "Theirs", "text", user_id="other")
        db.commit()

        response = api.post(
            "/v1/source-items/dismiss", json={"ids": [queued, theirs]}, headers=AUTH
        )
        assert response.json() == {"dismissed": 0, "skipped": 2}
        states = db.execute(
            "SELECT state FROM source_items WHERE id = ANY(%s)", ([queued, theirs],)
        ).fetchall()
        assert {row["state"] for row in states} == {"pending"}


class TestDetail:
    """`GET /v1/source-items/{id}` — one source item as three stages."""

    def test_a_held_item_has_stage_one_only(
        self, api: TestClient, db: psycopg.Connection[Any], gmail: str
    ) -> None:
        sent = datetime.now(UTC) - timedelta(days=2)
        item = held_item(db, gmail, "Acme raises", "Acme raises $20M.", received_at=sent)
        db.commit()

        body = api.get(f"/v1/source-items/{item}", headers=AUTH).json()
        assert body["id"] == item
        assert (body["state"], body["status"]) == ("pending", "held")

        pulled = body["pulled"]
        assert pulled["source_kind"] == "gmail"
        assert pulled["source_name"] == "Inbox"
        assert pulled["external_id"] == "msg-Acme raises"
        assert pulled["text"] == "Acme raises $20M."
        assert pulled["chars"] == len("Acme raises $20M.")
        assert datetime.fromisoformat(pulled["received_at"]) == sent
        assert datetime.fromisoformat(pulled["stored_at"]) > sent
        assert pulled["raw_stored"] is False, "only the extracted text is kept"

        assert body["processed"] == [], "nobody has asked for inference"
        assert body["news_items"] == []

    def test_a_queued_item_reports_its_job(
        self, api: TestClient, db: psycopg.Connection[Any], gmail: str
    ) -> None:
        item = held_item(db, gmail, "Queued", "text")
        job = jobs.enqueue(db, Queue.INTEGRATE, {"source_item_id": item})
        db.commit()

        body = api.get(f"/v1/source-items/{item}", headers=AUTH).json()
        assert body["status"] == "queued"
        (step,) = body["processed"]
        assert (step["step"], step["status"]) == ("dedup", "queued")
        assert step["job"]["id"] == job
        assert step["job"]["state"] == "ready"
        assert step["job"]["attempts"] == 0
        assert step["job"]["max_attempts"] == jobs.DEFAULT_MAX_ATTEMPTS
        assert (step["outcome"], step["decision"], step["cost_recorded"]) == (None, None, False)
        assert body["news_items"] == []

    def test_an_integrated_item_reports_its_decision_and_links_its_news_item(
        self, api: TestClient, db: psycopg.Connection[Any], gmail: str, _migrated: str
    ) -> None:
        """Through the real worker: ingest now, drain, read the lifecycle back."""
        # Dated apart, because the claim queues in message order and the first one
        # integrated is the one that creates the story.
        now = datetime.now(UTC)
        first = held_item(
            db,
            gmail,
            "Acme raises $20M",
            "Acme raised $20M on Tuesday.",
            received_at=now - timedelta(days=2),
        )
        second = held_item(
            db,
            gmail,
            "ACME raises $20M!",
            "Acme's round closed this week.",
            received_at=now - timedelta(days=1),
        )
        db.commit()
        assert api.post(
            "/v1/source-items/integrate", json={"ids": [first, second]}, headers=AUTH
        ).json() == {"queued": 2, "skipped": 0}
        assert drain(Queue.INTEGRATE, _migrated) == 2

        created = api.get(f"/v1/source-items/{first}", headers=AUTH).json()
        (step,) = created["processed"]
        assert (created["status"], step["status"], step["outcome"]) == ("done", "done", "new")
        assert step["finished_at"]
        assert step["decision"]["relation"] == "unrelated"
        assert step["decision"]["basis"] == "first_pass"
        assert step["decision"]["model"] == "fake"
        assert step["decision"]["candidate_id"] is None
        assert step["decision"]["title"] == "Acme raises $20M"

        merged = api.get(f"/v1/source-items/{second}", headers=AUTH).json()
        (step,) = merged["processed"]
        (news_item,) = merged["news_items"]
        assert step["outcome"] == "merged"
        assert step["decision"]["relation"] == "same_event"
        assert step["decision"]["candidate_id"] == news_item["id"]
        assert step["decision"]["candidate_title"] == news_item["title"]
        assert news_item["source_count"] == 2
        assert news_item["position"] == 1
        assert news_item["read"] is False

        # And the backlog names its sources, so a list can open them.
        [listed] = api.get("/v1/news-items", headers=AUTH).json()
        assert listed["sources"] == [
            {"id": first, "title": "Acme raises $20M"},
            {"id": second, "title": "ACME raises $20M!"},
        ]

    def test_a_link_from_before_decisions_were_recorded_reports_none(
        self, api: TestClient, db: psycopg.Connection[Any], gmail: str
    ) -> None:
        """Every link written before migration 0012: the outcome, and no invented why."""
        item = held_item(db, gmail, "Old", "text")
        repo.insert_news_item(
            db, user_id=repo.OWNER_USER_ID, title="Old", summary="s", source_item_id_=item
        )
        repo.mark_source_item(db, item, SourceItemState.INTEGRATED)
        db.commit()

        (step,) = api.get(f"/v1/source-items/{item}", headers=AUTH).json()["processed"]
        assert (step["outcome"], step["decision"]) == ("new", None)

    def test_unknown_or_someone_elses_item_is_404(
        self, api: TestClient, db: psycopg.Connection[Any]
    ) -> None:
        """The answer is mailbox text, so "not yours" and "no such item" are one answer."""
        theirs = held_item(db, someone_else(db), "Theirs", "Private text.", user_id="other")
        db.commit()

        assert api.get("/v1/source-items/si_nope", headers=AUTH).status_code == 404
        response = api.get(f"/v1/source-items/{theirs}", headers=AUTH)
        assert response.status_code == 404
        assert "Private text." not in response.text
