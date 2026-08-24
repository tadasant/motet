"""The identity seam, as a Protocol — the same shape as every other vendor seam here.

Google is a vendor, so nothing in this repo calls it directly. A caller asks
:mod:`motet_api.auth.registry` for an :class:`IdentityProvider` and gets either the real
adapter or a deterministic fake, decided by ``MOTET_INFERENCE_MODE`` — the one variable,
parsed in the one place (``motet_inference.mode``). Invariant 7 is why: a test that
completed a real consent flow would be slow, non-deterministic, and impossible to run in
CI, which is offline.

**This is not the Gmail seam, even though it is the same OAuth client.** Sign-in and
mailbox access are two different authorizations against one registered client, and they
want different requests: consent for a mailbox asks for offline access and forces the
consent screen so that a refresh token is issued, while signing in wants neither — it
needs no refresh token at all, and re-prompting on every sign-in is friction for nothing.
Reusing ``motet_sources``' ``OAuthClient`` would have meant one class quietly serving two
sets of parameters.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol, runtime_checkable


class IdentityError(RuntimeError):
    """Sign-in could not be completed. Never carries a token or a code."""


class IdentityConfigError(IdentityError):
    """Sign-in is selected but not configured well enough to attempt.

    Distinct from :class:`IdentityError` because the answer is different: this is a
    deployment that is missing a variable — a 503 — rather than a request that failed.
    """


@dataclass(frozen=True)
class VerifiedIdentity:
    """Who just signed in, according to an ID token that has already been verified.

    **Only ever constructed after verification.** ``email`` here is a claim out of a token
    whose signature, audience, issuer, expiry, nonce, and ``email_verified`` flag have all
    been checked; a provider that could return one without doing that would make the
    allowlist meaningless, because the allowlist compares against this field.

    It is still not an identity the system resolves anything by: Motet has one account,
    and this is the record of which Google account was standing at the door.
    """

    email: str
    subject: str
    #: The provider's display name, when it sent one. Cosmetic; nothing depends on it.
    name: str | None = None


@runtime_checkable
class IdentityProvider(Protocol):
    """Start a sign-in, and turn its callback into a verified identity."""

    def authorization_url(
        self, *, redirect_uri: str, state: str, nonce: str, code_challenge: str
    ) -> str:
        """Where to send the browser to ask the human who they are."""
        ...

    def complete(
        self, *, code: str, redirect_uri: str, code_verifier: str, nonce: str
    ) -> VerifiedIdentity:
        """Exchange the callback's code and verify the ID token it comes back with.

        Raises :class:`IdentityError` for anything that is not a verified, email-verified
        identity — never returns a partially checked one for a caller to finish judging.
        """
        ...
