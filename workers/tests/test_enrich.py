"""Agentic enrichment from the worker's side, against a real Postgres and the fakes.

Three things are on trial here and they fail in different ways, so they are tested apart:

* **The rule** (design option A2) — which items go to the agent at all. It is deterministic
  and reads only rows, so it is testable exactly.
* **The handler** — what happens to the item, the run log and the sealed browser state for
  each of the outcomes a run can have. The property that binds all of them: *the item
  always integrates*, because a briefing made from a newsletter's preview is better than no
  briefing.
* **What reaches the agent, and what comes back** — a credential travels to the service and
  never into a stored transcript.

A real database rather than a mock, for ``test_pipeline.py``'s reason: the held predicate,
the job's payload and the vault's AAD are all things a fake database would verify none of.
No browser, no vendor and no subprocess anywhere — :class:`motet_enrich.FakeRunner` behind
:class:`~motet_workers.enrich.FakeEnrichClient` is what every one of these runs.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any

import psycopg
import pytest
from motet_db import SourceItemState, connectors, repo
from motet_db import enrichment as enrichment_repo
from motet_enrich.runner import FakeRunner
from motet_vault import LocalKeyManager
from motet_workers import Queue, enqueue_integration, enqueue_paste, handlers, jobs
from motet_workers.enrich import (
    MIN_ARTICLE_CHARS,
    EnrichConfig,
    FakeEnrichClient,
    handle_enrich,
    load_config,
    plan_enrichment,
    record_enrich_failure,
)

USER = repo.OWNER_USER_ID

ARTICLE_LINK = "https://url3396.example.com/ls/click?upn=Zm9vYmFy"
FOOTER_LINK = "https://example.com/preferences"
ELSEWHERE = "https://other.test/story"


@pytest.fixture
def key() -> LocalKeyManager:
    return LocalKeyManager(kek=hashlib.sha256(b"enrich-test-kek").digest())


@pytest.fixture
def on() -> EnrichConfig:
    """A deployment with enrichment switched on and pointed somewhere."""
    return load_config({"MOTET_ENRICH": "on", "MOTET_ENRICH_SERVICE_URL": "https://enrich.invalid"})


class Context:
    """What ``handle_enrich`` is allowed to reach, with the seam's fake behind it.

    ``announce_running`` replaces the side connection the real handler opens: the handler's
    own transaction is invisible for as long as the agent runs, so the status write has to
    happen somewhere else — and in a test "somewhere else" is this list.
    """

    def __init__(
        self,
        conn: psycopg.Connection[Any],
        key: LocalKeyManager,
        runner: Any | None = None,
    ) -> None:
        self.conn = conn
        self.key_manager = key
        self.enrich_client = FakeEnrichClient(runner)
        self.announced: list[str] = []
        self.after_commit: list[Any] = []
        self.stages = None
        self.store = None

    def announce_running(self, item_id: str) -> None:
        self.announced.append(item_id)


def a_site(
    db: psycopg.Connection[Any], key: LocalKeyManager, *, domain: str = "example.com", **kw: Any
) -> connectors.StoredConnector:
    return connectors.create_connector(
        db, key, user_id=USER, kind="site", label=domain, domain=domain, **kw
    )


def an_item(db: psycopg.Connection[Any], *, links: tuple[str, ...] = ()) -> str:
    stored = repo.insert_source_item(
        db,
        user_id=USER,
        title="The Information: today",
        text="Two preview paragraphs and a link. " * 20,
        links=links,
    )
    return stored.id


def held_item(db: psycopg.Connection[Any], *, links: tuple[str, ...] = ()) -> str:
    """An item in the state "Ingest now" acts on: pending, with no job of any kind."""
    return an_item(db, links=links)


class TestTheRule:
    """Option A2: a link to a site the owner added. No model call, and no other input."""

    def test_an_item_linking_to_an_added_site_is_enriched(
        self, db: psycopg.Connection[Any], key: LocalKeyManager, on: EnrichConfig
    ) -> None:
        a_site(db, key)
        item = an_item(db, links=(ELSEWHERE, ARTICLE_LINK))
        target = plan_enrichment(db, user_id=USER, item_id=item, config=on)
        assert target is not None
        assert target.domain == "example.com"
        assert target.article_url == ARTICLE_LINK

    def test_a_click_tracking_subdomain_belongs_to_the_site(
        self, db: psycopg.Connection[Any], key: LocalKeyManager, on: EnrichConfig
    ) -> None:
        """``url3396.example.com`` is the publisher's own wrapper; ``notexample.com`` is not."""
        a_site(db, key)
        item = an_item(db, links=("https://notexample.com/x", ARTICLE_LINK))
        target = plan_enrichment(db, user_id=USER, item_id=item, config=on)
        assert target is not None and target.article_url == ARTICLE_LINK

    def test_an_item_with_no_matching_link_is_not_enriched(
        self, db: psycopg.Connection[Any], key: LocalKeyManager, on: EnrichConfig
    ) -> None:
        a_site(db, key)
        item = an_item(db, links=(ELSEWHERE,))
        assert plan_enrichment(db, user_id=USER, item_id=item, config=on) is None

    def test_an_item_with_no_links_at_all_is_not_enriched(
        self, db: psycopg.Connection[Any], key: LocalKeyManager, on: EnrichConfig
    ) -> None:
        """Every item ingested before this shipped, and every item whose message kept no
        href. There is nothing to backfill from, so they simply integrate."""
        a_site(db, key)
        assert plan_enrichment(db, user_id=USER, item_id=an_item(db), config=on) is None

    def test_nothing_is_enriched_without_a_site_row_for_the_domain(
        self, db: psycopg.Connection[Any], on: EnrichConfig
    ) -> None:
        """Adding the site **is** the opt-in (option B3). No site, no fetch."""
        item = an_item(db, links=(ARTICLE_LINK,))
        assert plan_enrichment(db, user_id=USER, item_id=item, config=on) is None

    def test_every_link_on_the_site_goes_to_the_agent_in_order(
        self, db: psycopg.Connection[Any], key: LocalKeyManager, on: EnrichConfig
    ) -> None:
        a_site(db, key)
        item = an_item(db, links=(ARTICLE_LINK, ELSEWHERE, FOOTER_LINK))
        target = plan_enrichment(db, user_id=USER, item_id=item, config=on)
        assert target is not None
        assert target.urls == (ARTICLE_LINK, FOOTER_LINK)

    def test_a_deployment_with_the_switch_off_never_enriches(
        self, db: psycopg.Connection[Any], key: LocalKeyManager
    ) -> None:
        a_site(db, key)
        item = an_item(db, links=(ARTICLE_LINK,))
        assert plan_enrichment(db, user_id=USER, item_id=item, config=load_config({})) is None

    def test_the_routing_decision_does_not_need_the_services_address(
        self, db: psycopg.Connection[Any], key: LocalKeyManager
    ) -> None:
        """The API makes this call and never talks to the service, so it is not told where
        one is — telling it would put a fact about the private estate into the
        internet-facing service's configuration for nothing."""
        a_site(db, key)
        item = an_item(db, links=(ARTICLE_LINK,))
        config = load_config({"MOTET_ENRICH": "on"})
        assert config.usable is False
        assert plan_enrichment(db, user_id=USER, item_id=item, config=config) is not None


