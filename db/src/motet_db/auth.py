"""Browser sessions: what a signed-in tab holds instead of a hand-typed API token.

**Still one account.** Every session in this table belongs to ``users.motet-owner``, the
row migration 0002 seeds, and nothing here can create a user. Signup and multi-tenancy are
Phase 3; this module exists so that *proving you may talk to* ``/v1`` stops meaning
"paste the shared secret into a form".

Two properties are the whole reason these are rows rather than a signed token:

* **Logout revokes.** A self-contained token stays valid until it expires however loudly
  the client throws it away, and the only place a revocation list could live is a table
  like this one — at which point the signing was buying nothing.
* **There is no key to provision.** The token is opaque and random, so a deployment needs
  no session signing secret, no rotation procedure, and has nothing to leak.

**The token is never stored.** Only its SHA-256 is, because — unlike the feed token, which
the owner must be able to read back onto a new device — nothing ever needs this value again
after the browser that will present it has been handed it. Lookup is by hash for the same
reason the feed token's comparison is constant-time: the column is an index, and an index
probe on a full-length hash leaks nothing a timing measurement can use.
"""

from __future__ import annotations

import hashlib
import secrets
from dataclasses import dataclass
from datetime import datetime
from typing import Any

import psycopg

from .ids import new_id
from .repo import _maybe_one, _one

#: How long a signed-in browser stays signed in.
#:
#: Long, deliberately. The point of this whole path is that the owner stops typing a
#: secret into a browser on a dog walk, and a session that expired every hour would just
#: move the friction rather than remove it. It is bounded rather than infinite because an
#: abandoned laptop should eventually stop being a way in, and revocation is one row
#: delete away for the cases that cannot wait.
DEFAULT_TTL_SECONDS: int = 30 * 24 * 60 * 60


@dataclass(frozen=True)
class AuthSession:
    """One signed-in browser."""

    id: str
    user_id: str
    #: The Google account that signed in. A record of *who*, never how anything is
    #: resolved — every session points at the same single user.
    email: str
    created_at: datetime
    last_seen_at: datetime
    expires_at: datetime


def token_digest(token: str) -> str:
    """The stored form of a session token: hex SHA-256, and nothing reversible."""
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def new_session_token() -> str:
    """A bearer secret handed to a browser, so it is sized as one.

    32 bytes, URL-safe — the same shape as the feed token, and for the same reason: it is
    the only thing between the internet and an API that spends money.
    """
    return secrets.token_urlsafe(32)


def create_session(
    conn: psycopg.Connection[Any],
    *,
    user_id: str,
    email: str,
    token: str,
    ttl_seconds: int = DEFAULT_TTL_SECONDS,
) -> AuthSession:
    """Record a session for a token the caller has already minted.

    The token arrives as a parameter rather than being generated here so that the caller
    is the only thing that ever holds the plaintext — it returns it to the browser and
    forgets it, and this module never had a copy to log.
    """
    row = _one(
        conn,
        """
        INSERT INTO auth_sessions (id, user_id, token_sha256, email, expires_at)
        VALUES (%s, %s, %s, %s, now() + make_interval(secs => %s))
        RETURNING id, user_id, email, created_at, last_seen_at, expires_at
        """,
        (new_id("sess"), user_id, token_digest(token), email, ttl_seconds),
    )
    return _session(row)


#: How stale ``last_seen_at`` may get before a request bothers to write it.
#:
#: See :func:`session_for_token`: the point of the coarseness is that the *common* request
#: takes no row lock at all.
TOUCH_INTERVAL_SECONDS: int = 5 * 60


def session_for_token(conn: psycopg.Connection[Any], token: str) -> AuthSession | None:
    """Resolve a presented token to its session, touching ``last_seen_at`` occasionally.

    Expired rows are filtered in the predicate rather than swept first: a session that
    lapsed a second ago must stop working immediately, and waiting for a cleanup job to
    notice would make expiry advisory.

    **A `SELECT`, and only sometimes an `UPDATE`** — which matters more than it looks.
    Writing `last_seen_at` on every call takes an exclusive lock on the session row and
    holds it until the request commits, so two concurrent requests from one signed-in
    browser serialize: the second waits out the whole of the first. The SPA fires several
    calls at once on boot and pasting text is the slow one, so that is a UI that stalls
    behind itself, on a path no test with one shared API token would ever exercise.

    So the touch happens at most every :data:`TOUCH_INTERVAL_SECONDS`. It only ever
    answers "is anything still using this session", and five minutes of resolution is
    plenty for that. The expiry is deliberately **not** extended by it either: a sliding
    window on a 30-day session is an unbounded one for anything in daily use.
    """
    if not token:
        return None
    row = _maybe_one(
        conn,
        """
        SELECT id, user_id, email, created_at, last_seen_at, expires_at
        FROM auth_sessions
        WHERE token_sha256 = %s AND expires_at > now()
        """,
        (token_digest(token),),
    )
    if row is None:
        return None
    session = _session(row)
    conn.execute(
        """
        UPDATE auth_sessions SET last_seen_at = now()
        WHERE id = %s AND last_seen_at < now() - make_interval(secs => %s)
        """,
        (session.id, TOUCH_INTERVAL_SECONDS),
    )
    return session


def delete_session(conn: psycopg.Connection[Any], session_id: str) -> bool:
    """Revoke one session. True when a row went away."""
    return bool(conn.execute("DELETE FROM auth_sessions WHERE id = %s", (session_id,)).rowcount)


def delete_sessions_for_user(conn: psycopg.Connection[Any], user_id: str) -> int:
    """Revoke every session for a user — the answer to a stolen laptop."""
    return conn.execute("DELETE FROM auth_sessions WHERE user_id = %s", (user_id,)).rowcount


def purge_expired_sessions(conn: psycopg.Connection[Any]) -> int:
    """Sweep lapsed rows. Housekeeping only: expiry is already enforced on read."""
    return conn.execute("DELETE FROM auth_sessions WHERE expires_at <= now()").rowcount


def _session(row: dict[str, Any]) -> AuthSession:
    return AuthSession(
        id=row["id"],
        user_id=row["user_id"],
        email=row["email"],
        created_at=row["created_at"],
        last_seen_at=row["last_seen_at"],
        expires_at=row["expires_at"],
    )
