"""Enrichment's rows: the vault's third kind of sealed record, and the run log.

The shape is ``db/tests/test_connectors.py``'s, because the claim is the same one: a
ciphertext moved onto another row must fail to authenticate rather than open. Here the row
is a browser session on a publisher's site, so "opens on the wrong row" would mean one
account's cookies on another account's domain.
"""

from __future__ import annotations

import hashlib
import json
from datetime import timedelta
from typing import Any

import psycopg
import pytest
from motet_db import enrichment, repo
from motet_vault import DecryptionError, LocalKeyManager

USER = repo.OWNER_USER_ID

STATE = json.dumps({"cookies": [{"name": "session", "value": "abc"}, {"name": "cf", "value": "d"}]})


@pytest.fixture
def key() -> LocalKeyManager:
    return LocalKeyManager(kek=hashlib.sha256(b"enrichment-test-kek").digest())


def an_item(db: psycopg.Connection[Any], **kw: Any) -> str:
    return repo.insert_source_item(db, user_id=USER, title="t", text="the newsletter", **kw).id


class TestTheBrowserState:
    def test_it_seals_and_opens(self, db: psycopg.Connection[Any], key: LocalKeyManager) -> None:
        enrichment.store_browser_state(
            db, key, user_id=USER, domain="example.com", state=STATE, cookies=2
        )
        assert enrichment.load_browser_state(db, key, user_id=USER, domain="example.com") == STATE

    def test_a_cookie_count_is_readable_without_the_key(
        self, db: psycopg.Connection[Any], key: LocalKeyManager
    ) -> None:
        """ "A session was saved and it is empty" and "no session was saved" are otherwise
        the same row to anyone debugging a login that will not stick."""
        assert enrichment.browser_state_cookies(db, user_id=USER, domain="example.com") is None
        enrichment.store_browser_state(
            db, key, user_id=USER, domain="example.com", state=STATE, cookies=2
        )
        assert enrichment.browser_state_cookies(db, user_id=USER, domain="example.com") == 2

    def test_nothing_of_the_session_is_stored_in_the_clear(
        self, db: psycopg.Connection[Any], key: LocalKeyManager
    ) -> None:
        enrichment.store_browser_state(
            db, key, user_id=USER, domain="example.com", state=STATE, cookies=2
        )
        row = db.execute("SELECT * FROM browser_states").fetchone()
        assert row is not None
        assert b"session" not in bytes(row["ciphertext"])
        assert "abc" not in json.dumps({k: str(v) for k, v in row.items()})

    def test_a_ciphertext_moved_to_another_domain_will_not_open(
        self, db: psycopg.Connection[Any], key: LocalKeyManager
    ) -> None:
        """The AAD is the design, not decoration."""
        enrichment.store_browser_state(
            db, key, user_id=USER, domain="example.com", state=STATE, cookies=2
        )
        db.execute("UPDATE browser_states SET domain = 'elsewhere.test'")
        with pytest.raises(DecryptionError):
            enrichment.load_browser_state(db, key, user_id=USER, domain="elsewhere.test")

    def test_a_second_run_replaces_the_session_rather_than_versioning_it(
        self, db: psycopg.Connection[Any], key: LocalKeyManager
    ) -> None:
        for cookies in (2, 5):
            enrichment.store_browser_state(
                db, key, user_id=USER, domain="example.com", state=STATE, cookies=cookies
            )
        assert db.execute("SELECT count(*) AS n FROM browser_states").fetchone()["n"] == 1
        assert enrichment.browser_state_cookies(db, user_id=USER, domain="example.com") == 5

    def test_an_unknown_domain_is_a_fresh_browser(
        self, db: psycopg.Connection[Any], key: LocalKeyManager
    ) -> None:
        assert enrichment.load_browser_state(db, key, user_id=USER, domain="nobody.test") is None


