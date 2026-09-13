"""The rows behind Motet's OAuth authorization server for MCP clients (motet#111).

Motet as the *issuer*. ``motet_sources.mcp_oauth`` (motet#102) is the other direction:
Motet as a client of a remote MCP server, whose state lives on ``connectors``.

**Per-user auth for ``/mcp`` was Tadas's pick** ("C2", 2026-09-13): an MCP client does not
hold ``MOTET_API_TOKEN``, it registers itself, sends its user through a Google sign-in, and
gets a grant of its own. What that grant *is* is the design, and it is deliberately small:

* **The access token is an ``auth_sessions`` row** (:func:`motet_db.auth.create_session`,
  with ``mcp_client_id`` set and a one-hour life). So the one function that decides who may
  call this API — ``motet_api.deps.require_caller`` — verifies it without knowing MCP
  exists, re-checks the allowlist on every request, and revokes it the same way.
* **The refresh token is a row here**, and it is what makes an hour-long access token
  bearable. Rotated on every use: the old pair is deleted before the new pair is written.
* **Codes and refresh tokens are stored as SHA-256**, like session tokens, because nothing
  needs the value again after it is handed to the client.

Still one account. Every row belongs to ``motet-owner``; ``email`` records which allowlisted
person approved the client, and is what the allowlist is re-checked against.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any

import psycopg
from psycopg.types.json import Jsonb

from .auth import token_digest
from .repo import _maybe_one

#: How long an authorization code may wait to be redeemed. RFC 6749 recommends ten minutes
#: at most; a client redeems one within a second of receiving it.
CODE_TTL_SECONDS: int = 5 * 60

#: How long an MCP access token (a session row) lives. Short, because the refresh token is
#: what a client keeps, and a leaked access token should stop working soon on its own.
ACCESS_TOKEN_TTL_SECONDS: int = 60 * 60

#: How long a refresh token lives unused. The same thirty days a signed-in browser gets:
#: an agent connected once should not have to send its person back through Google weekly.
REFRESH_TOKEN_TTL_SECONDS: int = 30 * 24 * 60 * 60

#: How long a registration that never led to a grant is kept. Registration is
#: unauthenticated, so this is what bounds the table.
UNUSED_CLIENT_TTL_SECONDS: int = 24 * 60 * 60


@dataclass(frozen=True)
class StoredCode:
    client_id: str
    user_id: str
    email: str
    scopes: list[str]
    code_challenge: str
    redirect_uri: str
    redirect_uri_provided_explicitly: bool
    resource: str | None
    expires_at: datetime


@dataclass(frozen=True)
class StoredRefreshToken:
    client_id: str
    user_id: str
    email: str
    scopes: list[str]
    resource: str | None
    session_id: str | None
    expires_at: datetime


# --- clients ---------------------------------------------------------------------------


def register_client(
    conn: psycopg.Connection[Any], *, client_id: str, client_info: dict[str, Any]
) -> None:
    """Record a dynamic client registration. It grants nothing on its own."""
    conn.execute(
        "INSERT INTO mcp_oauth_clients (client_id, client_info) VALUES (%s, %s)",
        (client_id, Jsonb(client_info)),
    )


def get_client(conn: psycopg.Connection[Any], client_id: str) -> dict[str, Any] | None:
    row = _maybe_one(
        conn, "SELECT client_info FROM mcp_oauth_clients WHERE client_id = %s", (client_id,)
    )
    return None if row is None else dict(row["client_info"])


def purge_unused_clients(conn: psycopg.Connection[Any]) -> int:
    """Sweep registrations older than a day that hold no live code, token or session."""
    purge_expired(conn)
    return conn.execute(
        """
        DELETE FROM mcp_oauth_clients c
        WHERE c.created_at < now() - make_interval(secs => %s)
          AND NOT EXISTS (SELECT 1 FROM mcp_oauth_codes k WHERE k.client_id = c.client_id)
          AND NOT EXISTS (
                SELECT 1 FROM mcp_oauth_refresh_tokens r WHERE r.client_id = c.client_id)
          AND NOT EXISTS (
                SELECT 1 FROM auth_sessions s
                WHERE s.mcp_client_id = c.client_id AND s.expires_at > now())
        """,
        (UNUSED_CLIENT_TTL_SECONDS,),
    ).rowcount


def purge_expired(conn: psycopg.Connection[Any]) -> None:
    """Sweep lapsed codes and refresh tokens. Expiry is already enforced on read."""
    conn.execute("DELETE FROM mcp_oauth_codes WHERE expires_at <= now()")
    conn.execute("DELETE FROM mcp_oauth_refresh_tokens WHERE expires_at <= now()")


# --- authorization codes -----------------------------------------------------------------


def create_code(
    conn: psycopg.Connection[Any],
    *,
    code: str,
    client_id: str,
    user_id: str,
    email: str,
    scopes: list[str],
    code_challenge: str,
    redirect_uri: str,
    redirect_uri_provided_explicitly: bool,
    resource: str | None,
    ttl_seconds: int = CODE_TTL_SECONDS,
) -> None:
    conn.execute(
        """
        INSERT INTO mcp_oauth_codes
            (code_sha256, client_id, user_id, email, scopes, code_challenge, redirect_uri,
             redirect_uri_provided_explicitly, resource, expires_at)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, now() + make_interval(secs => %s))
        """,
        (
            token_digest(code),
            client_id,
            user_id,
            email,
            scopes,
            code_challenge,
            redirect_uri,
            redirect_uri_provided_explicitly,
            resource,
            ttl_seconds,
        ),
    )


_CODE_COLUMNS = """client_id, user_id, email, scopes, code_challenge, redirect_uri,
    redirect_uri_provided_explicitly, resource, expires_at"""


def load_code(conn: psycopg.Connection[Any], code: str, client_id: str) -> StoredCode | None:
    """A live code issued to this client, without spending it."""
    row = _maybe_one(
        conn,
        f"""
        SELECT {_CODE_COLUMNS} FROM mcp_oauth_codes
        WHERE code_sha256 = %s AND client_id = %s AND expires_at > now()
        """,
        (token_digest(code), client_id),
    )
    return None if row is None else StoredCode(**row)


def consume_code(conn: psycopg.Connection[Any], code: str, client_id: str) -> StoredCode | None:
    """Spend a code, exactly once. ``DELETE ... RETURNING``, so a replay finds nothing."""
    row = _maybe_one(
        conn,
        f"""
        DELETE FROM mcp_oauth_codes
        WHERE code_sha256 = %s AND client_id = %s AND expires_at > now()
        RETURNING {_CODE_COLUMNS}
        """,
        (token_digest(code), client_id),
    )
    return None if row is None else StoredCode(**row)


# --- refresh tokens ------------------------------------------------------------------------


def create_refresh_token(
    conn: psycopg.Connection[Any],
    *,
    token: str,
    client_id: str,
    user_id: str,
    email: str,
    scopes: list[str],
    resource: str | None,
    session_id: str | None,
    ttl_seconds: int = REFRESH_TOKEN_TTL_SECONDS,
) -> None:
    conn.execute(
        """
        INSERT INTO mcp_oauth_refresh_tokens
            (token_sha256, client_id, user_id, email, scopes, resource, session_id, expires_at)
        VALUES (%s, %s, %s, %s, %s, %s, %s, now() + make_interval(secs => %s))
        """,
        (
            token_digest(token),
            client_id,
            user_id,
            email,
            scopes,
            resource,
            session_id,
            ttl_seconds,
        ),
    )


_REFRESH_COLUMNS = "client_id, user_id, email, scopes, resource, session_id, expires_at"


def load_refresh_token(
    conn: psycopg.Connection[Any], token: str, client_id: str | None = None
) -> StoredRefreshToken | None:
    """A live refresh token, optionally only if it was issued to ``client_id``."""
    row = _maybe_one(
        conn,
        f"""
        SELECT {_REFRESH_COLUMNS} FROM mcp_oauth_refresh_tokens
        WHERE token_sha256 = %s AND expires_at > now()
          AND (%s::text IS NULL OR client_id = %s)
        """,
        (token_digest(token), client_id, client_id),
    )
    return None if row is None else StoredRefreshToken(**row)


def consume_refresh_token(
    conn: psycopg.Connection[Any], token: str, client_id: str | None = None
) -> StoredRefreshToken | None:
    """Spend a refresh token, exactly once, taking the access token issued beside it.

    Rotation deletes the old pair before a new one is written, so a refresh token that was
    copied can be used by the copy or the original, never both.
    """
    row = _maybe_one(
        conn,
        f"""
        DELETE FROM mcp_oauth_refresh_tokens
        WHERE token_sha256 = %s AND expires_at > now()
          AND (%s::text IS NULL OR client_id = %s)
        RETURNING {_REFRESH_COLUMNS}
        """,
        (token_digest(token), client_id, client_id),
    )
    if row is None:
        return None
    stored = StoredRefreshToken(**row)
    if stored.session_id is not None:
        conn.execute("DELETE FROM auth_sessions WHERE id = %s", (stored.session_id,))
    return stored


def revoke_session_grant(conn: psycopg.Connection[Any], session_id: str) -> None:
    """Revoke an MCP access token and the refresh token issued beside it."""
    conn.execute("DELETE FROM mcp_oauth_refresh_tokens WHERE session_id = %s", (session_id,))
    conn.execute("DELETE FROM auth_sessions WHERE id = %s", (session_id,))
