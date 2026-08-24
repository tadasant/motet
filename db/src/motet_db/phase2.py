"""Phase 2's queries: connected sources, the credential vault, smart selection,
highlights, and playback progress.

Split from :mod:`motet_db.repo` because that file is already the longest in the tree and
these are a coherent, separable set — but the same rules apply and are worth restating:
**nothing here commits**, transactions belong to the caller, and this is the only place
these statements live. A worker that stored a credential and updated a source's cursor
does both in one transaction, and it can only do that if this module stays out of
transaction management.

**The credential functions never see plaintext except at the boundary.** Sealing and
unsealing happen here, against a key manager the caller supplies, so that no caller ever
has to remember to encrypt — and so that the encrypt-only/decrypt-capable split is
carried through in the signatures. :func:`store_source_credential` takes a
:class:`~motet_vault.DekWrapper`; :func:`load_source_credential` takes a
:class:`~motet_vault.KeyManager`. The API can hold the first and not the second.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

import psycopg
from motet_vault import DekWrapper, KeyManager, SealedSecret, aad, open_sealed, seal

from .ids import highlight_id, new_id, source_id
from .models import (
    Highlight,
    SourceItemState,
    StoredNewsItem,
    StoredSource,
)
from .repo import _all, _attach_sources, _maybe_one, _one
from .rules import Ranking, SmartRule

# --- sources -------------------------------------------------------------------------


def create_source(
    conn: psycopg.Connection[Any],
    *,
    user_id: str,
    kind: str,
    name: str,
    config: dict[str, Any] | None = None,
) -> StoredSource:
    """Add a connected source. Ids are ours, never the provider's."""
    import json  # noqa: PLC0415

    row = _one(
        conn,
        """
        INSERT INTO sources (id, user_id, kind, name, config)
        VALUES (%s, %s, %s, %s, %s::jsonb)
        RETURNING id, user_id, kind, name, config, sync_state, active,
                  last_polled_at, last_error, created_at
        """,
        (source_id(), user_id, kind, name, json.dumps(config or {})),
    )
    return _source(row)


def get_source(
    conn: psycopg.Connection[Any], source_id_: str, *, user_id: str | None = None
) -> StoredSource | None:
    row = _maybe_one(
        conn,
        """
        SELECT id, user_id, kind, name, config, sync_state, active,
               last_polled_at, last_error, created_at
        FROM sources WHERE id = %s AND (%s::text IS NULL OR user_id = %s)
        """,
        (source_id_, user_id, user_id),
    )
    return _source(row) if row else None


def list_sources(conn: psycopg.Connection[Any], user_id: str) -> list[StoredSource]:
    rows = _all(
        conn,
        """
        SELECT id, user_id, kind, name, config, sync_state, active,
               last_polled_at, last_error, created_at
        FROM sources WHERE user_id = %s ORDER BY created_at, id
        """,
        (user_id,),
    )
    return [_source(row) for row in rows]


def list_pollable_sources(conn: psycopg.Connection[Any], kind: str) -> list[StoredSource]:
    """Every active source of ``kind``, across every user — what a poll scheduler reads.

    Across users deliberately: the scheduler is one Cloud Run job, and per-user
    serialization is the *job's* concern (each poll carries its own serialize key) rather
    than something to enforce by querying one user at a time.
    """
    rows = _all(
        conn,
        """
        SELECT id, user_id, kind, name, config, sync_state, active,
               last_polled_at, last_error, created_at
        FROM sources WHERE kind = %s AND active ORDER BY id
        """,
        (kind,),
    )
    return [_source(row) for row in rows]


