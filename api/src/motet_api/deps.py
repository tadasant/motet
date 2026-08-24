"""Request-scoped dependencies: a database connection, a store, and who is asking.

Two authentication paths, deliberately different, because they serve two different
clients:

* **``/v1`` takes a bearer token.** The SPA holds it. It is one shared token for one
  hardcoded account — Phase 1 cuts signup and OAuth entirely — so this is a lock on the
  door rather than an identity system.
* **The feed and the audio it links to take a token in the query string.** That is not a
  weaker choice made for convenience: podcast clients handle a secret in a URL far better
  than they handle HTTP auth, and a feed nobody's player can subscribe to is not a feed.
  The token resolves to a user through the database, so revoking it is a row update.

The bearer token is compared in constant time — with ``==`` it would leak its prefix to
anyone patient enough to measure, and it is the only thing standing between the internet
and a bill. The feed token is *not*: it is looked up with a SQL ``=``, whose timing is the
database's business rather than ours. Said plainly rather than papered over, because
closing it would mean loading every token and comparing in Python — trading a timing
channel nobody can practically exploit for a table scan on every feed poll.
"""

from __future__ import annotations

import logging
import secrets
from collections.abc import Iterator
from typing import Annotated, Any

import psycopg
from fastapi import Depends, Header, HTTPException, Query, status
from motet_db import repo
from motet_storage import ObjectStore, build_store

from .config import Settings

logger = logging.getLogger("motet.api")

_store: ObjectStore | None = None


def settings() -> Settings:
    return Settings.from_env()


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


def connection(
    config: Annotated[Settings, Depends(settings)],
) -> Iterator[psycopg.Connection[Any]]:
    """A connection per request, committed on success and rolled back on failure.

    A connection per request rather than a pool because Phase 1 has one user and Cloud Run
    already bounds concurrency; a pool here would be tuning for load that does not exist.
    The transaction boundary is the *request*, so a route that writes two rows either
    writes both or neither.
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


def require_api_token(
    config: Annotated[Settings, Depends(settings)],
    authorization: Annotated[str | None, Header()] = None,
) -> str:
    """Authorize a ``/v1`` request and return the user it belongs to.

    When no token is configured the API is open, and says so in the log on every request
    rather than only at startup — a warning nobody sees after the first minute of uptime
    is a warning that does not exist.
    """
    if config.api_token is None:
        logger.warning(
            "serving an unauthenticated request: %s is unset, so anyone who can reach this "
            "API can ingest text and spend inference budget",
            "MOTET_API_TOKEN",
        )
        return repo.OWNER_USER_ID

    presented = ""
    if authorization and authorization.lower().startswith("bearer "):
        presented = authorization[7:].strip()
    if not presented or not secrets.compare_digest(presented, config.api_token):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="A valid bearer token is required.",
            headers={"WWW-Authenticate": "Bearer"},
        )
    return repo.OWNER_USER_ID


def require_feed_token(
    conn: Annotated[psycopg.Connection[Any], Depends(connection)],
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
