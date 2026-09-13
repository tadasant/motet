"""Connected-source ingestion: the ``poll`` and ``extract`` stages.

``Poll → Extract``, and then a person. Both stages were named in
:class:`~motet_workers.queues.Queue` from the start — Phase 1 shipped the enum with them in
it and no handlers — so this is filling in a shape that was already settled rather than
adding one.

**Extraction does not queue integration.** Polling, fetching and parsing are deterministic
and free; ``integrate`` is the first stage that spends inference. A connected source
therefore does all of the former on its own and stops: the source item sits ``pending``
with no ``integrate`` job — that combination *is* the "held, awaiting ingest" state, and
no column says so — until the owner asks for it through ``POST /v1/source-items/integrate``
(:func:`~motet_workers.handlers.enqueue_integration`). Paste is the deliberate exception
and still queues integration on arrival: a person pasting is a person asking.

**Where the invariants land in this file:**

* **Invariant 6 — ingestion is serialized per user.** ``integrate`` already carries the
  user id as its serialization key, and that is the stage where concurrency actually
  races (dedup reads the window, decides, and writes). ``poll`` carries a *narrower* key,
  ``poll:<source id>``, because the property it needs is only "two polls of one mailbox do
  not overlap". Giving poll the user key too would be safe but wasteful: a slow mailbox
  fetch would defer that user's integrate jobs behind it for no correctness gain.
* **Invariant 8 — only workers decrypt.** This module is the *only* place in the tree that
  calls :func:`~motet_db.phase2.load_source_credential`, and workers are the only thing
  that imports it. The API cannot reach it: it holds a
  :class:`~motet_vault.DekWrapper`, which has no ``unwrap``.
* **Idempotence.** A poll that crashed after fetching and before committing re-fetches the
  same messages; ``source_items`` is unique on ``(source_id, external_id)``, so the second
  pass inserts nothing.
"""

from __future__ import annotations

import logging
from collections.abc import Mapping
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any

import psycopg
from motet_db import CredentialPurpose, SourceKind, phase2
from motet_sources import (
    DEFAULT_QUERY,
    GMAIL_READONLY_SCOPE,
    PROVIDER,
    ExtractionError,
    SourceAuthError,
    build_mail_client,
    build_oauth_client,
    extract_newsletter,
)
from motet_vault import build_key_manager

from .jobs import enqueue
from .queues import Queue

if TYPE_CHECKING:
    from .handlers import Context

logger = logging.getLogger("motet.worker.ingest")

#: Messages one poll will queue, and the page size it asks the provider for. A ceiling
#: rather than a target, and it bounds the *run* rather than the search: a poll stops paging
#: once it has queued this many, and the next poll resumes the same search from its cursor.
#: A run can pass it by less than a page, because a page is queued whole or not at all — a
#: cursor that pointed half-way into a page would need an offset Gmail does not have.
POLL_PAGE_SIZE = 50

#: Pages one poll will read, whatever they held. The other half of the run's bound: a page
#: of messages that are all already queued costs a request and queues nothing, and a long
#: overlap of those must not keep one job listing for longer than its lease is worth.
MAX_PAGES_PER_POLL = 10


class IngestError(RuntimeError):
    """Ingestion failed in a way worth retrying."""


