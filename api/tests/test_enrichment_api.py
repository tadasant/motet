"""What the API says about an agentic fetch (motet#102).

Two surfaces, and they are apart for a reason the tests here make concrete: the lifecycle
detail carries the *shape* of what happened, and the transcript — the largest thing on an
item — is its own route, so nothing that lists items pays for it.

The third thing under test is a health field. ``enrich_enabled`` exists for
``drain_trigger``'s reason: a switch that is set and inert looks exactly like one nobody
set, and this one decides whether an ingest spends half a dollar.
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any

import psycopg
import pytest
from fastapi.testclient import TestClient
from motet_api import app
from motet_api.deps import reset_drain_trigger, reset_store
from motet_db import enrichment as enrichment_repo
from motet_db import repo

TOKEN = "test-api-token"
AUTH = {"Authorization": f"Bearer {TOKEN}"}
USER = repo.OWNER_USER_ID


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


def an_enriched_item(db: psycopg.Connection[Any], **run: Any) -> str:
    item = repo.insert_source_item(
        db,
        user_id=USER,
        title="The Information: today",
        text="Two preview paragraphs.",
        links=["https://url3396.example.com/ls/click?upn=abc"],
    ).id
    enrichment_repo.mark_enrichment_queued(
        db, item, article_url="https://url3396.example.com/ls/click?upn=abc", domain="example.com"
    )
    enrichment_repo.apply_enriched_article(
        db, item, article_url="https://example.com/articles/x", article="# Head\n\nBody. " * 60
    )
    enrichment_repo.record_enrich_run(
        db,
        source_item_id=item,
        user_id=USER,
        domain="example.com",
        status=run.get("status", "ok"),
        tool_calls=run.get("tool_calls", 3),
        cost_usd=run.get("cost_usd", 0.0766),
        article_chars=run.get("article_chars", 7645),
        login_performed=run.get("login_performed", False),
        transcript=run.get(
            "transcript",
            [
                {
                    "seq": 1,
                    "kind": "tool_call",
                    "tool": "browser__browser_execute",
                    "args": "page.goto('https://example.com/articles/x?eu=<redacted>')",
                },
                {
                    "seq": 2,
                    "kind": "tool_result",
                    "tool": "cn-1__get_email",
                    "ok": True,
                    "result": "<812 chars from cn-1__get_email, not stored>",
                },
            ],
        ),
    )
    db.commit()
    return item


class TestTheLifecycleDetail:
    def test_stage_two_leads_with_the_agentic_fetch(
        self, api: TestClient, db: psycopg.Connection[Any]
    ) -> None:
        item = an_enriched_item(db)
        body = api.get(f"/v1/source-items/{item}", headers=AUTH).json()
        steps = body["processed"]
        assert [step["step"] for step in steps][0] == "enrich"
        enrich = steps[0]["enrich"]
        assert enrich["status"] == "done"
        assert enrich["domain"] == "example.com"
        assert enrich["run"]["cost_usd"] == pytest.approx(0.0766)
        assert enrich["run"]["tool_calls"] == 3

    def test_the_enrich_step_says_its_spend_is_kept_and_dedups_does_not(
        self, api: TestClient, db: psycopg.Connection[Any]
    ) -> None:
        """`enrich_runs.cost_usd` is what the rolling daily cap is summed from, so it had to
        be a row rather than a metric. Dedup's is a metric and a log line."""
        item = an_enriched_item(db)
        steps = api.get(f"/v1/source-items/{item}", headers=AUTH).json()["processed"]
        assert {step["step"]: step["cost_recorded"] for step in steps}["enrich"] is True

    def test_stage_one_reports_what_arrived_once_the_article_replaced_it(
        self, api: TestClient, db: psycopg.Connection[Any]
    ) -> None:
        item = an_enriched_item(db)
        body = api.get(f"/v1/source-items/{item}", headers=AUTH).json()
        assert body["processed"][0]["enrich"]["original_chars"] == len("Two preview paragraphs.")
        assert body["pulled"]["text"].startswith("Full article fetched from")

    def test_an_item_nothing_enriched_carries_no_enrich_step(
        self, api: TestClient, db: psycopg.Connection[Any]
    ) -> None:
        """Most items: a paste, or one whose links reach no site the owner added."""
        item = repo.insert_source_item(db, user_id=USER, title="Pasted", text="text").id
        db.commit()
        body = api.get(f"/v1/source-items/{item}", headers=AUTH).json()
        assert [step["step"] for step in body["processed"]] == []
        assert body["status"] == "held"

    def test_an_item_mid_run_reads_as_enriching_rather_than_held(
        self, api: TestClient, db: psycopg.Connection[Any]
    ) -> None:
        """Its own word rather than "running": one is a model call of a few seconds and the
        other is a browser that may take minutes and costs real money."""
        item = repo.insert_source_item(db, user_id=USER, title="t", text="x", links=["u"]).id
        enrichment_repo.mark_enrichment_queued(
            db, item, article_url="https://example.com/a", domain="example.com"
        )
        db.commit()
        body = api.get(f"/v1/source-items/{item}", headers=AUTH).json()
        assert body["status"] == "enriching"
        assert body["processed"][0]["enrich"]["run"] is None


