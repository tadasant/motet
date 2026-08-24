"""Pick the identity implementation for this process.

Reads the **same variable** as every other seam — ``MOTET_INFERENCE_MODE``, through
``motet_inference.mode.current_mode``. Google is a vendor, and "may this process talk to a
vendor" is one question with one answer; AGENTS.md records the specific failure a second
parser causes, which is that two readings can disagree silently.

**The login flow's state is minted here**, because it belongs to the flow rather than to a
provider — the same argument ``motet_sources.registry`` makes for PKCE, which this reuses
rather than duplicating.
"""

from __future__ import annotations

import os
import secrets
from collections.abc import Mapping
from typing import Final

from motet_inference.mode import current_mode

from .fakes import FakeIdentityProvider
from .google import (
    DEFAULT_TIMEOUT_SECONDS,
    TIMEOUT_ENV,
    GoogleIdentityProvider,
    oauth_client_config,
)
from .interfaces import IdentityProvider

#: Marks a `state` value as belonging to a sign-in rather than to connecting a mailbox.
#:
#: Both flows land on the SPA's one `/oauth/callback` path, and `state` is the only thing
#: guaranteed to survive the round trip through the provider — so the flow is encoded in
#: it. A dot is the discriminator because `secrets.token_urlsafe` emits only
#: `[A-Za-z0-9_-]`: a mailbox state can never accidentally look like a sign-in one.
LOGIN_STATE_PREFIX: Final = "login."


def build_identity_provider(env: Mapping[str, str] | None = None) -> IdentityProvider:
    """The identity provider for this process. Fake unless real mode is set explicitly.

    In fake mode the Google client id and secret are never read, which is what lets the
    whole sign-in path run — and be tested — offline.
    """
    environ = dict(os.environ) if env is None else dict(env)
    if current_mode(environ) != "real":
        return FakeIdentityProvider()
    client_id, client_secret = oauth_client_config(environ)
    return GoogleIdentityProvider(
        client_id=client_id, client_secret=client_secret, timeout_seconds=_timeout(environ)
    )


def new_login_state() -> str:
    """The CSRF token for one sign-in, tagged as a sign-in.

    Sized as a secret because it is one: it names a pending authorization, and anything
    that can guess it can try to complete somebody else's.
    """
    return f"{LOGIN_STATE_PREFIX}{secrets.token_urlsafe(32)}"


def is_login_state(state: str) -> bool:
    """Whether a callback's ``state`` belongs to a sign-in."""
    return state.startswith(LOGIN_STATE_PREFIX)


def new_nonce() -> str:
    """OpenID Connect's replay defence, stored with the state and checked in the token."""
    return secrets.token_urlsafe(32)


def _timeout(environ: Mapping[str, str]) -> float:
    raw = environ.get(TIMEOUT_ENV, "").strip()
    if not raw:
        return DEFAULT_TIMEOUT_SECONDS
    try:
        seconds = float(raw)
    except ValueError:
        raise ValueError(f"{TIMEOUT_ENV}={raw!r} is not a number") from None
    if seconds <= 0:
        raise ValueError(f"{TIMEOUT_ENV}={raw!r} must be positive")
    return seconds
