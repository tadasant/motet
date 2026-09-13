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

import base64
import hashlib
import hmac
import re
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
    #: The MCP client this session was issued to as an access token (motet#111), or
    #: ``None`` for a signed-in browser and the staging mint.
    mcp_client_id: str | None = None


def token_digest(token: str) -> str:
    """The stored form of a session token: hex SHA-256, and nothing reversible."""
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


#: What :func:`token_digest` emits, and therefore the only thing ``token_sha256`` may hold.
#:
#: A pattern rather than a docstring because one caller — :mod:`motet_db.mint_session` —
#: takes the digest from the outside world instead of computing it, and a digest with a
#: stray newline or an uppercase nibble would insert a row that simply never matches a
#: lookup. That failure has no symptom at mint time and looks like a broken token later.
DIGEST_RE: re.Pattern[str] = re.compile(r"\A[0-9a-f]{64}\Z")


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
    mcp_client_id: str | None = None,
) -> AuthSession:
    """Record a session for a token the caller has already minted.

    The token arrives as a parameter rather than being generated here so that the caller
    is the only thing that ever holds the plaintext — it returns it to the browser and
    forgets it, and this module never had a copy to log.
    """
    return create_session_for_digest(
        conn,
        user_id=user_id,
        email=email,
        token_sha256=token_digest(token),
        ttl_seconds=ttl_seconds,
        mcp_client_id=mcp_client_id,
    )


def create_session_for_digest(
    conn: psycopg.Connection[Any],
    *,
    user_id: str,
    email: str,
    token_sha256: str,
    ttl_seconds: int = DEFAULT_TTL_SECONDS,
    mcp_client_id: str | None = None,
) -> AuthSession:
    """Record a session from its digest alone, for a caller that never held the plaintext.

    :func:`create_session` is this function plus one ``sha256``. The split exists for the
    staging session mint (:mod:`motet_db.mint_session`), where the plaintext is generated
    in the deploy workflow's shell, encrypted to the requesting agent, and never crosses a
    process boundary: the job that writes this row is handed the digest as an argument and
    has nothing to leak, log, or accidentally record in an execution's override list.

    The row is otherwise identical to a signed-in browser's, which is the point — a minted
    session is resolved by the same :func:`session_for_token`, revoked by the same
    ``/v1/auth/logout``, and expires by the same predicate.

    **This function does not check the allowlist, and no writer of this table may skip
    it.** ``motet_api``'s sign-in route and :func:`motet_db.mint_session.check_email` each
    ask :mod:`motet_db.allowlist` before calling in here. A third caller that forgot would
    quietly create a session for an address Google sign-in refuses — which is the property
    AGENTS.md claims, so add the check rather than relying on the per-request re-check in
    ``require_caller`` to delete the row on first use.
    """
    if not DIGEST_RE.match(token_sha256):
        raise ValueError("token_sha256 must be a 64-character lowercase hex SHA-256 digest")
    row = _one(
        conn,
        """
        INSERT INTO auth_sessions (id, user_id, token_sha256, email, expires_at, mcp_client_id)
        VALUES (%s, %s, %s, %s, now() + make_interval(secs => %s), %s)
        RETURNING id, user_id, email, created_at, last_seen_at, expires_at, mcp_client_id
        """,
        (new_id("sess"), user_id, token_sha256, email, ttl_seconds, mcp_client_id),
    )
    _backfill_user_email(conn, user_id=user_id, email=email)
    return _session(row)


def _backfill_user_email(conn: psycopg.Connection[Any], *, user_id: str, email: str) -> None:
    """Give the user row the address this session just proved, if it has none — motet#98.

    Migration 0002 seeds ``('motet-owner', NULL)``, because the account predates there
    being any way to learn an address: the shared API token proves nothing about who is
    holding it. Sign-in is the moment one *is* known, and until this ran the Admin users
    table showed a row identified by its id and nothing else, on the one screen whose job
    is to say who is who.

    **Only ever fills a NULL**, and never rewrites an address — the row is the account,
    not this session. A second person on the allowlist signing into the same account would
    otherwise flip the label back and forth between two sessions of equal standing, and
    which one the screen showed would be "whoever signed in last". The cost of that choice
    is that whichever allowlisted address signs in *first* is the label for good, and with
    no database shell (invariant 10) nothing corrects it; on a one-account deployment that
    is the owner. In staging the first writer may well be the CI session mint rather than a
    browser, so there the label is whichever allowlisted address that mint was handed.

    Here rather than in the sign-in route so that the staging mint
    (:mod:`motet_db.mint_session`) writes it too: both are a caller proving an allowlisted
    address, and both already come through this function. In the same transaction as the
    session row for the same reason — a session that exists is exactly the evidence this
    is written from.
    """
    conn.execute(
        "UPDATE users SET email = %s WHERE id = %s AND email IS NULL",
        (email, user_id),
    )


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
        SELECT id, user_id, email, created_at, last_seen_at, expires_at, mcp_client_id
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
    """Revoke one session. True when a row went away.

    When the session is an MCP client's access token, the refresh token issued beside it goes
    too (motet#111): otherwise `/v1/auth/logout` on that token would revoke it for as long as
    it took the client to mint the next one.
    """
    conn.execute("DELETE FROM mcp_oauth_refresh_tokens WHERE session_id = %s", (session_id,))
    return bool(conn.execute("DELETE FROM auth_sessions WHERE id = %s", (session_id,)).rowcount)


