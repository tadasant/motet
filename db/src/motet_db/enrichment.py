"""PROTOTYPE — agentic enrichment: triage decisions, enrich runs and sealed browser states.

The columns migration 0014 adds to ``source_items``, the ``enrich_runs`` table and the
``browser_states`` table, with the same rules as :mod:`motet_db.connectors`: nothing here
commits, and the one secret — a browser's cookies for a site — crosses this module only
sealed. :func:`store_browser_state` takes a :class:`~motet_vault.DekWrapper` and
:func:`load_browser_state` a :class:`~motet_vault.KeyManager`, so only a worker can read a
cookie jar back (invariant 8). In practice both are called from the worker, which holds the
full manager; the split is what keeps the API from *being able* to ask.

**The AAD is ``user_id:<domain>:browser_state``**, through :func:`motet_vault.aad`'s
``source_id`` slot. A state copied onto another user's row, or onto another domain, fails
to authenticate rather than logging one person into another's subscription.
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Final, Literal

import psycopg
from motet_vault import DekWrapper, KeyManager, SealedSecret, aad, open_sealed, seal

from .ids import new_id
from .repo import _all, _maybe_one

TriageDecisionValue = Literal["raw", "fetch"]
EnrichStatus = Literal["pending", "running", "done", "failed", "skipped"]
RunStatus = Literal["running", "done", "failed"]

BROWSER_STATE_PROVIDER: Final = "browser_state"


@dataclass(frozen=True)
class Enrichment:
    """The enrichment columns of one source item, as a unit."""

    source_item_id: str
    user_id: str
    text: str
    triage_decision: str | None
    triage_reason: str | None
    article_url: str | None
    enrich_status: str | None
    enrich_error: str | None
    original_text: str | None
    enriched_at: datetime | None


@dataclass(frozen=True)
class EnrichRun:
    """One agent run, transcript included (already redacted when written)."""

    id: str
    source_item_id: str
    user_id: str
    started_at: datetime
    finished_at: datetime | None
    status: str
    tool_calls: int
    cost_usd: float | None
    transcript: list[dict[str, Any]]
    article_chars: int
    login_performed: bool
    error: str | None


@dataclass(frozen=True)
class BrowserState:
    """A site's cookies + local storage for one user, as Playwright's ``storageState``."""

    user_id: str
    domain: str
    state_json: str
    cookies: int
    updated_at: datetime


_ENRICHMENT_COLUMNS: Final = """
    id AS source_item_id, user_id, text, triage_decision, triage_reason, article_url,
    enrich_status, enrich_error, original_text, enriched_at
"""


def get_enrichment(conn: psycopg.Connection[Any], source_item_id: str) -> Enrichment | None:
    row = _maybe_one(
        conn,
        f"SELECT {_ENRICHMENT_COLUMNS} FROM source_items WHERE id = %s",
        (source_item_id,),
    )
    return _enrichment(row) if row else None


def record_triage(
    conn: psycopg.Connection[Any],
    source_item_id: str,
    *,
    decision: TriageDecisionValue,
    reason: str,
    article_url: str | None,
    enrich_status: EnrichStatus | None,
) -> None:
    """Persist what triage decided. Written once per item; a second pass skips triage."""
    conn.execute(
        """
        UPDATE source_items
        SET triage_decision = %s, triage_reason = %s, article_url = %s, enrich_status = %s
        WHERE id = %s
        """,
        (decision, reason, article_url, enrich_status, source_item_id),
    )


def set_enrich_status(
    conn: psycopg.Connection[Any],
    source_item_id: str,
    status: EnrichStatus,
    *,
    error: str | None = None,
) -> None:
    conn.execute(
        "UPDATE source_items SET enrich_status = %s, enrich_error = %s WHERE id = %s",
        (status, error, source_item_id),
    )


def apply_enrichment(
    conn: psycopg.Connection[Any], source_item_id: str, *, article_text: str
) -> None:
    """Replace the preview with the article, keeping the preview in ``original_text``.

    ``original_text`` is only written when it is still NULL, so a second enrichment of one
    item cannot overwrite the email with the first article.
    """
    conn.execute(
        """
        UPDATE source_items
        SET original_text = COALESCE(original_text, text),
            text = %s,
            enrich_status = 'done',
            enrich_error = NULL,
            enriched_at = now()
        WHERE id = %s
        """,
        (article_text, source_item_id),
    )


# --- enrich runs -----------------------------------------------------------------------


def insert_enrich_run(
    conn: psycopg.Connection[Any],
    *,
    source_item_id: str,
    user_id: str,
    started_at: datetime,
    finished_at: datetime,
    status: RunStatus,
    tool_calls: int,
    cost_usd: float | None,
    transcript: Sequence[dict[str, Any]],
    article_chars: int,
    login_performed: bool,
    error: str | None,
) -> str:
    """Record one finished agent run. Written whole, after the run, inside the handler's
    transaction — a half-written run row is not a thing the UI should ever see."""
    run_id = new_id("er")
    conn.execute(
        """
        INSERT INTO enrich_runs
            (id, source_item_id, user_id, started_at, finished_at, status, tool_calls,
             cost_usd, transcript, article_chars, login_performed, error)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s::jsonb, %s, %s, %s)
        """,
        (
            run_id,
            source_item_id,
            user_id,
            started_at,
            finished_at,
            status,
            tool_calls,
            cost_usd,
            json.dumps(list(transcript)),
            article_chars,
            login_performed,
            error,
        ),
    )
    return run_id


_RUN_COLUMNS: Final = """
    id, source_item_id, user_id, started_at, finished_at, status, tool_calls, cost_usd,
    transcript, article_chars, login_performed, error
