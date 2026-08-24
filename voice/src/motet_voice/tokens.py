"""Session tokens: signed, stateless, and short-lived.

**Cloud Run has no sticky sessions and no shared memory**, and this service is deliberately
allowed to scale to more than one instance. So there is no session table — there could not
be one anyway, because invariant 3 says this service has no database. The token carries the
session: an id, an expiry, and a digest of the config it was minted for, all under an HMAC.

That last part is what makes it more than an opaque string. The WebSocket handshake arrives
on an instance that has never seen the ``StartSession`` request, and it carries the config
again. Binding a digest of the config into the signature means the socket cannot be opened
with *a* valid token and *different* settings — a tool list it was not granted, say.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
from collections.abc import Callable
from dataclasses import dataclass
from time import time
from typing import Any, Final

TOKEN_VERSION: Final = "v1"


class SessionTokenError(ValueError):
    """A token was absent, malformed, expired, or not ours."""


@dataclass(frozen=True)
class SessionClaims:
    """What a verified token asserts."""

    session_id: str
    expires_at: int
    config_digest: str


def config_digest(payload: Any) -> str:
    """A stable digest of a session config.

    ``sort_keys`` and a compact separator make the digest depend on the *content* rather
    than on how a JSON encoder happened to lay it out, so a client that re-serializes the
    config before opening the socket still matches.
    """
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def mint(
    *,
    session_id: str,
    secret: str,
    ttl_seconds: int,
    digest: str,
    now: Callable[[], float] = time,
) -> tuple[str, int]:
    """Return ``(token, expires_at)``."""
    expires_at = int(now()) + ttl_seconds
    body = f"{TOKEN_VERSION}.{session_id}.{expires_at}.{digest}"
    return f"{body}.{_sign(body, secret)}", expires_at


def verify(
    token: str,
    *,
    secret: str,
    digest: str | None = None,
    now: Callable[[], float] = time,
) -> SessionClaims:
    """Check a token and return its claims, or raise :class:`SessionTokenError`.

    ``digest`` is optional so that a caller which only needs "is this token ours and still
    alive" — a health probe, a log line — does not have to reconstruct the config. Every
    path that acts on the session passes it.
    """
    parts = token.split(".")
    if len(parts) != 5 or parts[0] != TOKEN_VERSION:
        raise SessionTokenError("malformed session token")
    _, session_id, raw_expiry, token_digest, signature = parts

    body = ".".join(parts[:4])
    if not hmac.compare_digest(signature, _sign(body, secret)):
        # Constant-time, and deliberately checked before the expiry: comparing an expiry
        # first would answer "is this token shape valid" for a forged token.
        raise SessionTokenError("session token signature does not verify")

    try:
        expires_at = int(raw_expiry)
    except ValueError as exc:  # pragma: no cover — unreachable behind a valid signature
        raise SessionTokenError("session token has a malformed expiry") from exc

    if expires_at <= int(now()):
        raise SessionTokenError("session token has expired")
    if digest is not None and not hmac.compare_digest(token_digest, digest):
        raise SessionTokenError("session token was minted for a different session config")

    return SessionClaims(session_id=session_id, expires_at=expires_at, config_digest=token_digest)


def _sign(body: str, secret: str) -> str:
    mac = hmac.new(secret.encode("utf-8"), body.encode("utf-8"), hashlib.sha256).digest()
    return base64.urlsafe_b64encode(mac).decode("ascii").rstrip("=")