def handle_poll(context: Context, payload: Mapping[str, Any]) -> None:
    """Find what has arrived in one connected source, and queue it for extraction.

    Fetches nothing itself. Listing is one cheap request and fetching is one request per
    message, so they are separate stages: a mailbox with forty new newsletters becomes
    forty independently retryable jobs rather than one job that fails on the thirty-ninth
    and re-fetches all forty.

    The cursor is advanced **in the same transaction** as the enqueued extract jobs. On its
    own it would be the classic ingestion bug — a crash between the two either loses a
    day's newsletters or replays them forever.

    **The cursor never moves past a page nobody read.** A search with more pages than one
    run takes leaves the cursor on the next page, and this handler queues the next poll to
    read it (motet#94). That chain is bounded by the search itself: it ends on the run that
    finds no further page. Each link is one short job, so the lease argument that bounds a
    run is unchanged — what changed is that the rest of the backlog is *later* rather than
    *never*.
    """
    source_id = _require(payload, "source_id")
    source = phase2.get_source(context.conn, source_id)
    if source is None:
        raise _permanent(f"source {source_id} no longer exists")
    if not source.active:
        logger.info("source %s is paused; not polling", source_id)
        return
    if source.kind != SourceKind.GMAIL.value:
        raise _permanent(f"source {source_id} is a {source.kind!r} source and cannot be polled")

    access_token = _access_token(context.conn, source_id=source_id, user_id=source.user_id)
    client = build_mail_client(access_token)
    stored = source.sync_state.get("cursor")
    cursor = stored if isinstance(stored, str) else None
    query = source_query(source.config)

    seen = queued = pages = 0
    window_days: int | None = None
    more = True
    while more and queued < POLL_PAGE_SIZE and pages < MAX_PAGES_PER_POLL:
        page = client.list_messages(query=query, cursor=cursor, limit=POLL_PAGE_SIZE)
        pages += 1
        seen += len(page.messages)
        if page.first_sync_days is not None:
            window_days = page.first_sync_days
        # A message already handed on — a row, or an extract job in any state — is not
        # queued again. The search overlaps its previous pass on purpose, and without the
        # job half a receipt extraction skipped would be fetched again on every poll inside
        # that overlap. The unique index is still what guarantees one row per message; this
        # is what keeps the normal case from paying for a fetch it would discard.
        fresh = phase2.unqueued_message_ids(
            context.conn,
            source_id_=source_id,
            external_ids=[message.id for message in page.messages],
        )
        for message_id in fresh:
            enqueue(
                context.conn,
                Queue.EXTRACT,
                {"source_id": source_id, "message_id": message_id},
            )
        queued += len(fresh)
        cursor = page.cursor
        more = page.more

    sync_state = {
        **source.sync_state,
        "cursor": cursor,
        "last_sync": {
            "at": datetime.now(UTC).isoformat(),
            "seen": seen,
            "queued": queued,
            "caught_up": not more,
            "error": None,
        },
    }
    if window_days is not None:
        # The window of the most recent first sync, as a fact on the source. Recorded only
        # by the run that started one, so it describes what was actually searched rather
        # than what the deployment would choose today.
        logger.info("source %s: first sync bounded to the last %d days", source_id, window_days)
        sync_state["first_sync_days"] = window_days
    phase2.set_source_sync_state(context.conn, source_id, sync_state)

    if more:
        # No delay: a drain claims it as soon as this job's lock is released, so one
        # execution reads the whole window as a chain of bounded jobs.
        enqueue_source_poll(context.conn, source_id)
    logger.info(
        "polled source %s: %d message(s) seen over %d page(s), %d queued for extraction; %s",
        source_id,
        seen,
        pages,
        queued,
        "more waiting, next poll queued" if more else "caught up",
    )


def source_query(config: Mapping[str, Any]) -> str:
    """The search a source is polled with: its own, or the default. Used by every poll."""
    query = config.get("query")
    return query.strip() if isinstance(query, str) and query.strip() else DEFAULT_QUERY


def record_poll_failure(
    conn: psycopg.Connection[Any], payload: Mapping[str, Any], error: str
) -> None:
    """Put a poll that gave up on its source — ``last_error``, and the last-sync result.

    The runner calls this once the retries are spent (``handlers.failure_recorders``).
    Written from outside the handler because the handler's own transaction is the one that
    rolled back. The cursor is left exactly where it was: nothing was read, so nothing may
    be stepped over, and the next poll resumes from the same place.
    """
    source_id = payload.get("source_id")
    source = phase2.get_source(conn, source_id) if isinstance(source_id, str) else None
    if source is None or source.kind != SourceKind.GMAIL.value:
        # Gone, or a poll asked of a source nothing polls — the job row says so, and a
        # sync result on a source that has no sync would be a fact about nothing.
        return
    previous = source.sync_state.get("last_sync")
    caught_up = previous.get("caught_up", False) if isinstance(previous, dict) else False
    sync_state = {
        **source.sync_state,
        "last_sync": {
            "at": datetime.now(UTC).isoformat(),
            "seen": 0,
            "queued": 0,
            "caught_up": caught_up,
            "error": error[:2000],
        },
    }
    phase2.set_source_sync_state(conn, source.id, sync_state, error=error[:2000])