def set_source_sync_state(
    conn: psycopg.Connection[Any],
    source_id_: str,
    sync_state: dict[str, Any],
    *,
    error: str | None = None,
) -> None:
    """Record where the poll got to.

    Written in the same transaction as the source items the poll produced, so a crash
    either advances the cursor and keeps the messages or does neither. Advancing a cursor
    in its own transaction is how a mailbox silently skips a day.
    """
    import json  # noqa: PLC0415

    conn.execute(
        """
        UPDATE sources
        SET sync_state = %s::jsonb, last_polled_at = now(), last_error = %s
        WHERE id = %s
        """,
        (json.dumps(sync_state), error, source_id_),
    )


def set_source_active(conn: psycopg.Connection[Any], source_id_: str, *, active: bool) -> None:
    conn.execute("UPDATE sources SET active = %s WHERE id = %s", (active, source_id_))


# --- the credential vault ------------------------------------------------------------


@dataclass(frozen=True)
class StoredCredential:
    """A sealed credential, plus what a caller needs without opening it.

    Deliberately does **not** carry the plaintext. Opening is
    :func:`load_source_credential`, which needs a key manager and therefore the decrypt
    permission — so a caller that only wanted to know whether a mailbox is connected, or
    when its token expires, does not touch the key at all.
    """

    id: str
    user_id: str
    source_id: str
    provider: str
    purpose: str
    scopes: tuple[str, ...]
    expires_at: datetime | None
    backend: str
    key_name: str

    def expired(self, *, now: datetime, skew_seconds: int = 120) -> bool:
        """Whether this token should be refreshed before use.

        The skew is not superstition: an access token that passes this check and then
        expires during a multi-request poll fails halfway through, after some messages
        have been fetched. Refreshing two minutes early costs one extra token request and
        removes the partial-failure case entirely.
        """
        if self.expires_at is None:
            return False
        return self.expires_at <= now + timedelta(seconds=skew_seconds)


def store_source_credential(
    conn: psycopg.Connection[Any],
    wrapper: DekWrapper,
    *,
    user_id: str,
    source_id_: str,
    provider: str,
    purpose: str,
    secret: str,
    scopes: Sequence[str] = (),
    expires_at: datetime | None = None,
) -> StoredCredential:
    """Seal a token and store it. **The only way a credential enters the database.**

    Takes a :class:`~motet_vault.DekWrapper` — the encrypt-only half — because the API
    calls this from the OAuth callback and must not hold anything that could read a
    credential back (invariant 8). Cloud KMS enforces the same split in IAM; this
    signature is what makes a refactor that needed more than encrypt visible in review.

    Upserts on ``(source_id, purpose)``: re-running consent replaces the grant rather than
    accumulating grants, so there is never an ambiguity about which token is current.
    """
    sealed = seal(
        wrapper,
        secret.encode(),
        aad(user_id=user_id, source_id=source_id_, provider=provider),
    )
    row = _one(
        conn,
        """
        INSERT INTO source_credentials
            (id, user_id, source_id, provider, purpose, ciphertext, nonce, wrapped_dek,
             backend, key_name, scopes, expires_at)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
        ON CONFLICT (source_id, purpose) DO UPDATE SET
            ciphertext = EXCLUDED.ciphertext,
            nonce = EXCLUDED.nonce,
            wrapped_dek = EXCLUDED.wrapped_dek,
            backend = EXCLUDED.backend,
            key_name = EXCLUDED.key_name,
            scopes = EXCLUDED.scopes,
            expires_at = EXCLUDED.expires_at,
            updated_at = now()
        RETURNING id, user_id, source_id, provider, purpose, scopes, expires_at,
                  backend, key_name
        """,
        (
            new_id("cred"),
            user_id,
            source_id_,
            provider,
            purpose,
            sealed.ciphertext,
            sealed.nonce,
            sealed.wrapped_dek,
            sealed.backend,
            sealed.key_name,
            " ".join(scopes),
            expires_at,
        ),
    )
    return _credential(row)


