"""The poll and extract stages, end to end against a real queue and a fake mailbox.

What these defend is not "Gmail works" — the mailbox is a fake, and it has to be, because
the Google OAuth client does not exist. What they defend is the *pipeline shape*: that a
crashed poll is safe to retry, that a message becomes exactly one source item, that the
cursor and the enqueued work move together, and that a revoked mailbox stops rather than
retrying forever.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import psycopg
import pytest
from motet_db import CredentialPurpose, SourceItemState, SourceKind, phase2, repo
from motet_inference import fake_stages
from motet_sources import GMAIL_READONLY_SCOPE, PROVIDER, SourceAuthError
from motet_storage import LocalObjectStore
from motet_vault import build_key_manager
from motet_workers import Queue, drain, enqueue_source_poll, poll_key
from motet_workers.handlers import Context, PermanentFailure
from motet_workers.ingest import handle_extract, handle_poll
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


def test_an_expired_cursor_schedules_a_resync_rather_than_failing(
    db: psycopg.Connection[Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    """A provider's history window moving past our bookmark is not an error.

    The repair is a bounded first sync, which is a different action from a retry — so the
    cursor is dropped and a fresh poll is queued, and the source records why.
    """
    from motet_sources import FakeMailClient

    source_id = connected_source(db)
    phase2.set_source_sync_state(db, source_id, {"cursor": "999"})

    monkeypatch.setattr(
        "motet_workers.ingest.build_mail_client",
        lambda token, env=None: FakeMailClient(expire_cursor=True),
    )
    handle_poll(context(db), {"source_id": source_id})

    source = phase2.get_source(db, source_id)
    assert source is not None
    assert source.sync_state["cursor"] is None
    assert source.last_error is not None and "history window" in source.last_error
    assert len(_jobs(db, Queue.POLL)) == 1, "a fresh bounded sync should be queued"


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


# --- extract -------------------------------------------------------------------------


def test_extraction_produces_a_source_item_and_queues_integration(
    db: psycopg.Connection[Any],
) -> None:
    source_id = connected_source(db)
    handle_extract(context(db), {"source_id": source_id, "message_id": "01_acme_series_a"})

    items = _source_items(db, source_id)
    assert len(items) == 1
    assert items[0]["external_id"] == "01_acme_series_a"
    assert "Acme" in items[0]["title"]
    assert "Northwind Ventures" in items[0]["text"]
    assert "Unsubscribe" not in items[0]["text"], "the footer should have been cut"

    integrate = _jobs(db, Queue.INTEGRATE)
    assert len(integrate) == 1
    assert integrate[0]["payload"]["source_item_id"] == items[0]["id"]
    assert integrate[0]["serialize_key"] == USER, "invariant 6 lives on the integrate stage"


def test_extracting_the_same_message_twice_is_a_no_op(
    db: psycopg.Connection[Any],
) -> None:
    """A retry after the insert committed but the job update did not."""
    source_id = connected_source(db)
    payload = {"source_id": source_id, "message_id": "01_acme_series_a"}
    handle_extract(context(db), payload)
    handle_extract(context(db), payload)
    assert len(_source_items(db, source_id)) == 1
    assert len(_jobs(db, Queue.INTEGRATE)) == 1, "and exactly one integrate job"


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
    """`poll -> extract -> integrate`, drained by the actual runner.

    The point of going through `drain` rather than calling handlers is that it exercises the
    three transaction boundaries and the advisory lock — which is where a serialization bug
    would live, and which a direct handler call would skip entirely.
    """
    source_id = connected_source(db)
    enqueue_source_poll(db, source_id)
    db.commit()

    assert drain(Queue.POLL, database_url) == 1
    assert drain(Queue.EXTRACT, database_url) >= 3
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
    source_id = connected_source(db)
    enqueue_source_poll(db, source_id)
    db.commit()
    for queue in (Queue.POLL, Queue.EXTRACT, Queue.INTEGRATE):
        drain(queue, database_url)
    before = {item.id for item in repo.list_news_items(db, USER)}

    enqueue_source_poll(db, source_id)
    db.commit()
    for queue in (Queue.POLL, Queue.EXTRACT, Queue.INTEGRATE):
        drain(queue, database_url)

    assert {item.id for item in repo.list_news_items(db, USER)} == before


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
    statuses = repo.list_ingestion(db, USER)
    assert len(statuses) == len(items), "each message is reported once, from its own row"
    assert {status.id for status in statuses} == {item["id"] for item in items}
    assert all(not status.title.startswith("Gmail message ") for status in statuses)

    # And re-running an extract job that already has its row — the reclaimed-lease case —
    # changes neither the row count nor the ingestion list.
    for job in _jobs_any_state(db, Queue.EXTRACT):
        handle_extract(context(db), job["payload"])
    db.commit()
    assert len(_source_items(db, source_id)) == len(items)
    assert {status.id for status in repo.list_ingestion(db, USER)} == {item["id"] for item in items}


# --- helpers -------------------------------------------------------------------------


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
            "SELECT id, external_id, title, text FROM source_items WHERE source_id = %s "
            "ORDER BY created_at, id",
            (source_id,),
        )
        return list(cur.fetchall())


def _jobs_any_state(db: psycopg.Connection[Any], queue: Queue) -> list[dict[str, Any]]:
    """Every job on a queue, whatever became of it — including the ones already done."""
    with db.cursor() as cur:
        cur.execute("SELECT payload FROM jobs WHERE queue = %s ORDER BY id", (queue.value,))
        return list(cur.fetchall())
