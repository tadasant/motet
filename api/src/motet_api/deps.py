"""Request-scoped dependencies: a database connection, a store, a drain nudge, and who is
asking.

Authentication paths, deliberately different, because they serve different clients:

* **``/v1`` takes a bearer token, and there are two kinds.** The configured
  ``MOTET_API_TOKEN`` is the shared secret the RSS tooling, the iOS app and any script
  hold — unchanged, and it keeps working. A **session token** is what a browser gets by
  signing in with Google, so that a human stops typing the shared secret into a form.
  Both arrive in the same header and mean the same thing, because there is still exactly
  one account: this is a lock on the door, not an identity system.
* **The feed and the audio it links to take a token in the query string.** That is not a
  weaker choice made for convenience: podcast clients handle a secret in a URL far better
  than they handle HTTP auth, and a feed nobody's player can subscribe to is not a feed.
  The token resolves to a user through the database, so revoking it is a row update.

The shared-secret comparison is constant-time. A token compared with ``==`` leaks its
prefix to anyone patient enough to measure, and this one is the only thing standing
between the internet and a bill. A session token is looked up by SHA-256 instead, which
is a full-length index probe and gives a timing attack nothing partial to work with.
"""

from __future__ import annotations

import logging
import secrets
import threading
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import datetime
from typing import Annotated, Any, Literal

import psycopg
from fastapi import Depends, Header, HTTPException, Query, status
from motet_db import auth as auth_repo
from motet_db import repo
from motet_storage import ObjectStore, build_store
from motet_vault import DekWrapper, VaultConfigError, build_dek_wrapper

from .auth import ADMIN_EMAILS_ENV, ALLOWED_EMAILS_ENV, is_allowed
from .config import Settings
from .drain import DrainNudge, DrainTrigger, build_trigger

logger = logging.getLogger("motet.api")

_store: ObjectStore | None = None
_trigger: DrainTrigger | None = None
_trigger_lock = threading.Lock()


def settings() -> Settings:
    return Settings.from_env()


def drain_trigger() -> DrainTrigger:
    """One trigger per process, resolved from the environment on first use.

    Cached like ``store`` and for the same reasons: it holds an HTTP client and an ambient
    credential that is refreshed roughly hourly, and rebuilding it per request would hit
    the metadata server on every paste.
    """
    global _trigger
    trigger = _trigger
    if trigger is None:
        # Locked because sync routes run in a threadpool: two first requests arriving
        # together would otherwise each build a trigger and leak one HTTP client.
        with _trigger_lock:
            if _trigger is None:
                _trigger = build_trigger()
            trigger = _trigger
    return trigger


def reset_drain_trigger() -> None:
    """Drop the cached trigger. For tests that change the environment between cases."""
    global _trigger
    _trigger = None


def drain_nudge(trigger: Annotated[DrainTrigger, Depends(drain_trigger)]) -> DrainNudge:
    """This request's intent to nudge the worker, armed by a route and fired by commit.

    FastAPI caches a dependency per request, so the object a route arms is the object
    ``connection`` fires — which is the whole mechanism, and the reason this is a
    dependency rather than something hung off ``Request.state``.
    """
    return DrainNudge(trigger)


def store() -> ObjectStore:
    """One object store per process. Built lazily so importing the app needs no cloud."""
    global _store
    if _store is None:
        _store = build_store()
    return _store


def reset_store() -> None:
    """Drop the cached store. For tests that switch backends between cases."""
    global _store
    _store = None


def dek_wrapper() -> DekWrapper:
    """The encrypt-only half of the credential vault.

    **The API can seal a token and cannot open one** (invariant 8). The OAuth callback is
    an HTTP redirect, so this is where a third-party token first arrives and therefore
    where it must be sealed — but sealing is all this process may ever do.

    Two things enforce that, and only one of them is code. The type has no ``unwrap``, so
    a route that wanted plaintext would have to change a signature and be seen in review.
    The real control is IAM: the deployed API's service account holds
    ``cloudkms...useToEncrypt`` and not ``useToDecrypt``, so the same call from here fails
    inside Cloud KMS regardless of what this process believes it may do.

    Built per request rather than cached: it holds no connection, and the KMS client
    underneath it is created lazily on first use.

    **A misconfigured vault is a 503, not a 500**, and it is translated here rather than
    in the route because a dependency is resolved *before* the route body — so the
    ``except VaultError`` around the seal cannot see this one. Same shape as
    ``connection`` refusing without a ``DATABASE_URL``: nothing is wrong with the request,
    the capability is not configured. The message is the vault's own, which names the
    variable to set.
    """
    try:
        return build_dek_wrapper()
    except VaultConfigError as exc:
        logger.error("the credential vault is not usable: %s", exc)
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=f"The credential vault is not configured, so nothing can be stored: {exc}",
        ) from exc