def handle_extract(context: Context, payload: Mapping[str, Any]) -> None:
    """Fetch one message and turn it into a source item — and stop there.

    The row is left ``pending`` with no ``integrate`` job. Dedup is the first stage that
    spends inference, and a connected source must not spend it until the owner says so;
    the module docstring says why. The extract job is *not* serialized per user, and
    integrate is: extraction writes only its own row, while dedup reads the whole window
    and decides against it — so serializing extraction would cost throughput and buy
    nothing (invariant 6 is about the compare-and-write, not about the fetch).
    """
    source_id = _require(payload, "source_id")
    message_id = _require(payload, "message_id")

    source = phase2.get_source(context.conn, source_id)
    if source is None:
        raise _permanent(f"source {source_id} no longer exists")
    if phase2.source_item_exists(context.conn, source_id_=source_id, external_id=message_id):
        # A retry after the insert committed but the job update did not. Nothing to do,
        # and importantly nothing to do twice.
        logger.info("message %s from source %s is already ingested", message_id, source_id)
        return

    access_token = _access_token(context.conn, source_id=source_id, user_id=source.user_id)
    raw = build_mail_client(access_token).fetch_message(message_id)

    try:
        extracted = extract_newsletter(raw.raw)
    except ExtractionError as exc:
        # A receipt, a calendar invite, or a message with no subject. It will still be
        # those things in ten minutes, so this is permanent — and it is *not* an episode
        # failure either, which is why it returns rather than raising: the message is
        # simply not a newsletter, and a mailbox is full of those.
        logger.info("skipping message %s from source %s: %s", message_id, source_id, exc)
        _record_skip(context.conn, source_id, message_id, str(exc))
        return

    source_item_id = phase2.insert_polled_source_item(
        context.conn,
        user_id=source.user_id,
        source_id_=source_id,
        external_id=message_id,
        title=extracted.title,
        text=extracted.text,
        received_at=_sent_at(extracted.date),
    )
    if source_item_id is None:
        # Another worker won the race. The unique index did its job; there is exactly one
        # row, which is the whole point of it.
        logger.info("message %s was ingested concurrently", message_id)
        return

    logger.info(
        "extracted message %s from source %s into source item %s (%d chars); held for ingest",
        message_id,
        source_id,
        source_item_id,
        len(extracted.text),
    )


def _sent_at(date: str) -> datetime | None:
    """The message's ``Date:`` as an aware datetime, or ``None`` if it had no usable one.

    ``extract`` hands it over as RFC 3339 text. A ``-0000`` zone parses to a naive
    datetime — RFC 5322's "UTC, local zone unknown" — and is read as UTC, because a naive
    value handed to a ``timestamptz`` column would be read in the session's zone instead.
    """
    if not date:
        return None
    try:
        parsed = datetime.fromisoformat(date)
    except ValueError:
        return None
    return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=UTC)


# --- credentials ---------------------------------------------------------------------


