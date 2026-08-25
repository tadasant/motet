"""Google Sign-In for the SPA — a nicer key to the same lock.

**This is not a user system, and adding one is still Phase 3.** Motet has exactly one
account. What this package changes is how a *browser* proves it may talk to ``/v1``: a
Google sign-in that mints a session, instead of ``MOTET_API_TOKEN`` pasted into a text
field on a phone during a dog walk. The bearer token keeps working — the RSS feed, the iOS
app and any script use it — it simply stops being something a human types.

Three pieces, and the middle one is the security control:

* :mod:`~motet_api.auth.interfaces`, :mod:`~motet_api.auth.google`,
  :mod:`~motet_api.auth.fakes`, :mod:`~motet_api.auth.registry` — the vendor seam,
  switched by ``MOTET_INFERENCE_MODE`` like every other.
* :mod:`motet_db.allowlist` — **who may sign in.** The consent screen on this
  OAuth client is published and unverified, so completing a Google sign-in proves only
  that somebody has a Google account. Without the allowlist this would be a strictly
  worse door than the shared token it replaces. Unset means deny. It is re-exported here,
  where the sign-in route reads it, but it *lives* one package down — see that module for
  why the staging session mint has to be able to ask the same question.
* :mod:`motet_db.auth` — the session rows a signed-in browser holds.
"""

from __future__ import annotations

from motet_db.allowlist import ALLOWED_EMAILS_ENV, allowed_emails, is_allowed

from .fakes import FAKE_EMAIL, FakeIdentityProvider
from .google import (
    CLIENT_ID_ENV,
    CLIENT_SECRET_ENV,
    LOGIN_SCOPES,
    PROVIDER,
    GoogleIdentityProvider,
)
from .interfaces import (
    IdentityConfigError,
    IdentityError,
    IdentityProvider,
    IdentityUnavailableError,
    VerifiedIdentity,
)
from .registry import (
    LOGIN_STATE_PREFIX,
    build_identity_provider,
    is_login_state,
    new_login_state,
    new_nonce,
)

__all__ = [
    "ALLOWED_EMAILS_ENV",
    "CLIENT_ID_ENV",
    "CLIENT_SECRET_ENV",
    "FAKE_EMAIL",
    "LOGIN_SCOPES",
    "LOGIN_STATE_PREFIX",
    "PROVIDER",
    "FakeIdentityProvider",
    "GoogleIdentityProvider",
    "IdentityConfigError",
    "IdentityError",
    "IdentityProvider",
    "IdentityUnavailableError",
    "VerifiedIdentity",
    "allowed_emails",
    "build_identity_provider",
    "is_allowed",
    "is_login_state",
    "new_login_state",
    "new_nonce",
]