def connection(
    config: Annotated[Settings, Depends(settings)],
    nudge: Annotated[DrainNudge, Depends(drain_nudge)],
) -> Iterator[psycopg.Connection[Any]]:
    """A connection per request, committed on success and rolled back on failure.

    A connection per request rather than a pool because Phase 1 has one user and Cloud Run
    already bounds concurrency; a pool here would be tuning for load that does not exist.
    The transaction boundary is the *request*, so a route that writes two rows either
    writes both or neither.

    **It is also where a drain gets nudged**, which is why the transaction and the trigger
    meet here rather than in a route. A route that enqueued and then asked Cloud Run to
    drain would be asking on behalf of a job row no other process can see yet, and would
    still be asking on behalf of a request that goes on to fail — the rollback path below
    re-raises, so the fire never happens. Firing here, after the commit, makes "there is
    work" and "start a worker" the same event.

    Deliberately **not** a background task, which is where a best-effort call belongs on
    most runtimes. Cloud Run throttles a container's CPU between requests unless the
    service asks otherwise, so a task scheduled after the response is a task that may not
    run until the next request arrives — and a nudge that fires unpredictably is worse
    than no nudge, because the scheduled sweep is the thing it would be silently relying
    on. The cost is the invoke's latency inside the request, bounded by
    :data:`~motet_api.drain.DEFAULT_TIMEOUT_SECONDS`.

    **"Inside the request" is only true because every ``Depends(connection)`` says
    ``scope="function"``.** FastAPI's default scope for a ``yield`` dependency is
    ``"request"``, whose teardown runs *after* the response has been sent — so before this
    the commit itself ran post-response, a failed commit was a 201 the client had already
    received, and the nudge would have landed in exactly the throttled window the paragraph
    above rules out. ``"function"`` tears down when the route returns, before the response
    starts. All three sites must agree: the scope is part of FastAPI's dependency cache
    key, so a mismatch would open two connections per request.
    ``api/tests/test_drain.py`` pins the order against a raw ASGI ``send``.
    """
    if not config.database_url:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="DATABASE_URL is not configured, so this API cannot serve data.",
        )
    conn = repo.connect(config.database_url)
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()
    # Reached only when the transaction committed: every other path above re-raises, and
    # a raise here would skip this line rather than nudge for work that was rolled back.
    # `fire` swallows everything it can go wrong with, so this cannot fail the request.
    nudge.fire()


@dataclass(frozen=True)
class Caller:
    """Who made this ``/v1`` request, and how they proved it.

    ``user_id`` is always the one account. ``how`` exists so the SPA can render "signed in
    as …" and offer a logout that actually revokes something, and so an operator reading
    ``/v1/auth/session`` can tell a browser session from the shared secret from a
    deployment with no lock on it at all.
    """

    user_id: str
    how: Literal["token", "session", "open"]
    #: The Google account on the session, when the caller signed in. Never set for the
    #: shared token, which belongs to no person.
    email: str | None = None
    session_id: str | None = None
    #: When this browser has to sign in again. ``None`` for the shared token, which does
    #: not expire — rotating it is a deploy.
    expires_at: datetime | None = None


def require_caller(
    config: Annotated[Settings, Depends(settings)],
    conn: Annotated[psycopg.Connection[Any], Depends(connection, scope="function")],
    authorization: Annotated[str | None, Header()] = None,
) -> Caller:
    """Authorize a ``/v1`` request, by shared token or by browser session.

    The shared token is tried first and compared in constant time; a session lookup only
    happens for a bearer value that is not it. That ordering is what keeps the path every
    non-browser client takes — the feed tooling, the iOS app, any script — off the
    database entirely.

    When no token is configured the API is open, and says so in the log on every request
    rather than only at startup — a warning nobody sees after the first minute of uptime
    is a warning that does not exist.
    """
    presented = ""
    if authorization and authorization.lower().startswith("bearer "):
        presented = authorization[7:].strip()

    if config.api_token is None:
        logger.warning(
            "serving an unauthenticated request: %s is unset, so anyone who can reach this "
            "API can ingest text and spend inference budget",
            "MOTET_API_TOKEN",
        )
        return Caller(user_id=repo.OWNER_USER_ID, how="open")

    # `isascii` before the comparison: Starlette decodes headers as latin-1, and
    # `compare_digest` raises TypeError on a str with a codepoint above 127 — so
    # `Authorization: Bearer é` would be a 500 from an unauthenticated request rather
    # than the 401 it is. A token this process generated is always URL-safe ASCII.
    if presented and presented.isascii() and secrets.compare_digest(presented, config.api_token):
        return Caller(user_id=repo.OWNER_USER_ID, how="token")

    session = auth_repo.session_for_token(conn, presented) if presented else None
    if session is not None:
        # **The allowlist is re-checked on every request, not only at sign-in.** Otherwise
        # taking an address off `MOTET_ALLOWED_EMAILS` would revoke nothing for up to the
        # session's whole 30-day life, and there is no other lever: `/v1/auth/logout`
        # needs the very token you are trying to revoke, and invariant 10 says nobody has
        # a shell to run a DELETE from. De-listed has to mean gone, so the row goes.
        if not is_allowed(session.email, config.allowed_emails):
            logger.warning(
                "revoking a session for %s: no longer on %s",
                session.email,
                ALLOWED_EMAILS_ENV,
            )
            auth_repo.delete_session(conn, session.id)
            # Committed here, not left to the request. `connection` rolls back on the
            # exception this is about to raise, which would undo the delete and leave the
            # row to linger until its expiry — refused on every request, but present, and
            # quietly contradicting everything this comment and the migration claim.
            conn.commit()
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="This session is no longer allowed. Sign in again.",
                headers={"WWW-Authenticate": "Bearer"},
            )
        return Caller(
            user_id=session.user_id,
            how="session",
            email=session.email,
            session_id=session.id,
            expires_at=session.expires_at,
        )

    raise HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="A valid bearer token is required. Sign in, or set the API token.",
        headers={"WWW-Authenticate": "Bearer"},
    )