"""


def latest_enrich_run(
    conn: psycopg.Connection[Any], source_item_id: str, *, user_id: str
) -> EnrichRun | None:
    row = _maybe_one(
        conn,
        f"""
        SELECT {_RUN_COLUMNS} FROM enrich_runs
        WHERE source_item_id = %s AND user_id = %s
        ORDER BY started_at DESC, id DESC LIMIT 1
        """,
        (source_item_id, user_id),
    )
    return _run(row) if row else None


def list_enrich_runs(conn: psycopg.Connection[Any], source_item_id: str) -> list[EnrichRun]:
    rows = _all(
        conn,
        f"SELECT {_RUN_COLUMNS} FROM enrich_runs WHERE source_item_id = %s ORDER BY started_at",
        (source_item_id,),
    )
    return [_run(row) for row in rows]


# --- browser states --------------------------------------------------------------------


def browser_state_aad(*, user_id: str, domain: str) -> bytes:
    return aad(user_id=user_id, source_id=domain, provider=BROWSER_STATE_PROVIDER)


def store_browser_state(
    conn: psycopg.Connection[Any],
    wrapper: DekWrapper,
    *,
    user_id: str,
    domain: str,
    state_json: str,
) -> int:
    """Seal a Playwright ``storageState`` document onto ``(user, domain)``. Returns cookies."""
    try:
        cookies = len(json.loads(state_json).get("cookies", []))
    except (ValueError, AttributeError):
        raise ValueError("a browser state must be a JSON object with a cookies list") from None
    sealed = seal(wrapper, state_json.encode(), browser_state_aad(user_id=user_id, domain=domain))
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
    return cookies


def load_browser_state(
    conn: psycopg.Connection[Any], manager: KeyManager, *, user_id: str, domain: str
) -> BrowserState | None:
    """Open the sealed state for ``(user, domain)``. **Workers only** — the decrypt half."""
    row = _maybe_one(
        conn,
        """
        SELECT ciphertext, nonce, wrapped_dek, cookies, updated_at
        FROM browser_states WHERE user_id = %s AND domain = %s
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
    plaintext = open_sealed(manager, sealed, browser_state_aad(user_id=user_id, domain=domain))
    return BrowserState(
        user_id=user_id,
        domain=domain,
        state_json=plaintext.decode(),
        cookies=row["cookies"],
        updated_at=row["updated_at"],
    )


def delete_browser_state(conn: psycopg.Connection[Any], *, user_id: str, domain: str) -> bool:
    result = conn.execute(
        "DELETE FROM browser_states WHERE user_id = %s AND domain = %s", (user_id, domain)
    )
    return result.rowcount == 1


def _enrichment(row: dict[str, Any]) -> Enrichment:
    return Enrichment(
        source_item_id=row["source_item_id"],
        user_id=row["user_id"],
        text=row["text"],
        triage_decision=row["triage_decision"],
        triage_reason=row["triage_reason"],
        article_url=row["article_url"],
        enrich_status=row["enrich_status"],
        enrich_error=row["enrich_error"],
        original_text=row["original_text"],
        enriched_at=row["enriched_at"],
    )


def _run(row: dict[str, Any]) -> EnrichRun:
    transcript = row["transcript"]
    if isinstance(transcript, str):
        transcript = json.loads(transcript)
    return EnrichRun(
        id=row["id"],
        source_item_id=row["source_item_id"],
        user_id=row["user_id"],
        started_at=row["started_at"],
        finished_at=row["finished_at"],
        status=row["status"],
        tool_calls=row["tool_calls"],
        cost_usd=float(row["cost_usd"]) if row["cost_usd"] is not None else None,
        transcript=list(transcript or []),
        article_chars=row["article_chars"],
        login_performed=bool(row["login_performed"]),
        error=row["error"],
    )


__all__ = [
    "BROWSER_STATE_PROVIDER",
    "BrowserState",
    "EnrichRun",
    "EnrichStatus",
    "Enrichment",
    "RunStatus",
    "TriageDecisionValue",
    "apply_enrichment",
    "browser_state_aad",
    "delete_browser_state",
    "get_enrichment",
    "latest_enrich_run",
    "list_enrich_runs",
    "load_browser_state",
    "record_triage",
    "set_enrich_status",
    "insert_enrich_run",
    "store_browser_state",
]
