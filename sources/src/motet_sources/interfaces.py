"""The mailbox seam, as Protocols.

The same shape as ``motet_inference.interfaces`` and for the same reason: Gmail is a
vendor, so nothing in this repo calls it directly. A caller asks the registry for a
:class:`MailClient` and gets either the real adapter or a deterministic fake, decided by
``MOTET_INFERENCE_MODE`` — the one variable, parsed in the one place.

**The interface is deliberately smaller than Gmail's API.** Two reads carry ingestion:
list what has arrived since a cursor, and fetch one message's raw RFC 822 bytes. Two more
carry the one write the connector makes (motet#96): list the mailbox's labels, and move one
message between them. Everything else — threading, attachments, the ``format=metadata``
shortcut — is either not needed or is a detail of the adapter. A narrow interface is what
makes the fake honest; a fake that had to model Gmail's history API would be a worse Gmail
rather than a better test.

**The write is the exception, and it has its own scope.** :meth:`MailClient.modify_labels`
needs ``gmail.modify``, which a mailbox grants only when its owner turns label sync on for
it. Nothing on the ingestion path calls it — see ``motet_workers.labels``.

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


class StaleReferenceError(SourceError):
    """The provider did not recognise an id we sent — a message or a label.

    Distinct from :class:`SourceError` because the repair is different: a label id cached
    from an earlier ``list_labels`` goes stale when the user deletes and recreates the
    label, and re-resolving the name is what fixes it, where retrying the same request
    would fail the same way. ``target`` says which, when the provider made it knowable: a
    message that is gone is not fixed by re-reading the labels.
    """

    def __init__(self, message: str, *, target: str = "unknown") -> None:
        super().__init__(message)
        #: ``"message"``, ``"label"``, or ``"unknown"``.
        self.target = target


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


@dataclass(frozen=True)
class Label:
    """One label in a mailbox, as the provider names it.

    ``id`` is what a modify request carries and ``name`` is what a person types; they are
    the same string for Gmail's system labels (``INBOX``) and unrelated for a user's own
    (``Label_12`` / ``Newsletters``). ``system`` says which, because only a handful of
    system labels are safe to move a message into or out of.
    """

    id: str
    name: str
    system: bool = False


@runtime_checkable
class MailClient(Protocol):
    """List and fetch newsletter messages from one connected mailbox — and, when its owner
    has asked for it, move one between labels."""

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

    def list_labels(self) -> tuple[Label, ...]:
        """Every label in the mailbox. A read, and ``gmail.readonly`` is enough for it."""
        ...

    def mailbox_address(self) -> str | None:
        """The address of the mailbox this token reaches. A read; readonly covers it.

        What lets a re-consent be checked against the mailbox a source already is: Google's
        account chooser will happily hand back a *different* account's grant, and a source
        whose token silently changed mailbox would read one inbox under another's cursor.
        """
        ...

    def modify_labels(self, message_id: str, *, add: Sequence[str], remove: Sequence[str]) -> None:
        """Add and remove label **ids** on one message. Needs ``gmail.modify``.

        Idempotent by nature — adding a label a message already carries, or removing one it
        does not, is a no-op — so a retried call converges rather than compounding. Raises
        :class:`StaleReferenceError` when the message or a label id is unknown, and
        :class:`SourceAuthError` when the grant does not carry the scope.
        """
        ...


@runtime_checkable
class OAuthClient(Protocol):
    """Turn a user's consent into tokens, and keep an access token fresh.

    Separate from :class:`MailClient` because the two have different lifetimes and
    different callers: the API completes consent, and workers refresh. Splitting them is
    what lets the API hold something that can seal a token without holding something that
    could read a mailbox.
    """

    def authorization_url(
        self,
        *,
        redirect_uri: str,
        state: str,
        code_challenge: str,
        scopes: Sequence[str],
        login_hint: str | None = None,
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