def get_source_credential(
    conn: psycopg.Connection[Any], *, source_id_: str, purpose: str
) -> StoredCredential | None:
    """Credential metadata without opening it — no key manager, no decrypt permission."""
    row = _maybe_one(
        conn,
        """
        SELECT id, user_id, source_id, provider, purpose, scopes, expires_at,
               backend, key_name
        FROM source_credentials WHERE source_id = %s AND purpose = %s
        """,
        (source_id_, purpose),
    )
    return _credential(row) if row else None


def load_source_credential(
    conn: psycopg.Connection[Any], manager: KeyManager, *, source_id_: str, purpose: str
) -> str | None:
    """Open a sealed credential. **Workers only** — this is the decrypt half.

    The AAD is rebuilt from the row's own ``user_id``, ``source_id`` and ``provider``
    rather than from anything the caller passes, which is what makes the binding worth
    having: a ciphertext copied into another row authenticates against that row's identity
    and fails, instead of handing one account another's mailbox.
    """
    row = _maybe_one(
        conn,
        """
        SELECT user_id, source_id, provider, ciphertext, nonce, wrapped_dek
        FROM source_credentials WHERE source_id = %s AND purpose = %s
        """,
        (source_id_, purpose),
    )
    if row is None:
        return None
    sealed = SealedSecret(
        ciphertext=bytes(row["ciphertext"]),
        nonce=bytes(row["nonce"]),
        wrapped_dek=bytes(row["wrapped_dek"]),
        backend="",
        key_name="",
    )
    plaintext = open_sealed(
        manager,
        sealed,
        aad(
            user_id=row["user_id"],
            source_id=row["source_id"],
            provider=row["provider"],
        ),
    )
    return plaintext.decode()


def delete_source_credentials(conn: psycopg.Connection[Any], source_id_: str) -> int:
    """Forget every credential for a source — what disconnecting a mailbox means."""
    result = conn.execute("DELETE FROM source_credentials WHERE source_id = %s", (source_id_,))
    return result.rowcount


# --- OAuth handshake state -----------------------------------------------------------


def start_oauth(
    conn: psycopg.Connection[Any],
    *,
    state: str,
    user_id: str,
    provider: str,
    source_id_: str | None,
    code_verifier: str,
    redirect_uri: str,
    scopes: Sequence[str],
    ttl_seconds: int = 600,
) -> None:
    """Record an in-flight authorization so its callback can be believed.

    The state and the PKCE verifier have to be *stored* to be checked — a callback that
    validated a state it derived from its own parameters would defend against nothing.
    """
    conn.execute(
        """
        INSERT INTO oauth_states
            (state, user_id, provider, source_id, code_verifier, redirect_uri, scopes,
             expires_at)
        VALUES (%s, %s, %s, %s, %s, %s, %s, now() + make_interval(secs => %s))
        """,
        (
            state,
            user_id,
            provider,
            source_id_,
            code_verifier,
            redirect_uri,
            " ".join(scopes),
            ttl_seconds,
        ),
    )


def consume_oauth_state(conn: psycopg.Connection[Any], state: str) -> dict[str, Any] | None:
    """Take an in-flight authorization, exactly once.

    ``DELETE ... RETURNING`` rather than select-then-delete: a state is single-use, and
    doing it in one statement means a replayed callback finds nothing rather than racing
    a concurrent one into two token exchanges.
    """
    return _maybe_one(
        conn,
        """
        DELETE FROM oauth_states
        WHERE state = %s AND expires_at > now()
        RETURNING state, user_id, provider, source_id, code_verifier, redirect_uri, scopes
        """,
        (state,),
    )


def purge_expired_oauth_states(conn: psycopg.Connection[Any]) -> int:
    """Sweep authorizations nobody came back from — the normal outcome of a closed tab."""
    return conn.execute("DELETE FROM oauth_states WHERE expires_at <= now()").rowcount


# --- polled source items -------------------------------------------------------------


