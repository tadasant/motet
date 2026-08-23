"""The credential seam: an enum, a value, and a resolver. One file, on purpose.

Today there is exactly one credential kind, and it is an API key. Tomorrow there will be
a second — "bring your Claude Max account", which is a quota grant against a user's own
subscription rather than a key we own. The seam is here so that arriving does not reshape
any call site; it is deliberately *not* a plugin system, a registry, or an abstract
factory, because a framework built for one implementation is a framework built for none.

**What the whole seam is.** A caller asks :func:`resolve_credential` for a kind and gets
back something whose :meth:`Credential.token` it can present. That is the entire
contract. Adding a kind means adding an enum member and a branch in the resolver — a
couple of dozen lines in this file — and changing nothing anywhere else.

**This file knows nothing about how a token is presented on the wire.** That is the
provider's business, not the credential's: the *same* kind of credential — an API key we
own — travels as ``Authorization: Bearer`` to OpenRouter and as ``x-api-key`` plus
``anthropic-version`` to Anthropic direct. Putting header shapes here would force that
provider distinction onto the credential-kind axis, and a second provider would have to
invent a second "kind" for what is plainly the same kind of secret. So each adapter
builds its own headers from :meth:`Credential.token`.

**What the quota kind will need when it lands**, recorded so the next session does not
have to rediscover it: an access token with a refresh cycle, and provider routing pinned
to Anthropic, since a subscription grant is meaningless to any other upstream. The
refresh is why :meth:`token` is a *method* rather than a bare attribute — a token that
expires cannot be a frozen string, and making that shape change later would touch every
call site, which is precisely what this file exists to avoid.

**Secrets never land in this repo or in a log.** The value arrives from the environment,
placed there by Secret Manager through the service definition, which lives in the private
infrastructure repo. :class:`Credential` redacts itself on ``repr`` so it cannot leak
through a traceback or a debug log line.
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass, field
from enum import StrEnum


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

    def token(self) -> str:
        """The bearer value to authorize with.

        A method rather than a bare attribute so a future kind can refresh an expiring
        token here without any call site noticing.
        """
        return self.secret

    def __repr__(self) -> str:
        return f"Credential(kind={self.kind.value!r}, secret=<redacted>)"


def resolve_credential(
    env_var: str,
    kind: CredentialKind = CredentialKind.API_KEY,
    env: Mapping[str, str] | None = None,
) -> Credential:
    """Resolve ``kind`` from ``env_var``, or say precisely what is missing.

    ``env_var`` is supplied by the provider, because which variable holds the key is a
    fact about the provider rather than about the kind of credential.

    Raises :class:`~motet_inference.llm.types.LlmConfigError` rather than returning None:
    a process that cannot authorize should stop at startup, not surface a 500 on the
    first request an hour later.
    """
    from .types import LlmConfigError

    environ = os.environ if env is None else env

    if kind is CredentialKind.API_KEY:
        secret = environ.get(env_var, "").strip()
        if not secret:
            raise LlmConfigError(
                f"{env_var} is unset or empty, so no LLM call can be authorized. "
                "It is injected from Secret Manager by the service definition (private "
                "infrastructure repo); locally, put it in .env. To run with no credential "
                "at all, set MOTET_INFERENCE_MODE=fake."
            )
        return Credential(kind=kind, secret=secret)

    raise LlmConfigError(f"No resolver for credential kind {kind.value!r}")
