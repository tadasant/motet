"""PROTOTYPE — agentic enrichment against a real Postgres, the fake stages, and a fake agent.

Nothing here starts ``pi``, a browser, or a model (invariant 7): triage is a
:class:`FakeTriager` with a scripted answer, and the agent is a :class:`FakePiRunner` with a
canned outcome. What *is* real is everything the design rests on — the routing through the
job table, the vault sealing the browser state, the columns on ``source_items``, and the
redaction of what gets stored.
"""

from __future__ import annotations

import json
from typing import Any

import psycopg
import pytest
from motet_db import SourceItemState, connectors, enrichment, repo
from motet_inference import Stages, TriageDecision
from motet_inference.fakes import FakeTriager
from motet_inference.registry import fake_stages
from motet_vault import LocalKeyManager, build_key_manager
from motet_workers import Queue, drain, enqueue_paste, jobs
from motet_workers import enrich as enrich_module
from motet_workers.enrich import (
    EnrichOutcome,
    EnrichRequest,
    FakePiRunner,
    McpServerSpec,
    enrich_failed,
    matching_site,
    redact,
    redact_transcript,
)

USER = repo.OWNER_USER_ID
ARTICLE_URL = "https://link.theinformation.com/click/abc123/xyz"
PREVIEW_TEXT = (
    "Two paragraphs of teaser prose about the whitelist and the confusion it caused.\n\n"
    f"Read the full article: {ARTICLE_URL}"
)
ARTICLE_MD = "# The Whitelist\n\n" + "\n\n".join(
    f"Paragraph {n} of the article." * 4 for n in range(12)
)
STORAGE_STATE = json.dumps(
    {
        "cookies": [
            {"name": "_session_id", "value": "s3cr3tsessionvalue", "domain": ".theinformation.com"},
            {"name": "cf_clearance", "value": "cfcfcfcfcfcfcf", "domain": ".theinformation.com"},
        ],
        "origins": [],
    }
)
RAW_TRANSCRIPT = [
    {
        "seq": 1,
        "at": "t",
        "kind": "tool_call",
        "tool": "playwright_browser_execute",
        "args": "page.goto(...)",
    },
    {
        "seq": 2,
        "at": "t",
        "kind": "tool_result",
        "tool": "playwright_browser_execute",
        "ok": True,
        "result": "Subscribe to read",
    },
    {
        "seq": 3,
        "at": "t",
        "kind": "tool_call",
        "tool": "email_gmail-ro__search_email_conversations",
        "args": '{"query":"from:theinformation.com"}',
    },
    {
        "seq": 4,
        "at": "t",
        "kind": "tool_result",
        "tool": "email_gmail-ro__get_email_conversation",
        "ok": True,
        "result": (
            "Sign in: https://www.theinformation.com/magic/AbCdEfGhIjKlMnOpQrStUvWxYz0123 "
            "for reader@example.com"
        ),
    },
    {
        "seq": 5,
        "at": "t",
        "kind": "tool_call",
        "tool": "playwright_browser_execute",
        "args": (
            "page.goto('https://www.theinformation.com/magic/AbCdEfGhIjKlMnOpQrStUvWxYz0123') "
            "code 123456 password hunter2secret"
        ),
    },
    {
        "seq": 6,
        "at": "t",
        "kind": "assistant",
        "text": "STATUS: ok\nLOGGED_IN: yes",
        "cost_usd": 0.41,
    },
]


def key_manager() -> LocalKeyManager:
    manager = build_key_manager({"MOTET_VAULT_BACKEND": "local", "MOTET_INFERENCE_MODE": "fake"})
    assert isinstance(manager, LocalKeyManager)
    return manager


def fetch_decision() -> TriageDecision:
    return TriageDecision(
        decision="fetch",
        article_url=ARTICLE_URL,
        domain="www.theinformation.com",
        reason="Two paragraphs and a read-the-full-article link.",
    )


def stages_with(triager: FakeTriager) -> Stages:
    base = fake_stages()
    return Stages(
        integrator=base.integrator,
        script_generator=base.script_generator,
        speech_synthesizer=base.speech_synthesizer,
        triager=triager,
    )


def paste_preview(db: psycopg.Connection[Any]) -> str:
    stored = enqueue_paste(db, user_id=USER, title="Whitelist", text=PREVIEW_TEXT)
    db.commit()
    return stored.id