def source_item_exists(conn: psycopg.Connection[Any], *, source_id_: str, external_id: str) -> bool:
    """Whether this provider message has already been ingested.

    A cheap pre-check, not the guarantee — the guarantee is the unique index, which is
    what actually holds when two polls race. This exists so the normal case skips the
    fetch rather than paying for it and then hitting a conflict.
    """
    row = _maybe_one(
        conn,
        "SELECT 1 AS found FROM source_items WHERE source_id = %s AND external_id = %s",
        (source_id_, external_id),
    )
    return row is not None


def insert_polled_source_item(
    conn: psycopg.Connection[Any],
    *,
    user_id: str,
    source_id_: str,
    external_id: str,
    title: str,
    text: str,
) -> str | None:
    """Store a fetched message, or return ``None`` if it was already stored.

    ``ON CONFLICT DO NOTHING`` against the ``(source_id, external_id)`` index is what makes
    a re-poll after a crash idempotent. Returning ``None`` rather than raising lets the
    caller treat "already have it" as the ordinary outcome it is, and — importantly — skip
    enqueueing a second integrate job for a source item that already has one.
    """
    from .ids import source_item_id  # noqa: PLC0415

    row = _maybe_one(
        conn,
        """
        INSERT INTO source_items (id, user_id, source_id, title, text, external_id)
        VALUES (%s, %s, %s, %s, %s, %s)
        ON CONFLICT (source_id, external_id) WHERE external_id IS NOT NULL DO NOTHING
        RETURNING id
        """,
        (source_item_id(), user_id, source_id_, title, text, external_id),
    )
    return row["id"] if row else None


# --- smart episode selection ---------------------------------------------------------


def select_for_rule(
    conn: psycopg.Connection[Any],
    user_id: str,
    rule: SmartRule,
    *,
    now: datetime | None = None,
) -> list[StoredNewsItem]:
    """The stories a rule selects, already in the order the cap should be applied in.

    One query for both episode kinds. A manual episode is ``SmartRule.manual()`` — unread,
    no window, oldest first — so "smart" adds knobs rather than a second code path, and
    the two cannot drift into disagreeing about what "unread" means (invariant 5).

    Ordering is fully specified down to the id in every branch. Without the tiebreak,
    ``coverage`` would return an arbitrary order among equally-covered stories, and an
    episode built twice from the same backlog would contain different stories — which the
    golden set is there to catch and which would be maddening to debug in production.
    """
    conditions = ["ni.user_id = %s"]
    params: list[Any] = [user_id]

    if rule.unread_only:
        conditions.append("ni.read_at IS NULL")
    if rule.window_days > 0:
        cutoff = (now or _db_now(conn)) - timedelta(days=rule.window_days)
        conditions.append("ni.created_at >= %s")
        params.append(cutoff)
    if rule.source_ids:
        # EXISTS rather than a join: a story with three qualifying source items must
        # appear once, and a join would return it three times.
        conditions.append(
            """EXISTS (
                SELECT 1 FROM news_item_sources nis
                JOIN source_items si ON si.id = nis.source_item_id
                WHERE nis.news_item_id = ni.id AND si.source_id = ANY(%s)
            )"""
        )
        params.append(list(rule.source_ids))

    order = {
        Ranking.OLDEST_FIRST: "ni.created_at ASC, ni.id ASC",
        Ranking.NEWEST_FIRST: "ni.created_at DESC, ni.id DESC",
        # Most-covered first, then oldest — so among equally covered stories the backlog
        # still drains from the front.
        Ranking.COVERAGE: "source_count DESC, ni.created_at ASC, ni.id ASC",
    }[rule.ranking]

    params.append(rule.max_items)
    rows = _all(
        conn,
        f"""
        SELECT ni.id, ni.user_id, ni.title, ni.summary, ni.read_at, ni.created_at,
               (SELECT count(*) FROM news_item_sources nis WHERE nis.news_item_id = ni.id)
                   AS source_count
        FROM news_items ni
        WHERE {" AND ".join(conditions)}
        ORDER BY {order}
        LIMIT %s
        """,
        tuple(params),
    )
    return _attach_sources(conn, rows)


