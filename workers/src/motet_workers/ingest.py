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
* **Neither stage ever writes to the mailbox.** A poll *reads* the label catalog as well as
  the message list, so the label-sync pickers have names to offer (motet#96), but the one
  write the connector can make lives in :mod:`motet_workers.labels` and fires only after a
  deliberate ingest.
"""

from __future__ import annotations

import logging
from collections.abc import Mapping
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any

import psycopg
from motet_db import CredentialPurpose, SourceKind, phase2
from motet_db.models import StoredSource
from motet_sources import (
    DEFAULT_QUERY,
    GMAIL_READONLY_SCOPE,
    PROVIDER,
    ExtractionError,
    MailClient,
    SourceAuthError,
    SourceError,
    build_mail_client,
    build_oauth_client,
    extract_newsletter,
)
from motet_sources.labels import (
    CATALOG_KEY,
    CATALOG_MAX_AGE,
    MAILBOX_ADDRESS_KEY,
    MAILBOX_VERIFIED_FOR_KEY,
    catalog_fetched_at,
    catalog_to_sync_state,
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

    due = mailbox_check_due(context.conn, source)
    if due is not None:
        access_token, stamp = mint_token(context.conn, source_id=source_id, user_id=source.user_id)
    else:
        access_token = _access_token(context.conn, source_id=source_id, user_id=source.user_id)
    client = build_mail_client(access_token)
    verified: dict[str, Any] = {}
    if due is not None:
        mismatch, verified = check_mailbox(context.conn, source, client, stamp)
        if mismatch is not None:
            # Returned, not raised: raising would roll back the disconnect it just recorded.
            logger.error("source %s: %s", source_id, mismatch)
            return
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

    # The mailbox keys from a fresh read rather than from the snapshot this poll started with:
    # a refresh above may have carried the check to a rotated grant, and the snapshot would
    # write the stale stamp back and make every later poll re-check.
    reread = phase2.get_source(context.conn, source_id)
    mailbox_keys: dict[str, Any] = {
        key: reread.sync_state[key]
        for key in (MAILBOX_ADDRESS_KEY, MAILBOX_VERIFIED_FOR_KEY)
        if reread is not None and key in reread.sync_state
    }
    sync_state = {
        **source.sync_state,
        **mailbox_keys,
        **verified,
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
    catalog = _read_label_catalog(client, source)
    if catalog is not None:
        sync_state[CATALOG_KEY] = catalog
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
        links=extracted.links,
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


def _read_label_catalog(client: MailClient, source: StoredSource) -> dict[str, Any] | None:
    """The mailbox's labels, as ``sync_state`` caches them — or ``None`` to keep the cache.

    Read only when there is no catalog or it is older than ``CATALOG_MAX_AGE``: the pickers
    are suggestions and the write-back re-reads on a miss, so a mailbox that never turns
    label sync on costs one extra read a day. ``gmail.readonly`` covers it. **Swallowed**,
    because the poll's job is ingestion and a catalog is a convenience for a settings screen:
    a poll that failed because it could not list labels would stall newsletters over a
    dropdown.
    """
    fetched_at = catalog_fetched_at(source.sync_state)
    now = datetime.now(UTC)
    if fetched_at is not None and now - fetched_at < CATALOG_MAX_AGE:
        return None
    try:
        labels = client.list_labels()
    except Exception:  # noqa: BLE001 — see above: never fail a poll over the label list
        logger.warning(
            "could not list labels for source %s; keeping the cached catalog",
            source.id,
            exc_info=True,
        )
        return None
    return catalog_to_sync_state(labels, fetched_at=now)


def mailbox_check_due(conn: psycopg.Connection[Any], source: StoredSource) -> str | None:
    """The refresh grant's stamp, if this source's mailbox has not been checked for it.

    ``None`` means the recorded address was checked against *this* grant and nothing needs
    asking. Anything else is the grant's ``updated_at``, which the caller hands to
    :func:`check_mailbox` after minting a token from that grant.
    """
    grant = phase2.get_source_credential(
        conn, source_id_=source.id, purpose=CredentialPurpose.REFRESH.value
    )
    if grant is None or grant.updated_at is None:
        # No grant to check. Callers reach this only for an active source, and minting a
        # token for one with no grant refuses — a cached access token is never used when a
        # check could be due, because `mint_token` is what a due check calls.
        return None
    stamp = grant_stamp(grant)
    expected = source.sync_state.get(MAILBOX_ADDRESS_KEY)
    if (
        isinstance(expected, str)
        and expected
        and source.sync_state.get(MAILBOX_VERIFIED_FOR_KEY) == stamp
    ):
        return None
    return stamp


def check_mailbox(
    conn: psycopg.Connection[Any], source: StoredSource, client: MailClient, stamp: str
) -> tuple[str | None, dict[str, Any]]:
    """Whether this token reaches the mailbox the source is — ``(mismatch, sync_state keys)``.

    **A consent can hand back a different account.** Re-authorizing for label sync replaces
    an existing source's grant (motet#96), and Google's account chooser returns whichever
    account was picked. A source whose token silently changed mailbox would run one inbox's
    cursor against another, pull that inbox in under the wrong source, and write labels in
    it. So before a poll or a write uses a grant :func:`mailbox_check_due` has not seen, this
    asks Gmail which mailbox it reaches — with a token the caller minted **from that grant**
    (:func:`mint_token`), never a cached one, because an access token from
    the previous grant stays valid for up to an hour and would answer for the old account.

    No recorded address — a new source, or one connected before this existed — records the
    one it sees. A different one **disconnects** the source: its credentials are the other
    account's, so they are deleted rather than kept, the source is paused, and
    ``last_error`` names both addresses and the repair. The mismatch is returned, not
    raised, so a caller inside a transaction can commit the disconnect. A profile that names
    no address raises: an unchecked grant is not one to read with.

    Returns the ``sync_state`` keys to write on a match — the address, and ``stamp`` as the
    grant it was checked for — which the poll folds into its own write and the write-back
    merges. One profile read per consent, and nothing per poll after that.
    """
    address = client.mailbox_address()
    if not address:
        raise SourceError(
            f"Gmail did not say which mailbox source {source.id}'s grant reaches; not reading "
            "or writing it until it does"
        )
    expected = source.sync_state.get(MAILBOX_ADDRESS_KEY)
    if isinstance(expected, str) and expected and address.casefold() != expected.casefold():
        reason = (
            f"this source was re-authorized with a different Gmail account ({address}) than "
            f"the one it was connected with ({expected}), so it has been disconnected rather "
            f"than read. Connect {expected} again from the Sources screen."
        )
        phase2.delete_source_credentials(conn, source.id)
        phase2.mark_source_disconnected(conn, source.id)
        phase2.set_source_error(conn, source.id, reason)
        return reason, {}
    return None, {MAILBOX_ADDRESS_KEY: address, MAILBOX_VERIFIED_FOR_KEY: stamp}


def _access_token(
    conn: psycopg.Connection[Any],
    *,
    source_id: str,
    user_id: str,
    required_scope: str | None = None,
) -> str:
    """An access token for this source, refreshing it first if it is close to expiring.

    **This is the decrypt boundary.** :func:`~motet_vault.build_key_manager` returns the
    full key manager, which in a deployed worker is backed by Cloud KMS and works only
    because the worker service account holds ``useToDecrypt``. The same call in the API
    would return the same object and then fail inside KMS with PermissionDenied — the IAM
    grant is the control, and this comment is the reminder not to route around it.

    Refreshing early by a fixed skew is deliberate: a token that passes the check and then
    expires midway through a forty-message poll fails after paying for half of it.

    ``required_scope`` is for the one caller that needs more than the connect grant — the
    label write-back, which needs ``gmail.modify``. An access token minted *before* a
    label-sync re-consent carries only the old scopes and stays valid for up to an hour, so
    a cached token that lacks the scope is refreshed rather than used: the refresh is made
    against the new grant and comes back wider.

    A due mailbox check does not come here: it calls :func:`mint_token` directly, so the
    token it asks with is minted from the grant stored *now* and never a cached one.
    """
    manager = build_key_manager()
    now = datetime.now(UTC)

    access = phase2.get_source_credential(
        conn, source_id_=source_id, purpose=CredentialPurpose.ACCESS.value
    )
    if (
        access is not None
        and not access.expired(now=now)
        and (required_scope is None or required_scope in access.scopes)
    ):
        token = phase2.load_source_credential(
            conn, manager, source_id_=source_id, purpose=CredentialPurpose.ACCESS.value
        )
        if token:
            return token

    token, _stamp = mint_token(conn, source_id=source_id, user_id=user_id)
    return token


def mint_token(conn: psycopg.Connection[Any], *, source_id: str, user_id: str) -> tuple[str, str]:
    """A fresh access token from the refresh grant stored now — and that grant's stamp.

    **The token is stored only if the grant it was minted from is still the stored one.** A
    refresh is a network call, and a consent can replace the grant while it is in flight —
    a label-sync re-consent, possibly as another Google account (motet#96). Storing the
    result anyway would cache a token for a grant that is gone, valid for an hour and
    trusted by every later caller. So the grant's ``updated_at`` is read before the refresh
    and again after it, and a change raises a retryable :class:`SourceError` rather than
    storing: the retry mints from the new grant.

    Returns ``(token, stamp)``, where ``stamp`` identifies the grant the token came from —
    re-read after a rotated refresh token is stored, so a check recorded against it is not
    made stale by the rotation itself. A rotated refresh token keeps the scopes the grant
    was recorded with, and the access token's scopes are narrowed to them: what the OAuth
    callback recorded as asked-for is what every later decision reads.
    """
    manager = build_key_manager()
    now = datetime.now(UTC)
    grant_row = phase2.get_source_credential(
        conn, source_id_=source_id, purpose=CredentialPurpose.REFRESH.value
    )
    refresh_token = phase2.load_source_credential(
        conn, manager, source_id_=source_id, purpose=CredentialPurpose.REFRESH.value
    )
    if grant_row is None or not refresh_token:
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

    # `lock=True` is what makes the comparison hold against a consent still in flight: an
    # uncommitted callback has the row locked, so this waits for it and sees its stamp.
    # The lock then lasts this caller's transaction, so a consent arriving *after* waits
    # for the token to be stored and replaces the grant behind it — which the next check
    # notices.
    current = phase2.get_source_credential(
        conn, source_id_=source_id, purpose=CredentialPurpose.REFRESH.value, lock=True
    )
    if current is None or current.updated_at != grant_row.updated_at:
        raise SourceError(
            f"source {source_id}'s grant was replaced while a token was being minted from the "
            "previous one; not storing it, and retrying with the new grant"
        )

    recorded = grant_row.scopes or (GMAIL_READONLY_SCOPE,)
    scopes = tuple(scope for scope in grant.scopes if scope in recorded) or recorded
    phase2.store_source_credential(
        conn,
        manager,
        user_id=user_id,
        source_id_=source_id,
        provider=PROVIDER,
        purpose=CredentialPurpose.ACCESS.value,
        secret=grant.access_token,
        scopes=scopes,
        expires_at=now + timedelta(seconds=grant.expires_in_seconds),
    )
    stamp = grant_stamp(grant_row)
    # `grant.refresh_token` is deliberately NOT written back unless it is a new one. Google
    # issues one only at first consent and sends None on every refresh; storing that None
    # would disconnect the mailbox an hour after it was connected, with no error anywhere.
    if grant.refresh_token and grant.refresh_token != refresh_token:
        rotated = phase2.store_source_credential(
            conn,
            manager,
            user_id=user_id,
            source_id_=source_id,
            provider=PROVIDER,
            purpose=CredentialPurpose.REFRESH.value,
            secret=grant.refresh_token,
            scopes=recorded,
        )
        rotated_stamp = grant_stamp(rotated)
        # The same grant, rotated — so a mailbox check recorded for it still holds. Carried
        # only if the check was recorded for exactly the grant this refresh was made from.
        phase2.carry_source_sync_state_key(
            conn, source_id, MAILBOX_VERIFIED_FOR_KEY, old=stamp, new=rotated_stamp
        )
        stamp = rotated_stamp
    return grant.access_token, stamp


def grant_stamp(credential: Any) -> str:
    """Which grant a credential row is: its ``updated_at``, in UTC so every process agrees."""
    updated_at = credential.updated_at
    return updated_at.astimezone(UTC).isoformat() if updated_at is not None else ""


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
