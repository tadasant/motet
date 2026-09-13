"""The poll and extract stages, end to end against a real queue and a fake mailbox.

What these defend is not "Gmail works" — the mailbox is a fake, and it has to be, because
the Google OAuth client does not exist. What they defend is the *pipeline shape*: that a
crashed poll is safe to retry, that a message becomes exactly one source item, that the
cursor and the enqueued work move together, and that a revoked mailbox stops rather than
retrying forever.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import psycopg
import pytest
from motet_db import CredentialPurpose, SourceItemState, SourceKind, phase2, repo
from motet_inference import fake_stages
from motet_sources import (
    DEFAULT_QUERY,
    GMAIL_READONLY_SCOPE,
    PROVIDER,
    FakeMailClient,
    RawMessage,
    SourceAuthError,
)
from motet_sources.gmail import DEFAULT_FIRST_SYNC_DAYS, FIRST_SYNC_DAYS_ENV
from motet_storage import LocalObjectStore
from motet_vault import build_key_manager
from motet_workers import Queue, drain, enqueue_integration, enqueue_source_poll, poll_key
from motet_workers.handlers import Context, PermanentFailure
from motet_workers.ingest import (
    MAX_PAGES_PER_POLL,
    POLL_PAGE_SIZE,
    _sent_at,
    handle_extract,
    handle_poll,
)
from motet_workers.jobs import DEFAULT_MAX_ATTEMPTS

USER = repo.OWNER_USER_ID


@pytest.fixture(autouse=True)
def _local_vault(monkeypatch: pytest.MonkeyPatch) -> None:
    """The fake vault backend, explicitly. Real mode would refuse it, which is the point."""
    monkeypatch.setenv("MOTET_VAULT_BACKEND", "local")
    monkeypatch.setenv("MOTET_INFERENCE_MODE", "fake")


def connected_source(db: psycopg.Connection[Any]) -> str:
    """A Gmail source with a sealed refresh token, as the OAuth callback would leave it."""
    source = phase2.create_source(db, user_id=USER, kind=SourceKind.GMAIL.value, name="Gmail")
    # Sealed with the key manager the *worker* will resolve, not a bespoke one: the
    # AAD-bound DEK is unwrappable only under the same KEK, so a test that sealed with its
    # own key would be testing nothing but its own fixture.
    phase2.store_source_credential(
        db,
        build_key_manager(),
        user_id=USER,
        source_id_=source.id,
        provider=PROVIDER,
        purpose=CredentialPurpose.REFRESH.value,
        secret="fake-refresh-token",
        scopes=[GMAIL_READONLY_SCOPE],
    )
    db.commit()
    return source.id


def context(db: psycopg.Connection[Any], tmp: Path | None = None) -> Context:
    """A handler context. Neither poll nor extract touches object storage, so the store is
    a placeholder rather than something these tests exercise."""
    return Context(
        conn=db, stages=fake_stages(), store=LocalObjectStore(root=tmp or Path("/tmp/motet-x"))
    )


# --- poll ----------------------------------------------------------------------------


def test_a_poll_queues_extraction_and_advances_the_cursor(
    db: psycopg.Connection[Any],
) -> None:
    """Both, in one transaction. Either alone is the classic ingestion bug.

    Cursor without the jobs loses a day's newsletters; jobs without the cursor replays them
    forever.
    """
    source_id = connected_source(db)
    handle_poll(context(db), {"source_id": source_id})

    queued = _jobs(db, Queue.EXTRACT)
    assert len(queued) >= 3, "the fixture mailbox has several messages"
    assert {job["payload"]["source_id"] for job in queued} == {source_id}

    source = phase2.get_source(db, source_id)
    assert source is not None
    assert source.sync_state.get("cursor"), "the cursor must have advanced"
    assert source.last_polled_at is not None


def test_a_second_poll_does_not_requeue_the_same_messages(
    db: psycopg.Connection[Any],
) -> None:
    """Idempotence at the poll stage, which is what makes a crashed run safe."""
    source_id = connected_source(db)
    handle_poll(context(db), {"source_id": source_id})
    first = len(_jobs(db, Queue.EXTRACT))

    # Everything the first poll queued is now ingested.
    for job in _jobs(db, Queue.EXTRACT):
        handle_extract(context(db), job["payload"])
    _clear(db, Queue.EXTRACT)

    handle_poll(context(db), {"source_id": source_id})
    assert _jobs(db, Queue.EXTRACT) == [], "already-ingested messages must not be queued again"
    assert first >= 3


def test_a_paused_source_is_not_polled(db: psycopg.Connection[Any]) -> None:
    source_id = connected_source(db)
    phase2.set_source_active(db, source_id, active=False)
    handle_poll(context(db), {"source_id": source_id})
    assert _jobs(db, Queue.EXTRACT) == []


def test_polling_a_paste_source_is_a_permanent_failure(
    db: psycopg.Connection[Any],
) -> None:
    """Retrying cannot turn paste-in into a mailbox."""
    with pytest.raises(PermanentFailure, match="cannot be polled"):
        handle_poll(context(db), {"source_id": repo.PASTE_SOURCE_ID})


def test_a_source_with_no_credential_is_a_permanent_failure(
    db: psycopg.Connection[Any],
) -> None:
    """Only re-consent fixes this, so burning five retries just delays the message."""
    source = phase2.create_source(db, user_id=USER, kind=SourceKind.GMAIL.value, name="Unconnected")
    with pytest.raises(PermanentFailure, match="reconnected"):
        handle_poll(context(db), {"source_id": source.id})


def test_a_history_id_cursor_from_before_the_search_is_resynced_not_errored(
    db: psycopg.Connection[Any],
) -> None:
    """A source connected before listing moved to search carries a Gmail history id.

    The adapter cannot resume from one, so it starts a bounded first sync — in the same
    poll, without an error on the source, and recording the window it used.
    """
    source_id = connected_source(db)
    phase2.set_source_sync_state(db, source_id, {"cursor": "10892907"})

    handle_poll(context(db), {"source_id": source_id})

    source = phase2.get_source(db, source_id)
    assert source is not None
    assert source.last_error is None
    assert source.sync_state["cursor"].startswith("fake:"), "a search watermark now"
    assert source.sync_state["first_sync_days"] == DEFAULT_FIRST_SYNC_DAYS
    assert len(_jobs(db, Queue.EXTRACT)) == 4


def test_a_revoked_mailbox_is_deactivated_rather_than_retried(
    db: psycopg.Connection[Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    """`invalid_grant` means the user revoked access. Only they can fix it.

    Deactivating stops the scheduler from hammering a dead connection every few minutes,
    and the permanent failure surfaces it instead of hiding it behind a retry ladder.
    """

    class Revoked:
        def refresh(self, *, refresh_token: str) -> Any:
            raise SourceAuthError("invalid_grant")

    source_id = connected_source(db)
    monkeypatch.setattr("motet_workers.ingest.build_oauth_client", lambda env=None: Revoked())

    with pytest.raises(PermanentFailure, match="reconnecting"):
        handle_poll(context(db), {"source_id": source_id})
    source = phase2.get_source(db, source_id)
    assert source is not None and not source.active


def test_the_poll_serialization_key_is_per_source(db: psycopg.Connection[Any]) -> None:
    """Per source, not per user.

    Invariant 6 is about the dedup compare-and-write, which `integrate` serializes on the
    user. Giving poll the user key too would defer that user's integrate jobs behind a slow
    mailbox fetch for no correctness gain.
    """
    source_id = connected_source(db)
    enqueue_source_poll(db, source_id)
    job = _jobs(db, Queue.POLL)[0]
    assert job["serialize_key"] == poll_key(source_id)
    assert job["serialize_key"] != USER


# --- a search longer than one run, and the filter on every poll ----------------------
#
# motet#94: the first sync read one page and moved the cursor to the mailbox's live
# position, so whatever that page did not include was never listed again. motet#95: every
# poll after the first ignored the source's filter. The mailbox here is the fake, so these
# pin the *handler's* half — the cursor, the chain, the bounds, and what lands on the
# source; `sources/tests/test_gmail.py` pins the adapter's half on the wire.


def synthesized_mailbox(count: int) -> list[RawMessage]:
    """``count`` messages that exist only to be listed. Polling fetches no bodies."""
    return [RawMessage(id=f"synth_{i:04d}", raw=b"") for i in range(count)]


def use_mailbox(monkeypatch: pytest.MonkeyPatch, mailbox: FakeMailClient) -> None:
    """Hand every poll the same mailbox, so messages can arrive between polls."""
    monkeypatch.setattr("motet_workers.ingest.build_mail_client", lambda token, env=None: mailbox)


def test_a_search_longer_than_one_run_leaves_the_cursor_on_the_next_page(
    db: psycopg.Connection[Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    """The run stops at its bound and says so; it does not step past what it did not read.

    Pages come back short — 20 against a limit of 50 — so a run needs three of them to
    reach its ceiling, and "fewer than asked for" never reads as the end of the search.
    """
    mailbox = FakeMailClient(messages=synthesized_mailbox(170), page_size=20)
    use_mailbox(monkeypatch, mailbox)
    source_id = connected_source(db)

    handle_poll(context(db), {"source_id": source_id})

    queued = [job["payload"]["message_id"] for job in _jobs(db, Queue.EXTRACT)]
    assert POLL_PAGE_SIZE <= len(queued) < POLL_PAGE_SIZE + 20, "bounded, page-whole"
    source = phase2.get_source(db, source_id)
    assert source is not None
    last = source.sync_state["last_sync"]
    assert last["caught_up"] is False
    assert (last["seen"], last["queued"], last["error"]) == (len(queued), len(queued), None)
    # The cursor holds the pass's place — "after:0, pass ended at 170, next offset 60" —
    # rather than a watermark past the 110 messages nobody has read yet.
    assert source.sync_state["cursor"] == f"fake:0:170:{len(queued)}"
    assert len(_jobs(db, Queue.POLL)) == 1, "the next link of the chain is queued"


def test_a_chain_of_polls_drains_the_whole_search_exactly_once(
    db: psycopg.Connection[Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    """170 matching messages become 170 extract jobs — the number motet#94 lost 130 of."""
    mailbox = FakeMailClient(messages=synthesized_mailbox(170), page_size=20)
    use_mailbox(monkeypatch, mailbox)
    source_id = connected_source(db)

    polls = 0
    while True:
        handle_poll(context(db), {"source_id": source_id})
        polls += 1
        rearmed = _jobs(db, Queue.POLL)
        _clear(db, Queue.POLL)
        if not rearmed:
            break
        assert polls < 10, "the chain must end"

    queued = [job["payload"]["message_id"] for job in _jobs(db, Queue.EXTRACT)]
    assert len(queued) == 170
    assert set(queued) == {f"synth_{i:04d}" for i in range(170)}
    assert polls == 3, "60 + 60 + 50: each run bounded, the search finished"
    source = phase2.get_source(db, source_id)
    assert source is not None
    assert source.sync_state["last_sync"]["caught_up"] is True
    assert source.sync_state["cursor"] == "fake:170", "the watermark, once and only once exhausted"


