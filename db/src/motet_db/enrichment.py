"""Enrichment's rows: what a run decided, what it produced, and the browser it left.

The same rules as :mod:`motet_db.connectors` and :mod:`motet_db.phase2`, and for the same
reasons: **nothing here commits**, the statements live only here, and a secret crosses this
module at exactly two boundaries — :func:`store_browser_state` takes a
:class:`~motet_vault.DekWrapper` and seals, :func:`load_browser_state` takes a
:class:`~motet_vault.KeyManager` and opens. Invariant 8 is the IAM grant behind those two
types, and the split is what stops a well-meaning refactor from quietly needing it widened.

**A browser state is the vault's third kind of sealed record**, after a Gmail refresh token
and a connector's secret. It is a Playwright storage state: the cookies and localStorage of
a logged-in session on one publisher's site. Its AAD is ``user_id:<domain>:browser_state``,
built through :func:`motet_vault.aad` whose middle slot is named ``source_id`` for what it
was first written for — the *string* has the same shape and buys the same property, which
is that a ciphertext moved onto another user's row, or another domain's, fails to
authenticate rather than logging one account into another's site.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Final, Literal

import psycopg
from motet_vault import DekWrapper, KeyManager, SealedSecret, aad, open_sealed, seal
from psycopg.types.json import Jsonb

from .ids import new_id
from .repo import _all, _maybe_one, _one

EnrichStatus = Literal["queued", "running", "done", "failed", "skipped"]
RunStatus = Literal["ok", "blocked", "capped", "timeout", "failed", "skipped"]

#: The ``provider`` slot of a browser state's AAD. A constant rather than a literal at each
#: call site, because the two calls that must agree are a seal and an open written months
#: apart, and a typo in one of them is a credential that will not reopen.
BROWSER_STATE_AAD_KIND: Final = "browser_state"

#: How far back the per-user daily spend cap looks. Rolling, not a calendar day: a cap that
#: resets at midnight UTC is a cap that does nothing to a backlog ingested at 23:55.
DAILY_SPEND_WINDOW: Final = timedelta(hours=24)


def browser_state_aad(*, user_id: str, domain: str) -> bytes:
    """``user_id:<domain>:browser_state`` — see the module docstring for the slot names."""
    return aad(user_id=user_id, source_id=domain, provider=BROWSER_STATE_AAD_KIND)


@dataclass(frozen=True)
class StoredEnrichRun:
    """One run, as any caller may see it. The transcript is already redacted."""

    id: str
    source_item_id: str
    user_id: str
    domain: str
    status: str
    tool_calls: int
    cost_usd: float
    article_chars: int
    login_performed: bool
    transcript: list[dict[str, Any]]
    error: str | None
    started_at: datetime
    finished_at: datetime | None


@dataclass(frozen=True)
class EnrichmentState:
    """What ``source_items`` records about one item's enrichment."""

    status: str | None
    article_url: str | None
    domain: str | None
    error: str | None
    enriched_at: datetime | None
    original_chars: int | None


# --- the decision on the item --------------------------------------------------------


def mark_enrichment_queued(
    conn: psycopg.Connection[Any], item_id: str, *, article_url: str, domain: str
) -> None:
    """Record, in the transaction that writes the job, what that job is for.

    Written beside the enqueue rather than by the handler, so that an item whose run never
    starts still says which link it was pointed at and which site's credentials it would
    have used — which is the difference between "nothing happened" and "nothing happened
    *yet*" on the lifecycle view.
    """
    conn.execute(
        """
        UPDATE source_items
        SET enrich_status = 'queued', article_url = %s, enrich_domain = %s, enrich_error = NULL
        WHERE id = %s
        """,
        (article_url, domain, item_id),
    )


def mark_enrichment_running(conn: psycopg.Connection[Any], item_id: str) -> None:
    """Say the run has started.

    **Meant to be called on a connection of its own**, outside the handler's transaction:
    the handler holds one open for as long as the agent runs, which can be ten minutes, and
    nothing written on it is visible to the panel somebody is watching until it commits.
    """
    conn.execute(
        """
        UPDATE source_items SET enrich_status = 'running'
        WHERE id = %s AND enrich_status IS DISTINCT FROM 'done'
        """,
        (item_id,),
    )