class TestTheTranscript:
    def test_it_returns_the_stored_entries(
        self, api: TestClient, db: psycopg.Connection[Any]
    ) -> None:
        item = an_enriched_item(db)
        body = api.get(f"/v1/source-items/{item}/enrich-transcript", headers=AUTH).json()
        assert body["run"]["status"] == "ok"
        assert [entry["seq"] for entry in body["entries"]] == [1, 2]
        assert body["entries"][0]["tool"] == "browser__browser_execute"

    def test_a_foreign_tools_result_was_never_stored_and_is_not_reported(
        self, api: TestClient, db: psycopg.Connection[Any]
    ) -> None:
        """Redacted on the service, before it crossed the network. This route redacts
        nothing and must not start to — a second pass at read time would be a second
        definition of what is safe."""
        item = an_enriched_item(db)
        body = api.get(f"/v1/source-items/{item}/enrich-transcript", headers=AUTH).json()
        assert body["entries"][1]["result"] == "<812 chars from cn-1__get_email, not stored>"

    def test_an_item_with_no_run_is_a_404(
        self, api: TestClient, db: psycopg.Connection[Any]
    ) -> None:
        item = repo.insert_source_item(db, user_id=USER, title="Pasted", text="text").id
        db.commit()
        assert (
            api.get(f"/v1/source-items/{item}/enrich-transcript", headers=AUTH).status_code == 404
        )

    def test_another_users_item_is_a_404(
        self, api: TestClient, db: psycopg.Connection[Any]
    ) -> None:
        """The transcript carries page excerpts from a mailbox, so "not yours" and "does not
        exist" have to be one answer."""
        db.execute("INSERT INTO users (id, email) VALUES ('other', NULL) ON CONFLICT DO NOTHING")
        item = repo.insert_source_item(db, user_id="other", title="Theirs", text="x").id
        enrichment_repo.record_enrich_run(
            db, source_item_id=item, user_id="other", domain="example.com", status="ok"
        )
        db.commit()
        assert (
            api.get(f"/v1/source-items/{item}/enrich-transcript", headers=AUTH).status_code == 404
        )

    def test_it_requires_a_caller(self, api: TestClient, db: psycopg.Connection[Any]) -> None:
        item = an_enriched_item(db)
        assert api.get(f"/v1/source-items/{item}/enrich-transcript").status_code == 401


class TestHealth:
    def test_enrich_is_off_unless_a_deployment_names_a_service(self, api: TestClient) -> None:
        assert api.get("/internal/health").json()["enrich_enabled"] is False

    def test_the_switch_alone_turns_it_on_and_needs_no_service_address(
        self, api: TestClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The API decides the routing and never calls the service.

        Requiring the service's address here would put a fact about the private estate into
        the internet-facing service's configuration for nothing — and the infra issue's env
        list gives the API ``MOTET_ENRICH`` alone.
        """
        monkeypatch.setenv("MOTET_ENRICH", "on")
        monkeypatch.delenv("MOTET_ENRICH_SERVICE_URL", raising=False)
        assert api.get("/internal/health").json()["enrich_enabled"] is True

    def test_the_service_address_is_never_reported(
        self, api: TestClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Topology, on an unauthenticated route, in a public repo."""
        monkeypatch.setenv("MOTET_ENRICH", "on")
        monkeypatch.setenv("MOTET_ENRICH_SERVICE_URL", "https://enrich.invalid")
        assert "enrich.invalid" not in api.get("/internal/health").text
