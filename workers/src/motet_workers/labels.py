"""Moving a Gmail message between labels when its owner ingests it — motet#96.

The owner's mailbox workflow is ``Newsletters → Completed``: moving a message across is how
they record having read it. Ingesting an item into Motet *is* reading it, so without this
the bookkeeping happens twice. Per source, two optional label names (see
:mod:`motet_sources.labels`), applied to the item's message after a **deliberate** ingest
and at no other time.

Four properties, and each is the reason something is where it is:

* **Only a deliberate ingest.** The trigger is ``DELIBERATE_KEY`` on the integrate job's
  payload, which only ``handlers.enqueue_integration`` — the owner's "Ingest now" (motet#91)
  — writes. Absent — as on a paste's job, or one ``handle_extract`` queued before the ingest
  gate existed — means no write, so a poll, an extract, a stale job, or a future "always
  ingest from this sender" can never touch the mailbox by accident. A paste has no mailbox
  and never gets that far even with the flag.
* **After the commit, never inside it.** :func:`schedule` registers the step on the
  handler's :class:`~motet_workers.handlers.Context`, and ``loop.drain`` runs it only once
  the dedup transaction *and* the job's completion have committed, **and** the user's
  serialization lock has been released. So a Gmail outage cannot roll back an integrate, a
  slow Gmail call holds neither the dedup transaction's locks nor the user's other integrate
  jobs, and an integrate that rolled back never moved a label for a story that does not
  exist.
* **Never a failure of the job.** Every outcome of the write — including a bug in this
  module — is recorded on the source item (``label_synced_at`` / ``label_error``) and
  counted on :data:`_writebacks`, and none of them raises. The item is already ingested; a
  label is bookkeeping beside it. (:func:`schedule` does run inside the work transaction and
  makes one read there; a database that cannot answer it has already failed the integrate.)
* **Worker work, because only workers decrypt** (invariant 8). The route that receives
  "Ingest now" holds the encrypt-only half of the vault and could not open the token.

**Not a job.** A ``label`` queue would be a new mechanism in the job queue (invariant 12)
for one API call, and a retried label write is not worth a retry ladder: the next
deliberate ingest writes again, and the recorded error says what to fix. The cost of that,
said plainly, is that a worker that dies between the commit and this step leaves the
message where it was, with neither column set.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any, Final

import psycopg
from motet_db import CredentialPurpose, SourceKind, phase2, repo
from motet_db.models import StoredSource, StoredSourceItem
from motet_sources import (
    GMAIL_MODIFY_SCOPE,
    PROVIDER,
    Label,
    MailClient,
    SourceAuthError,
    SourceError,
    StaleReferenceError,
    build_mail_client,
)
from motet_sources.labels import (
    CATALOG_KEY,
    LabelSettings,
    Resolved,
    catalog_from_sync_state,
    catalog_to_sync_state,
    resolve,
)
from opentelemetry import metrics

if TYPE_CHECKING:
    from .handlers import Context

logger = logging.getLogger("motet.worker.labels")

#: The integrate payload key that says the owner asked for this item. See the module
#: docstring: its absence is the safe default, and the only default.
DELIBERATE_KEY: Final = "deliberate"

_meter = metrics.get_meter("motet.worker")

#: One point per write-back attempt, by how it ended. **This is what makes "label sync
#: silently stopped working" visible** — the never-infer-"no errors"-from-"no data" rule:
#: a mailbox whose grant was revoked, or whose label was renamed, would otherwise look
#: exactly like one nobody has ingested from. Outcomes:
#:
#: * ``applied`` — the message's labels were changed.
#: * ``needs_reauthorization`` — the grant does not carry ``gmail.modify``, or Gmail
#:   refused it; only the owner re-consenting fixes it.
#: * ``label_not_found`` — a configured name is not a label in this mailbox, even after
#:   re-reading the catalog; or it names a system label Motet will not write.
#: * ``message_not_found`` — Gmail no longer has the message (deleted since it was polled).
#: * ``failed`` — anything else: an outage, a rate limit, a bug.
_writebacks = _meter.create_counter(
    "motet.gmail.label_writeback",
    unit="{writeback}",
    description="Gmail label write-backs after a deliberate ingest, by outcome (motet#96).",
)


def schedule(context: Context, stored: StoredSourceItem, payload: dict[str, Any]) -> None:
    """Register the write-back to run once ``handle_integrate``'s work has committed.

    Called from inside the handler's transaction, which is the point: whether the source
    is a mailbox with label sync on is read from the same snapshot the integrate decided
    against. Registers nothing — and so can reach no mailbox — unless all three hold: the
    job was a deliberate ingest, the source is Gmail, and it has label settings.
    """
    if payload.get(DELIBERATE_KEY) is not True:
        return
    source = phase2.get_source(context.conn, stored.source_id)
    if source is None or source.kind != SourceKind.GMAIL.value:
        return
    if LabelSettings.from_config(source.config) is None:
        return
    source_item_id = stored.id
    context.after_commit.append(lambda: write_back(context.conn, source_item_id))


def write_back(conn: psycopg.Connection[Any], source_item_id: str) -> str | None:
    """Move one ingested item's message between its source's labels. Returns the outcome.

    ``None`` means there was nothing to do — the settings were turned off between the
    commit and now, or the item has no message id — and records nothing. Every other
    return is recorded on the item and counted. Never raises.
    """
    try:
        outcome, error = _attempt(conn, source_item_id)
    except Exception as exc:  # noqa: BLE001 — a label is never worth failing an ingest
        logger.exception("label write-back for source item %s raised", source_item_id)
        outcome, error = "failed", f"{type(exc).__name__}: {exc}"
    if outcome is None:
        return None

    _writebacks.add(1, {"outcome": outcome})
    try:
        with conn.transaction():
            phase2.record_label_writeback(conn, source_item_id, error=error)
    except Exception:  # noqa: BLE001 — see above; the counter already has the outcome
        logger.exception("could not record the label write-back for %s", source_item_id)
    return outcome


def _attempt(conn: psycopg.Connection[Any], source_item_id: str) -> tuple[str | None, str | None]:
    from .handlers import PermanentFailure  # noqa: PLC0415  — avoids a circular import
    from .ingest import (  # noqa: PLC0415
        _access_token,
        check_mailbox,
        mailbox_check_due,
        mint_token,
    )

    message_id = phase2.source_item_external_id(conn, source_item_id)
    source = _source_of(conn, source_item_id)
    if message_id is None or source is None or source.kind != SourceKind.GMAIL.value:
        return None, None
    settings = LabelSettings.from_config(source.config)
    if settings is None:
        return None, None
    if not source.active:
        # Paused, disconnected, or deactivated after its grant was revoked: nothing in the
        # system offers a pause, so every way here needs the owner to reconnect.
        return "needs_reauthorization", _INACTIVE

    # Asked of the *grant*, and without decrypting anything: the refresh credential's
    # recorded scopes are what the owner said yes to. A mailbox connected read-only is the
    # common case until its owner re-consents, and it is answered here with no Gmail call
    # at all rather than with a 403 from one.
    grant = phase2.get_source_credential(
        conn, source_id_=source.id, purpose=CredentialPurpose.REFRESH.value
    )
    if grant is None or GMAIL_MODIFY_SCOPE not in grant.scopes:
        logger.info(
            "source %s has label sync set but its grant is read-only; not writing labels "
            "for source item %s",
            source.id,
            source_item_id,
        )
        return "needs_reauthorization", _NEEDS_REAUTH

    try:
        due = mailbox_check_due(conn, source)
        try:
            with conn.transaction():
                if due is not None:
                    token, stamp = mint_token(conn, source_id=source.id, user_id=source.user_id)
                else:
                    token = _access_token(
                        conn,
                        source_id=source.id,
                        user_id=source.user_id,
                        required_scope=GMAIL_MODIFY_SCOPE,
                    )
        except PermanentFailure as exc:
            return "needs_reauthorization", f"The mailbox needs reconnecting: {exc}"
        client = build_mail_client(token)
        if due is not None:
            # Before any write: the grant may be a re-consent that came back as another
            # account, and the token above was minted from it for exactly this question.
            with conn.transaction():
                mismatch, verified = check_mailbox(conn, source, client, stamp)
                for key, value in verified.items():
                    phase2.merge_source_sync_state(conn, source.id, key, value)
            if mismatch is not None:
                logger.error("source %s: %s", source.id, mismatch)
                return "needs_reauthorization", mismatch

        catalog = catalog_from_sync_state(source.sync_state)
        resolved = resolve(settings, catalog)
        relisted = False
        if resolved.missing:
            # A cache miss — the catalog predates the label, or no poll has read one yet.
            resolved, relisted = resolve(settings, _relist(conn, client, source.id)), True
        problem = _unresolvable(resolved)
        if problem is not None:
            return "label_not_found", problem

        try:
            client.modify_labels(message_id, add=resolved.add, remove=resolved.remove)
        except StaleReferenceError as exc:
            # A message that is gone is not repaired by re-reading the labels.
            if relisted or exc.target == "message":
                raise
            # A cached id went stale — the label was deleted and recreated, which gives it
            # a new id under the same name. Re-resolve once; a second miss is the message.
            resolved = resolve(settings, _relist(conn, client, source.id))
            problem = _unresolvable(resolved)
            if problem is not None:
                return "label_not_found", problem
            client.modify_labels(message_id, add=resolved.add, remove=resolved.remove)
    except StaleReferenceError as exc:
        return "message_not_found", f"Gmail no longer has this message: {exc}"
    except SourceAuthError as exc:
        return "needs_reauthorization", f"Gmail refused the label change: {exc}"
    except SourceError as exc:
        # An outage or a rate limit. A WARNING rather than the ERROR `write_back` gives an
        # unexpected exception, because only ERROR reaches GlitchTip and a Gmail blip is not
        # a Motet fault; the counter is where "how often" is answered.
        logger.warning("label write-back for source item %s failed: %s", source_item_id, exc)
        return "failed", str(exc)

    logger.info(
        "moved message %s for source item %s: removed %s, added %s",
        message_id,
        source_item_id,
        settings.remove or "nothing",
        settings.add or "nothing",
    )
    return "applied", None


_INACTIVE: Final = (
    "This mailbox is disconnected or paused, so Motet did not change its labels. Reconnect "
    "it on the Sources screen."
)

_NEEDS_REAUTH: Final = (
    "This mailbox was connected read-only. Re-authorize it on the Sources screen to let "
    "Motet change labels."
)


def _source_of(conn: psycopg.Connection[Any], source_item_id: str) -> StoredSource | None:
    stored = repo.get_source_item(conn, source_item_id)
    return phase2.get_source(conn, stored.source_id) if stored is not None else None


def _relist(conn: psycopg.Connection[Any], client: MailClient, source_id: str) -> tuple[Label, ...]:
    """Read the mailbox's labels again and cache them, by merging the one key."""
    from datetime import UTC, datetime  # noqa: PLC0415

    labels = client.list_labels()
    with conn.transaction():
        phase2.merge_source_sync_state(
            conn,
            source_id,
            CATALOG_KEY,
            catalog_to_sync_state(labels, fetched_at=datetime.now(UTC)),
        )
    return labels


def _unresolvable(resolved: Resolved) -> str | None:
    if resolved.refused:
        return (
            f"Motet will not move mail into or out of the system label "
            f"{', '.join(repr(name) for name in resolved.refused)}."
        )
    if resolved.missing:
        return (
            f"No label named {', '.join(repr(name) for name in resolved.missing)} exists in "
            f"this {PROVIDER} mailbox."
        )
    return None