def apply_enriched_article(
    conn: psycopg.Connection[Any], item_id: str, *, article_url: str, article: str
) -> None:
    """The article replaces the preview, and the preview is kept (design option H1).

    ``original_text`` is only written the first time, with ``COALESCE``, so a replayed job
    cannot overwrite the newsletter with the article it already wrote — which would lose
    the one copy of what actually arrived.

    ``clock_timestamp()`` for :func:`record_enrich_run`'s reason: the transaction this runs
    in opened before the agent started, so ``now()`` would date the article to before it
    was fetched.
    """
    conn.execute(
        """
        UPDATE source_items
        SET original_text = COALESCE(original_text, text),
            text = %s,
            enrich_status = 'done',
            enrich_error = NULL,
            enriched_at = clock_timestamp()
        WHERE id = %s
        """,
        (f"Full article fetched from {article_url}\n\n{article}", item_id),
    )


def mark_enrichment_finished(
    conn: psycopg.Connection[Any], item_id: str, *, status: EnrichStatus, error: str | None
) -> None:
    """Record a run that produced no article. The preview stays; the item still integrates.

    ``skipped`` and ``failed`` are kept apart because they mean different things to a
    person: skipped is a cap or a policy declining to spend, failed is something that did
    not work. Neither is an error on the *item* — ``source_items.last_error`` is the
    pipeline giving up, and enrichment giving up is not that.
    """
    conn.execute(
        "UPDATE source_items SET enrich_status = %s, enrich_error = %s WHERE id = %s",
        (status, error, item_id),
    )


def enrichment_state(conn: psycopg.Connection[Any], item_id: str) -> EnrichmentState | None:
    row = _maybe_one(
        conn,
        """
        SELECT enrich_status, article_url, enrich_domain, enrich_error, enriched_at,
               length(original_text) AS original_chars
        FROM source_items WHERE id = %s
        """,
        (item_id,),
    )
    if row is None:
        return None
    return EnrichmentState(
        status=row["enrich_status"],
        article_url=row["article_url"],
        domain=row["enrich_domain"],
        error=row["enrich_error"],
        enriched_at=row["enriched_at"],
        original_chars=row["original_chars"],
    )


def source_item_links(conn: psycopg.Connection[Any], item_id: str) -> list[str]:
    """The links the newsletter carried, in document order."""
    row = _maybe_one(conn, "SELECT links FROM source_items WHERE id = %s", (item_id,))
    return list(row["links"] or ()) if row is not None else []


# --- the run log ---------------------------------------------------------------------


def record_enrich_run(
    conn: psycopg.Connection[Any],
    *,
    source_item_id: str,
    user_id: str,
    domain: str,
    status: RunStatus,
    tool_calls: int = 0,
    cost_usd: float = 0.0,
    article_chars: int = 0,
    login_performed: bool = False,
    transcript: Sequence[dict[str, Any]] = (),
    error: str | None = None,
    started_at: datetime | None = None,
) -> str:
    """Append one run. Never updated afterwards — this table is a log.

    ``started_at`` is passed in rather than defaulted here because the run began before the
    transaction that records it: the handler holds a connection open for the length of the
    agent's work, and ``now()`` at the end of that would report a ten-minute run as
    instantaneous.

    **``clock_timestamp()``, not ``now()``, and that is the same point one step further.**
    ``now()`` is the *transaction's* start time in Postgres, and the transaction that writes
    this row opened before the agent did anything — so ``finished_at = now()`` would be
    earlier than the work it claims to have finished. It also makes the ordering in
    :func:`latest_enrich_run` sound: two rows written in one transaction would otherwise
    share a timestamp and be separated only by a random id.
    """
    run_id = new_id("er")
    conn.execute(
        """
        INSERT INTO enrich_runs
            (id, source_item_id, user_id, domain, status, tool_calls, cost_usd,
             article_chars, login_performed, transcript, error, started_at, finished_at)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s,
                COALESCE(%s, clock_timestamp()), clock_timestamp())
        """,
        (
            run_id,
            source_item_id,
            user_id,
            domain,
            status,
            tool_calls,
            cost_usd,
            article_chars,
            login_performed,
            Jsonb(list(transcript)),
            error,
            started_at,
        ),
    )
    return run_id


def latest_enrich_run(conn: psycopg.Connection[Any], source_item_id: str) -> StoredEnrichRun | None:
    """The newest run for an item, transcript included.

    Ordered by ``started_at``, **not** by the id. The id here is random hex rather than a
    sequence — ``new_id`` is what every row in this schema is keyed by — so ``ORDER BY id
    DESC`` would pick an arbitrary run and would be right about half the time, which is the
    worst way for this to be wrong. The id is the tie-break and nothing more.
    """
    row = _maybe_one(
        conn,
        """
        SELECT * FROM enrich_runs WHERE source_item_id = %s
        ORDER BY started_at DESC, id DESC LIMIT 1
        """,
        (source_item_id,),
    )
    return None if row is None else _run(row)


