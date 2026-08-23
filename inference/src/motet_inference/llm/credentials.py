"""The credential seam: an enum, a value, and a resolver. One file, on purpose.

Today there is exactly one credential kind, and it is an API key. Tomorrow there will be
a second — "bring your Claude Max account", which is a quota grant against a user's own
subscription rather than a key we own. The seam is here so that arriving does not reshape
any call site; it is deliberately *not* a plugin system, a registry, or an abstract
factory, because a framework built for one implementation is a framework built for none.

**What the whole seam is.** A caller asks :func:`resolve_credential` for a kind and gets
back something with :meth:`Credential.auth_headers`. That is the entire contract. Adding
a kind means adding an enum member and a branch in the resolver — roughly twenty lines
in this file — and changing nothing anywhere else.

**What the quota kind will need when it lands**, recorded so the next session does not
have to rediscover it: an access token with a refresh cycle (so ``auth_headers`` becomes
a call that may refresh rather than a pure read of a stored string), the OAuth beta
header the subscription path requires, and provider routing pinned to Anthropic, since a
subscription grant is meaningless to any other upstream. The first of those is why
``auth_headers`` is a *method* and not an attribute: a token that refreshes cannot be a
frozen string, and making that shape change later would touch every call site, which is
precisely what this file exists to avoid.

**Secrets never land in this repo or in a log.** The value arrives from the environment,
placed there by Secret Manager through the Cloud Run service definition, which lives in
the private infrastructure repo. :class:`Credential` redacts itself on ``repr`` so it
cannot leak through a traceback or a debug log line.
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Final

#: Where the OpenRouter key is expected. Cloud Run injects it from Secret Manager under
#: exactly this name; nothing ever reads a key from a file or an image layer.
API_KEY_ENV: Final = "OPENROUTER_API_KEY"


class CredentialKind(StrEnum):
    """How the process is authorized to spend inference.

    ``API_KEY`` — a key we own, billed to us. The only kind Phase 1 has, and the one
    the whole path is optimized for.
    """

    API_KEY = "api_key"


@dataclass(frozen=True)
class Credential:
    """A resolved authorization, carrying its secret and knowing how to present it."""

    kind: CredentialKind
    secret: str = field(repr=False)

    def auth_headers(self) -> dict[str, str]:
        """The headers that authorize a request.

        A method rather than a stored mapping so a future kind can refresh an expiring
        token here without any call site noticing.
        """
        return {"Authorization": f"Bearer {self.secret}"}

    def __repr__(self) -> str:
        return f"Credential(kind={self.kind.value!r}, secret=<redacted>)"


def resolve_credential(
    kind: CredentialKind = CredentialKind.API_KEY,
    env: Mapping[str, str] | None = None,
) -> Credential:
    """Resolve ``kind`` from the environment, or say precisely what is missing.

    Raises :class:`~motet_inference.llm.types.LlmConfigError` rather than returning None:
    a process that cannot authorize should stop at startup, not surface a 500 on the
    first request an hour later.
    """
    from .types import LlmConfigError

    environ = os.environ if env is None else env

    if kind is CredentialKind.API_KEY:
        secret = environ.get(API_KEY_ENV, "").strip()
        if not secret:
            raise LlmConfigError(
                f"{API_KEY_ENV} is unset or empty, so no LLM call can be authorized. "
                "It is injected from Secret Manager by the Cloud Run service definition "
                "(private infrastructure repo); locally, put it in .env. To run with no "
                "credential at all, set MOTET_INFERENCE_MODE=fake."
            )
        return Credential(kind=kind, secret=secret)

    raise LlmConfigError(f"No resolver for credential kind {kind.value!r}")
