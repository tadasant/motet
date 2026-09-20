"""A deterministic mailbox, and a deterministic OAuth provider.

The same contract as the inference fakes: honest implementations of the interface with a
trivial rule standing in for the vendor, deterministic, offline, and free. They are what
CI runs, and — because the Google OAuth client does not exist yet — they are what *every*
environment runs until it does.

**The fake mailbox serves real message bytes.** Its fixtures are complete RFC 822
messages with the encodings real newsletters use: quoted-printable, base64, RFC 2047
subjects, multipart/alternative, table-based HTML, hidden preheaders, tracking pixels,
unsubscribe footers. That is the point — the part of Gmail ingestion that can actually be
wrong is :mod:`motet_sources.extract`, and it must be exercised against the shapes it will
meet rather than against something convenient.

**The fake mailbox records every write.** :attr:`FakeMailClient.modify_calls` is the
list a test asserts on — "exactly this call after a deliberate ingest, and none after a
poll" is the whole contract of label sync (motet#96), and a fake that merely accepted the
call would let either half of it break silently. It answers an unknown message or label id
the way Gmail does, with :class:`~motet_sources.interfaces.StaleReferenceError`.

**The fake OAuth provider issues tokens that are obviously fake and obviously secret.**
They are sealed and unsealed by the same vault path a real token takes, so the
envelope-encryption invariant is exercised end to end before a single real credential
exists.
"""

from __future__ import annotations

import hashlib
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path

from .interfaces import (
    Label,
    MailClient,
    MessagePage,
    MessageRef,
    OAuthClient,
    RawMessage,
    SourceError,
    StaleReferenceError,
    TokenGrant,
)

#: Where the fake mailbox reads its messages from. One `.eml` per message, applied in
#: filename order, which is also arrival order.
FIXTURES_DIR = Path(__file__).resolve().parent / "fixtures" / "mailbox"


#: The fake mailbox's labels: Gmail's system set, plus the owner's own workflow pair —
#: ``Newsletters`` and ``Completed`` — so the settings UI and the write-back have something
#: real-shaped to resolve against in every environment that runs the fake.
FAKE_LABELS: tuple[Label, ...] = (
    Label(id="INBOX", name="INBOX", system=True),
    Label(id="UNREAD", name="UNREAD", system=True),
    Label(id="STARRED", name="STARRED", system=True),
    Label(id="IMPORTANT", name="IMPORTANT", system=True),
    Label(id="SENT", name="SENT", system=True),
    Label(id="TRASH", name="TRASH", system=True),
    Label(id="SPAM", name="SPAM", system=True),
    Label(id="CATEGORY_UPDATES", name="CATEGORY_UPDATES", system=True),
    Label(id="Label_101", name="Newsletters"),
    Label(id="Label_102", name="Completed"),
    Label(id="Label_103", name="Reading/Later"),
)


def load_fixture_messages(directory: Path = FIXTURES_DIR) -> list[RawMessage]:
    """Every fixture message, oldest first.

    The message id is the filename stem, so it is stable across runs — which is what makes
    ``source_items.external_id`` deduplication testable: polling twice must produce the
    same ids and therefore the same one row.
    """
    if not directory.is_dir():
        return []
    return [
        RawMessage(id=path.stem, raw=path.read_bytes()) for path in sorted(directory.glob("*.eml"))
    ]


@dataclass
class FakeMailClient:
    """A mailbox that hands out fixture messages as a paged, watermarked search.

    The same shape as the real adapter, because that shape is what a poll has to get right:
    a pass lists everything past the watermark newest first, a page at a time; the cursor
    holds the pass's place while it has more; and only an exhausted pass moves the
    watermark, to where that pass began. Arrival order stands in for time — a message's
    index in :attr:`messages` is its timestamp — so appending to the list is a message
    arriving. Resuming from a caught-up cursor returns nothing new rather than replaying
    the mailbox.

    The cursor is opaque in the same sense the interface means: ``fake:`` and a few
    integers, and anything else — a history id stored before listing moved to search, most
    obviously — is treated as no cursor, exactly as the real adapter treats it.
    """

    messages: list[RawMessage] = field(default_factory=load_fixture_messages)
    #: The provider's own page size. Below the caller's ``limit`` it produces a *short*
    #: page with more behind it, which is the case that proves "fewer than asked for" is
    #: not the end of a search.
    page_size: int | None = None
    #: Every search this mailbox was sent, in the form the real adapter sends it — so a test
    #: can see that the source's filter rode on an incremental poll and not only a first
    #: sync (motet#95).
    searches: list[str] = field(default_factory=list)
    labels: list[Label] = field(default_factory=lambda: list(FAKE_LABELS))
    #: Every ``modify_labels`` call, in order, as ``(message_id, add, remove)``.
    modify_calls: list[tuple[str, tuple[str, ...], tuple[str, ...]]] = field(default_factory=list)
    #: How many times ``list_labels`` was asked — the cache-miss path is a count.
    label_lists: int = 0
    #: Which mailbox this is, as ``users.getProfile`` would say.
    address: str = "owner@example.invalid"

    def list_messages(
        self, *, query: str, cursor: str | None, limit: int, window_days: int | None = None
    ) -> MessagePage:
        from .gmail import first_sync_days, search_query  # noqa: PLC0415

        state = _fake_cursor(cursor)
        chosen_days: int | None = None
        if state is None:
            # The fake's window is the whole mailbox; it reports the one it was asked for so
            # the caller's handling of the reported window is exercised all the same — which
            # is what lets a test prove the source's chosen window reached the adapter.
            chosen_days = first_sync_days(window_days)
            state = (0, None, 0)
        after, started, offset = state
        if started is None:
            started, offset = len(self.messages), 0
        self.searches.append(search_query(query, after=after))

        in_pass = list(reversed(self.messages[after:started]))  # newest first, like Gmail
        size = max(1, min(limit, self.page_size or limit))
        page = in_pass[offset : offset + size]
        more = offset + size < len(in_pass)
        return MessagePage(
            messages=tuple(MessageRef(id=message.id) for message in reversed(page)),
            cursor=f"fake:{after}:{started}:{offset + size}" if more else f"fake:{started}",
            more=more,
            first_sync_days=chosen_days,
        )

    def fetch_message(self, message_id: str) -> RawMessage:
        for message in self.messages:
            if message.id == message_id:
                return message
        raise SourceError(f"no such message in the fake mailbox: {message_id!r}")

    def list_labels(self) -> tuple[Label, ...]:
        self.label_lists += 1
        return tuple(self.labels)

    def mailbox_address(self) -> str | None:
        return self.address

    def modify_labels(self, message_id: str, *, add: Sequence[str], remove: Sequence[str]) -> None:
        if not any(message.id == message_id for message in self.messages):
            raise StaleReferenceError(
                f"Requested entity was not found: {message_id!r}", target="message"
            )
        known = {label.id for label in self.labels}
        unknown = [label_id for label_id in (*add, *remove) if label_id not in known]
        if unknown:
            raise StaleReferenceError(f"Invalid label: {', '.join(unknown)}", target="label")
        self.modify_calls.append((message_id, tuple(add), tuple(remove)))