def jobs_on(db: psycopg.Connection[Any], queue: Queue) -> list[dict[str, Any]]:
    return db.execute(
        "SELECT id, state, payload, serialize_key FROM jobs WHERE queue = %s ORDER BY id",
        (queue.value,),
    ).fetchall()


@pytest.fixture
def runner(monkeypatch: pytest.MonkeyPatch) -> FakePiRunner:
    fake = FakePiRunner(
        EnrichOutcome(
            status="ok",
            article_markdown=ARTICLE_MD,
            logged_in="yes",
            notes="Logged in via magic link.",
            tool_calls=11,
            cost_usd=0.4146,
            transcript=RAW_TRANSCRIPT,
            storage_state_json=STORAGE_STATE,
        )
    )
    monkeypatch.setattr(enrich_module, "build_pi_runner", lambda: fake)
    monkeypatch.setenv("MOTET_VAULT_BACKEND", "local")
    return fake


class TestTriageRouting:
    def test_a_fetch_decision_queues_enrich_and_does_not_dedup(
        self, db: psycopg.Connection[Any], _migrated: str
    ) -> None:
        item_id = paste_preview(db)
        triager = FakeTriager({item_id: fetch_decision()})

        assert drain(Queue.INTEGRATE, _migrated, stages=stages_with(triager)) == 1

        assert triager.calls == [item_id]
        stored = repo.get_source_item(db, item_id)
        assert stored is not None and stored.state is SourceItemState.PENDING
        count = db.execute("SELECT count(*) AS n FROM news_items").fetchone()
        assert count is not None and count["n"] == 0
        current = enrichment.get_enrichment(db, item_id)
        assert current is not None
        assert current.triage_decision == "fetch"
        assert current.article_url == ARTICLE_URL
        assert current.enrich_status == "pending"
        assert "read-the-full-article" in (current.triage_reason or "")
        (job,) = jobs_on(db, Queue.ENRICH)
        assert job["state"] == "ready"
        assert job["serialize_key"] == USER, "one browser login per user at a time"
        assert job["payload"] == {
            "source_item_id": item_id,
            "article_url": ARTICLE_URL,
            "domain": "theinformation.com",
        }

    def test_a_raw_decision_dedups_as_before_and_records_itself(
        self, db: psycopg.Connection[Any], _migrated: str
    ) -> None:
        item_id = paste_preview(db)
        triager = FakeTriager()

        drain(Queue.INTEGRATE, _migrated, stages=stages_with(triager))

        stored = repo.get_source_item(db, item_id)
        assert stored is not None and stored.state is SourceItemState.INTEGRATED
        current = enrichment.get_enrichment(db, item_id)
        assert current is not None
        assert current.triage_decision == "raw"
        assert current.enrich_status is None
        assert jobs_on(db, Queue.ENRICH) == []

    def test_an_enriched_payload_skips_triage(
        self, db: psycopg.Connection[Any], _migrated: str
    ) -> None:
        stored = enqueue_paste(db, user_id=USER, title="Whitelist", text=PREVIEW_TEXT)
        db.execute("DELETE FROM jobs")
        jobs.enqueue(
            db, Queue.INTEGRATE, {"source_item_id": stored.id, "enriched": True}, serialize_key=USER
        )
        db.commit()
        triager = FakeTriager({stored.id: fetch_decision()})

        drain(Queue.INTEGRATE, _migrated, stages=stages_with(triager))

        assert triager.calls == []
        assert repo.get_source_item(db, stored.id).state is SourceItemState.INTEGRATED  # type: ignore[union-attr]

    def test_a_recorded_decision_is_not_made_twice(
        self, db: psycopg.Connection[Any], _migrated: str
    ) -> None:
        """A replayed integrate job without the flag still must not re-triage."""
        item_id = paste_preview(db)
        enrichment.record_triage(
            db, item_id, decision="raw", reason="already", article_url=None, enrich_status=None
        )
        db.commit()
        triager = FakeTriager({item_id: fetch_decision()})

        drain(Queue.INTEGRATE, _migrated, stages=stages_with(triager))

        assert triager.calls == []
        assert jobs_on(db, Queue.ENRICH) == []

    def test_no_triager_means_no_triage(self, db: psycopg.Connection[Any], _migrated: str) -> None:
        item_id = paste_preview(db)
        base = fake_stages()
        stages = Stages(base.integrator, base.script_generator, base.speech_synthesizer)
        drain(Queue.INTEGRATE, _migrated, stages=stages)
        current = enrichment.get_enrichment(db, item_id)
        assert current is not None and current.triage_decision is None
        assert repo.get_source_item(db, item_id).state is SourceItemState.INTEGRATED  # type: ignore[union-attr]


