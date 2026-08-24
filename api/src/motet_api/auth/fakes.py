"""Signing in without Google, a browser, or a network.

The same contract as every other fake in this tree: an honest implementation of the
interface with a trivial rule standing in for the vendor. This is what CI runs, and it is
what a laptop runs, because ``MOTET_INFERENCE_MODE`` defaults to ``fake``.

**The fake authorizes nobody by itself.** It produces a verified identity for a fixed
address, and that address still has to be on ``MOTET_ALLOWED_EMAILS`` before a session is
minted — the allowlist is checked in the route, outside the seam, precisely so that no
provider (real or fake) can be the thing that decides who gets in. A developer who wants
to sign in locally puts :data:`FAKE_EMAIL` on the allowlist deliberately.

**Its authorization URL points back at the caller's own redirect URI**, carrying a code
and the state. That is what a real provider does after a human clicks "allow", and doing
it here means the whole round trip — start, redirect, callback, session — is exercisable
in a browser with no Google client and no network.
"""

from __future__ import annotations

from dataclasses import dataclass
from urllib.parse import urlencode

from .interfaces import IdentityError, IdentityProvider, VerifiedIdentity

#: Who the fake says you are. A `.test` address (RFC 6761) so it can never be a real
#: mailbox, and constant so that a test's allowlist can name it.
FAKE_EMAIL = "owner@motet.test"
FAKE_SUBJECT = "fake-google-subject"


@dataclass(frozen=True)
class FakeIdentityProvider:
    """A provider that always answers with the same verified identity."""

    email: str = FAKE_EMAIL
    subject: str = FAKE_SUBJECT
    name: str | None = "Motet Owner"

    def authorization_url(
        self, *, redirect_uri: str, state: str, nonce: str, code_challenge: str
    ) -> str:
        # Straight back to the caller, as though consent had been granted instantly. The
        # code is derived from the state so that a callback carrying a code from a
        # different authorization is a thing a test can construct.
        query = urlencode({"code": f"fake-code-{state}", "state": state})
        separator = "&" if "?" in redirect_uri else "?"
        return f"{redirect_uri}{separator}{query}"

    def complete(
        self, *, code: str, redirect_uri: str, code_verifier: str, nonce: str
    ) -> VerifiedIdentity:
        if not code:
            raise IdentityError("No authorization code was presented.")
        return VerifiedIdentity(email=self.email, subject=self.subject, name=self.name)


_: type[IdentityProvider] = FakeIdentityProvider
"""Structural conformance, checked by mypy rather than asserted at runtime.

If the fake drifts from its Protocol this assignment stops type-checking, which is a build
failure rather than a surprise the first time real mode is switched on.
"""