def enrich_runs_for_item(
    conn: psycopg.Connection[Any], source_item_id: str
) -> list[StoredEnrichRun]:
    rows = _all(
        conn,
        "SELECT * FROM enrich_runs WHERE source_item_id = %s ORDER BY started_at DESC, id DESC",
        (source_item_id,),
    )
    return [_run(row) for row in rows]


def spend_since(
    conn: psycopg.Connection[Any], user_id: str, *, window: timedelta = DAILY_SPEND_WINDOW
) -> float:
    """What this user's enrichment runs have cost in the last ``window``.

    The per-user daily cap (design option C2) is answered from here, and it counts **every**
    run rather than only the successful ones: a run that hit a wall after twenty tool calls
    was billed for all twenty, and a cap that ignored those would be no cap at all on the
    case that actually runs away.
    """
    row = _one(
        conn,
        """
        SELECT COALESCE(sum(cost_usd), 0) AS spent
        FROM enrich_runs
        WHERE user_id = %s AND started_at > now() - %s::interval
        """,
        (user_id, window),
    )
    return float(row["spent"])


# --- the browser state ---------------------------------------------------------------


def store_browser_state(
    conn: psycopg.Connection[Any],
    wrapper: DekWrapper,
    *,
    user_id: str,
    domain: str,
    state: str,
    cookies: int,
) -> None:
    """Seal this domain's session and replace whatever was there.

    Replaced wholesale rather than versioned: a storage state is a live session, and the
    previous one is superseded rather than a version of anything worth keeping.
    """
    sealed = seal(wrapper, state.encode(), browser_state_aad(user_id=user_id, domain=domain))
    conn.execute(
        """
        INSERT INTO browser_states
            (user_id, domain, ciphertext, nonce, wrapped_dek, backend, key_name, cookies)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
        ON CONFLICT (user_id, domain) DO UPDATE
        SET ciphertext = EXCLUDED.ciphertext, nonce = EXCLUDED.nonce,
            wrapped_dek = EXCLUDED.wrapped_dek, backend = EXCLUDED.backend,
            key_name = EXCLUDED.key_name, cookies = EXCLUDED.cookies, updated_at = now()
        """,
        (
            user_id,
            domain,
            sealed.ciphertext,
            sealed.nonce,
            sealed.wrapped_dek,
            sealed.backend,
            sealed.key_name,
            cookies,
        ),
    )


def load_browser_state(
    conn: psycopg.Connection[Any], manager: KeyManager, *, user_id: str, domain: str
) -> str | None:
    """Open this domain's saved session. **Workers only** — this is the decrypt half."""
    row = _maybe_one(
        conn,
        """
        SELECT ciphertext, nonce, wrapped_dek FROM browser_states
        WHERE user_id = %s AND domain = %s
        """,
        (user_id, domain),
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
    return open_sealed(manager, sealed, browser_state_aad(user_id=user_id, domain=domain)).decode()


def browser_state_cookies(
    conn: psycopg.Connection[Any], *, user_id: str, domain: str
) -> int | None:
    """How many cookies this domain's saved session holds, without opening it.

    The one thing about a sealed state that is readable without the decrypt half, and it
    exists because "a session was saved and it is empty" and "no session was saved" are
    otherwise the same row to anyone debugging a login that will not stick.
    """
    row = _maybe_one(
        conn,
        "SELECT cookies FROM browser_states WHERE user_id = %s AND domain = %s",
        (user_id, domain),
    )
    return None if row is None else int(row["cookies"])


def _run(row: dict[str, Any]) -> StoredEnrichRun:
    return StoredEnrichRun(
        id=row["id"],
        source_item_id=row["source_item_id"],
        user_id=row["user_id"],
        domain=row["domain"],
        status=row["status"],
        tool_calls=row["tool_calls"],
        cost_usd=float(row["cost_usd"]),
        article_chars=row["article_chars"],
        login_performed=row["login_performed"],
        transcript=list(row["transcript"] or ()),
        error=row["error"],
        started_at=row["started_at"],
        finished_at=row["finished_at"],
    )