def _fake_cursor(cursor: str | None) -> tuple[int, int | None, int] | None:
    """``(after, started, offset)`` out of a cursor this fake wrote, or None for any other."""
    if not cursor or not cursor.startswith("fake:"):
        return None
    try:
        parts = [int(part) for part in cursor.removeprefix("fake:").split(":")]
    except ValueError:
        return None
    if len(parts) == 1:
        return (parts[0], None, 0)
    if len(parts) == 3:
        return (parts[0], parts[1], parts[2])
    return None


@dataclass(frozen=True)
class FakeOAuthClient:
    """Consent without a browser, a Google client, or a network.

    Tokens are derived from their inputs by hash, so they are deterministic and distinct:
    two different authorization codes cannot silently produce the same access token, which
    is the bug a constant would hide.
    """

    #: What a real provider would host. Only ever rendered, never fetched.
    authorization_endpoint: str = "https://accounts.example.invalid/o/oauth2/v2/auth"
    expires_in_seconds: int = 3600
    #: What the fake says was granted. Read-only unless a test says otherwise, because that
    #: is what every real connect grants — a label-sync re-consent is the one flow that
    #: comes back wider, and a test of it has to say so rather than get it by default.
    granted_scopes: tuple[str, ...] | None = None

    def authorization_url(
        self,
        *,
        redirect_uri: str,
        state: str,
        code_challenge: str,
        scopes: Sequence[str],
        login_hint: str | None = None,
    ) -> str:
        from urllib.parse import urlencode  # noqa: PLC0415

        query = urlencode(
            {
                "client_id": "fake-client-id",
                "redirect_uri": redirect_uri,
                "response_type": "code",
                "scope": " ".join(scopes),
                "state": state,
                "code_challenge": code_challenge,
                "code_challenge_method": "S256",
                "access_type": "offline",
                "prompt": "consent",
                **({"login_hint": login_hint} if login_hint else {}),
            }
        )
        return f"{self.authorization_endpoint}?{query}"

    def exchange_code(self, *, code: str, redirect_uri: str, code_verifier: str) -> TokenGrant:
        from .gmail import GMAIL_READONLY_SCOPE  # noqa: PLC0415

        return TokenGrant(
            access_token=_fake_token("access", code),
            refresh_token=_fake_token("refresh", code),
            expires_in_seconds=self.expires_in_seconds,
            scopes=self.granted_scopes or (GMAIL_READONLY_SCOPE,),
        )

    def refresh(self, *, refresh_token: str) -> TokenGrant:
        from .gmail import GMAIL_READONLY_SCOPE  # noqa: PLC0415

        # No `refresh_token` in the result, exactly like Google: a refresh returns only a
        # new access token, and a caller that overwrote its stored grant with this one's
        # `None` would disconnect the mailbox an hour after connecting it.
        return TokenGrant(
            access_token=_fake_token("access", refresh_token),
            refresh_token=None,
            expires_in_seconds=self.expires_in_seconds,
            scopes=self.granted_scopes or (GMAIL_READONLY_SCOPE,),
        )


def _fake_token(purpose: str, seed: str) -> str:
    return f"fake-{purpose}-{hashlib.sha256(f'{purpose}:{seed}'.encode()).hexdigest()[:32]}"


_: tuple[type[MailClient], type[OAuthClient]] = (FakeMailClient, FakeOAuthClient)
"""Structural conformance, checked by mypy rather than asserted at runtime.

If either fake drifts from its Protocol this assignment stops type-checking, which is a
build failure rather than a surprise the first time real mode is switched on.
"""