def require_api_token(caller: Annotated[Caller, Depends(require_caller)]) -> str:
    """The user a ``/v1`` request belongs to — which is the only user there is.

    Kept as its own dependency so that every route that only needs "who owns this row"
    says exactly that, and so adding a second way to authenticate did not mean touching
    twenty route signatures.
    """
    return caller.user_id


def is_admin(caller: Caller, config: Settings) -> bool:
    """Whether this caller may read the operator view — every user's data at once.

    **Only a signed-in person, and only one on ``MOTET_ADMIN_EMAILS``.** Three callers are
    refused however the list is set, and each for a reason rather than by accident:

    * **The shared API token.** It belongs to no person — the feed tooling, the iOS app
      and any script hold it — so there is no address to compare, and "unset means nobody
      is an admin" could not be literally true if a credential with no name on it passed.
    * **An open deployment** (``MOTET_API_TOKEN`` unset). Nobody has proved anything.
    * **A session on an empty list.** :func:`~motet_db.allowlist.is_allowed` fails closed.

    The sign-in allowlist is not re-checked here because it does not need to be:
    :func:`require_caller` has already revoked any session whose address left it, so every
    session that reaches this line is on both lists. The one predicate serves the guard and
    ``/v1/auth/session``'s ``admin`` flag, so the SPA can never offer a screen the API
    would refuse.
    """
    if caller.how != "session" or caller.email is None:
        return False
    return is_allowed(caller.email, config.admin_emails)


def require_admin(
    caller: Annotated[Caller, Depends(require_caller)],
    config: Annotated[Settings, Depends(settings)],
) -> Caller:
    """Refuse anyone who is not an operator. Mounted on the whole ``/v1/admin`` router.

    A 403 rather than a 401: the caller *is* authenticated — asking again with the same
    credential will not help, and a 401 would make the SPA drop a perfectly good session.
    An unauthenticated caller never gets this far; :func:`require_caller` answers 401 first.

    The detail says which of the two refusals this is, because "the deployment has no
    operators" and "you are not one of them" are fixed in different places — and this is
    only ever shown to somebody already on the sign-in allowlist.
    """
    if is_admin(caller, config):
        return caller
    if not config.admin_emails:
        detail = f"{ADMIN_EMAILS_ENV} is unset on this deployment, so nobody is an admin."
    elif caller.how != "session":
        detail = "The admin view needs a signed-in session; the shared API token is not one."
    else:
        detail = f"This account is not on {ADMIN_EMAILS_ENV}."
    logger.warning(
        "refused an admin request from %s: %s", caller.email or f"<{caller.how}>", detail
    )
    raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=detail)


def require_feed_token(
    conn: Annotated[psycopg.Connection[Any], Depends(connection, scope="function")],
    token: Annotated[str, Query(description="The feed's secret, from GET /v1/feed.")] = "",
) -> str:
    """Resolve a feed token to its owner, or refuse.

    Looked up rather than compared against configuration, so that rotating a leaked feed
    URL is a database write and takes effect on the next request — no redeploy, and no
    coordination with whatever else holds the API token.
    """
    user_id = repo.user_for_feed_token(conn, token.strip())
    if user_id is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="This feed link is not valid. Get the current one from the app.",
        )
    return user_id


def public_base_url(config: Settings, request_base_url: str) -> str:
    """The origin to build absolute feed and enclosure URLs from.

    Configured value first, then the request's own origin. Both exist because both fail in
    different places: an RSS enclosure must be absolute, so deriving it from the request
    is what makes the feed work on a laptop with no configuration — and a proxy that
    rewrites the Host header is why a deployed environment gets to state the answer
    outright instead of inferring it.
    """
    return (config.public_base_url or request_base_url).rstrip("/")
