"""Moving a Gmail message between labels when its owner ingests it — motet#96.

What these defend is the *trigger* and the *failure direction*, because those are the two
ways this feature can do damage to a real mailbox without anything turning red:

* **Only a deliberate ingest writes.** A poll, an extract, the automatic integrate
  ``handle_extract`` queues, and a paste never reach ``modify_labels``. Asserted on the fake
  mailbox's own record of every call, so "no call" means none rather than "none we saw".
* **Only after the commit.** The write happens once the dedup transaction and the job's
  completion have landed — checked from a *second* connection at the moment the fake is
  called — and an integrate that rolls back writes nothing at all.
* **A failed write never fails the ingest.** The job is ``done``, the item is integrated,
  and the reason is on the item.

Through ``drain`` wherever the claim is about ordering, because the transaction boundaries
live in the runner and a direct handler call would skip them.
"""

from __future__ import annotations

import dataclasses
from datetime import UTC, datetime, timedelta
from typing import Any

import psycopg
import pytest
from motet_db import CredentialPurpose, SourceItemState, SourceKind, phase2, repo
from motet_inference import fake_stages
from motet_sources import (
    GMAIL_MODIFY_SCOPE,
    GMAIL_READONLY_SCOPE,
    LABEL_SYNC_SCOPES,
    PROVIDER,
    FakeMailClient,
    FakeOAuthClient,
    Label,
    SourceError,
)
from motet_sources.labels import CATALOG_KEY, CONFIG_KEY, catalog_to_sync_state
from motet_vault import build_key_manager
from motet_workers import (
    Queue,
    drain,
    enqueue_integration,
    enqueue_paste,
    enqueue_source_poll,
)
from motet_workers import labels as labels_module
from motet_workers.handlers import Context, handle_integrate
from motet_workers.ingest import grant_stamp, mint_token
from motet_workers.labels import DELIBERATE_KEY

USER = repo.OWNER_USER_ID
OWNER_PAIR = {"remove": "Newsletters", "add": "Completed"}


