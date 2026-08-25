"""Who may sign in — and the reason this file is the security control, not the login.

**The consent screen for Motet's Google OAuth client is published, and unverified.** That
means anybody on the internet with a Google account can walk through it to the end. A
"Sign in with Google" button with no allowlist behind it would therefore be *worse* than
the shared bearer token it replaces: it would turn a secret into an open door, while
looking like an upgrade.

So the allowlist is the lock. Google establishes *who* somebody is; this decides whether
that person is allowed in, server-side, after the ID token has been verified and before a
session is minted.

**Unset means deny everything.** Not "allow everything", not "allow the first person to
arrive". A deployment that forgot the variable must be a deployment nobody can sign in to,
because the alternative failure is silent and unbounded. ``/internal/health`` reports
``login_configured: false`` for exactly that state, in the same spirit as ``authenticated``.

**Why this lives in ``motet_db`` rather than next to the sign-in route it guards.** Two
things now decide whether an address may hold a session: the Google sign-in route in
``motet_api``, and :mod:`motet_db.mint_session`, the staging deploy's job entrypoint. A
second copy of these nine lines is the failure to avoid — an allowlist that admits
somebody through one door and refuses them at the other is worse than either door alone,
and it would drift silently because nothing compares the two. ``motet-api`` depends on
``motet-db`` and never the reverse, so the shared definition has to sit here; the API
re-exports it from :mod:`motet_api.auth`, which is still where a reader of the sign-in
route will look for it.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Final

#: Comma-separated addresses. Not a secret — it is a list of people, not a credential —
#: but it is personal data, so like every other deployment value it is set by the private
#: infrastructure repo and never written down in this one.
ALLOWED_EMAILS_ENV: Final = "MOTET_ALLOWED_EMAILS"


def allowed_emails(env: Mapping[str, str]) -> frozenset[str]:
    """The configured addresses, lowercased. Empty means nobody may sign in.

    Lowercased on both sides because the domain part of an address is case-insensitive and
    no provider in practice treats the local part otherwise — a deployment that wrote
    ``Tadas@example.com`` should not be a deployment where sign-in silently never works.

    Deliberately **no** provider-specific normalisation: Gmail ignores dots and everything
    after a ``+``, and folding those here would silently *widen* the allowlist to addresses
    the operator did not write down. An address is allowed if it was listed.
    """
    raw = env.get(ALLOWED_EMAILS_ENV, "")
    return frozenset(part.strip().lower() for part in raw.split(",") if part.strip())


def is_allowed(email: str, allowed: frozenset[str]) -> bool:
    """Whether a verified address is on the list. Fails closed on an empty list."""
    if not allowed:
        return False
    return email.strip().lower() in allowed
