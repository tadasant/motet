"""Request-scoped dependencies: a database connection, a store, a drain nudge, and who is
asking.

Authentication paths, deliberately different, because they serve different clients:

* **``/v1`` takes a bearer token, and there are three kinds.** The configured
  ``MOTET_API_TOKEN`` is the shared secret the RSS tooling, the iOS app and any script
  hold — unchanged, and it keeps working. A **session token** is what a browser gets by
  signing in with Google, so that a human stops typing the shared secret into a form. A
  **personal access token** is the non-interactive third: minted from a session, hashed
  at rest, revocable, and the credential an agent driving staging holds. All three arrive
  in the same header and mean the same thing, because there is still exactly one account:
  this is a lock on the door, not an identity system.
* **The feed and the audio it links to take a token in the query string.** That is not a
  weaker choice made for convenience: podcast clients handle a secret in a URL far better
  than they handle HTTP auth, and a feed nobody's player can subscribe to is not a feed.
  The token resolves to a user through the database, so revoking it is a row update.

The shared-secret comparison is constant-time. A token compared with ``==`` leaks its
prefix to anyone patient enough to measure, and this one is the only thing standing
between the internet and a bill. A session token and a personal access token are looked
up by SHA-256 instead, which is a full-length index probe and gives a timing attack
nothing partial to work with — and the PAT's digest is compared again with
``hmac.compare_digest`` after the probe, so that property is local to
:mod:`motet_db.api_tokens` rather than a claim about Postgres.

**Every local on this path that holds a credential is named ``token`` or ``secret``, and
that is load-bearing rather than taste.** ``sentry_sdk`` captures frame locals into an
error report and its default scrubber redacts by *variable name*; both of those names are
on its denylist and ``presented``, which this function used to call it, is not. So an
unhandled exception anywhere on the auth path redacts the bearer instead of shipping it to
GlitchTip. ``api/tests/test_api_tokens.py`` pins the names against the installed SDK's own
list, because a rename would break it silently.
"""

from __future__ import annotations

import logging
import os
import secrets
import threading
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import datetime
from typing import Annotated, Any, Literal

import psycopg
from fastapi import Depends, Header, HTTPException, Query, status
from motet_db import api_tokens as token_repo
from motet_db import auth as auth_repo
from motet_db import repo
from motet_storage import ObjectStore, build_store
from motet_vault import DekWrapper, VaultConfigError, build_dek_wrapper

from .auth import ADMIN_EMAILS_ENV, ALLOWED_EMAILS_ENV, is_allowed
from .config import Settings
from .drain import DrainNudge, DrainTrigger, build_trigger
from .slack import WEBHOOK_ENV, SlackAlerter, WaitlistAlert, build_alerter, deployment_label
from .throttle import auth_failures, failed_auth

logger = logging.getLogger("motet.api")

_store: ObjectStore | None = None
_trigger: DrainTrigger | None = None
_trigger_lock = threading.Lock()
_alerter: SlackAlerter | None = None
_alerter_lock = threading.Lock()


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


def slack_alerter() -> SlackAlerter:
    """One alerter per process, resolved from the environment on first use.

    Cached like ``store`` and ``drain_trigger`` and for the same reason: it holds an HTTP
    client, and rebuilding it per request would open a connection pool per signup.
    """
    global _alerter
    alerter = _alerter
    if alerter is None:
        # Locked for `drain_trigger`'s reason: sync routes run in a threadpool, so two
        # first requests arriving together would each build one and leak a client.
        with _alerter_lock:
            if _alerter is None:
                _alerter = build_alerter(os.environ.get(WEBHOOK_ENV))
            alerter = _alerter
    return alerter


def reset_slack_alerter() -> None:
    """Drop the cached alerter. For tests that change the environment between cases."""
    global _alerter
    _alerter = None