def delete_sessions_for_user(conn: psycopg.Connection[Any], user_id: str) -> int:
    """Revoke every session for a user — the answer to a stolen laptop.

    An MCP client's refresh tokens go too (motet#111): a refresh token mints the next
    session, so revoking the sessions and leaving it would revoke nothing for longer than
    an access token's hour. Counted as sessions only, which is what the route reports.
    """
    conn.execute("DELETE FROM mcp_oauth_refresh_tokens WHERE user_id = %s", (user_id,))
    return conn.execute("DELETE FROM auth_sessions WHERE user_id = %s", (user_id,)).rowcount


def purge_expired_sessions(conn: psycopg.Connection[Any]) -> int:
    """Sweep lapsed rows. Housekeeping only: expiry is already enforced on read."""
    return conn.execute("DELETE FROM auth_sessions WHERE expires_at <= now()").rowcount


# --- handing a sign-in back to the iOS app (migration 0019) ----------------------------

#: How long a verified sign-in waits for the app that started it.
#:
#: Short, because the only thing between the callback and the redeem is the in-app browser
#: navigating to a `motet://` link and the app making one request. Anything longer is a
#: window in which a code read off that link is worth trying.
HANDOFF_TTL_SECONDS: int = 120


@dataclass(frozen=True)
class AuthHandoff:
    """A verified, allowlisted sign-in, waiting to be collected by the app that started it."""

    user_id: str
    email: str
    code_challenge: str


def pkce_challenge(verifier: str) -> str:
    """RFC 7636's S256 transform: base64url(SHA-256(verifier)), unpadded."""
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    return base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")


def verifier_matches(verifier: str, challenge: str) -> bool:
    """Whether a verifier is the one a stored challenge was made from. Constant-time."""
    return hmac.compare_digest(pkce_challenge(verifier), challenge)


def create_handoff(
    conn: psycopg.Connection[Any],
    *,
    user_id: str,
    email: str,
    code: str,
    code_challenge: str,
    ttl_seconds: int = HANDOFF_TTL_SECONDS,
) -> None:
    """Record a sign-in for the app to redeem. Only the code's hash is kept.

    **Like :func:`create_session_for_digest`, this does not check the allowlist.** The
    callback that calls it already has, and :func:`take_handoff`'s caller checks it again
    before minting anything, so an address removed in the two minutes between gets nothing.
    """
    conn.execute(
        """
        INSERT INTO auth_handoffs (code_sha256, user_id, email, code_challenge, expires_at)
        VALUES (%s, %s, %s, %s, now() + make_interval(secs => %s))
        """,
        (token_digest(code), user_id, email, code_challenge, ttl_seconds),
    )


def take_handoff(conn: psycopg.Connection[Any], code: str) -> AuthHandoff | None:
    """Consume a handoff, exactly once — the same ``DELETE ... RETURNING`` as a state.

    Exactly once *per committed transaction*: a caller that raises after this rolls the delete
    back, which is what lets a refused redeem leave the code for the app holding the verifier.
    """
    if not code:
        return None
    row = _maybe_one(
        conn,
        """
        DELETE FROM auth_handoffs
        WHERE code_sha256 = %s AND expires_at > now()
        RETURNING user_id, email, code_challenge
        """,
        (token_digest(code),),
    )
    if row is None:
        return None
    return AuthHandoff(
        user_id=row["user_id"], email=row["email"], code_challenge=row["code_challenge"]
    )


def purge_expired_handoffs(conn: psycopg.Connection[Any]) -> int:
    """Sweep handoffs nobody collected — the app was closed mid-sign-in."""
    return conn.execute("DELETE FROM auth_handoffs WHERE expires_at <= now()").rowcount


def _session(row: dict[str, Any]) -> AuthSession:
    return AuthSession(
        id=row["id"],
        user_id=row["user_id"],
        email=row["email"],
        created_at=row["created_at"],
        last_seen_at=row["last_seen_at"],
        expires_at=row["expires_at"],
        mcp_client_id=row.get("mcp_client_id"),
    )