@pytest.fixture(autouse=True)
def _local_vault(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MOTET_VAULT_BACKEND", "local")
    monkeypatch.setenv("MOTET_INFERENCE_MODE", "fake")


class _Outcomes:
    """Stands in for the ``motet.gmail.label_writeback`` counter and keeps what it was told."""

    def __init__(self) -> None:
        self.outcomes: list[str] = []

    def add(self, amount: int, attributes: dict[str, str]) -> None:
        assert amount == 1
        self.outcomes.append(attributes["outcome"])


@pytest.fixture
def outcomes(monkeypatch: pytest.MonkeyPatch) -> _Outcomes:
    recorder = _Outcomes()
    monkeypatch.setattr(labels_module, "_writebacks", recorder)
    return recorder


@pytest.fixture
def mailbox(monkeypatch: pytest.MonkeyPatch) -> FakeMailClient:
    """One mailbox for the whole test, shared by the poll, the extract and the write-back.

    ``build_mail_client`` hands out a fresh fake per call, which would lose the record of
    calls between stages; patching both call sites to the same instance is what lets a test
    say "no stage ever asked to modify".
    """
    shared = FakeMailClient()
    monkeypatch.setattr("motet_workers.ingest.build_mail_client", lambda token, env=None: shared)
    monkeypatch.setattr("motet_workers.labels.build_mail_client", lambda token, env=None: shared)
    monkeypatch.setattr(
        "motet_workers.ingest.build_oauth_client",
        lambda env=None: FakeOAuthClient(granted_scopes=LABEL_SYNC_SCOPES),
    )
    return shared


def gmail_source(
    db: psycopg.Connection[Any],
    *,
    scopes: tuple[str, ...] = LABEL_SYNC_SCOPES,
    labels: dict[str, str] | None = OWNER_PAIR,
) -> str:
    """A connected Gmail source, as the OAuth callback leaves it, with label sync set."""
    source = phase2.create_source(
        db,
        user_id=USER,
        kind=SourceKind.GMAIL.value,
        name="Gmail",
        config={CONFIG_KEY: labels} if labels else {},
    )
    phase2.store_source_credential(
        db,
        build_key_manager(),
        user_id=USER,
        source_id_=source.id,
        provider=PROVIDER,
        purpose=CredentialPurpose.REFRESH.value,
        secret="fake-refresh-token",
        scopes=scopes,
    )
    db.commit()
    return source.id


def held_items(
    db: psycopg.Connection[Any], database_url: str, source_id: str
) -> list[dict[str, Any]]:
    """Poll and extract: each item is then held, as motet#91's gate leaves it — extracted,
    pending, with no job — for the owner to ingest the one way that counts as deliberate."""
    enqueue_source_poll(db, source_id)
    db.commit()
    drain(Queue.POLL, database_url)
    drain(Queue.EXTRACT, database_url)
    db.commit()
    return _items(db, source_id)


def ingest_now(db: psycopg.Connection[Any], item_ids: list[str]) -> None:
    """ "Ingest now" — the real claim-and-enqueue the API's route calls."""
    assert enqueue_integration(db, user_id=USER, source_item_ids=item_ids) == item_ids
    db.commit()


def raw_integrate_job(db: psycopg.Connection[Any], item_id: str, *, deliberate: bool) -> None:
    """An integrate job written directly — a paste's, a stale one — bypassing the claim."""
    from motet_workers import enqueue as enqueue_job

    payload: dict[str, Any] = {"source_item_id": item_id}
    if deliberate:
        payload[DELIBERATE_KEY] = True
    enqueue_job(db, Queue.INTEGRATE, payload, serialize_key=USER)
    db.commit()


# --- (a) only after a committed integrate ------------------------------------------------


def test_a_deliberate_ingest_moves_the_message_after_the_integrate_commits(
    db: psycopg.Connection[Any],
    database_url: str,
    mailbox: FakeMailClient,
    outcomes: _Outcomes,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The exact call, and proof it was made after the work was durable.

    The check runs *inside* the fake's ``modify_labels``, on a connection of its own, so it
    sees only what has committed: if the write-back ran inside the dedup transaction the
    item would still read ``pending`` there, and the job ``running``.
    """
    source_id = gmail_source(db)
    item = held_items(db, database_url, source_id)[0]
    seen_at_call: list[tuple[str, str]] = []
    real_modify = mailbox.modify_labels

    def modify_and_look(message_id: str, **kwargs: Any) -> None:
        with psycopg.connect(database_url) as other:
            state = other.execute(
                "SELECT state FROM source_items WHERE id = %s", (item["id"],)
            ).fetchone()
            job = other.execute(
                "SELECT state FROM jobs WHERE queue = 'integrate' "
                "AND payload ->> 'source_item_id' = %s",
                (item["id"],),
            ).fetchone()
        assert state is not None and job is not None
        seen_at_call.append((state[0], job[0]))
        real_modify(message_id, **kwargs)

    monkeypatch.setattr(mailbox, "modify_labels", modify_and_look)
    ingest_now(db, [item["id"]])

    assert drain(Queue.INTEGRATE, database_url) == 1
    assert mailbox.modify_calls == [(item["external_id"], ("Label_102",), ("Label_101",))]
    assert seen_at_call == [(SourceItemState.INTEGRATED.value, "done")]
    assert outcomes.outcomes == ["applied"]

    db.commit()
    row = _items(db, source_id, item_id=item["id"])[0]
    assert row["label_synced_at"] is not None
    assert row["label_error"] is None


def test_an_integrate_that_rolls_back_moves_nothing(
    db: psycopg.Connection[Any],
    database_url: str,
    mailbox: FakeMailClient,
    outcomes: _Outcomes,
) -> None:
    """The write is registered inside the handler and discarded with it.

    A label moved for a story that never landed would be the mailbox recording a read that
    Motet has no record of — and the retry that later succeeds writes it then, once.
    """

    class Down:
        def integrate(self, item: Any, window: Any) -> Any:
            raise RuntimeError("dedup is down")

    source_id = gmail_source(db)
    item = held_items(db, database_url, source_id)[0]
    ingest_now(db, [item["id"]])

    drain(
        Queue.INTEGRATE, database_url, stages=dataclasses.replace(fake_stages(), integrator=Down())
    )
    assert mailbox.modify_calls == []
    assert outcomes.outcomes == []
    db.commit()
    job = db.execute(
        "SELECT state FROM jobs WHERE queue = 'integrate' AND payload ->> 'source_item_id' = %s",
        (item["id"],),
    ).fetchone()
    assert job is not None and job["state"] == "ready", "the integrate is being retried"

    # The retry that succeeds is the one that writes.
    db.execute("UPDATE jobs SET run_at = now() WHERE queue = 'integrate'")
    db.commit()
    assert drain(Queue.INTEGRATE, database_url) == 1
    assert [call[0] for call in mailbox.modify_calls] == [item["external_id"]]


def test_the_handler_registers_the_write_and_does_not_make_it(
    db: psycopg.Connection[Any],
    database_url: str,
    mailbox: FakeMailClient,
) -> None:
    """Called directly — no runner, no commit — the handler leaves one step and no call."""
    source_id = gmail_source(db)
    item = held_items(db, database_url, source_id)[0]
    context = Context(conn=db, stages=fake_stages(), store=None)  # type: ignore[arg-type]

    handle_integrate(context, {"source_item_id": item["id"], DELIBERATE_KEY: True})
    assert len(context.after_commit) == 1
    assert mailbox.modify_calls == []


# --- (b) no mailbox write on poll, extract, the automatic integrate, or paste -----------


def test_poll_extract_and_the_automatic_integrate_never_modify_the_mailbox(
    db: psycopg.Connection[Any],
    database_url: str,
    mailbox: FakeMailClient,
    outcomes: _Outcomes,
) -> None:
    """The whole automatic path, with label sync set and the wide grant in hand.

    Every precondition for a write is true except the one that matters — nobody asked —
    and so nothing is written: poll and extract hold every item, and an integrate job
    without the flag (one queued before the ingest gate existed) integrates and writes
    nothing. The poll does *read* the label list, for the settings pickers; that is a read
    under the readonly scope and is asserted here too, so the difference stays visible.
    """
    source_id = gmail_source(db)
    enqueue_source_poll(db, source_id)
    db.commit()
    assert drain(Queue.POLL, database_url) == 1
    assert drain(Queue.EXTRACT, database_url) >= 3
    assert drain(Queue.INTEGRATE, database_url) == 0, "extraction holds; nothing is queued"

    items = _items(db, source_id)
    for item in items:
        raw_integrate_job(db, item["id"], deliberate=False)
    assert drain(Queue.INTEGRATE, database_url) == len(items)

    assert repo.list_news_items(db, USER), "the unflagged jobs did ingest"
    assert mailbox.modify_calls == []
    assert outcomes.outcomes == []
    assert mailbox.label_lists == 1, "the poll reads the catalog, once"
    db.commit()
    source = phase2.get_source(db, source_id)
    assert source is not None and "Completed" in str(source.sync_state[CATALOG_KEY])
    assert all(
        row["label_synced_at"] is None and row["label_error"] is None
        for row in _items(db, source_id)
    )


def test_a_paste_never_reaches_a_mailbox_even_when_ingested_deliberately(
    db: psycopg.Connection[Any],
    database_url: str,
    mailbox: FakeMailClient,
    outcomes: _Outcomes,
) -> None:
    """A paste has no mailbox, so the flag alone must not be enough to reach one."""
    gmail_source(db)  # a mailbox with label sync on exists, and is not this item's source
    pasted = enqueue_paste(db, user_id=USER, title="Pasted", text="Acme raises $20M. " * 20)
    db.execute("DELETE FROM jobs WHERE queue = 'integrate'")
    raw_integrate_job(db, pasted.id, deliberate=True)

    assert drain(Queue.INTEGRATE, database_url) == 1
    assert mailbox.modify_calls == []
    assert outcomes.outcomes == []


def test_a_mailbox_without_label_settings_is_never_written(
    db: psycopg.Connection[Any],
    database_url: str,
    mailbox: FakeMailClient,
    outcomes: _Outcomes,
) -> None:
    source_id = gmail_source(db, labels=None)
    item = held_items(db, database_url, source_id)[0]
    ingest_now(db, [item["id"]])
    assert drain(Queue.INTEGRATE, database_url) == 1
    assert mailbox.modify_calls == []
    assert outcomes.outcomes == [], "off means no attempt, not a failed one"


# --- (c) a read-only grant stays read-only ----------------------------------------------


def test_a_read_only_grant_is_recorded_as_needing_reauthorization_without_a_gmail_call(
    db: psycopg.Connection[Any],
    database_url: str,
    mailbox: FakeMailClient,
    outcomes: _Outcomes,
) -> None:
    """Settings alone widen nothing. The worker asks the grant, not Gmail."""
    source_id = gmail_source(db, scopes=(GMAIL_READONLY_SCOPE,))
    item = held_items(db, database_url, source_id)[0]
    lists_before = mailbox.label_lists
    ingest_now(db, [item["id"]])

    assert drain(Queue.INTEGRATE, database_url) == 1
    assert mailbox.modify_calls == []
    assert mailbox.label_lists == lists_before, "no Gmail call of any kind"
    assert outcomes.outcomes == ["needs_reauthorization"]
    db.commit()
    row = _items(db, source_id, item_id=item["id"])[0]
    assert row["label_error"] is not None and "Re-authorize" in row["label_error"]
    assert row["label_synced_at"] is None


# --- (d) a failed write is recorded and the job still succeeds ---------------------------


@pytest.mark.parametrize(
    ("error", "outcome"),
    [
        (SourceError("Gmail is unavailable (503); retry later"), "failed"),
        (RuntimeError("a bug in the write-back"), "failed"),
    ],
)
def test_a_write_back_failure_is_recorded_and_the_job_still_succeeds(
    db: psycopg.Connection[Any],
    database_url: str,
    mailbox: FakeMailClient,
    outcomes: _Outcomes,
    monkeypatch: pytest.MonkeyPatch,
    error: Exception,
    outcome: str,
) -> None:
    source_id = gmail_source(db)
    item = held_items(db, database_url, source_id)[0]

    def refuse(message_id: str, **kwargs: Any) -> None:
        raise error

    monkeypatch.setattr(mailbox, "modify_labels", refuse)
    ingest_now(db, [item["id"]])

    assert drain(Queue.INTEGRATE, database_url) == 1
    assert outcomes.outcomes == [outcome]
    db.commit()
    job = db.execute(
        "SELECT state, attempts FROM jobs WHERE queue = 'integrate' "
        "AND payload ->> 'source_item_id' = %s",
        (item["id"],),
    ).fetchone()
    assert job is not None and job["state"] == "done" and job["attempts"] == 1
    row = _items(db, source_id, item_id=item["id"])[0]
    assert row["state"] == SourceItemState.INTEGRATED.value
    assert row["label_synced_at"] is None
    assert row["label_error"] is not None and str(error) in row["label_error"]
    assert repo.list_news_items(db, USER), "the item is in the backlog regardless"


# --- the label-id cache ---------------------------------------------------------------


def test_a_name_missing_from_the_cached_catalog_is_re_read_once(
    db: psycopg.Connection[Any],
    database_url: str,
    mailbox: FakeMailClient,
    outcomes: _Outcomes,
) -> None:
    """The owner created ``Completed`` after the last poll read the labels."""
    source_id = gmail_source(db)
    item = held_items(db, database_url, source_id)[0]
    _cache(db, source_id, [Label("Label_101", "Newsletters")])
    lists_before = mailbox.label_lists
    ingest_now(db, [item["id"]])

    assert drain(Queue.INTEGRATE, database_url) == 1
    assert mailbox.label_lists == lists_before + 1
    assert mailbox.modify_calls == [(item["external_id"], ("Label_102",), ("Label_101",))]
    assert outcomes.outcomes == ["applied"]
    db.commit()
    source = phase2.get_source(db, source_id)
    assert source is not None
    assert "Label_102" in str(source.sync_state[CATALOG_KEY]), "the re-read is cached"
    assert source.sync_state.get("cursor"), "and the merge left the poll's cursor alone"


def test_a_stale_cached_label_id_is_re_resolved_and_retried_once(
    db: psycopg.Connection[Any],
    database_url: str,
    mailbox: FakeMailClient,
    outcomes: _Outcomes,
) -> None:
    """``Completed`` was deleted and recreated, so the cached id no longer exists."""
    source_id = gmail_source(db)
    item = held_items(db, database_url, source_id)[0]
    _cache(db, source_id, [Label("Label_101", "Newsletters"), Label("Label_OLD", "Completed")])
    lists_before = mailbox.label_lists
    ingest_now(db, [item["id"]])

    assert drain(Queue.INTEGRATE, database_url) == 1
    assert mailbox.label_lists == lists_before + 1
    assert mailbox.modify_calls == [(item["external_id"], ("Label_102",), ("Label_101",))]
    assert outcomes.outcomes == ["applied"]


def test_a_label_the_mailbox_does_not_have_is_recorded_not_created(
    db: psycopg.Connection[Any],
    database_url: str,
    mailbox: FakeMailClient,
    outcomes: _Outcomes,
) -> None:
    source_id = gmail_source(db, labels={"remove": "Newsletters", "add": "Archive"})
    item = held_items(db, database_url, source_id)[0]
    ingest_now(db, [item["id"]])

    assert drain(Queue.INTEGRATE, database_url) == 1
    assert mailbox.modify_calls == []
    assert outcomes.outcomes == ["label_not_found"]
    db.commit()
    row = _items(db, source_id, item_id=item["id"])[0]
    assert row["label_error"] is not None and "'Archive'" in row["label_error"]


def test_an_access_token_minted_before_the_reconsent_is_refreshed_before_writing(
    db: psycopg.Connection[Any],
    database_url: str,
    mailbox: FakeMailClient,
    outcomes: _Outcomes,
) -> None:
    """A cached token from the read-only grant is valid for an hour and cannot write."""
    source_id = gmail_source(db)
    item = held_items(db, database_url, source_id)[0]
    phase2.store_source_credential(
        db,
        build_key_manager(),
        user_id=USER,
        source_id_=source_id,
        provider=PROVIDER,
        purpose=CredentialPurpose.ACCESS.value,
        secret="fake-access-from-the-readonly-grant",
        scopes=(GMAIL_READONLY_SCOPE,),
        expires_at=datetime.now(UTC) + timedelta(minutes=50),
    )
    ingest_now(db, [item["id"]])

    assert drain(Queue.INTEGRATE, database_url) == 1
    assert outcomes.outcomes == ["applied"]
    db.commit()
    access = phase2.get_source_credential(
        db, source_id_=source_id, purpose=CredentialPurpose.ACCESS.value
    )
    assert access is not None and GMAIL_MODIFY_SCOPE in access.scopes


# --- review findings: replay, lock, account, pause, catalog age ------------------------


def test_a_replayed_integrate_job_does_not_write_again(
    db: psycopg.Connection[Any],
    database_url: str,
    mailbox: FakeMailClient,
    outcomes: _Outcomes,
) -> None:
    """The work fence completes a replay without calling a handler, so nothing registers.

    A worker that died between the integrate's commit and ``jobs.complete`` leaves the row
    ``running`` with ``work_committed_attempt`` set; the reclaim settles it. The write-back it
    may or may not have made is not made again — a lost write, never a second one.
    """
    source_id = gmail_source(db)
    item = held_items(db, database_url, source_id)[0]
    ingest_now(db, [item["id"]])
    db.execute(
        "UPDATE jobs SET work_committed_attempt = 1 WHERE queue = 'integrate' "
        "AND payload ->> 'source_item_id' = %s",
        (item["id"],),
    )
    db.commit()

    assert drain(Queue.INTEGRATE, database_url) == 1
    assert mailbox.modify_calls == []
    assert outcomes.outcomes == []


def test_a_deliberate_job_for_an_item_already_integrated_writes_nothing(
    db: psycopg.Connection[Any],
    database_url: str,
    mailbox: FakeMailClient,
    outcomes: _Outcomes,
) -> None:
    """``handle_integrate`` returns before registering anything for a finished item."""
    source_id = gmail_source(db)
    item = held_items(db, database_url, source_id)[0]
    ingest_now(db, [item["id"]])
    assert drain(Queue.INTEGRATE, database_url) == 1
    assert len(mailbox.modify_calls) == 1

    raw_integrate_job(db, item["id"], deliberate=True)  # a stale duplicate, past the claim
    assert drain(Queue.INTEGRATE, database_url) == 1
    assert len(mailbox.modify_calls) == 1, "a second deliberate job is not a second write"
    assert outcomes.outcomes == ["applied"]


def test_the_write_back_runs_after_the_users_lock_is_released(
    db: psycopg.Connection[Any],
    database_url: str,
    mailbox: FakeMailClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A slow Gmail call must not hold up this user's other serialized jobs.

    Checked from a second connection at the moment of the call: if the drain still held
    the user's advisory lock, ``pg_try_advisory_lock`` there would say no.
    """
    from motet_workers import jobs

    source_id = gmail_source(db)
    item = held_items(db, database_url, source_id)[0]
    lock_free: list[bool] = []
    real_modify = mailbox.modify_labels

    def modify_and_try_the_lock(message_id: str, **kwargs: Any) -> None:
        with psycopg.connect(database_url, autocommit=True) as other:
            taken = jobs.try_lock(other, USER)
            if taken:
                jobs.unlock(other, USER)
        lock_free.append(taken)
        real_modify(message_id, **kwargs)

    monkeypatch.setattr(mailbox, "modify_labels", modify_and_try_the_lock)
    ingest_now(db, [item["id"]])
    assert drain(Queue.INTEGRATE, database_url) == 1
    assert lock_free == [True]


def test_gmail_refusing_the_scope_is_recorded_as_needing_reauthorization(
    db: psycopg.Connection[Any],
    database_url: str,
    mailbox: FakeMailClient,
    outcomes: _Outcomes,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The recorded grant says modify, and Gmail answers 403 anyway."""
    from motet_sources import SourceAuthError

    source_id = gmail_source(db)
    item = held_items(db, database_url, source_id)[0]

    def forbidden(message_id: str, **kwargs: Any) -> None:
        raise SourceAuthError("Gmail rejected the credential (403): Insufficient Permission")

    monkeypatch.setattr(mailbox, "modify_labels", forbidden)
    ingest_now(db, [item["id"]])
    assert drain(Queue.INTEGRATE, database_url) == 1
    assert outcomes.outcomes == ["needs_reauthorization"]
    db.commit()
    row = _items(db, source_id, item_id=item["id"])[0]
    assert row["label_error"] is not None and "403" in row["label_error"]


def test_a_deleted_message_is_recorded_without_re_reading_the_labels(
    db: psycopg.Connection[Any],
    database_url: str,
    mailbox: FakeMailClient,
    outcomes: _Outcomes,
) -> None:
    source_id = gmail_source(db)
    item = held_items(db, database_url, source_id)[0]
    mailbox.messages = [m for m in mailbox.messages if m.id != item["external_id"]]
    lists_before = mailbox.label_lists
    ingest_now(db, [item["id"]])

    assert drain(Queue.INTEGRATE, database_url) == 1
    assert outcomes.outcomes == ["message_not_found"]
    assert mailbox.label_lists == lists_before, "a missing message is not a stale label"


def test_a_reconsent_as_another_account_disconnects_instead_of_writing(
    db: psycopg.Connection[Any],
    database_url: str,
    mailbox: FakeMailClient,
    outcomes: _Outcomes,
) -> None:
    """The owner picked a different account on Google's chooser while re-authorizing."""
    source_id = gmail_source(db)
    item = held_items(db, database_url, source_id)[0]
    phase2.merge_source_sync_state(db, source_id, "mailbox_address", "owner@example.invalid")
    phase2.merge_source_sync_state(db, source_id, "mailbox_verified_for", "a-previous-grant")
    db.commit()
    mailbox.address = "someone-else@example.invalid"
    ingest_now(db, [item["id"]])

    assert drain(Queue.INTEGRATE, database_url) == 1
    assert mailbox.modify_calls == []
    assert outcomes.outcomes == ["needs_reauthorization"]
    db.commit()
    source = phase2.get_source(db, source_id)
    assert source is not None and not source.active
    assert source.last_error is not None
    assert "someone-else@example.invalid" in source.last_error
    assert "owner@example.invalid" in source.last_error
    assert (
        phase2.get_source_credential(
            db, source_id_=source_id, purpose=CredentialPurpose.REFRESH.value
        )
        is None
    ), "the other account's credential is not kept"


def test_a_poll_after_a_reconsent_as_another_account_reads_nothing(
    db: psycopg.Connection[Any],
    database_url: str,
    mailbox: FakeMailClient,
) -> None:
    source_id = gmail_source(db, labels=None)
    phase2.merge_source_sync_state(db, source_id, "mailbox_address", "owner@example.invalid")
    phase2.merge_source_sync_state(db, source_id, "mailbox_verified_for", "a-previous-grant")
    db.commit()
    mailbox.address = "someone-else@example.invalid"
    enqueue_source_poll(db, source_id)
    db.commit()

    assert drain(Queue.POLL, database_url) == 1
    db.commit()
    assert db.execute("SELECT count(*) AS n FROM jobs WHERE queue = 'extract'").fetchone() == {
        "n": 0
    }
    source = phase2.get_source(db, source_id)
    assert source is not None and not source.active
    assert source.last_error is not None and "different Gmail account" in source.last_error


def test_the_first_poll_records_the_mailbox_for_its_grant_and_a_new_grant_is_checked_again(
    db: psycopg.Connection[Any],
    database_url: str,
    mailbox: FakeMailClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source_id = gmail_source(db, labels=None)
    enqueue_source_poll(db, source_id)
    db.commit()
    drain(Queue.POLL, database_url)
    db.commit()
    source = phase2.get_source(db, source_id)
    grant = _grant(db, source_id)
    assert source is not None and grant.updated_at is not None
    assert source.sync_state["mailbox_address"] == "owner@example.invalid"
    assert source.sync_state["mailbox_verified_for"] == grant_stamp(grant)

    asked: list[str] = []
    real_address = mailbox.mailbox_address

    def counted() -> str | None:
        asked.append("profile")
        return real_address()

    monkeypatch.setattr(mailbox, "mailbox_address", counted)

    # A second poll on the same grant asks Gmail nothing about the mailbox.
    enqueue_source_poll(db, source_id)
    db.commit()
    drain(Queue.POLL, database_url)
    assert asked == []

    # A re-consent — the grant rewritten — is checked again, and passes as the same account.
    _regrant(db, source_id, "fake-refresh-after-reconsent")
    enqueue_source_poll(db, source_id)
    db.commit()
    drain(Queue.POLL, database_url)
    db.commit()
    source = phase2.get_source(db, source_id)
    regrant = _grant(db, source_id)
    assert asked == ["profile"]
    assert source is not None and source.active and regrant.updated_at is not None
    assert source.sync_state["mailbox_verified_for"] == grant_stamp(regrant)


def test_a_cached_token_from_the_old_grant_cannot_vouch_for_a_new_one(
    db: psycopg.Connection[Any],
    database_url: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The second review's reproduction. The mailbox a token reaches depends on the token.

    Account A's access token is still cached and unexpired when the owner re-consents as
    account B. Asking the profile with that cached token would say "A" and wave B's grant
    through — so the check mints its token from the grant stored now.
    """
    from motet_sources import FakeOAuthClient
    from motet_sources.fakes import _fake_token

    b_token = _fake_token("access", "refresh-from-account-b")
    monkeypatch.setattr(
        "motet_workers.ingest.build_mail_client",
        lambda token, env=None: FakeMailClient(
            address="b@example.invalid" if token == b_token else "a@example.invalid"
        ),
    )
    monkeypatch.setattr(
        "motet_workers.ingest.build_oauth_client", lambda env=None: FakeOAuthClient()
    )

    source_id = gmail_source(db, labels=None, scopes=(GMAIL_READONLY_SCOPE,))
    enqueue_source_poll(db, source_id)
    db.commit()
    drain(Queue.POLL, database_url)  # records a@, and caches an access token for A's grant
    db.commit()
    phase2.store_source_credential(
        db,
        build_key_manager(),
        user_id=USER,
        source_id_=source_id,
        provider=PROVIDER,
        purpose=CredentialPurpose.ACCESS.value,
        secret="account-a-access-token",
        scopes=(GMAIL_READONLY_SCOPE,),
        expires_at=datetime.now(UTC) + timedelta(minutes=50),
    )
    _regrant(db, source_id, "refresh-from-account-b")  # what the callback does for B

    enqueue_source_poll(db, source_id)
    db.commit()
    drain(Queue.POLL, database_url)
    db.commit()
    source = phase2.get_source(db, source_id)
    assert source is not None and not source.active
    assert source.last_error is not None and "b@example.invalid" in source.last_error


def test_a_token_minted_from_a_grant_replaced_mid_refresh_is_not_stored(
    db: psycopg.Connection[Any],
    database_url: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The third review's race: a consent lands while a refresh is in flight.

    Storing the token anyway would cache, for an hour, a token for a grant that is gone —
    possibly another account's — and every later caller would trust it.
    """
    source_id = gmail_source(db, labels=None, scopes=(GMAIL_READONLY_SCOPE,))

    class ConsentLandsMidRefresh:
        def refresh(self, *, refresh_token: str) -> Any:
            with psycopg.connect(database_url) as other:
                phase2.store_source_credential(
                    other,
                    build_key_manager(),
                    user_id=USER,
                    source_id_=source_id,
                    provider=PROVIDER,
                    purpose=CredentialPurpose.REFRESH.value,
                    secret="the-new-grant",
                    scopes=LABEL_SYNC_SCOPES,
                )
            return FakeOAuthClient().refresh(refresh_token=refresh_token)

    monkeypatch.setattr(
        "motet_workers.ingest.build_oauth_client", lambda env=None: ConsentLandsMidRefresh()
    )
    with pytest.raises(SourceError, match="replaced"):
        mint_token(db, source_id=source_id, user_id=USER)
    db.rollback()
    assert (
        phase2.get_source_credential(
            db, source_id_=source_id, purpose=CredentialPurpose.ACCESS.value
        )
        is None
    ), "no token for the replaced grant was stored"


def test_a_consent_still_uncommitted_when_the_refresh_returns_is_waited_for(
    db: psycopg.Connection[Any],
    database_url: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The fourth review's race: the callback has written the new grant but not committed.

    A plain re-read would still see the old grant and store its token. The locked re-read
    waits for the consent to commit, sees the new stamp, and refuses.
    """
    import threading

    source_id = gmail_source(db, labels=None, scopes=(GMAIL_READONLY_SCOPE,))
    consent = psycopg.connect(database_url)

    class ConsentInFlight:
        def refresh(self, *, refresh_token: str) -> Any:
            phase2.store_source_credential(
                consent,
                build_key_manager(),
                user_id=USER,
                source_id_=source_id,
                provider=PROVIDER,
                purpose=CredentialPurpose.REFRESH.value,
                secret="the-new-grant",
                scopes=LABEL_SYNC_SCOPES,
            )  # written, not committed: the callback is still running
            threading.Timer(0.5, consent.commit).start()
            return FakeOAuthClient().refresh(refresh_token=refresh_token)

    monkeypatch.setattr(
        "motet_workers.ingest.build_oauth_client", lambda env=None: ConsentInFlight()
    )
    try:
        with pytest.raises(SourceError, match="replaced"):
            mint_token(db, source_id=source_id, user_id=USER)
    finally:
        db.rollback()
        consent.close()
    assert (
        phase2.get_source_credential(
            db, source_id_=source_id, purpose=CredentialPurpose.ACCESS.value
        )
        is None
    )


def test_a_rotated_refresh_token_keeps_its_scopes_and_its_mailbox_check(
    db: psycopg.Connection[Any],
    database_url: str,
    mailbox: FakeMailClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Google occasionally rotates a refresh token. Re-storing it must neither widen the
    recorded scopes nor make the mailbox check due on every poll after it."""
    from motet_sources import TokenGrant

    source_id = gmail_source(db, labels=None, scopes=(GMAIL_READONLY_SCOPE,))

    class Rotating:
        def refresh(self, *, refresh_token: str) -> TokenGrant:
            return TokenGrant(
                access_token=f"access-for-{refresh_token}",
                refresh_token=f"{refresh_token}-rotated",
                expires_in_seconds=60,  # inside the refresh skew, so every poll refreshes
                scopes=LABEL_SYNC_SCOPES,  # wider than the grant was recorded with
            )

    monkeypatch.setattr("motet_workers.ingest.build_oauth_client", lambda env=None: Rotating())
    asked: list[str] = []
    real_address = mailbox.mailbox_address
    monkeypatch.setattr(
        mailbox, "mailbox_address", lambda: asked.append("profile") or real_address()
    )

    for _ in range(3):
        enqueue_source_poll(db, source_id)
        db.commit()
        drain(Queue.POLL, database_url)
    db.commit()
    assert asked == ["profile"], "one check, not one per rotation"
    assert _grant(db, source_id).scopes == (GMAIL_READONLY_SCOPE,)
    access = phase2.get_source_credential(
        db, source_id_=source_id, purpose=CredentialPurpose.ACCESS.value
    )
    assert access is not None and access.scopes == (GMAIL_READONLY_SCOPE,)


def test_a_paused_source_is_not_written(
    db: psycopg.Connection[Any],
    database_url: str,
    mailbox: FakeMailClient,
    outcomes: _Outcomes,
) -> None:
    source_id = gmail_source(db)
    item = held_items(db, database_url, source_id)[0]
    phase2.set_source_active(db, source_id, active=False)
    ingest_now(db, [item["id"]])

    assert drain(Queue.INTEGRATE, database_url) == 1
    assert mailbox.modify_calls == []
    assert outcomes.outcomes == ["needs_reauthorization"]


def test_a_poll_reads_the_label_catalog_only_when_it_is_missing_or_a_day_old(
    db: psycopg.Connection[Any],
    database_url: str,
    mailbox: FakeMailClient,
) -> None:
    """A read-only mailbox that never uses label sync pays one label read a day."""
    source_id = gmail_source(db, labels=None)
    for _ in range(2):
        enqueue_source_poll(db, source_id)
        db.commit()
        drain(Queue.POLL, database_url)
    assert mailbox.label_lists == 1

    _cache(db, source_id, list(mailbox.labels), fetched_at=datetime.now(UTC) - timedelta(days=2))
    enqueue_source_poll(db, source_id)
    db.commit()
    drain(Queue.POLL, database_url)
    assert mailbox.label_lists == 2


# --- helpers -------------------------------------------------------------------------


def _grant(db: psycopg.Connection[Any], source_id: str) -> Any:
    grant = phase2.get_source_credential(
        db, source_id_=source_id, purpose=CredentialPurpose.REFRESH.value
    )
    assert grant is not None
    return grant


def _regrant(db: psycopg.Connection[Any], source_id: str, secret: str) -> None:
    """Store a new refresh grant the way the OAuth callback does."""
    db.commit()  # a new transaction, so `updated_at` moves
    phase2.store_source_credential(
        db,
        build_key_manager(),
        user_id=USER,
        source_id_=source_id,
        provider=PROVIDER,
        purpose=CredentialPurpose.REFRESH.value,
        secret=secret,
        scopes=LABEL_SYNC_SCOPES,
    )
    db.commit()


def _cache(
    db: psycopg.Connection[Any],
    source_id: str,
    labels: list[Label],
    *,
    fetched_at: datetime | None = None,
) -> None:
    phase2.merge_source_sync_state(
        db,
        source_id,
        CATALOG_KEY,
        catalog_to_sync_state(labels, fetched_at=fetched_at or datetime.now(UTC)),
    )
    db.commit()


def _items(
    db: psycopg.Connection[Any], source_id: str, *, item_id: str | None = None
) -> list[dict[str, Any]]:
    with db.cursor() as cur:
        cur.execute(
            "SELECT id, external_id, state, label_synced_at, label_error FROM source_items "
            "WHERE source_id = %s AND (%s::text IS NULL OR id = %s) ORDER BY created_at, id",
            (source_id, item_id, item_id),
        )
        return list(cur.fetchall())
