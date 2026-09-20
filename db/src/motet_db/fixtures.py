"""The staging test harness's reads and writes — the SQL half.

`motet_api.fixtures` is the half that decides whether this deployment may run any of it;
this module is the statements themselves, kept beside the rest of the SQL so that a reset
and the queries it has to stay in step with (`repo._ADMIN_JOBS_SQL`, `conftest.TABLES`)
are read together rather than one of them being remembered.

**Nothing here checks the flag**, deliberately, and it is the same shape as
:mod:`motet_db.mint_session`: an interlock parsed in two places is an interlock that can
disagree with itself, so `MOTET_TEST_FIXTURES` is read in exactly one — `motet_api.fixtures`
— and this module is reachable only from behind it. What keeps these functions out of
production is not their own guard: it is that the one process which calls them refuses to
start in production with the flag on, and that no other caller exists.

**The reset is scoped to one user and to one question** — "what has this account ingested
and produced" — and the list of what it leaves alone is as much the design as the list of
what it removes. See :data:`RESET_KEEPS`.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Final

import psycopg

from .repo import _ADMIN_JOBS_SQL, PASTE_SOURCE_ID, _all, _maybe_one

#: What :func:`reset_user` removes, in the order it removes it, as ``(label, statement)``.
#:
#: **Jobs go first, and that ordering is the mechanism rather than tidiness.** A job is
#: resolved to a user by joining its payload to the row it is about, so a job whose source
#: item or episode has already been deleted resolves to nobody and would be left behind —
#: to be claimed by the next drain, to fail on a row that is gone, and to sit in the
#: queue's failure counts for ninety days (`jobs.prune`).
#:
#: The join tables are deleted **explicitly** rather than left to ``ON DELETE CASCADE``,
#: even though every one of them would go anyway. The reason is the response: a reset that
#: reported only the rows its own statements touched would say "3 episodes" and nothing
#: about the segments and claims under them, and this surface exists so that an agent can
#: assert a baseline is clean. A count nobody can check is worse than no count.
_RESET_STEPS: Final[tuple[tuple[str, str], ...]] = (
    (
        "jobs",
        f"""
        DELETE FROM jobs
        WHERE id IN (SELECT id FROM ({_ADMIN_JOBS_SQL}) resolved WHERE user_id = %(user_id)s)
        """,
    ),
    ("enrich_runs", "DELETE FROM enrich_runs WHERE user_id = %(user_id)s"),
    ("highlights", "DELETE FROM highlights WHERE user_id = %(user_id)s"),
    (
        "segment_claims",
        """
        DELETE FROM segment_claims
        WHERE segment_id IN (
            SELECT s.id FROM episode_segments s
            JOIN episodes e ON e.id = s.episode_id
            WHERE e.user_id = %(user_id)s
        )
        """,
    ),
    (
        "episode_segments",
        """
        DELETE FROM episode_segments
        WHERE episode_id IN (SELECT id FROM episodes WHERE user_id = %(user_id)s)
        """,
    ),
    ("episodes", "DELETE FROM episodes WHERE user_id = %(user_id)s"),
    (
        "news_item_sources",
        """
        DELETE FROM news_item_sources
        WHERE news_item_id IN (SELECT id FROM news_items WHERE user_id = %(user_id)s)
        """,
    ),
    ("news_items", "DELETE FROM news_items WHERE user_id = %(user_id)s"),
    ("source_items", "DELETE FROM source_items WHERE user_id = %(user_id)s"),
    ("source_credentials", "DELETE FROM source_credentials WHERE user_id = %(user_id)s"),
    ("browser_states", "DELETE FROM browser_states WHERE user_id = %(user_id)s"),
    (
        "sources",
        "DELETE FROM sources WHERE user_id = %(user_id)s AND id <> %(paste_source_id)s",
    ),
)

#: What a reset deliberately leaves standing, and why. Prose rather than code because
#: every entry is a decision somebody could reasonably have made the other way, and a
#: reader deciding whether to add one needs the reasons more than the list.
#:
#: * ``users`` — the account is the fixture, not the state.
#: * ``auth_sessions`` — the agent driving the loop is *holding* one. A reset that revoked
#:   it would log the caller out halfway through its own test run.
#: * ``feed_tokens`` — rotating the feed URL unsubscribes every podcast client already on
#:   it, which is a side effect well outside "sources, items and episodes".
#: * ``connectors`` — site logins and MCP servers the owner added by hand, each behind a
#:   one-time human step (invariant 9). Re-establishing one is not something this can do.
#: * ``settings``, ``llm_usage`` — the model overrides an operator set, and an append-only
#:   spend ledger. Deleting the second would destroy the record of money already spent.
#: * ``worker_heartbeats`` — a statement about the deployment, not about this user.
#: * ``waitlist_signups`` — other people's addresses.
#: * ``auth_handoffs``, ``mcp_oauth_clients``, ``mcp_oauth_codes``,
#:   ``mcp_oauth_refresh_tokens`` — sign-in and MCP-client grants, which are the same case
#:   as ``auth_sessions``: they are how a caller is *holding* this connection, and the
#:   harness has no business deciding a client must authorize again.
#: * ``oauth_states`` — an authorization *in flight*: a browser mid-sign-in, a phone
#:   mid-handoff, an MCP client at its consent screen. Deleting one turns the callback into
#:   "unknown or already used" for a person who did nothing wrong, which is the same
#:   argument as the row above one step earlier. The one kind of state a reset *should*
#:   remove — a mailbox consent for a source it deletes — cascades from ``sources`` on its
#:   own (``oauth_states.source_id`` references it), so an explicit statement would only
#:   ever reach the ones that must stay.
#:
#: **This list is complete against the schema, and that is a property worth keeping.** It
#: is reported on the wire as "tables a reset never touches", so a caller asserting a
#: baseline from it is entitled to read it as exhaustive —
#: ``db/tests/test_fixtures_reset.py`` holds it and :data:`_RESET_STEPS` together against
#: every table the migrations create, so a new table has to be classified rather than
#: quietly falling into neither.
RESET_KEEPS: Final[tuple[str, ...]] = (
    "users",
    "auth_sessions",
    "auth_handoffs",
    "oauth_states",
    "feed_tokens",
    "connectors",
    "settings",
    "llm_usage",
    "mcp_oauth_clients",
    "mcp_oauth_codes",
    "mcp_oauth_refresh_tokens",
    "worker_heartbeats",
    "waitlist_signups",
)

#: Namespace for the transaction lock a seed takes, beside
#: :data:`motet_db.repo.HELD_CLAIM_LOCK_NAMESPACE`'s 2 and the worker's one-argument form.
#:
#: Its own number rather than the held-claim one because the two serialize different
#: things: this stops a *second seed* creating a second mailbox, and has no reason to make
#: an "Ingest now" wait behind it.
SEED_LOCK_NAMESPACE: Final = 3


def reset_user(conn: psycopg.Connection[Any], *, user_id: str) -> dict[str, int]:
    """Delete one user's ingested and produced state. Returns rows removed, per table.

    Runs in the caller's transaction, so a failure part-way leaves the account exactly as
    it was rather than half-wiped.

    **A ``running`` job is deleted like any other**, and the worker holding it finds out
    rather than being surprised: :func:`motet_workers.jobs.touch` already classifies "this
    row is not ``running`` any more" as the benign outcome it meets routinely when a job
    finishes while a touch is in flight, and ``complete`` and ``fail`` update zero rows.
    Refusing to reset while anything is running would be the wrong trade for a harness
    whose whole job is to reach a known state on demand.

    **What that leaves, stated rather than left to be found:** a handler already inside its
    own transaction can commit *after* this one reads. A ``handle_poll`` mid-flight can
    write ``extract`` rows after the ``jobs`` delete and before the ``sources`` delete, and
    those either cascade away with the source or — if they land after it — fail their
    foreign key and roll the handler back. Either way nothing is stranded; what is not
    guaranteed is that the counts returned are the last word while a worker is running. A
    harness run that resets and then asserts should not have a worker draining underneath
    it, which is the condition the loop already arranges.
    """
    removed: dict[str, int] = {}
    params = {"user_id": user_id, "paste_source_id": PASTE_SOURCE_ID}
    with conn.cursor() as cur:
        for table, statement in _RESET_STEPS:
            cur.execute(statement, params)
            removed[table] = cur.rowcount
    return removed


@dataclass(frozen=True)
class JobStatus:
    """One job row, resolved to the user it belongs to, with the queue's liveness beside it.

    The heartbeat is what answers the question this surface exists for. A job sitting in
    ``ready`` looks identical whether a worker is chewing through a backlog or whether none
    has run for a week — which is motet#38's defect, and the reason a caller has to be told
    both numbers rather than left to infer one from the other.
    """

    id: int
    queue: str
    state: str
    attempts: int
    user_id: str | None
    subject: str | None
    last_error: str | None
    run_at: datetime
    created_at: datetime
    updated_at: datetime
    locked_at: datetime | None
    #: The database's clock, read in the same statement as the row, for
    #: :func:`motet_db.repo.worker_heartbeats`' reason: a caller subtracting these against
    #: its own clock is one resumed laptop away from a wrong answer.
    now: datetime
    #: When a worker was last seen on *this job's* queue, not on any queue.
    queue_last_seen_at: datetime | None


def job_status(conn: psycopg.Connection[Any], job_id: int) -> JobStatus | None:
    """One job by id, or None. ``user_id`` is the caller's to check.

    Resolved through the same expression the operator view uses, rather than through a
    second one: "which user is this job about" already has an answer in this repository
    and a second copy would eventually disagree with it about a queue somebody added.
    """
    row = _maybe_one(
        conn,
        f"""
        SELECT resolved.*, now() AS now, wh.last_seen_at AS queue_last_seen_at
        FROM ({_ADMIN_JOBS_SQL}) resolved
        LEFT JOIN worker_heartbeats wh ON wh.queue = resolved.queue
        WHERE resolved.id = %s
        """,
        (job_id,),
    )
    return None if row is None else _job_status(row)


def newest_job_for(
    conn: psycopg.Connection[Any], *, queue: str, subject_id: str
) -> JobStatus | None:
    """The newest job on ``queue`` about ``subject_id`` — the one the enqueue just made.

    ``subject`` is the operator view's own COALESCE over the three payload keys a queue can
    name its subject under. **It is a sequential scan of ``jobs``**, because that COALESCE
    is what none of the partial expression indexes can serve — bounded by ``jobs.prune``'s
    retention windows rather than by the deployment's age, and paid once per episode
    trigger on a test harness. So it is used only where nothing better exists:
    ``enqueue_episode`` returns the episode id and not its assemble job's, and widening a
    helper the product route and the worker share for one harness caller is the wrong
    trade. ``enqueue_source_poll`` *does* return the job id, and the poll trigger reads
    that row by primary key instead. Called inside the transaction that did the enqueue, so
    the row it finds is that one.
    """
    row = _maybe_one(
        conn,
        f"""
        SELECT resolved.*, now() AS now, wh.last_seen_at AS queue_last_seen_at
        FROM ({_ADMIN_JOBS_SQL}) resolved
        LEFT JOIN worker_heartbeats wh ON wh.queue = resolved.queue
        WHERE resolved.queue = %s AND resolved.subject = %s
        ORDER BY resolved.id DESC
        LIMIT 1
        """,
        (queue, subject_id),
    )
    return None if row is None else _job_status(row)


def find_gmail_source(conn: psycopg.Connection[Any], *, user_id: str, name: str) -> str | None:
    """The user's Gmail source with this name, if one exists.

    Name rather than address, because the address is not on the row: it is a key in
    ``sync_state`` that only a poll writes. Matching on it is what makes re-seeding
    idempotent — the second call replaces the grant on the source the first one made
    rather than accumulating a mailbox per run, which is the failure a repeatable loop
    would otherwise produce once a day forever.
    """
    row = _maybe_one(
        conn,
        """
        SELECT id FROM sources
        WHERE user_id = %s AND kind = 'gmail' AND name = %s
        ORDER BY created_at
        LIMIT 1
        """,
        (user_id, name),
    )
    return None if row is None else str(row["id"])


def lock_seed(conn: psycopg.Connection[Any], user_id: str) -> None:
    """Serialize this user's seeds for the caller's transaction.

    The seed is a check-then-insert and ``sources`` carries no unique index on
    ``(user_id, kind, name)``, so without this two concurrent seeds — a client retrying a
    request that timed out, most realistically — each read no source and each create one.
    Two mailboxes of the same name is not a cosmetic duplicate: the trigger route then has
    no single source to poll and the unattended loop stops.
    """
    with conn.cursor() as cur:
        cur.execute(
            "SELECT pg_advisory_xact_lock(%s, hashtext(%s))", (SEED_LOCK_NAMESPACE, user_id)
        )


@dataclass(frozen=True)
class PollableSource:
    """A Gmail source that holds a credential, and whether a poll would actually read it."""

    id: str
    name: str
    #: False once a refused refresh paused it. The credential is **kept** in that state, so
    #: "has a credential" and "would read the mailbox" are different questions.
    active: bool
    last_error: str | None


def pollable_gmail_sources(conn: psycopg.Connection[Any], *, user_id: str) -> list[PollableSource]:
    """Every Gmail source of this user's holding a refresh credential, oldest first.

    Holding a credential is what the Sources screen means by *connected* — a row left by an
    abandoned consent has none, and offering it would enqueue a job that fails on a missing
    token. ``active`` is the second question and the caller has to ask it: ``ingest`` pauses
    a source on a permanently refused refresh **without deleting the credential**, and
    ``handle_poll`` then short-circuits on a paused source and returns *normally*. A caller
    that read "has a credential" as "would sync" would therefore be told a poll finished
    against a mailbox nothing opened — a false green, on the surface built to remove exactly
    that ambiguity.
    """
    rows = _all(
        conn,
        """
        SELECT s.id, s.name, s.active, s.last_error
        FROM sources s
        WHERE s.user_id = %s
          AND s.kind = 'gmail'
          AND EXISTS (
              SELECT 1 FROM source_credentials c
              WHERE c.source_id = s.id AND c.purpose = 'refresh'
          )
        ORDER BY s.created_at, s.id
        """,
        (user_id,),
    )
    return [
        PollableSource(
            id=str(row["id"]),
            name=str(row["name"]),
            active=bool(row["active"]),
            last_error=row["last_error"],
        )
        for row in rows
    ]


def _job_status(row: Mapping[str, Any]) -> JobStatus:
    return JobStatus(
        id=row["id"],
        queue=row["queue"],
        state=row["state"],
        attempts=row["attempts"],
        user_id=row["user_id"],
        subject=row["subject"],
        last_error=row["last_error"],
        run_at=row["run_at"],
        created_at=row["created_at"],
        updated_at=row["updated_at"],
        locked_at=row["locked_at"],
        now=row["now"],
        queue_last_seen_at=row["queue_last_seen_at"],
    )