# --- playback progress, and the read state it drives ---------------------------------


def record_listen_progress(
    conn: psycopg.Connection[Any], *, user_id: str, episode_id_: str, listened_through_ms: int
) -> tuple[int, int]:
    """Advance an episode's playback position. Returns ``(position, news items marked read)``.

    **Monotonic.** ``greatest(listened_through_ms, %s)`` — a client that seeks backwards is
    reviewing something, not un-hearing it, and a position that could decrease would
    un-mark stories as a side effect of scrubbing. Invariant 4 says the position is ours;
    this is what owning it actually means, rather than mirroring whatever a player last
    said.

    Invariant 5 is the second half: a story counts as read once its segment has been
    *passed*, so the comparison is against the end of the segment rather than its start.
    Marking at the start would tick off a story the moment its first word played.
    """
    row = _maybe_one(
        conn,
        """
        UPDATE episodes
        SET listened_through_ms = greatest(listened_through_ms, %s), updated_at = now()
        WHERE id = %s AND user_id = %s
        RETURNING listened_through_ms
        """,
        (max(0, listened_through_ms), episode_id_, user_id),
    )
    if row is None:
        raise LookupError(f"no episode {episode_id_!r} for this user")
    position = int(row["listened_through_ms"])

    passed = _all(
        conn,
        """
        SELECT news_item_id FROM episode_segments
        WHERE episode_id = %s AND duration_ms > 0 AND start_ms + duration_ms <= %s
        """,
        (episode_id_, position),
    )
    from .repo import mark_news_items_read  # noqa: PLC0415

    marked = mark_news_items_read(
        conn, user_id=user_id, item_ids=[row["news_item_id"] for row in passed]
    )
    return position, marked


# --- highlights ----------------------------------------------------------------------


def save_highlight(
    conn: psycopg.Connection[Any],
    *,
    user_id: str,
    news_item_id: str,
    source_item_id_: str,
    span_start: int,
    span_end: int,
    note: str | None = None,
    episode_id_: str | None = None,
    anchor_ms: int | None = None,
) -> Highlight | None:
    """Save a passage, anchored to its source span. Idempotent.

    ``quote`` is read out of the source item **inside this statement** rather than taken
    from the caller. That is the whole trust property: a highlight's text is what the
    source actually says at that span, not what a caller — a model calling the
    ``save_highlight`` tool, in the voice case — asserted it says. A model that quoted
    loosely would otherwise write its paraphrase into the user's saved highlights and it
    would look verbatim.

    Returns ``None`` when the span does not resolve, which is the same rule
    ``segment_claims`` enforces with a CHECK: an anchor that points at nothing is not an
    anchor. Saving the same span twice returns the existing highlight rather than a
    duplicate — voice and touch can both reach for one sentence.
    """
    row = _maybe_one(
        conn,
        """
        INSERT INTO highlights
            (id, user_id, news_item_id, source_item_id, span_start, span_end, quote,
             note, episode_id, anchor_ms)
        SELECT %s, %s, %s, si.id, %s, %s, substring(si.text FROM %s FOR %s), %s, %s, %s
        FROM source_items si
        WHERE si.id = %s AND si.user_id = %s AND %s >= 0 AND %s > %s
          AND %s <= length(si.text)
        ON CONFLICT (user_id, source_item_id, span_start, span_end) DO UPDATE
            SET note = coalesce(EXCLUDED.note, highlights.note)
        RETURNING id, user_id, news_item_id, source_item_id, span_start, span_end,
                  quote, note, episode_id, anchor_ms, created_at
        """,
        (
            highlight_id(),
            user_id,
            news_item_id,
            span_start,
            span_end,
            # `substring` is 1-indexed and takes a length; the span is a half-open
            # 0-indexed range, the same convention every claim uses.
            span_start + 1,
            span_end - span_start,
            note,
            episode_id_,
            anchor_ms,
            source_item_id_,
            user_id,
            span_start,
            span_end,
            span_start,
            span_end,
        ),
    )
    return _highlight(row) if row else None