class TestTheRunLog:
    def test_a_run_is_appended_with_its_transcript(self, db: psycopg.Connection[Any]) -> None:
        item = an_item(db)
        run_id = enrichment.record_enrich_run(
            db,
            source_item_id=item,
            user_id=USER,
            domain="example.com",
            status="ok",
            tool_calls=3,
            cost_usd=0.0766,
            article_chars=7645,
            login_performed=False,
            transcript=[{"seq": 1, "kind": "tool_call", "tool": "browser__browser_execute"}],
        )
        stored = enrichment.latest_enrich_run(db, item)
        assert stored is not None
        assert stored.id == run_id
        assert stored.cost_usd == pytest.approx(0.0766)
        assert stored.transcript[0]["tool"] == "browser__browser_execute"
        assert stored.finished_at is not None

    def test_the_newest_run_is_the_one_reported(self, db: psycopg.Connection[Any]) -> None:
        item = an_item(db)
        for status in ("failed", "ok"):
            enrichment.record_enrich_run(
                db,
                source_item_id=item,
                user_id=USER,
                domain="example.com",
                status=status,  # type: ignore[arg-type]
            )
        newest = enrichment.latest_enrich_run(db, item)
        assert newest is not None and newest.status == "ok"
        assert len(enrichment.enrich_runs_for_item(db, item)) == 2

    def test_spend_is_summed_over_a_rolling_window(self, db: psycopg.Connection[Any]) -> None:
        """Rolling, not a calendar day: a cap that resets at midnight does nothing to a
        backlog ingested at 23:55."""
        item = an_item(db)
        for cost in (0.10, 0.25):
            enrichment.record_enrich_run(
                db,
                source_item_id=item,
                user_id=USER,
                domain="example.com",
                status="ok",
                cost_usd=cost,
            )
        db.execute(
            "UPDATE enrich_runs SET started_at = now() - interval '30 hours' WHERE cost_usd = 0.1"
        )
        assert enrichment.spend_since(db, USER) == pytest.approx(0.25)
        assert enrichment.spend_since(db, USER, window=timedelta(days=3)) == pytest.approx(0.35)

    def test_a_deleted_item_takes_its_runs_with_it(self, db: psycopg.Connection[Any]) -> None:
        item = an_item(db)
        enrichment.record_enrich_run(
            db, source_item_id=item, user_id=USER, domain="example.com", status="ok"
        )
        db.execute("DELETE FROM source_items WHERE id = %s", (item,))
        assert db.execute("SELECT count(*) AS n FROM enrich_runs").fetchone()["n"] == 0


class TestTheStateOnTheItem:
    def test_nothing_is_recorded_for_an_item_nobody_decided_about(
        self, db: psycopg.Connection[Any]
    ) -> None:
        """Null is a third state: "not applicable" and "queued and waiting" must not be one
        value, because one of them means a worker owes the item something."""
        state = enrichment.enrichment_state(db, an_item(db))
        assert state is not None and state.status is None

    def test_the_article_replaces_the_text_and_the_preview_is_kept(
        self, db: psycopg.Connection[Any]
    ) -> None:
        item = an_item(db)
        enrichment.apply_enriched_article(
            db, item, article_url="https://example.com/a", article="# Head\n\nBody."
        )
        stored = repo.get_source_item(db, item)
        assert stored is not None
        assert stored.text.startswith("Full article fetched from https://example.com/a")
        assert "Body." in stored.text
        state = enrichment.enrichment_state(db, item)
        assert state is not None
        assert state.status == "done"
        assert state.original_chars == len("the newsletter")

    def test_a_replay_cannot_overwrite_the_preview_with_the_article(
        self, db: psycopg.Connection[Any]
    ) -> None:
        """``COALESCE`` on ``original_text``: the one copy of what actually arrived."""
        item = an_item(db)
        for _ in range(2):
            enrichment.apply_enriched_article(
                db, item, article_url="https://example.com/a", article="# Head\n\nBody."
            )
        state = enrichment.enrichment_state(db, item)
        assert state is not None and state.original_chars == len("the newsletter")

    def test_queueing_records_what_the_run_is_for(self, db: psycopg.Connection[Any]) -> None:
        item = an_item(db)
        enrichment.mark_enrichment_queued(
            db, item, article_url="https://url1.example.com/x", domain="example.com"
        )
        state = enrichment.enrichment_state(db, item)
        assert state is not None
        assert (state.status, state.domain) == ("queued", "example.com")

    def test_running_never_walks_back_a_finished_item(self, db: psycopg.Connection[Any]) -> None:
        """The status write happens on a side connection, so it can arrive out of order."""
        item = an_item(db)
        enrichment.apply_enriched_article(
            db, item, article_url="https://example.com/a", article="x"
        )
        enrichment.mark_enrichment_running(db, item)
        state = enrichment.enrichment_state(db, item)
        assert state is not None and state.status == "done"

    def test_the_links_a_message_carried_are_readable(self, db: psycopg.Connection[Any]) -> None:
        item = an_item(db, links=["https://example.com/a", "https://example.com/b"])
        assert enrichment.source_item_links(db, item) == [
            "https://example.com/a",
            "https://example.com/b",
        ]

    def test_an_item_with_no_links_reads_as_an_empty_list(
        self, db: psycopg.Connection[Any]
    ) -> None:
        assert enrichment.source_item_links(db, an_item(db)) == []
        assert enrichment.source_item_links(db, "si_nonexistent") == []
