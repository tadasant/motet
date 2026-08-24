"""Connected-source ingestion: the ``poll`` and ``extract`` stages.

``Poll → Extract → Integrate``. Both stages were named in :class:`~motet_workers.queues.Queue`
from the start — Phase 1 shipped the enum with them in it and no handlers — so this is
filling in a shape that was already settled rather than adding one.

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
  pass inserts nothing and enqueues nothing.
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

#: Messages one poll will look at. A ceiling rather than a target: the cursor advances by
#: what was actually seen, so a mailbox with more waiting is drained over several runs
#: instead of in one job that outlives its lease.
POLL_PAGE_SIZE = 50


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
    cursor = source.sync_state.get("cursor")
    query = source.config.get("query") or DEFAULT_QUERY

    page = client.list_messages(
        query=query, cursor=cursor if isinstance(cursor, str) else None, limit=POLL_PAGE_SIZE
    )

    if page.cursor_expired:
        # The provider's history window has moved past our bookmark. Not an error and not
        # retryable: dropping the cursor makes the next poll do a bounded first sync,
        # which is the actual repair.
        logger.warning("source %s cursor expired; scheduling a bounded resync", source_id)
        phase2.set_source_sync_state(
            context.conn,
            source_id,
            {"cursor": None, "resynced_from": cursor},
            error="the provider's history window expired; resyncing from a date watermark",
        )
        enqueue(
            context.conn,
            Queue.POLL,
            {"source_id": source_id},
            serialize_key=poll_key(source_id),
            delay_seconds=5,
        )
        return

    queued = 0
    for message in page.messages:
        # Cheap pre-check so the normal case skips a fetch it would only discard. The
        # unique index is what actually guarantees this; the check is what makes it fast.
        if phase2.source_item_exists(context.conn, source_id_=source_id, external_id=message.id):
            continue
        enqueue(
            context.conn,
            Queue.EXTRACT,
            {"source_id": source_id, "message_id": message.id},
        )
        queued += 1

    phase2.set_source_sync_state(
        context.conn, source_id, {**source.sync_state, "cursor": page.cursor}
    )
    logger.info(
        "polled source %s: %d message(s) seen, %d queued for extraction, cursor -> %s",
        source_id,
        len(page.messages),
        queued,
        page.cursor,
    )


def handle_extract(context: Context, payload: Mapping[str, Any]) -> None:
    """Fetch one message, turn it into a source item, and queue it for dedup.

    The extract job is *not* serialized per user, and integrate is. That is the right
    split: extraction writes only its own row, while dedup reads the whole window and
    decides against it — so serializing extraction would cost throughput and buy nothing
    (invariant 6 is about the compare-and-write, not about the fetch).
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
    )
    if source_item_id is None:
        # Another worker won the race. The unique index did its job; there is exactly one
        # row and exactly one integrate job, which is the whole point of it.
        logger.info("message %s was ingested concurrently; not queueing again", message_id)
        return

    enqueue(
        context.conn,
        Queue.INTEGRATE,
        {"source_item_id": source_item_id},
        serialize_key=source.user_id,
    )
    logger.info(
        "extracted message %s from source %s into source item %s (%d chars)",
        message_id,
        source_id,
        source_item_id,
        len(extracted.text),
    )


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