def list_highlights(
    conn: psycopg.Connection[Any], user_id: str, *, news_item_id: str | None = None
) -> list[Highlight]:
    rows = _all(
        conn,
        f"""
        SELECT id, user_id, news_item_id, source_item_id, span_start, span_end,
               quote, note, episode_id, anchor_ms, created_at
        FROM highlights
        WHERE user_id = %s {"AND news_item_id = %s" if news_item_id else ""}
        ORDER BY created_at DESC, id DESC
        """,
        (user_id, news_item_id) if news_item_id else (user_id,),
    )
    return [_highlight(row) for row in rows]


def delete_highlight(conn: psycopg.Connection[Any], *, user_id: str, highlight_id_: str) -> bool:
    result = conn.execute(
        "DELETE FROM highlights WHERE id = %s AND user_id = %s", (highlight_id_, user_id)
    )
    return result.rowcount > 0


# --- claim timing, for subtitles -----------------------------------------------------


def set_claim_timings(
    conn: psycopg.Connection[Any], episode_id_: str, timings: Sequence[tuple[str, int, int]]
) -> None:
    """Write ``(claim id, start_ms, duration_ms)`` for an episode's claims."""
    for claim_id_, start_ms, duration_ms in timings:
        conn.execute(
            "UPDATE segment_claims SET start_ms = %s, duration_ms = %s WHERE id = %s",
            (start_ms, duration_ms, claim_id_),
        )


# --- row plumbing --------------------------------------------------------------------


def _db_now(conn: psycopg.Connection[Any]) -> datetime:
    value = _one(conn, "SELECT now() AS now", ())["now"]
    assert isinstance(value, datetime)
    return value


def _source(row: dict[str, Any]) -> StoredSource:
    return StoredSource(
        id=row["id"],
        user_id=row["user_id"],
        kind=row["kind"],
        name=row["name"],
        config=dict(row["config"] or {}),
        sync_state=dict(row["sync_state"] or {}),
        active=row["active"],
        last_polled_at=row["last_polled_at"],
        last_error=row["last_error"],
        created_at=row["created_at"],
    )


def _credential(row: dict[str, Any]) -> StoredCredential:
    return StoredCredential(
        id=row["id"],
        user_id=row["user_id"],
        source_id=row["source_id"],
        provider=row["provider"],
        purpose=row["purpose"],
        scopes=tuple(row["scopes"].split()) if row["scopes"] else (),
        expires_at=row["expires_at"],
        backend=row["backend"],
        key_name=row["key_name"],
    )


def _highlight(row: dict[str, Any]) -> Highlight:
    return Highlight(
        id=row["id"],
        user_id=row["user_id"],
        news_item_id=row["news_item_id"],
        source_item_id=row["source_item_id"],
        span_start=row["span_start"],
        span_end=row["span_end"],
        quote=row["quote"],
        note=row["note"],
        episode_id=row["episode_id"],
        anchor_ms=row["anchor_ms"],
        created_at=row["created_at"],
    )


__all__ = [
    "Ranking",
    "SmartRule",
    "SourceItemState",
    "StoredCredential",
    "consume_oauth_state",
    "create_source",
    "delete_highlight",
    "delete_source_credentials",
    "get_source",
    "get_source_credential",
    "insert_polled_source_item",
    "list_highlights",
    "list_pollable_sources",
    "list_sources",
    "load_source_credential",
    "purge_expired_oauth_states",
    "record_listen_progress",
    "save_highlight",
    "select_for_rule",
    "set_claim_timings",
    "set_source_active",
    "set_source_sync_state",
    "source_item_exists",
    "start_oauth",
    "store_source_credential",
]