def test_a_run_of_already_queued_pages_is_bounded_by_pages_not_only_by_messages(
    db: psycopg.Connection[Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    """A page of messages already handed on costs a request and queues nothing.

    Without a page bound, a long run of those — a re-read after a refused page token, say —
    would keep one job listing for as long as the search is.
    """
    mailbox = FakeMailClient(messages=synthesized_mailbox(40), page_size=1)
    use_mailbox(monkeypatch, mailbox)
    source_id = connected_source(db)
    for i in range(40):
        db.execute(
            "INSERT INTO jobs (queue, payload) VALUES ('extract', %s::jsonb)",
            (f'{{"source_id": "{source_id}", "message_id": "synth_{i:04d}"}}',),
        )

    handle_poll(context(db), {"source_id": source_id})

    assert len(mailbox.searches) == MAX_PAGES_PER_POLL
    source = phase2.get_source(db, source_id)
    assert source is not None
    assert source.sync_state["last_sync"]["queued"] == 0
    assert source.sync_state["last_sync"]["caught_up"] is False
    assert len(_jobs(db, Queue.POLL)) == 1, "and it carries on from where it stopped"


def test_an_incremental_poll_sends_the_sources_filter(
    db: psycopg.Connection[Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    """motet#95. The filter rides on the poll after the first sync, not only on the first.

    Before, an incremental poll asked for "everything added since this history id" and the
    filter was never sent — a receipt in the inbox arrived under a Newsletters filter.
    """
    mailbox = FakeMailClient(messages=synthesized_mailbox(3))
    use_mailbox(monkeypatch, mailbox)
    source = phase2.create_source(
        db,
        user_id=USER,
        kind=SourceKind.GMAIL.value,
        name="Gmail",
        config={"query": "label:newsletters"},
    )
    phase2.store_source_credential(
        db,
        build_key_manager(),
        user_id=USER,
        source_id_=source.id,
        provider=PROVIDER,
        purpose=CredentialPurpose.REFRESH.value,
        secret="fake-refresh-token",
        scopes=[GMAIL_READONLY_SCOPE],
    )

    handle_poll(context(db), {"source_id": source.id})  # the first sync
    mailbox.messages.append(RawMessage(id="synth_arrived_later", raw=b""))
    handle_poll(context(db), {"source_id": source.id})  # incremental

    assert mailbox.searches == [
        "(label:newsletters) after:0",
        "(label:newsletters) after:3",
    ], "both polls are the same search; the second is bounded by the first's watermark"
    queued = [job["payload"]["message_id"] for job in _jobs(db, Queue.EXTRACT)]
    assert queued[-1] == "synth_arrived_later"
    assert len(queued) == 4


def test_a_source_with_no_filter_of_its_own_is_polled_with_the_default(
    db: psycopg.Connection[Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    mailbox = FakeMailClient(messages=synthesized_mailbox(1))
    use_mailbox(monkeypatch, mailbox)
    source_id = connected_source(db)
    handle_poll(context(db), {"source_id": source_id})
    handle_poll(context(db), {"source_id": source_id})
    assert all(search.startswith(f"({DEFAULT_QUERY}) after:") for search in mailbox.searches)
    assert len(mailbox.searches) == 2


def test_the_first_sync_window_is_a_fact_on_the_source(
    db: psycopg.Connection[Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Recorded by the run that chose it, and left alone by the polls after it."""
    monkeypatch.setenv(FIRST_SYNC_DAYS_ENV, "30")
    source_id = connected_source(db)
    handle_poll(context(db), {"source_id": source_id})
    monkeypatch.setenv(FIRST_SYNC_DAYS_ENV, "90")
    handle_poll(context(db), {"source_id": source_id})

    source = phase2.get_source(db, source_id)
    assert source is not None
    assert source.sync_state["first_sync_days"] == 30, "what was searched, not today's setting"


def test_a_message_extraction_skipped_is_not_fetched_again_when_re_listed(
    db: psycopg.Connection[Any],
) -> None:
    """The search overlaps its previous pass, so it re-lists messages on purpose.

    One that became a source item is caught by the unique index. One extraction *skipped*
    — a receipt — has no source item, only a finished extract job, and without the job half
    of the pre-check every poll inside the overlap would fetch it again.
    """
    source_id = connected_source(db)
    handle_poll(context(db), {"source_id": source_id})
    for job in _jobs(db, Queue.EXTRACT):
        handle_extract(context(db), job["payload"])
    db.execute("UPDATE jobs SET state = 'done' WHERE queue = 'extract'")
    assert "04_receipt_too_short" not in {
        row["external_id"] for row in _source_items(db, source_id)
    }

    # Re-read the whole mailbox, as a history-id source's resync does.
    phase2.set_source_sync_state(db, source_id, {"cursor": None})
    handle_poll(context(db), {"source_id": source_id})

    with db.cursor() as cur:
        cur.execute(
            "SELECT payload ->> 'message_id' AS id, count(*) AS n FROM jobs "
            "WHERE queue = 'extract' GROUP BY 1"
        )
        counts = {row["id"]: row["n"] for row in cur.fetchall()}
    assert counts["04_receipt_too_short"] == 1
    assert set(counts.values()) == {1}


def test_a_poll_that_gives_up_says_why_on_the_source_and_keeps_its_place(
    db: psycopg.Connection[Any], database_url: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Through the runner, because the handler's own transaction is the one that rolled back.

    A revoked mailbox fails permanently on the first attempt, which is the recorder's path
    without five rounds of backoff in front of it.
    """

    class Revoked:
        def refresh(self, *, refresh_token: str) -> Any:
            raise SourceAuthError("invalid_grant")

    source_id = connected_source(db)
    phase2.set_source_sync_state(
        db, source_id, {"cursor": "fake:0:170:60", "last_sync": {"caught_up": False}}
    )
    enqueue_source_poll(db, source_id)
    db.commit()
    monkeypatch.setattr("motet_workers.ingest.build_oauth_client", lambda env=None: Revoked())

    drain(Queue.POLL, database_url)
    db.commit()

    source = phase2.get_source(db, source_id)
    assert source is not None
    assert source.last_error is not None and "reconnecting" in source.last_error
    last = source.sync_state["last_sync"]
    assert last["error"] is not None and "reconnecting" in last["error"]
    assert (last["seen"], last["queued"], last["caught_up"]) == (0, 0, False)
    assert source.sync_state["cursor"] == "fake:0:170:60", "nothing read, nothing skipped"


# --- extract -------------------------------------------------------------------------


def test_extraction_produces_a_source_item_and_holds_it(
    db: psycopg.Connection[Any],
) -> None:
    """Extract is the last free stage; the item waits there for a person.

    Integration is the first stage that spends inference, so a connected source stops
    short of it: the row is `pending` with no integrate job, which is what "held" means.
    """
    source_id = connected_source(db)
    handle_extract(context(db), {"source_id": source_id, "message_id": "01_acme_series_a"})

    items = _source_items(db, source_id)
    assert len(items) == 1
    assert items[0]["external_id"] == "01_acme_series_a"
    assert items[0]["state"] == SourceItemState.PENDING.value
    assert "Acme" in items[0]["title"]
    assert "Northwind Ventures" in items[0]["text"]
    assert "Unsubscribe" not in items[0]["text"], "the footer should have been cut"

    assert _jobs(db, Queue.INTEGRATE) == [], "extraction must not spend inference"
    held = repo.list_held_source_items(db, USER)
    assert [item.id for item in held] == [items[0]["id"]]


def test_ingest_now_queues_a_held_item_exactly_as_a_paste_would(
    db: psycopg.Connection[Any],
) -> None:
    source_id = connected_source(db)
    handle_extract(context(db), {"source_id": source_id, "message_id": "01_acme_series_a"})
    item_id = _source_items(db, source_id)[0]["id"]

    queued = enqueue_integration(db, user_id=USER, source_item_ids=[item_id, "si_nope"])
    assert queued == [item_id]
    integrate = _jobs(db, Queue.INTEGRATE)
    assert len(integrate) == 1
    assert integrate[0]["payload"]["source_item_id"] == item_id
    assert integrate[0]["serialize_key"] == USER, "invariant 6 lives on the integrate stage"

    # Asking twice writes one job: the item is no longer held.
    assert enqueue_integration(db, user_id=USER, source_item_ids=[item_id]) == []
    assert len(_jobs(db, Queue.INTEGRATE)) == 1
    assert repo.list_held_source_items(db, USER) == []


def test_a_held_item_is_dated_by_its_message_not_by_when_it_was_stored(
    db: psycopg.Connection[Any],
) -> None:
    """`received_at` is the `Date:` header — motet#91's "every row read 5:04 PM today".

    A first sync over a wide window stores every message inside one minute, so the time of
    storing says nothing about which newsletter is which. The fixture is dated in August
    2026 and stored now.
    """
    source_id = connected_source(db)
    handle_extract(context(db), {"source_id": source_id, "message_id": "01_acme_series_a"})

    (held,) = repo.list_held_source_items(db, USER)
    assert held.received_at == datetime(2026, 8, 18, 7, 2, 11, tzinfo=UTC)


def test_a_zoneless_or_missing_date_reads_as_utc_or_as_now() -> None:
    """RFC 5322's `-0000` parses naive; a naive value into `timestamptz` would take the
    session's zone instead. No header at all falls back to the time of storing."""
    assert _sent_at("2026-08-18T07:02:11") == datetime(2026, 8, 18, 7, 2, 11, tzinfo=UTC)
    assert _sent_at("2026-08-19T06:30:00-04:00") == datetime(2026, 8, 19, 10, 30, tzinfo=UTC)
    assert _sent_at("") is None
    assert _sent_at("not a date") is None


def test_a_message_dated_in_the_future_is_clamped_to_now(db: psycopg.Connection[Any]) -> None:
    """A sender's clock is not ours; next week's date would sort after everything until then."""
    source_id = connected_source(db)
    item_id = phase2.insert_polled_source_item(
        db,
        user_id=USER,
        source_id_=source_id,
        external_id="from-the-future",
        title="Tomorrow's news",
        text="Something that has not happened yet.",
        received_at=datetime.now(UTC) + timedelta(days=7),
    )
    with db.cursor() as cur:
        cur.execute(
            "SELECT received_at <= now() AS clamped FROM source_items WHERE id = %s", (item_id,)
        )
        row = cur.fetchone()
    assert row is not None and row["clamped"] is True


def test_extracting_the_same_message_twice_is_a_no_op(
    db: psycopg.Connection[Any],
) -> None:
    """A retry after the insert committed but the job update did not."""
    source_id = connected_source(db)
    payload = {"source_id": source_id, "message_id": "01_acme_series_a"}
    handle_extract(context(db), payload)
    handle_extract(context(db), payload)
    assert len(_source_items(db, source_id)) == 1
    assert _jobs(db, Queue.INTEGRATE) == []


def test_a_message_that_is_not_a_newsletter_is_skipped_not_failed(
    db: psycopg.Connection[Any],
) -> None:
    """A mailbox is mostly not newsletters.

    Treating a receipt as an error would make the source permanently red and would retry
    it five times. Skipping it, and recording why on the source, is the honest outcome.
    """
    source_id = connected_source(db)
    handle_extract(context(db), {"source_id": source_id, "message_id": "04_receipt_too_short"})
    assert _source_items(db, source_id) == []
    assert _jobs(db, Queue.INTEGRATE) == []

    source = phase2.get_source(db, source_id)
    assert source is not None
    skipped = source.sync_state.get("last_skipped")
    assert skipped is not None
    assert skipped["message_id"] == "04_receipt_too_short"
    assert "below the" in skipped["reason"]


def test_a_missing_payload_field_is_a_permanent_failure(
    db: psycopg.Connection[Any],
) -> None:
    with pytest.raises(PermanentFailure, match="message_id"):
        handle_extract(context(db), {"source_id": "src_x"})


# --- the whole path, through the real runner ------------------------------------------


def test_gmail_ingestion_reaches_the_backlog(
    db: psycopg.Connection[Any], database_url: str
) -> None:
    """`poll -> extract`, then "ingest now", then `integrate` — drained by the actual runner.

    The point of going through `drain` rather than calling handlers is that it exercises the
    three transaction boundaries and the advisory lock — which is where a serialization bug
    would live, and which a direct handler call would skip entirely. The explicit step in
    the middle is the product: nothing reaches dedup until a person asks.
    """
    source_id = connected_source(db)
    enqueue_source_poll(db, source_id)
    db.commit()

    assert drain(Queue.POLL, database_url) == 1
    assert drain(Queue.EXTRACT, database_url) >= 3
    assert drain(Queue.INTEGRATE, database_url) == 0, "nothing is queued until asked for"
    assert repo.list_news_items(db, USER) == []

    held = [item.id for item in repo.list_held_source_items(db, USER)]
    assert len(held) >= 3
    assert enqueue_integration(db, user_id=USER, source_item_ids=held) == held
    db.commit()
    assert drain(Queue.INTEGRATE, database_url) >= 3

    items = repo.list_news_items(db, USER)
    assert items, "polled newsletters should have become news items"
    titles = " ".join(item.title for item in items)
    assert "Acme" in titles
    assert "Northbridge" in titles

    # Every stored source item reached the integrate stage.
    with db.cursor() as cur:
        cur.execute("SELECT state FROM source_items WHERE source_id = %s", (source_id,))
        states = {row["state"] for row in cur.fetchall()}
    assert states == {SourceItemState.INTEGRATED.value}


def test_a_second_full_run_adds_nothing(db: psycopg.Connection[Any], database_url: str) -> None:
    """The property that makes a scheduled poll safe to run every five minutes."""

    def full_run() -> None:
        enqueue_source_poll(db, source_id)
        db.commit()
        for queue in (Queue.POLL, Queue.EXTRACT):
            drain(queue, database_url)
        held = [item.id for item in repo.list_held_source_items(db, USER)]
        enqueue_integration(db, user_id=USER, source_item_ids=held)
        db.commit()
        drain(Queue.INTEGRATE, database_url)

    source_id = connected_source(db)
    full_run()
    before = {item.id for item in repo.list_news_items(db, USER)}
    assert before

    full_run()
    assert {item.id for item in repo.list_news_items(db, USER)} == before
    assert repo.list_held_source_items(db, USER) == [], "a re-poll holds nothing new"


# --- a message that never becomes a source item ---------------------------------------
#
# motet#35. `handle_extract` writes the `source_items` row when extraction *succeeds*, so
# until then the extract job row is the only record that the message was ever seen — and
# `handle_poll` advanced the cursor in the same transaction that queued it, so nothing
# will ever look at that message again. These go through `drain` rather than calling the
# handler, because what is under test is what survives the *runner's* failure path: the
# job row it writes, and whether the accounting surface can see it.


class _Unreachable:
    """A mailbox that lists fine and then fails every fetch, as a provider outage does."""

    def __init__(self, mailbox: Any) -> None:
        self._mailbox = mailbox

    def list_messages(self, **kwargs: Any) -> Any:
        return self._mailbox.list_messages(**kwargs)

    def fetch_message(self, message_id: str) -> Any:
        raise RuntimeError("gmail returned 503 for message " + message_id)


def _burn_the_retry_ladder(db: psycopg.Connection[Any], database_url: str) -> None:
    """Drain `extract` until the queue gives up on it.

    The backoff schedules each retry into the future, so the clock is moved rather than
    waited on — `run_at` is the only thing between a claimable job and one that is not.
    """
    for _ in range(DEFAULT_MAX_ATTEMPTS):
        db.execute("UPDATE jobs SET run_at = now() WHERE queue = 'extract'")
        db.commit()
        drain(Queue.EXTRACT, database_url)
    db.commit()


def test_a_message_that_never_extracts_is_reported_rather_than_lost(
    db: psycopg.Connection[Any], database_url: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The defect, end to end: polled, failed five times, and accounted for.

    Before this the answer was nothing at all — no source item, no news item, and an
    ingestion list built from `source_items` that could not report a row that does not
    exist. The message was gone, and the surface whose entire job is "where did my content
    go" said everything was fine.
    """
    from motet_sources import FakeMailClient

    source_id = connected_source(db)
    enqueue_source_poll(db, source_id)
    db.commit()
    assert drain(Queue.POLL, database_url) == 1

    monkeypatch.setattr(
        "motet_workers.ingest.build_mail_client",
        lambda token, env=None: _Unreachable(FakeMailClient()),
    )
    _burn_the_retry_ladder(db, database_url)

    # Nothing else in the system knows these messages existed.
    assert _source_items(db, source_id) == []
    assert repo.list_news_items(db, USER) == []

    statuses = repo.list_ingestion(db, USER)
    assert len(statuses) >= 3, "every polled message should be accounted for"
    for status in statuses:
        assert status.state is SourceItemState.FAILED
        assert status.attempts == DEFAULT_MAX_ATTEMPTS
        assert status.last_error is not None and "503" in status.last_error
        assert status.next_attempt_at is None
        assert status.source_kind == SourceKind.GMAIL.value
        assert status.title.startswith("Gmail message ")


def test_a_revoked_mailbox_reports_every_message_it_could_not_fetch(
    db: psycopg.Connection[Any], database_url: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The other failure class, and the one the extract stage cannot recover from.

    An auth failure happens *before* a single byte of the message is fetched, which is why
    the fix reads the job row rather than writing a source item from the raw message: at
    this point there is no raw message. The reason has to say `reconnecting` rather than
    the transport error above, because the repairs are different — one is a wait and the
    other is a consent screen.
    """

    class Revoked:
        def refresh(self, *, refresh_token: str) -> Any:
            raise SourceAuthError("invalid_grant")

    source_id = connected_source(db)
    enqueue_source_poll(db, source_id)
    db.commit()
    drain(Queue.POLL, database_url)

    # The poll left a fresh access token behind; dropping it is what sends the extract
    # stage back to the refresh grant the user has revoked.
    db.execute("DELETE FROM source_credentials WHERE purpose = 'access'")
    db.commit()
    monkeypatch.setattr("motet_workers.ingest.build_oauth_client", lambda env=None: Revoked())

    drain(Queue.EXTRACT, database_url)
    db.commit()

    statuses = repo.list_ingestion(db, USER)
    assert statuses, "a revoked mailbox must not swallow the messages it already saw"
    for status in statuses:
        assert status.state is SourceItemState.FAILED
        # One attempt, not five: only re-consent fixes this, so the ladder is skipped.
        assert status.attempts == 1
        assert status.last_error is not None and "reconnecting" in status.last_error


def test_a_message_still_queued_for_extraction_is_visible_before_it_fails(
    db: psycopg.Connection[Any], database_url: str
) -> None:
    """Not only failures. A queued fetch is content on its way in, and it says so.

    Same reason the paste half reports pending items: "working on it" and "nothing is
    coming for this" look identical from outside, and one Gmail poll can queue fifty.
    """
    source_id = connected_source(db)
    enqueue_source_poll(db, source_id)
    db.commit()
    drain(Queue.POLL, database_url)
    db.commit()

    statuses = repo.list_ingestion(db, USER)
    assert len(statuses) >= 3
    assert all(status.state is SourceItemState.PENDING for status in statuses)
    assert all(status.attempts == 0 for status in statuses)
    assert all(status.next_attempt_at is not None for status in statuses)


def test_extraction_succeeding_replaces_the_job_row_with_the_item_it_wrote(
    db: psycopg.Connection[Any], database_url: str
) -> None:
    """One message is one line, and extraction is the moment it changes which line.

    Since motet#91 the line it moves to is the *held* list, not the ingestion list: an
    extracted message is waiting for a person, not on its way in, and reporting it as
    pending is what made the Processing panel call it stalled. "Ingest now" is what moves
    it onto the ingestion list — once, from its own row.

    The idempotence case the unique index guarantees is the one that could break this
    quietly: a job re-run after its insert committed leaves both records describing one
    message, and a panel showing it twice would be the accounting surface disagreeing with
    itself.
    """
    source_id = connected_source(db)
    enqueue_source_poll(db, source_id)
    db.commit()
    drain(Queue.POLL, database_url)
    drain(Queue.EXTRACT, database_url)
    db.commit()

    items = _source_items(db, source_id)
    item_ids = {item["id"] for item in items}
    assert items
    assert repo.list_ingestion(db, USER) == [], "held is not in flight"
    assert {item.id for item in repo.list_held_source_items(db, USER)} == item_ids

    # Re-running an extract job that already has its row — the reclaimed-lease case —
    # changes neither the row count nor either list.
    for job in _jobs_any_state(db, Queue.EXTRACT):
        handle_extract(context(db), job["payload"])
    db.commit()
    assert len(_source_items(db, source_id)) == len(items)
    assert repo.list_ingestion(db, USER) == []
    assert {item.id for item in repo.list_held_source_items(db, USER)} == item_ids

    enqueue_integration(db, user_id=USER, source_item_ids=sorted(item_ids))
    db.commit()
    statuses = repo.list_ingestion(db, USER)
    assert len(statuses) == len(items), "each message is reported once, from its own row"
    assert {status.id for status in statuses} == item_ids
    assert all(not status.title.startswith("Gmail message ") for status in statuses)
    assert repo.list_held_source_items(db, USER) == []


def _jobs(db: psycopg.Connection[Any], queue: Queue) -> list[dict[str, Any]]:
    with db.cursor() as cur:
        cur.execute(
            "SELECT payload, serialize_key FROM jobs WHERE queue = %s AND state = 'ready' "
            "ORDER BY id",
            (queue.value,),
        )
        return list(cur.fetchall())


def _clear(db: psycopg.Connection[Any], queue: Queue) -> None:
    db.execute("DELETE FROM jobs WHERE queue = %s", (queue.value,))


def _source_items(db: psycopg.Connection[Any], source_id: str) -> list[dict[str, Any]]:
    with db.cursor() as cur:
        cur.execute(
            "SELECT id, external_id, title, text, state FROM source_items WHERE source_id = %s "
            "ORDER BY created_at, id",
            (source_id,),
        )
        return list(cur.fetchall())


def _jobs_any_state(db: psycopg.Connection[Any], queue: Queue) -> list[dict[str, Any]]:
    """Every job on a queue, whatever became of it — including the ones already done."""
    with db.cursor() as cur:
        cur.execute("SELECT payload FROM jobs WHERE queue = %s ORDER BY id", (queue.value,))
        return list(cur.fetchall())


def test_two_concurrent_ingest_nows_write_one_integrate_job(
    db: psycopg.Connection[Any], database_url: str
) -> None:
    """The claim's per-user transaction lock is what stops a double-enqueue.

    Two tabs press "Ingest now" on the same item at once. The first holds the lock with
    its job written but not committed; the second must wait, and then see the job, rather
    than answer from a snapshot in which the item still looked held.
    """
    import threading

    source_id = connected_source(db)
    handle_extract(context(db), {"source_id": source_id, "message_id": "01_acme_series_a"})
    db.commit()
    (item_id,) = [item.id for item in repo.list_held_source_items(db, USER)]

    first = repo.connect(database_url)
    second = repo.connect(database_url)
    try:
        assert enqueue_integration(first, user_id=USER, source_item_ids=[item_id]) == [item_id]
        answer: list[list[str]] = []
        waiter = threading.Thread(
            target=lambda: answer.append(
                enqueue_integration(second, user_id=USER, source_item_ids=[item_id])
            )
        )
        waiter.start()
        waiter.join(timeout=1.0)
        assert waiter.is_alive(), "the second claim must wait on the first's lock"

        first.commit()
        waiter.join(timeout=10.0)
        second.commit()
        assert answer == [[]], "and then find the item already queued"
    finally:
        first.close()
        second.close()

    assert len(_jobs(db, Queue.INTEGRATE)) == 1


def test_a_dismiss_and_an_ingest_now_racing_for_one_item_cannot_both_win(
    db: psycopg.Connection[Any], database_url: str
) -> None:
    """They take the same lock, and the second re-reads the held predicate after it.

    Otherwise an item could be dismissed *and* queued — a job that runs on an item the
    person said not to spend on.
    """
    import threading

    source_id = connected_source(db)
    handle_extract(context(db), {"source_id": source_id, "message_id": "01_acme_series_a"})
    db.commit()
    (item_id,) = [item.id for item in repo.list_held_source_items(db, USER)]

    dismisser = repo.connect(database_url)
    ingester = repo.connect(database_url)
    try:
        assert repo.dismiss_held_source_items(dismisser, USER, [item_id]) == [item_id]
        answer: list[list[str]] = []
        waiter = threading.Thread(
            target=lambda: answer.append(
                enqueue_integration(ingester, user_id=USER, source_item_ids=[item_id])
            )
        )
        waiter.start()
        waiter.join(timeout=1.0)
        assert waiter.is_alive(), "the ingest must wait on the dismiss's lock"

        dismisser.commit()
        waiter.join(timeout=10.0)
        ingester.commit()
        assert answer == [[]], "and then find the item no longer held"
    finally:
        dismisser.close()
        ingester.close()

    assert _jobs(db, Queue.INTEGRATE) == []
