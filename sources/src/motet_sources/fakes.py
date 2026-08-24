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
    MailClient,
    MessagePage,
    MessageRef,
    OAuthClient,
    RawMessage,
    SourceError,
    TokenGrant,
)

#: Where the fake mailbox reads its messages from. One `.eml` per message, applied in
#: filename order, which is also arrival order.
FIXTURES_DIR = Path(__file__).resolve().parent / "fixtures" / "mailbox"


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
    """A mailbox that hands out fixture messages, paging by an integer cursor.

    The cursor is the count already delivered, rendered as a string, because the interface
    says a cursor is opaque and the fake should not be the thing that quietly makes it
    structured. It behaves like a real one in the way that matters: resuming from a stale
    cursor returns nothing new rather than replaying the mailbox.
    """

    messages: list[RawMessage] = field(default_factory=load_fixture_messages)
    #: Set to make the next ``list_messages`` report an expired cursor, so the caller's
    #: full-resync path is reachable in a test without a real Gmail history window.
    expire_cursor: bool = False

    def list_messages(self, *, query: str, cursor: str | None, limit: int) -> MessagePage:
        if self.expire_cursor and cursor is not None:
            return MessagePage(messages=(), cursor=None, cursor_expired=True)
        start = int(cursor) if cursor else 0
        window = self.messages[start : start + max(1, limit)]
        return MessagePage(
            messages=tuple(MessageRef(id=message.id) for message in window),
            cursor=str(start + len(window)),
        )

    def fetch_message(self, message_id: str) -> RawMessage:
        for message in self.messages:
            if message.id == message_id:
                return message
        raise SourceError(f"no such message in the fake mailbox: {message_id!r}")


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

    def authorization_url(
        self, *, redirect_uri: str, state: str, code_challenge: str, scopes: Sequence[str]
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
            }
        )
        return f"{self.authorization_endpoint}?{query}"

    def exchange_code(self, *, code: str, redirect_uri: str, code_verifier: str) -> TokenGrant:
        from .gmail import GMAIL_READONLY_SCOPE  # noqa: PLC0415

        return TokenGrant(
            access_token=_fake_token("access", code),
            refresh_token=_fake_token("refresh", code),
            expires_in_seconds=self.expires_in_seconds,
            scopes=(GMAIL_READONLY_SCOPE,),
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
            scopes=(GMAIL_READONLY_SCOPE,),
        )


def _fake_token(purpose: str, seed: str) -> str:
    return f"fake-{purpose}-{hashlib.sha256(f'{purpose}:{seed}'.encode()).hexdigest()[:32]}"


_: tuple[type[MailClient], type[OAuthClient]] = (FakeMailClient, FakeOAuthClient)
"""Structural conformance, checked by mypy rather than asserted at runtime.

If either fake drifts from its Protocol this assignment stops type-checking, which is a
build failure rather than a surprise the first time real mode is switched on.
"""
