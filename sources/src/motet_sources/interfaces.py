"""The mailbox seam, as Protocols.

The same shape as ``motet_inference.interfaces`` and for the same reason: Gmail is a
vendor, so nothing in this repo calls it directly. A caller asks the registry for a
:class:`MailClient` and gets either the real adapter or a deterministic fake, decided by
``MOTET_INFERENCE_MODE`` — the one variable, parsed in the one place.

**The interface is deliberately smaller than Gmail's API.** Two operations: list what has
arrived since a cursor, and fetch one message's raw RFC 822 bytes. Everything else —
threading, labels, attachments, the ``format=metadata`` shortcut — is either not needed
or is a detail of the adapter. A narrow interface is what makes the fake honest; a fake
that had to model Gmail's history API would be a worse Gmail rather than a better test.

**Fetching returns raw bytes, not a parsed message.** Parsing is
:mod:`motet_sources.extract`, and it runs identically on real and fake input, so the
newsletter-sludge handling that is the actual risk here is exercised in CI against real
message formats rather than against something the adapter pre-digested.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Protocol, runtime_checkable


class SourceError(RuntimeError):
    """A source could not be polled or fetched. Retryable unless stated otherwise."""


class SourceAuthError(SourceError):
    """The credential was rejected. Retrying will not help until the user reconsents."""


@dataclass(frozen=True)
class MessageRef:
    """One message the provider says exists, without its content.

    ``id`` is the provider's own identifier and is what
    ``source_items.external_id`` stores, so re-polling the same mailbox cannot produce a
    second copy of a newsletter.
    """

    id: str
    thread_id: str | None = None


@dataclass(frozen=True)
class MessagePage:
    """One page of a poll, plus where to resume.

    ``cursor`` is opaque to everything above the adapter — for Gmail it is a search
    watermark and, mid-search, a page token. Persisted verbatim in ``sources.sync_state``,
    never parsed outside the adapter that produced it.
    """

    messages: tuple[MessageRef, ...]
    cursor: str | None
    #: True when the provider has another page for the search this one belongs to. The
    #: caller resumes from ``cursor`` — now, or on its next poll — and must not treat the
    #: search as caught up until this is False.
    more: bool = False
    #: Set on the page that began a first sync: the window, in days, the adapter bounded
    #: it to. ``None`` on every other page. Reported rather than returned silently so the
    #: bound is a fact on the source instead of a constant in the adapter (motet#94).
    first_sync_days: int | None = None


@dataclass(frozen=True)
class RawMessage:
    """One message as the provider stored it: RFC 822 bytes, plus its id."""

    id: str
    raw: bytes


@runtime_checkable
class MailClient(Protocol):
    """List and fetch newsletter messages from one connected mailbox."""

    def list_messages(self, *, query: str, cursor: str | None, limit: int) -> MessagePage:
        """One page of what matches ``query`` since ``cursor``, oldest first.

        ``query`` is the provider's own search syntax, carried from the source's config so
        that "only this label" is the user's decision rather than ours — and it applies to
        *every* page, not only the first sync's. A ``cursor`` of ``None`` means a first
        sync, which the adapter bounds itself — a first poll must not ingest a decade of
        archive. At most ``limit`` messages come back, and ``more`` says whether another
        page is waiting behind them.
        """
        ...

    def fetch_message(self, message_id: str) -> RawMessage: ...


@runtime_checkable
class OAuthClient(Protocol):
    """Turn a user's consent into tokens, and keep an access token fresh.

    Separate from :class:`MailClient` because the two have different lifetimes and
    different callers: the API completes consent, and workers refresh. Splitting them is
    what lets the API hold something that can seal a token without holding something that
    could read a mailbox.
    """

    def authorization_url(
        self, *, redirect_uri: str, state: str, code_challenge: str, scopes: Sequence[str]
    ) -> str: ...

    def exchange_code(self, *, code: str, redirect_uri: str, code_verifier: str) -> TokenGrant: ...

    def refresh(self, *, refresh_token: str) -> TokenGrant: ...


@dataclass(frozen=True)
class TokenGrant:
    """What an OAuth exchange returned.

    ``refresh_token`` is ``None`` on a refresh, because Google only issues one at first
    consent — a caller that overwrote the stored refresh token with ``None`` would
    disconnect the mailbox at the first hourly refresh, which is exactly the bug this
    field's nullability is here to make visible.
    """

    access_token: str
    expires_in_seconds: int
    scopes: tuple[str, ...]
    refresh_token: str | None = None