def waitlist_alert(
    alerter: Annotated[SlackAlerter, Depends(slack_alerter)],
    config: Annotated[Settings, Depends(settings)],
) -> WaitlistAlert:
    """This request's intent to announce a signup, armed by the route and fired by commit.

    The drain nudge's shape exactly — a per-request object so that the thing the route
    arms is the thing ``connection`` fires. The environment label is resolved here rather
    than in the alerter because ``Settings`` is already in hand, and because a label
    resolved per process would be wrong in exactly one situation nobody would notice: a
    test that changes the environment between cases.
    """
    return WaitlistAlert(alerter, environment=deployment_label(config.public_base_url))


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
    alert: Annotated[WaitlistAlert, Depends(waitlist_alert)],
) -> Iterator[psycopg.Connection[Any]]:
    """A connection per request, committed on success and rolled back on failure.

    A connection per request rather than a pool because Phase 1 has one user and Cloud Run
    already bounds concurrency; a pool here would be tuning for load that does not exist.
    The transaction boundary is the *request*, so a route that writes two rows either
    writes both or neither.

    **It is also where a drain gets nudged and a waitlist signup gets announced**, which is
    why the transaction and those two best-effort calls meet here rather than in a route.
    A route that enqueued and then asked Cloud Run to drain would be asking on behalf of a
    job row no other process can see yet, and would still be asking on behalf of a request
    that goes on to fail — the rollback path below re-raises, so the fire never happens.
    Firing here, after the commit, makes "there is work" and "start a worker" the same
    event, and makes "an address is on the list" and "Slack was told" the same event too.

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
    # a raise here would skip these lines rather than nudge for work, or announce a row,
    # that was rolled back. Both `fire`s swallow everything they can go wrong with, so
    # neither can fail the request.
    nudge.fire()
    alert.fire()


@dataclass(frozen=True)
class Caller:
    """Who made this ``/v1`` request, and how they proved it.

    ``user_id`` is always the one account. ``how`` exists so the SPA can render "signed in
    as …" and offer a logout that actually revokes something, and so an operator reading
    ``/v1/auth/session`` can tell a browser session from a personal access token from the
    shared secret from a deployment with no lock on it at all.
    """

    user_id: str
    how: Literal["token", "session", "pat", "open"]
    #: The Google account on the session, when the caller signed in — or, for a personal
    #: access token, the address of the session that minted it. Never set for the shared
    #: token, which belongs to no person.
    email: str | None = None
    session_id: str | None = None
    #: When this credential stops working: a browser session's expiry, or a personal
    #: access token's if it was given one. ``None`` for a token minted without an expiry
    #: and for the shared token, which does not expire — rotating it is a deploy.
    expires_at: datetime | None = None
    #: The MCP client this session was issued to, when it is an MCP client's OAuth access
    #: token rather than a browser's (motet#111). Such a grant acts as the person who approved
    #: it, and is never an operator: see :func:`is_admin`.
    mcp_client_id: str | None = None


def require_caller(
    config: Annotated[Settings, Depends(settings)],
    conn: Annotated[psycopg.Connection[Any], Depends(connection, scope="function")],
    authorization: Annotated[str | None, Header()] = None,
) -> Caller:
    """Authorize a ``/v1`` request: shared token, browser session, or personal access token.

    The shared token is tried first and compared in constant time, which keeps the path
    every non-browser client takes — the feed tooling, the iOS app, any script — off the
    database entirely. A bearer that is not it is looked up in **one** table on the way to
    succeeding: the ``mot_`` marker routes it to ``api_tokens`` and anything else to
    ``auth_sessions``, so a PAT never costs a session probe and a session never costs a
    token probe. Only a request on its way to a refusal pays for both — see the note at
    the marker, which is a routing hint rather than a commitment.

    **A PAT resolves to the same user and the same checks as the session that minted it,
    and to nothing more.** It is not an operator (:func:`is_admin` requires a session, for
    the reason an MCP grant is refused there), and it cannot mint or revoke another token
    — the three routes that manage tokens require a session. What it *can* do is
    everything else the owner can, which is the point: an agent driving staging needs the
    product, not a subset of it.

    When no token is configured the API is open, and says so in the log on every request
    rather than only at startup — a warning nobody sees after the first minute of uptime
    is a warning that does not exist.
    """
    token = ""
    if authorization and authorization.lower().startswith("bearer "):
        token = authorization[7:].strip()

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
    if token and token.isascii() and secrets.compare_digest(token, config.api_token):
        return Caller(user_id=repo.OWNER_USER_ID, how="token")

    # The marker routes the probe; it does not commit to an answer. A bearer carrying it
    # is almost always a PAT, so this is the lookup that saves the session table a probe
    # on every agent request — but a session token is 43 random url-safe characters and
    # one in 16.7 million of them begins `mot_`, and committing to the answer would refuse
    # that person until they signed in again. So an unrecognised PAT falls through to the
    # session lookup, which costs a second probe only on a request that was being refused
    # anyway.
    if token and token_repo.looks_like_api_token(token):
        caller = _caller_for_api_token(conn, config, token)
        if caller is not None:
            return caller

    session = auth_repo.session_for_token(conn, token) if token else None
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
            raise _refused(
                "This session is no longer allowed. Sign in again.",
                outcome="session_delisted",
                counted=True,
            )
        return Caller(
            user_id=session.user_id,
            how="session",
            email=session.email,
            session_id=session.id,
            expires_at=session.expires_at,
            mcp_client_id=session.mcp_client_id,
        )

    if token and token_repo.looks_like_api_token(token):
        # It was shaped like one of ours and matched nothing in either table. Say so, so
        # that "I pasted the wrong thing" and "that token has been revoked" are not the
        # same sentence — the detail still names no state of any particular row.
        raise _refused(
            "This access token is not valid. It may have been revoked or have expired.",
            outcome="unknown_token",
            counted=True,
        )
    raise _refused(
        "A valid bearer token is required. Sign in, or set the API token.",
        outcome="unknown_bearer",
        # A request with no bearer at all made no lookup, so there is nothing to bound
        # and a scanner cannot spend the budget a real stale session needs.
        counted=bool(token),
    )


def _caller_for_api_token(
    conn: psycopg.Connection[Any],
    config: Settings,
    token: str,
) -> Caller | None:
    """Resolve a bearer that is shaped like a PAT. ``None`` means "not one of these".

    ``None`` rather than a refusal, so that :func:`require_caller` can go on to the
    session table — see the note at its call site. It still *raises* for a token that
    resolved and is no longer allowed, because that one is an answer rather than a miss.

    Unknown, revoked and expired all come back as ``None`` on purpose: the refusal the
    caller eventually raises says which *kind* of credential was refused and never which
    state a particular row is in, because "that token exists but is revoked" is a fact
    worth nothing to its owner (who has the list route) and worth something to anyone else.
    """
    found = token_repo.token_for_secret(conn, token)
    if found is None:
        return None
    # The same re-check a session gets, and it has to be: a PAT outlives the browser
    # session that minted it, so without this, taking an address off the allowlist would
    # revoke every *session* that person holds and leave their long-lived tokens working.
    # Revoked rather than deleted, so the list still shows what happened and when.
    if not is_allowed(found.email, config.allowed_emails):
        logger.warning(
            "revoking access token %s for %s: no longer on %s",
            found.id,
            found.email,
            ALLOWED_EMAILS_ENV,
        )
        token_repo.revoke_token(conn, user_id=found.user_id, token_id=found.id)
        # Committed here for `require_caller`'s reason one branch up: the raise below
        # rolls the request back, and a revocation that vanished with it would be a
        # warning logged on every request about a row that is still live.
        conn.commit()
        raise _refused(
            "This access token is no longer allowed and has been revoked.",
            outcome="token_delisted",
            counted=True,
        )
    return Caller(
        user_id=found.user_id,
        how="pat",
        email=found.email,
        expires_at=found.expires_at,
    )


def _refused(detail: str, *, outcome: str, counted: bool) -> HTTPException:
    """The 401 for a bearer that did not resolve, counted and rate-limited.

    **The throttle is consulted here and nowhere else**, which is the property that makes
    it safe: a caller holding a valid credential never reaches this function, so no amount
    of hammering can refuse one. ``counted`` is the second half of that — a request that
    presented no credential at all cost no database probe, so it is reported and not
    counted, which is what keeps an unauthenticated probe (an MCP client's discovery, a
    browser before sign-in) answering 401 rather than 429. See :mod:`motet_api.throttle`.

    Returned rather than raised so that every call site reads ``raise _refused(...)`` and
    a reviewer can see the control flow leaves at each one.

    It takes no ``Request``, and that is worth a line because the first draft did: the
    throttle was keyed on the caller's address, and when that key turned out to be
    attacker-supplied the key went and the parameter stayed. A ``Request`` threaded down
    here is not free — ``/mcp`` would hand one across a thread boundary on every call for
    a value nobody reads.
    """
    if counted and failed_auth.record_failure():
        auth_failures.add(1, {"outcome": "throttled"})
        return HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail="Too many failed attempts. Wait a minute and try again.",
            headers={"Retry-After": str(failed_auth.retry_after_seconds())},
        )
    auth_failures.add(1, {"outcome": outcome})
    return HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail=detail,
        headers={"WWW-Authenticate": "Bearer"},
    )


def require_api_token(caller: Annotated[Caller, Depends(require_caller)]) -> str:
    """The user a ``/v1`` request belongs to — which is the only user there is.

    Kept as its own dependency so that every route that only needs "who owns this row"
    says exactly that, and so adding a second way to authenticate did not mean touching
    twenty route signatures.
    """
    return caller.user_id


def is_browser_session(caller: Caller) -> bool:
    """Whether this caller is a person signed in to a browser, and nothing else.

    **One predicate, because two spellings of it drift.** Both :func:`require_session` and
    :func:`is_admin` need exactly this question and need the same answer to it, and the
    interesting half is the second clause: an MCP client's access token *is* an
    ``auth_sessions`` row (motet#111), so ``how`` reads ``"session"`` for it and a check on
    that alone admits a delegated grant to both.

    The address is part of the question rather than an extra guard on top of it. A session
    always carries one — the column is ``NOT NULL`` and every writer of the table checks
    the allowlist first — so this narrows nothing today; what it does is let the callers
    rely on ``caller.email`` being a string instead of asserting it, and an ``assert`` is
    what ``python -O`` removes.
    """
    return caller.how == "session" and caller.mcp_client_id is None and caller.email is not None


def require_session(caller: Annotated[Caller, Depends(require_caller)]) -> Caller:
    """Refuse anyone who is not a signed-in person. The guard on the token routes.

    **Minting, listing and revoking a personal access token all require a session**, and
    the three credentials refused here are refused for different reasons:

    * **A personal access token cannot mint another one.** A credential that can issue
      its own successors makes revocation unbounded — revoke the one you know about and
      it has already produced three you do not — and there is no surface anywhere that
      could show you the tree. A token is a leaf, always.
    * **The shared ``MOTET_API_TOKEN`` cannot mint one either.** Rotating that secret is a
      deploy, and it is the recovery for it having leaked; a token minted from it would
      survive that rotation and quietly make the recovery incomplete.
    * **An MCP client's grant cannot, and this is the one that is easy to miss.** An MCP
      access token *is* an ``auth_sessions`` row (motet#111), so ``how`` is ``"session"``
      for it and a plain check on that alone would admit it. What the person approved on
      the consent screen was an agent acting as them for an hour, revocable by deleting
      the client registration; minting a PAT from it would produce a credential that
      outlives the grant, the revocation and the registration together.
      :func:`is_admin` refuses it for the same reason and asks the *same function* —
      :func:`is_browser_session` — so the two cannot drift apart.

    That leaves a signed-in browser, or the staging deploy's own
    ``motet_db.mint_session`` — which is how an agent bootstraps one without a human at a
    consent screen, and therefore how the agent this feature is for gets its first token.
    A 403 rather than a 401 for the same reason :func:`require_admin` answers 403: the
    caller is authenticated, and asking again with the same credential will not help.
    """
    if is_browser_session(caller):
        return caller
    if caller.mcp_client_id is not None:
        detail = (
            "Managing access tokens needs a signed-in browser session; an MCP client's "
            "grant is not one."
        )
    else:
        detail = {
            "pat": "An access token cannot manage access tokens. Sign in, and manage them there.",
            "token": (
                "Managing access tokens needs a signed-in session; the shared API token is not one."
            ),
            "open": (
                "Managing access tokens needs a signed-in session, and this deployment has "
                "no sign-in lock (MOTET_API_TOKEN is unset)."
            ),
        }.get(caller.how, "Managing access tokens needs a signed-in browser session.")
    logger.warning("refused a token-management request from <%s>: %s", caller.how, detail)
    raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=detail)


def is_admin(caller: Caller, config: Settings) -> bool:
    """Whether this caller may read the operator view — every user's data at once.

    **Only a signed-in person, and only one on ``MOTET_ADMIN_EMAILS``.** Four callers are
    refused however the list is set, and each for a reason rather than by accident:

    * **The shared API token.** It belongs to no person — the feed tooling, the iOS app
      and any script hold it — so there is no address to compare, and "unset means nobody
      is an admin" could not be literally true if a credential with no name on it passed.
    * **An open deployment** (``MOTET_API_TOKEN`` unset). Nobody has proved anything.
    * **A personal access token**, which *does* carry an address — the one that minted it
      — and is still refused, for the reason an MCP grant below is. It is a credential
      pasted into an agent's environment and read back out of it, and the operator view is
      the one route that returns every user's data at once. The owner reads it in a
      browser they are signed in to; nothing an agent does needs it. If that ever stops
      being true, it is a deliberate widening and a line in AGENTS.md, not a default.
    * **A session on an empty list.** :func:`~motet_db.allowlist.is_allowed` fails closed.

    The sign-in allowlist is not re-checked here because it does not need to be:
    :func:`require_caller` has already revoked any session whose address left it, so every
    session that reaches this line is on both lists. The one predicate serves the guard and
    ``/v1/auth/session``'s ``admin`` flag, so the SPA can never offer a screen the API
    would refuse.
    """
    # **An MCP client's grant is never an operator**, whoever approved it (motet#111). The
    # consent screen asks a person to let an agent act as them in their own account; reading
    # every user's data is not what they were asked about, and a delegated token is exactly
    # the credential that should not carry it. That, and the refusal of a PAT and of the
    # shared token, are all one question — :func:`is_browser_session` — so that this guard
    # and the token routes' cannot answer it differently.
    # `caller.email is not None` is part of `is_browser_session`; repeating it here is
    # what narrows the type without an `assert`, which `python -O` would remove.
    return (
        is_browser_session(caller)
        and caller.email is not None
        and is_allowed(caller.email, config.admin_emails)
    )


def require_admin(
    caller: Annotated[Caller, Depends(require_caller)],
    config: Annotated[Settings, Depends(settings)],
) -> Caller:
    """Refuse anyone who is not an operator. Every ``/v1/admin`` route takes it via ``Admin``.

    A 403 rather than a 401: the caller *is* authenticated — asking again with the same
    credential will not help, and a 401 would make the SPA drop a perfectly good session.
    An unauthenticated caller never gets this far; :func:`require_caller` answers 401 first.

    The detail says which refusal this is, because "the deployment has no operators" and
    "you are not one of them" are fixed in different places. It is shown to any caller
    that got past :func:`require_caller` — which on an open deployment is anybody — so it
    names only a public variable and whether it is set, never who is on it.
    """
    if is_admin(caller, config):
        return caller
    if not config.admin_emails:
        detail = f"{ADMIN_EMAILS_ENV} is unset on this deployment, so nobody is an admin."
    elif caller.how == "open":
        detail = (
            "The admin view needs a signed-in session, and this deployment has no sign-in "
            "lock (MOTET_API_TOKEN is unset), so nobody is an admin."
        )
    elif caller.mcp_client_id is not None:
        detail = (
            "The admin view needs a signed-in browser session; an MCP client's grant is not one."
        )
    elif caller.how == "pat":
        detail = (
            "The admin view needs a signed-in browser session; a personal access token is not one."
        )
    elif caller.how == "token":
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