def _access_token(conn: psycopg.Connection[Any], *, source_id: str, user_id: str) -> str:
    """An access token for this source, refreshing it first if it is close to expiring.

    **This is the decrypt boundary.** :func:`~motet_vault.build_key_manager` returns the
    full key manager, which in a deployed worker is backed by Cloud KMS and works only
    because the worker service account holds ``useToDecrypt``. The same call in the API
    would return the same object and then fail inside KMS with PermissionDenied — the IAM
    grant is the control, and this comment is the reminder not to route around it.

    Refreshing early by a fixed skew is deliberate: a token that passes the check and then
    expires midway through a forty-message poll fails after paying for half of it.
    """
    manager = build_key_manager()
    now = datetime.now(UTC)

    access = phase2.get_source_credential(
        conn, source_id_=source_id, purpose=CredentialPurpose.ACCESS.value
    )
    if access is not None and not access.expired(now=now):
        token = phase2.load_source_credential(
            conn, manager, source_id_=source_id, purpose=CredentialPurpose.ACCESS.value
        )
        if token:
            return token

    refresh_token = phase2.load_source_credential(
        conn, manager, source_id_=source_id, purpose=CredentialPurpose.REFRESH.value
    )
    if not refresh_token:
        raise _permanent(
            f"source {source_id} has no refresh credential, so it cannot be polled. "
            "The mailbox needs to be reconnected."
        )

    try:
        grant = build_oauth_client().refresh(refresh_token=refresh_token)
    except SourceAuthError as exc:
        # The user revoked access, or the grant expired. Only re-consent fixes it, so
        # burning five retries would just delay the message a human needs to see.
        phase2.set_source_active(conn, source_id, active=False)
        raise _permanent(f"source {source_id} needs reconnecting: {exc}") from exc

    phase2.store_source_credential(
        conn,
        manager,
        user_id=user_id,
        source_id_=source_id,
        provider=PROVIDER,
        purpose=CredentialPurpose.ACCESS.value,
        secret=grant.access_token,
        scopes=grant.scopes or (GMAIL_READONLY_SCOPE,),
        expires_at=now + timedelta(seconds=grant.expires_in_seconds),
    )
    # `grant.refresh_token` is deliberately NOT written back. Google issues one only at
    # first consent and sends None on every refresh; storing that None would disconnect
    # the mailbox an hour after it was connected, with no error anywhere.
    if grant.refresh_token:
        phase2.store_source_credential(
            conn,
            manager,
            user_id=user_id,
            source_id_=source_id,
            provider=PROVIDER,
            purpose=CredentialPurpose.REFRESH.value,
            secret=grant.refresh_token,
            scopes=grant.scopes or (GMAIL_READONLY_SCOPE,),
        )
    return grant.access_token


# --- shared --------------------------------------------------------------------------


def poll_key(source_id: str) -> str:
    """The serialization key a poll job takes.

    Per *source*, not per user. The property needed is "two polls of one mailbox do not
    overlap"; taking the user key instead would also defer that user's integrate jobs
    behind a slow mailbox fetch, which invariant 6 does not ask for.
    """
    return f"poll:{source_id}"


def enqueue_source_poll(
    conn: psycopg.Connection[Any], source_id: str, *, delay_seconds: int = 0
) -> int:
    return enqueue(
        conn,
        Queue.POLL,
        {"source_id": source_id},
        serialize_key=poll_key(source_id),
        delay_seconds=delay_seconds,
    )


def _record_skip(
    conn: psycopg.Connection[Any], source_id: str, message_id: str, reason: str
) -> None:
    """Note that a message was looked at and deliberately not ingested.

    Recorded on the source rather than dropped silently, so "why didn't my newsletter show
    up" has an answer that does not require reading worker logs. Not a failure: a mailbox
    is mostly not newsletters, and treating every receipt as an error would make the
    source permanently red.
    """
    source = phase2.get_source(conn, source_id)
    if source is None:
        return
    skipped = dict(source.sync_state)
    skipped["last_skipped"] = {"message_id": message_id, "reason": reason[:200]}
    phase2.set_source_sync_state(conn, source_id, skipped)


def _require(payload: Mapping[str, Any], key: str) -> str:
    from .handlers import PermanentFailure  # noqa: PLC0415  — avoids a circular import

    value = payload.get(key)
    if not isinstance(value, str) or not value:
        raise PermanentFailure(f"job payload is missing a usable {key!r}: {payload!r}")
    return value


def _permanent(message: str) -> Exception:
    from .handlers import PermanentFailure  # noqa: PLC0415

    return PermanentFailure(message)