def triaged_item(db: psycopg.Connection[Any], url: str) -> str:
    """A pasted preview taken through integrate with a fetch decision: enrich job queued."""
    item_id = paste_preview(db)
    drain(Queue.INTEGRATE, url, stages=stages_with(FakeTriager({item_id: fetch_decision()})))
    return item_id


class TestHandleEnrich:
    def test_success_replaces_the_text_and_keeps_the_preview(
        self, db: psycopg.Connection[Any], _migrated: str, runner: FakePiRunner
    ) -> None:
        item_id = triaged_item(db, _migrated)

        assert drain(Queue.ENRICH, _migrated) == 1

        stored = repo.get_source_item(db, item_id)
        assert stored is not None
        assert stored.text.startswith(f"Full article fetched from {ARTICLE_URL}\n\n# The Whitelist")
        assert stored.state is SourceItemState.PENDING, "dedup has not run yet"
        current = enrichment.get_enrichment(db, item_id)
        assert current is not None
        assert current.original_text == PREVIEW_TEXT
        assert current.enrich_status == "done"
        assert current.enrich_error is None
        assert current.enriched_at is not None

        (job,) = [j for j in jobs_on(db, Queue.INTEGRATE) if j["state"] == "ready"]
        assert job["payload"] == {"source_item_id": item_id, "enriched": True}
        assert job["serialize_key"] == USER

        # The request the agent got: no site connector on file, so no login, no cookies.
        (request,) = runner.requests
        assert request.article_url == ARTICLE_URL
        assert request.domain == "theinformation.com"
        assert request.login_email is None
        assert request.storage_state_json is None
        assert request.mcp_servers == ()

    def test_the_run_is_recorded_with_a_redacted_transcript(
        self, db: psycopg.Connection[Any], _migrated: str, runner: FakePiRunner
    ) -> None:
        item_id = triaged_item(db, _migrated)
        drain(Queue.ENRICH, _migrated)

        run = enrichment.latest_enrich_run(db, item_id, user_id=USER)
        assert run is not None
        assert run.status == "done"
        assert run.tool_calls == 11
        assert run.cost_usd == pytest.approx(0.4146)
        assert run.login_performed is True
        assert run.article_chars == len(ARTICLE_MD)
        assert run.finished_at is not None and run.started_at <= run.finished_at
        assert run.error is None
        stored = json.dumps(run.transcript)
        assert "AbCdEfGhIjKlMnOpQrStUvWxYz0123" not in stored, "the magic link"
        assert "reader@example.com" not in stored
        assert "123456" not in stored
        assert "Sign in:" not in stored, "a mailbox tool's result is not stored at all"
        mail_result = next(e for e in run.transcript if e["seq"] == 4)
        assert mail_result["result"].startswith("<") and "not stored" in mail_result["result"]
        browser_result = next(e for e in run.transcript if e["seq"] == 2)
        assert browser_result["result"] == "Subscribe to read"

    def test_the_browser_state_is_sealed_and_reused_on_the_next_item(
        self, db: psycopg.Connection[Any], _migrated: str, runner: FakePiRunner
    ) -> None:
        first = triaged_item(db, _migrated)
        drain(Queue.ENRICH, _migrated)

        raw = db.execute(
            "SELECT ciphertext, cookies FROM browser_states WHERE user_id = %s AND domain = %s",
            (USER, "theinformation.com"),
        ).fetchone()
        assert raw is not None
        assert raw["cookies"] == 2
        assert b"s3cr3tsessionvalue" not in bytes(raw["ciphertext"]), "sealed, not stored"
        opened = enrichment.load_browser_state(
            db, key_manager(), user_id=USER, domain="theinformation.com"
        )
        assert opened is not None and json.loads(opened.state_json) == json.loads(STORAGE_STATE)

        # A second preview on the same domain is handed the cookies before the agent starts.
        drain(Queue.INTEGRATE, _migrated)  # the first item's post-enrich integrate
        second = triaged_item(db, _migrated)
        assert second != first
        drain(Queue.ENRICH, _migrated)
        assert len(runner.requests) == 2
        assert runner.requests[1].storage_state_json == STORAGE_STATE

    def test_site_and_mcp_connectors_reach_the_agent_and_never_the_transcript(
        self, db: psycopg.Connection[Any], _migrated: str, runner: FakePiRunner
    ) -> None:
        manager = key_manager()
        site = connectors.create_connector(
            db,
            manager,
            user_id=USER,
            kind="site",
            label="The Information",
            domain="theinformation.com",
            username="reader@example.com",
            secret="hunter2secret",
        )
        mcp = connectors.create_connector(
            db,
            manager,
            user_id=USER,
            kind="mcp",
            label="Email (gmail-ro)",
            url="https://mcp.example/mcp",
        )
        connectors.store_connector_secret(
            db,
            manager,
            connector_id=mcp.id,
            secret=json.dumps(
                {"access_token": "strad_tokentokentoken", "refresh_token": "r", "expires_at": None}
            ),
        )
        db.commit()
        item_id = triaged_item(db, _migrated)

        drain(Queue.ENRICH, _migrated)

        (request,) = runner.requests
        assert request.login_email == "reader@example.com"
        assert request.login_password == "hunter2secret"
        assert request.mcp_servers == (
            McpServerSpec(
                name="email",
                label="Email (gmail-ro)",
                url="https://mcp.example/mcp",
                bearer="strad_tokentokentoken",
            ),
        )
        assert "hunter2secret" not in repr(request)
        assert "strad_tokentokentoken" not in repr(request)
        run = enrichment.latest_enrich_run(db, item_id, user_id=USER)
        assert run is not None
        assert "hunter2secret" not in json.dumps(run.transcript)
        assert site.id  # the row exists; the password only ever travelled sealed

    def test_failure_keeps_the_preview_and_still_integrates(
        self, db: psycopg.Connection[Any], _migrated: str, runner: FakePiRunner
    ) -> None:
        runner.outcome = EnrichOutcome(
            status="blocked",
            notes="Cloudflare challenge persisted.",
            tool_calls=3,
            cost_usd=0.05,
            error="BLOCKED: cloudflare",
        )
        item_id = triaged_item(db, _migrated)

        drain(Queue.ENRICH, _migrated)

        stored = repo.get_source_item(db, item_id)
        assert stored is not None and stored.text == PREVIEW_TEXT
        current = enrichment.get_enrichment(db, item_id)
        assert current is not None
        assert current.enrich_status == "failed"
        assert current.enrich_error == "BLOCKED: cloudflare"
        assert current.original_text is None
        run = enrichment.latest_enrich_run(db, item_id, user_id=USER)
        assert run is not None and run.status == "failed" and run.article_chars == 0
        ready = [j for j in jobs_on(db, Queue.INTEGRATE) if j["state"] == "ready"]
        assert [j["payload"] for j in ready] == [{"source_item_id": item_id, "enriched": True}]

        # And the item then integrates on the preview, with no second triage.
        triager = FakeTriager({item_id: fetch_decision()})
        assert drain(Queue.INTEGRATE, _migrated, stages=stages_with(triager)) == 1
        assert triager.calls == []
        assert repo.get_source_item(db, item_id).state is SourceItemState.INTEGRATED  # type: ignore[union-attr]

    def test_a_timeout_is_a_failed_run(
        self, db: psycopg.Connection[Any], _migrated: str, runner: FakePiRunner
    ) -> None:
        runner.outcome = EnrichOutcome(
            status="timeout",
            tool_calls=4,
            cost_usd=None,
            error="the agent did not finish within 600s",
        )
        item_id = triaged_item(db, _migrated)
        drain(Queue.ENRICH, _migrated)
        current = enrichment.get_enrichment(db, item_id)
        assert current is not None and current.enrich_status == "failed"
        assert "did not finish" in (current.enrich_error or "")
        run = enrichment.latest_enrich_run(db, item_id, user_id=USER)
        assert run is not None and run.status == "failed" and run.cost_usd is None
        assert [j["payload"] for j in jobs_on(db, Queue.INTEGRATE) if j["state"] == "ready"] == [
            {"source_item_id": item_id, "enriched": True}
        ]

    def test_an_ok_with_too_little_article_is_a_failure(
        self, db: psycopg.Connection[Any], _migrated: str, runner: FakePiRunner
    ) -> None:
        runner.outcome = EnrichOutcome(
            status="ok", article_markdown="# Stub\n\nSubscribe.", tool_calls=2
        )
        item_id = triaged_item(db, _migrated)
        drain(Queue.ENRICH, _migrated)
        current = enrichment.get_enrichment(db, item_id)
        assert current is not None and current.enrich_status == "failed"
        assert "chars of article" in (current.enrich_error or "")
        assert repo.get_source_item(db, item_id).text == PREVIEW_TEXT  # type: ignore[union-attr]

    def test_a_replayed_job_does_nothing(
        self, db: psycopg.Connection[Any], _migrated: str, runner: FakePiRunner
    ) -> None:
        item_id = triaged_item(db, _migrated)
        drain(Queue.ENRICH, _migrated)
        jobs.enqueue(db, Queue.ENRICH, {"source_item_id": item_id, "article_url": ARTICLE_URL})
        db.commit()
        drain(Queue.ENRICH, _migrated)
        assert len(runner.requests) == 1
        assert len(enrichment.list_enrich_runs(db, item_id)) == 1

    def test_the_failure_recorder_integrates_the_preview(
        self, db: psycopg.Connection[Any], _migrated: str
    ) -> None:
        item_id = triaged_item(db, _migrated)
        enrich_failed(db, {"source_item_id": item_id}, "RuntimeError: boom")
        db.commit()
        current = enrichment.get_enrichment(db, item_id)
        assert current is not None and current.enrich_status == "failed"
        assert current.enrich_error == "RuntimeError: boom"
        assert [j["payload"] for j in jobs_on(db, Queue.INTEGRATE) if j["state"] == "ready"] == [
            {"source_item_id": item_id, "enriched": True}
        ]