class TestIngestNowRouting:
    def test_a_matching_item_gets_an_enrich_job_and_no_integrate_job(
        self, db: psycopg.Connection[Any], key: LocalKeyManager, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("MOTET_ENRICH", "on")
        monkeypatch.setenv("MOTET_ENRICH_SERVICE_URL", "https://enrich.invalid")
        a_site(db, key)
        item = held_item(db, links=(ARTICLE_LINK,))
        db.commit()

        assert enqueue_integration(db, user_id=USER, source_item_ids=[item]) == [item]
        db.commit()

        assert _queues_for(db, item) == ["enrich"]
        state = enrichment_repo.enrichment_state(db, item)
        assert state is not None
        assert state.status == "queued"
        assert state.article_url == ARTICLE_LINK
        assert state.domain == "example.com"

    def test_a_non_matching_item_goes_straight_to_integrate(
        self, db: psycopg.Connection[Any], key: LocalKeyManager, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("MOTET_ENRICH", "on")
        monkeypatch.setenv("MOTET_ENRICH_SERVICE_URL", "https://enrich.invalid")
        a_site(db, key)
        item = held_item(db, links=(ELSEWHERE,))
        db.commit()
        enqueue_integration(db, user_id=USER, source_item_ids=[item])
        db.commit()
        assert _queues_for(db, item) == ["integrate"]
        state = enrichment_repo.enrichment_state(db, item)
        assert state is not None and state.status is None

    def test_an_item_waiting_on_enrichment_is_no_longer_held(
        self, db: psycopg.Connection[Any], key: LocalKeyManager, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Otherwise it stays on the panel and a second tab could start a second agent run.

        ``_HELD_WHERE`` asks about both queues since motet#102; this is the assertion that
        keeps it asking.
        """
        monkeypatch.setenv("MOTET_ENRICH", "on")
        monkeypatch.setenv("MOTET_ENRICH_SERVICE_URL", "https://enrich.invalid")
        a_site(db, key)
        item = held_item(db, links=(ARTICLE_LINK,))
        db.commit()
        enqueue_integration(db, user_id=USER, source_item_ids=[item])
        db.commit()

        assert [held.id for held in repo.list_held_source_items(db, USER)] == []
        # …and a second claim finds nothing left to claim.
        assert enqueue_integration(db, user_id=USER, source_item_ids=[item]) == []

    def test_an_item_mid_enrichment_is_on_the_ingestion_panel(
        self, db: psycopg.Connection[Any], key: LocalKeyManager, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Every item is on exactly one surface. An enrich job is a job, so the panel sees it."""
        monkeypatch.setenv("MOTET_ENRICH", "on")
        monkeypatch.setenv("MOTET_ENRICH_SERVICE_URL", "https://enrich.invalid")
        a_site(db, key)
        item = held_item(db, links=(ARTICLE_LINK,))
        db.commit()
        enqueue_integration(db, user_id=USER, source_item_ids=[item])
        db.commit()
        assert [entry.id for entry in repo.list_ingestion(db, USER)] == [item]

    def test_a_paste_is_never_enriched(
        self, db: psycopg.Connection[Any], key: LocalKeyManager, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Pasting *is* asking, and it queues its own job in the same transaction."""
        monkeypatch.setenv("MOTET_ENRICH", "on")
        monkeypatch.setenv("MOTET_ENRICH_SERVICE_URL", "https://enrich.invalid")
        a_site(db, key)
        stored = enqueue_paste(
            db, user_id=USER, title="Pasted", text=f"Read it at {ARTICLE_LINK} please."
        )
        db.commit()
        assert _queues_for(db, stored.id) == ["integrate"]
        # The links are still recorded, because nothing can recover them later.
        assert enrichment_repo.source_item_links(db, stored.id) == [ARTICLE_LINK]

    def test_the_deliberate_flag_survives_the_detour(
        self, db: psycopg.Connection[Any], key: LocalKeyManager, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """motet#96's label write-back keys on it, and an enriched item was still the
        owner's own "ingest now"."""
        monkeypatch.setenv("MOTET_ENRICH", "on")
        monkeypatch.setenv("MOTET_ENRICH_SERVICE_URL", "https://enrich.invalid")
        a_site(db, key)
        item = held_item(db, links=(ARTICLE_LINK,))
        db.commit()
        enqueue_integration(db, user_id=USER, source_item_ids=[item])
        db.commit()

        context = Context(db, key)
        handle_enrich(context, _payload(db, item))
        db.commit()

        integrate = _payload_for(db, item, "integrate")
        assert integrate["deliberate"] is True
        assert integrate["enriched"] is True


class TestTheHandler:
    def test_a_successful_run_replaces_the_text_and_keeps_the_preview(
        self, db: psycopg.Connection[Any], key: LocalKeyManager, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        item, context = _ready_to_run(db, key, monkeypatch)
        before = repo.get_source_item(db, item)
        assert before is not None
        handle_enrich(context, _payload(db, item))
        db.commit()

        after = repo.get_source_item(db, item)
        assert after is not None
        assert after.text.startswith("Full article fetched from")
        assert len(after.text) > len(before.text)
        state = enrichment_repo.enrichment_state(db, item)
        assert state is not None
        assert state.status == "done"
        assert state.original_chars == len(before.text)
        assert _queues_for(db, item) == ["enrich", "integrate"]

    def test_the_run_is_logged_with_its_cost_and_its_transcript(
        self, db: psycopg.Connection[Any], key: LocalKeyManager, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        item, context = _ready_to_run(db, key, monkeypatch)
        handle_enrich(context, _payload(db, item))
        db.commit()

        run = enrichment_repo.latest_enrich_run(db, item)
        assert run is not None
        assert run.status == "ok"
        assert run.cost_usd > 0
        assert run.tool_calls == 2
        assert run.domain == "example.com"
        assert run.transcript

    def test_the_browser_state_is_sealed_and_reused_by_the_next_item(
        self, db: psycopg.Connection[Any], key: LocalKeyManager, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """ "Log in once per domain", which is what the sealed cookies buy."""
        item, context = _ready_to_run(db, key, monkeypatch)
        handle_enrich(context, _payload(db, item))
        db.commit()

        cookies = enrichment_repo.browser_state_cookies(db, user_id=USER, domain="example.com")
        assert cookies == 1
        opened = enrichment_repo.load_browser_state(db, key, user_id=USER, domain="example.com")
        assert opened is not None and "fake_session" in opened

        second = held_item(db, links=(ARTICLE_LINK,))
        db.commit()
        enqueue_integration(db, user_id=USER, source_item_ids=[second])
        db.commit()
        handle_enrich(context, _payload(db, second))
        db.commit()
        assert context.enrich_client.requests[-1].browser_state == opened

    def test_a_stored_state_is_never_readable_without_the_key(
        self, db: psycopg.Connection[Any], key: LocalKeyManager, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Invariant 8, on the vault's third kind of sealed record."""
        item, context = _ready_to_run(db, key, monkeypatch)
        handle_enrich(context, _payload(db, item))
        db.commit()
        row = db.execute("SELECT ciphertext FROM browser_states").fetchone()
        assert row is not None
        assert b"fake_session" not in bytes(row["ciphertext"])

    def test_a_blocked_run_keeps_the_preview_and_still_integrates(
        self, db: psycopg.Connection[Any], key: LocalKeyManager, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        item, context = _ready_to_run(
            db, key, monkeypatch, runner=FakeRunner(blocked_domains=frozenset({"example.com"}))
        )
        before = repo.get_source_item(db, item)
        assert before is not None
        handle_enrich(context, _payload(db, item))
        db.commit()

        after = repo.get_source_item(db, item)
        assert after is not None and after.text == before.text
        state = enrichment_repo.enrichment_state(db, item)
        assert state is not None and state.status == "failed" and state.error
        assert state.original_chars is None
        assert _queues_for(db, item) == ["enrich", "integrate"]
        # The item itself is untouched: enrichment giving up is not the pipeline giving up.
        assert after.state is SourceItemState.PENDING

    def test_an_ok_answer_carrying_a_stub_is_not_an_article(
        self, db: psycopg.Connection[Any], key: LocalKeyManager, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A cookie banner and a "subscribe to continue" page are both pages with text on
        them, and replacing a 1,400-character newsletter with 200 characters of consent
        notice is the one way this feature makes a briefing worse."""

        class Stub:
            def run(self, request: Any, caps: Any) -> Any:
                from motet_enrich.contract import EnrichResult

                return EnrichResult(
                    status="ok",
                    article_url=ARTICLE_LINK,
                    article_markdown="Subscribe to continue reading.",
                )

        item, context = _ready_to_run(db, key, monkeypatch, runner=Stub())
        before = repo.get_source_item(db, item)
        assert before is not None
        handle_enrich(context, _payload(db, item))
        db.commit()

        after = repo.get_source_item(db, item)
        assert after is not None and after.text == before.text
        run = enrichment_repo.latest_enrich_run(db, item)
        assert run is not None and run.status == "failed"
        assert len("Subscribe to continue reading.") < MIN_ARTICLE_CHARS

    def test_the_item_is_announced_as_running_outside_the_handlers_transaction(
        self, db: psycopg.Connection[Any], key: LocalKeyManager, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Otherwise the panel says `queued` for the whole ten minutes somebody is watching."""
        item, context = _ready_to_run(db, key, monkeypatch)
        handle_enrich(context, _payload(db, item))
        assert context.announced == [item]

    def test_a_replay_does_not_run_the_agent_a_second_time(
        self, db: psycopg.Connection[Any], key: LocalKeyManager, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The one decision in this pipeline that can spend half a dollar by accident."""
        item, context = _ready_to_run(db, key, monkeypatch)
        payload = _payload(db, item)
        handle_enrich(context, payload)
        db.commit()
        handle_enrich(context, payload)
        db.commit()
        assert len(context.enrich_client.requests) == 1

    def test_an_item_that_has_moved_on_is_left_alone(
        self, db: psycopg.Connection[Any], key: LocalKeyManager, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        item, context = _ready_to_run(db, key, monkeypatch)
        repo.mark_source_item(db, item, SourceItemState.INTEGRATED)
        db.commit()
        handle_enrich(context, _payload(db, item))
        db.commit()
        assert context.enrich_client.requests == []


class TestTheCaps:
    def test_the_rolling_daily_cap_is_a_recorded_skip_not_a_failure(
        self, db: psycopg.Connection[Any], key: LocalKeyManager, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A person reading the spend view has to be able to tell "the agent tried and
        could not" from "the budget said no"."""
        monkeypatch.setenv("MOTET_ENRICH_MAX_USD_PER_DAY", "0.01")
        item, context = _ready_to_run(db, key, monkeypatch)
        enrichment_repo.record_enrich_run(
            db, source_item_id=item, user_id=USER, domain="example.com", status="ok", cost_usd=5.0
        )
        db.commit()

        handle_enrich(context, _payload(db, item))
        db.commit()

        assert context.enrich_client.requests == []
        state = enrichment_repo.enrichment_state(db, item)
        assert state is not None and state.status == "skipped"
        assert "cap is spent" in (state.error or "")
        assert _queues_for(db, item) == ["enrich", "integrate"]

    def test_every_run_counts_toward_the_cap_including_the_ones_that_failed(
        self, db: psycopg.Connection[Any], key: LocalKeyManager
    ) -> None:
        """A run that hit a wall after twenty tool calls was billed for all twenty."""
        item = an_item(db, links=(ARTICLE_LINK,))
        for status, cost in (("ok", 0.10), ("blocked", 0.20), ("timeout", 0.05)):
            enrichment_repo.record_enrich_run(
                db,
                source_item_id=item,
                user_id=USER,
                domain="example.com",
                status=status,  # type: ignore[arg-type]
                cost_usd=cost,
            )
        db.commit()
        assert enrichment_repo.spend_since(db, USER) == pytest.approx(0.35)

    def test_the_worker_passes_its_caps_to_the_service(
        self, db: psycopg.Connection[Any], key: LocalKeyManager, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("MOTET_ENRICH_MAX_TOOL_CALLS", "7")
        monkeypatch.setenv("MOTET_ENRICH_TIMEOUT_SECONDS", "120")
        config = load_config()
        assert config.caps.max_tool_calls == 7
        assert config.caps.timeout_seconds == 120

    def test_a_nonsense_cap_falls_back_rather_than_stopping_ingestion(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The caps bound spending; a typo in one must not take the pipeline down."""
        config = load_config({"MOTET_ENRICH_MAX_USD_PER_ITEM": "nonsense"})
        assert config.max_usd_per_item == 0.50


class TestWhatReachesTheAgent:
    def test_the_site_credential_travels_and_the_transcript_does_not_carry_it(
        self, db: psycopg.Connection[Any], key: LocalKeyManager, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("MOTET_ENRICH", "on")
        monkeypatch.setenv("MOTET_ENRICH_SERVICE_URL", "https://enrich.invalid")
        a_site(db, key, username="owner@example.com", secret="hunter2-the-password")
        item = held_item(db, links=(ARTICLE_LINK,))
        db.commit()
        enqueue_integration(db, user_id=USER, source_item_ids=[item])
        db.commit()

        context = Context(db, key)
        handle_enrich(context, _payload(db, item))
        db.commit()

        sent = context.enrich_client.requests[0]
        assert sent.site.username == "owner@example.com"
        assert sent.site.password == "hunter2-the-password"

        run = enrichment_repo.latest_enrich_run(db, item)
        assert run is not None
        stored = json.dumps(run.transcript)
        assert "hunter2-the-password" not in stored
        assert "owner@example.com" not in stored

    def test_an_mcp_connector_reaches_the_agent_with_its_bearer(
        self, db: psycopg.Connection[Any], key: LocalKeyManager, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        a_site(db, key)
        mailbox = connectors.create_connector(
            db,
            key,
            user_id=USER,
            kind="mcp",
            label="Mailbox",
            url="https://mail.example/mcp?servers=gmail-ro",
            risk_acknowledged=True,
        )
        connectors.store_connector_secret(
            db, key, connector_id=mailbox.id, secret=json.dumps({"access_token": "at_12345"})
        )
        item, context = _ready_to_run(db, key, monkeypatch)
        handle_enrich(context, _payload(db, item))
        db.commit()

        servers = context.enrich_client.requests[0].mcp_servers
        assert [server.access_token for server in servers] == ["at_12345"]
        assert servers[0].url.endswith("servers=gmail-ro")

    def test_a_server_scoped_to_other_domains_is_not_handed_over(
        self, db: psycopg.Connection[Any], key: LocalKeyManager, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        a_site(db, key)
        scoped = connectors.create_connector(
            db,
            key,
            user_id=USER,
            kind="mcp",
            label="Elsewhere only",
            url="https://mail.example/mcp",
            domains=["other.test"],
            risk_acknowledged=True,
        )
        connectors.store_connector_secret(
            db, key, connector_id=scoped.id, secret=json.dumps({"access_token": "at_1"})
        )
        item, context = _ready_to_run(db, key, monkeypatch)
        handle_enrich(context, _payload(db, item))
        db.commit()
        assert context.enrich_client.requests[0].mcp_servers == []

    def test_a_server_awaiting_authorization_is_not_handed_over(
        self, db: psycopg.Connection[Any], key: LocalKeyManager, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        a_site(db, key)
        connectors.create_connector(
            db,
            key,
            user_id=USER,
            kind="mcp",
            label="Not yet",
            url="https://mail.example/mcp",
            risk_acknowledged=True,
        )
        item, context = _ready_to_run(db, key, monkeypatch)
        handle_enrich(context, _payload(db, item))
        db.commit()
        assert context.enrich_client.requests[0].mcp_servers == []

    def test_a_servers_tool_namespace_is_its_id_rather_than_its_label(
        self, db: psycopg.Connection[Any], key: LocalKeyManager, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A connector labelled "browser" must not buy itself transcript retention.

        ``motet_enrich.redact`` decides whether a tool result may be stored from the
        server's namespace, and a label is free text the owner types.
        """
        a_site(db, key)
        hostile = connectors.create_connector(
            db,
            key,
            user_id=USER,
            kind="mcp",
            label="browser",
            url="https://evil.example/mcp",
            risk_acknowledged=True,
        )
        connectors.store_connector_secret(
            db, key, connector_id=hostile.id, secret=json.dumps({"access_token": "at_1"})
        )
        item, context = _ready_to_run(db, key, monkeypatch)
        handle_enrich(context, _payload(db, item))
        db.commit()
        assert context.enrich_client.requests[0].mcp_servers[0].name != "browser"


class TestTheFailureRecorder:
    def test_a_job_that_exhausted_its_retries_still_integrates(
        self, db: psycopg.Connection[Any], key: LocalKeyManager, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """An agent that cannot be reached at all costs the article, never the item."""
        monkeypatch.setenv("MOTET_ENRICH", "on")
        monkeypatch.setenv("MOTET_ENRICH_SERVICE_URL", "https://enrich.invalid")
        a_site(db, key)
        item = held_item(db, links=(ARTICLE_LINK,))
        db.commit()
        enqueue_integration(db, user_id=USER, source_item_ids=[item])
        db.commit()

        record_enrich_failure(db, _payload(db, item), "the enrichment service never answered")
        db.commit()

        state = enrichment_repo.enrichment_state(db, item)
        assert state is not None and state.status == "failed"
        assert _queues_for(db, item) == ["enrich", "integrate"]
        stored = repo.get_source_item(db, item)
        assert stored is not None and stored.state is SourceItemState.PENDING

    def test_it_is_registered_for_the_enrich_queue(self) -> None:
        assert handlers.failure_recorders()[Queue.ENRICH] is record_enrich_failure

    def test_the_enrich_queue_is_drained_between_extract_and_integrate(self) -> None:
        from motet_workers.queues import PIPELINE

        assert PIPELINE.index(Queue.ENRICH) == PIPELINE.index(Queue.EXTRACT) + 1
        assert PIPELINE.index(Queue.INTEGRATE) == PIPELINE.index(Queue.ENRICH) + 1
        assert Queue.ENRICH in handlers.HANDLERS


# --- helpers -------------------------------------------------------------------------


def _ready_to_run(
    db: psycopg.Connection[Any],
    key: LocalKeyManager,
    monkeypatch: pytest.MonkeyPatch,
    *,
    runner: Any | None = None,
) -> tuple[str, Context]:
    monkeypatch.setenv("MOTET_ENRICH", "on")
    monkeypatch.setenv("MOTET_ENRICH_SERVICE_URL", "https://enrich.invalid")
    if not connectors.list_connectors(db, USER):
        a_site(db, key)
    item = held_item(db, links=(ARTICLE_LINK,))
    db.commit()
    enqueue_integration(db, user_id=USER, source_item_ids=[item])
    db.commit()
    return item, Context(db, key, runner)


def _queues_for(conn: psycopg.Connection[Any], item_id: str) -> list[str]:
    rows = conn.execute(
        "SELECT queue FROM jobs WHERE payload ->> 'source_item_id' = %s ORDER BY id",
        (item_id,),
    ).fetchall()
    return [row["queue"] for row in rows]


def _payload_for(conn: psycopg.Connection[Any], item_id: str, queue: str) -> dict[str, Any]:
    row = conn.execute(
        "SELECT payload FROM jobs WHERE queue = %s AND payload ->> 'source_item_id' = %s "
        "ORDER BY id DESC LIMIT 1",
        (queue, item_id),
    ).fetchone()
    assert row is not None
    payload: dict[str, Any] = row["payload"]
    return payload


def _payload(conn: psycopg.Connection[Any], item_id: str) -> dict[str, Any]:
    return _payload_for(conn, item_id, "enrich")


def test_a_claimed_enrich_job_takes_the_users_serialization_key(
    db: psycopg.Connection[Any], key: LocalKeyManager, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Invariant 6, and here it has a second meaning: one browser session per user at a
    time, so two runs cannot both be writing that user's cookies for one domain."""
    monkeypatch.setenv("MOTET_ENRICH", "on")
    monkeypatch.setenv("MOTET_ENRICH_SERVICE_URL", "https://enrich.invalid")
    a_site(db, key)
    item = held_item(db, links=(ARTICLE_LINK,))
    db.commit()
    enqueue_integration(db, user_id=USER, source_item_ids=[item])
    db.commit()

    claimed = jobs.claim(db, Queue.ENRICH)
    assert claimed is not None
    assert claimed.serialize_key == USER


class TestThePermissionIsRecheckedAtRunTime:
    """Option B3 holds of the *fetch*, not only of the decision to queue one.

    A job can sit in the queue across a deletion, so an owner who removes a site between
    "Ingest now" and the run has removed it — rather than having removed it for everything
    except the jobs already written.
    """

    def test_a_deleted_site_stops_the_run(
        self, db: psycopg.Connection[Any], key: LocalKeyManager, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        item, context = _ready_to_run(db, key, monkeypatch)
        site = connectors.list_connectors(db, USER)[0]
        connectors.delete_connector(db, user_id=USER, connector_id=site.id)
        db.commit()

        handle_enrich(context, _payload(db, item))
        db.commit()

        assert context.enrich_client.requests == []
        state = enrichment_repo.enrichment_state(db, item)
        assert state is not None and state.status == "skipped"
        assert "no longer a site" in (state.error or "")
        assert _queues_for(db, item) == ["enrich", "integrate"]

    def test_a_run_with_no_candidate_url_fails_permanently(
        self, db: psycopg.Connection[Any], key: LocalKeyManager, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A malformed payload, not a run that could go ahead: there is nothing to open."""
        item, context = _ready_to_run(db, key, monkeypatch)
        payload = dict(_payload(db, item))
        payload["candidate_urls"] = []
        payload.pop("article_url", None)
        with pytest.raises(handlers.PermanentFailure, match="names no candidate URL"):
            handle_enrich(context, payload)


class TestOneItemIsNeverEnrichedTwice:
    """The rule money rides on, and the one the job queue's work fence cannot enforce.

    Everything that records a run's cost is inside the handler's transaction, which does not
    commit until the agent's answer comes back — so a worker killed mid-run leaves no
    `enrich_runs` row at all, and the daily cap would see nothing spent.
    """

    def test_a_second_claim_of_a_running_item_does_not_start_a_second_run(
        self, db: psycopg.Connection[Any], key: LocalKeyManager, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        item, context = _ready_to_run(db, key, monkeypatch)
        # What a worker killed mid-run leaves behind: `running`, written on a side
        # connection before the agent started, and no run row.
        enrichment_repo.mark_enrichment_running(db, item)
        db.commit()

        handle_enrich(context, _payload(db, item))
        db.commit()

        assert context.enrich_client.requests == []
        state = enrichment_repo.enrichment_state(db, item)
        assert state is not None and state.status == "failed"
        assert "interrupted" in (state.error or "")
        assert _queues_for(db, item) == ["enrich", "integrate"]

    def test_a_transport_failure_is_recorded_rather_than_retried(
        self, db: psycopg.Connection[Any], key: LocalKeyManager, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The client's timeout is shorter than the service's own, so a read timeout more
        often means "the run is still going" than "nothing happened". On the retry ladder
        that is up to five more billed runs, none of them visible to the daily cap."""
        item, context = _ready_to_run(db, key, monkeypatch)

        class Unreachable:
            requests: list[Any] = []

            def enrich(self, request: Any, caps: Any) -> Any:
                raise TimeoutError("read timeout")

        context.enrich_client = Unreachable()
        handle_enrich(context, _payload(db, item))
        db.commit()

        run = enrichment_repo.latest_enrich_run(db, item)
        assert run is not None and run.status == "failed"
        assert "did not answer" in (run.error or "")
        state = enrichment_repo.enrichment_state(db, item)
        assert state is not None and state.status == "failed"
        assert _queues_for(db, item) == ["enrich", "integrate"]


class TestTheApiNeedsNoTopology:
    """The API decides the routing and never calls the service, so it is not told where one is.

    That is the infra issue's env list exactly: `MOTET_ENRICH` on the API, the URL and the
    token on the worker.
    """

    def test_the_routing_switch_alone_queues_an_enrich_job(
        self, db: psycopg.Connection[Any], key: LocalKeyManager, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("MOTET_ENRICH", "on")
        monkeypatch.delenv("MOTET_ENRICH_SERVICE_URL", raising=False)
        a_site(db, key)
        item = held_item(db, links=(ARTICLE_LINK,))
        db.commit()
        enqueue_integration(db, user_id=USER, source_item_ids=[item])
        db.commit()
        assert _queues_for(db, item) == ["enrich"]

    def test_a_worker_with_no_service_url_skips_and_integrates(
        self, db: psycopg.Connection[Any], key: LocalKeyManager, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The safe half of the two halves disagreeing."""
        monkeypatch.setenv("MOTET_ENRICH", "on")
        monkeypatch.delenv("MOTET_ENRICH_SERVICE_URL", raising=False)
        a_site(db, key)
        item = held_item(db, links=(ARTICLE_LINK,))
        db.commit()
        enqueue_integration(db, user_id=USER, source_item_ids=[item])
        db.commit()

        context = Context(db, key)
        context.enrich_client = None  # type: ignore[assignment]
        handle_enrich(context, _payload(db, item))
        db.commit()

        state = enrichment_repo.enrichment_state(db, item)
        assert state is not None and state.status == "skipped"
        assert _queues_for(db, item) == ["enrich", "integrate"]


class TestTheRunningFlagOnTheRealContext:
    """The announce, driven through ``handlers.Context`` rather than the stand-in above.

    Every other test here replaces it with a list, which is what keeps them readable — and
    what leaves the mechanism the replay guard actually rests on untested. It is a side
    connection opened from ``Context.database_url``, and the two things worth pinning are
    that the flag is *committed* where another connection can see it (a write on the
    handler's own transaction would be invisible for the whole of a ten-minute run), and
    that a failure to write it stops the run rather than being shrugged off.
    """

    def _real_context(
        self,
        db: psycopg.Connection[Any],
        *,
        database_url: str,
        client: Any | None = None,
    ) -> handlers.Context:
        return handlers.Context(
            conn=db,
            stages=None,  # type: ignore[arg-type]
            store=None,  # type: ignore[arg-type]
            enrich_client=client if client is not None else FakeEnrichClient(),
            database_url=database_url,
        )

    def test_the_flag_is_committed_before_the_agent_starts(
        self,
        db: psycopg.Connection[Any],
        key: LocalKeyManager,
        _migrated: str,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Visible from a *second* connection while the handler's transaction is still open,
        which is the whole point of not writing it on `context.conn`."""
        item, _ = _ready_to_run(db, key, monkeypatch)
        seen: list[str | None] = []

        class Watching(FakeEnrichClient):
            def enrich(self, request: Any, caps: Any) -> Any:
                with psycopg.connect(_migrated) as other:
                    state = enrichment_repo.enrichment_state(other, request.item_id)
                seen.append(state.status if state is not None else None)
                return super().enrich(request, caps)

        context = self._real_context(db, database_url=_migrated, client=Watching())
        handle_enrich(context, _payload(db, item))
        db.commit()

        assert seen == ["running"]
        state = enrichment_repo.enrichment_state(db, item)
        assert state is not None and state.status == "done"

    def test_an_unwritable_flag_means_no_run_at_all(
        self,
        db: psycopg.Connection[Any],
        key: LocalKeyManager,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Half a dollar unspent is the cheap side of the trade: without the flag, a worker
        killed mid-run leaves nothing saying an agent was started, and the next claim starts
        another one that the daily cap cannot see."""
        item, _ = _ready_to_run(db, key, monkeypatch)
        # Refused immediately rather than a hostname that would wait on a resolver.
        context = self._real_context(db, database_url="postgresql://127.0.0.1:1/nope")

        handle_enrich(context, _payload(db, item))
        db.commit()

        assert isinstance(context.enrich_client, FakeEnrichClient)
        assert context.enrich_client.requests == []
        run = enrichment_repo.latest_enrich_run(db, item)
        assert run is not None and run.status == "failed"
        assert "no run was started" in (run.error or "")
        assert _queues_for(db, item) == ["enrich", "integrate"]

    def test_a_worker_that_knows_no_database_url_does_not_run_either(
        self,
        db: psycopg.Connection[Any],
        key: LocalKeyManager,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        item, _ = _ready_to_run(db, key, monkeypatch)
        monkeypatch.delenv("DATABASE_URL", raising=False)
        context = self._real_context(db, database_url="")

        handle_enrich(context, _payload(db, item))
        db.commit()

        assert isinstance(context.enrich_client, FakeEnrichClient)
        assert context.enrich_client.requests == []
        state = enrichment_repo.enrichment_state(db, item)
        assert state is not None and state.status == "failed"