class TestHelpers:
    def test_matching_site_is_a_suffix_match(self, db: psycopg.Connection[Any]) -> None:
        row = connectors.create_connector(
            db,
            key_manager(),
            user_id=USER,
            kind="site",
            label="TI",
            domain="theinformation.com",
            username="u",
        )
        assert matching_site([row], "www.theinformation.com") is row
        assert matching_site([row], "theinformation.com") is row
        assert matching_site([row], "nottheinformation.com") is None
        assert matching_site([row], "example.com") is None

    def test_redaction_rules(self) -> None:
        text = (
            "code 123456 Bearer abcdefghijklmnop strad_xyz_1234 reader@example.com "
            "https://t.example/articles/slug?eu=AbCdEfGhIjKl "
            "https://www.theinformation.com/magic/AbCdEfGhIjKlMnOpQrStUvWxYz0123 "
            '{"name":"_session_id","value":"s3cr3tsessionvalue"} hunter2secret'
        )
        out = redact(text, ["hunter2secret"])
        for leaked in (
            "123456",
            "abcdefghijklmnop",
            "strad_xyz_1234",
            "reader@example.com",
            "eu=AbCd",
            "AbCdEfGhIjKlMnOpQrStUvWxYz0123",
            "s3cr3tsessionvalue",
            "hunter2secret",
        ):
            assert leaked not in out, leaked
        assert "https://t.example/articles/slug?eu=<redacted>" in out

    def test_redact_transcript_keeps_browser_results_and_drops_the_rest(self) -> None:
        out = redact_transcript(RAW_TRANSCRIPT, ["hunter2secret"])
        assert out[1]["result"] == "Subscribe to read"
        assert "not stored" in out[3]["result"]
        assert "hunter2secret" not in out[4]["args"]
        assert "AbCdEfGhIjKlMnOpQrStUvWxYz0123" not in out[4]["args"]
        assert [e["seq"] for e in out] == [1, 2, 3, 4, 5, 6]

    def test_the_request_repr_carries_no_secret(self) -> None:
        request = EnrichRequest(
            source_item_id="si_x",
            user_id=USER,
            article_url="https://x",
            domain="x.example",
            login_email="u@x.example",
            login_password="hunter2secret",
            mcp_servers=(
                McpServerSpec(name="email", label="E", url="https://m", bearer="tokentoken"),
            ),
            storage_state_json="{}",
        )
        assert "hunter2secret" not in repr(request) and "tokentoken" not in repr(request)
        assert request.secrets == ("hunter2secret", "tokentoken")
