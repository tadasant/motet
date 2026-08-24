"""The real Gmail adapter — dormant until a Google OAuth client exists.

**Nothing here has ever run.** Tadas has not created the OAuth client, so there is no
client id, no client secret, and no consent screen. This module is written, typed, and
covered against a stub transport so that the day those arrive is a configuration change:
set ``GOOGLE_OAUTH_CLIENT_ID`` and ``GOOGLE_OAUTH_CLIENT_SECRET``, flip
``MOTET_INFERENCE_MODE=real``, and the registry hands out these classes instead of the
fakes. That is the whole switch — see :mod:`motet_sources.registry`.

Raw REST over ``httpx`` rather than ``google-api-python-client``: two endpoints are needed,
the SDK pulls in a discovery-document machine and its own auth stack, and its credential
object wants to hold and refresh tokens itself — which would put a plaintext refresh token
somewhere other than the vault. Refreshing is ours (see :meth:`GmailOAuthClient.refresh`),
so the token's whole lifetime stays inside invariant 8.

**Read-only, and incremental.** The only scope asked for is
``gmail.readonly``, requested on its own rather than bundled with anything else, so that
the consent screen says exactly one true thing. A later feature that needs more asks for
more *then*, against the same stored grant — which is what ``include_granted_scopes`` is
for and why ``source_credentials.scopes`` records what was actually granted rather than
what was asked.
"""

from __future__ import annotations

import base64
import logging
from collections.abc import Mapping, Sequence
from typing import Any, Final
from urllib.parse import urlencode

from .interfaces import (
    MessagePage,
    MessageRef,
    RawMessage,
    SourceAuthError,
    SourceError,
    TokenGrant,
)

logger = logging.getLogger("motet.sources.gmail")

PROVIDER: Final = "gmail"

#: The one scope. Read-only, and the narrowest read-only scope Gmail offers that can
#: actually fetch a message body — `gmail.metadata` cannot, and `gmail.modify` is write.
GMAIL_READONLY_SCOPE: Final = "https://www.googleapis.com/auth/gmail.readonly"

CLIENT_ID_ENV: Final = "GOOGLE_OAUTH_CLIENT_ID"
CLIENT_SECRET_ENV: Final = "GOOGLE_OAUTH_CLIENT_SECRET"
TIMEOUT_ENV: Final = "MOTET_GMAIL_TIMEOUT_SECONDS"

GMAIL_API_BASE: Final = "https://gmail.googleapis.com/gmail/v1"
GOOGLE_AUTH_ENDPOINT: Final = "https://accounts.google.com/o/oauth2/v2/auth"
GOOGLE_TOKEN_ENDPOINT: Final = "https://oauth2.googleapis.com/token"

DEFAULT_TIMEOUT_SECONDS: Final = 30.0

#: What a first sync pulls in. A mailbox has years of newsletters and ingesting them all
#: would spend a fortune on dedup and produce a backlog nobody would ever clear. Days
#: rather than a message count, because "the last week of newsletters" is a thing a user
#: can predict and "the last 50 messages" is not.
DEFAULT_FIRST_SYNC_DAYS: Final = 7

#: The default Gmail search. Category-based rather than label-based because it needs no
#: setup from the user — Gmail already sorts newsletters into `promotions` and `updates`.
#: Overridable per source in ``sources.config``, which is where a user's own label goes.
DEFAULT_QUERY: Final = "category:updates OR category:promotions"


class GmailConfigError(SourceError):
    """Gmail is selected but not configured well enough to call."""


def oauth_client_config(env: Mapping[str, str]) -> tuple[str, str]:
    """The OAuth client id and secret, or a clear statement of what is missing.

    Raises rather than returning empty strings so that a misconfigured deployment fails at
    the connect attempt with a message naming the variable, instead of at Google's token
    endpoint with ``invalid_client``.
    """
    client_id = env.get(CLIENT_ID_ENV, "").strip()
    client_secret = env.get(CLIENT_SECRET_ENV, "").strip()
    missing = [
        name
        for name, value in ((CLIENT_ID_ENV, client_id), (CLIENT_SECRET_ENV, client_secret))
        if not value
    ]
    if missing:
        raise GmailConfigError(
            f"{' and '.join(missing)} unset, so Gmail cannot be connected. The Google "
            "OAuth client is a one-time human-owned provisioning step; until it exists, "
            "run with MOTET_INFERENCE_MODE=fake and the fake mailbox."
        )
    return client_id, client_secret


class GmailOAuthClient:
    """Consent, exchange, and refresh against Google's OAuth 2.0 endpoints.

    **We hold the refresh token, not a vendor SDK.** Google issues a refresh token exactly
    once, at first consent, and never again unless the grant is revoked and re-granted. So
    :meth:`refresh` deliberately returns ``TokenGrant.refresh_token=None`` — mirroring what
    Google actually sends — and the caller must not write that ``None`` over its stored
    grant. Getting this wrong disconnects the mailbox an hour after connecting it, with no
    error anywhere; the nullability is the warning sign left in the type.
    """

    def __init__(
        self,
        *,
        client_id: str,
        client_secret: str,
        timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
        token_endpoint: str = GOOGLE_TOKEN_ENDPOINT,
        authorization_endpoint: str = GOOGLE_AUTH_ENDPOINT,
        transport: Any | None = None,
    ) -> None:
        self._client_id = client_id
        self._client_secret = client_secret
        self._timeout = timeout_seconds
        self._token_endpoint = token_endpoint
        self._authorization_endpoint = authorization_endpoint
        # An injected transport is how this is covered without a network. Invariant 7's
        # rule — no test in this repo makes a real vendor call — applies to Gmail exactly
        # as it does to a model.
        self._transport = transport

    def authorization_url(
        self, *, redirect_uri: str, state: str, code_challenge: str, scopes: Sequence[str]
    ) -> str:
        query = urlencode(
            {
                "client_id": self._client_id,
                "redirect_uri": redirect_uri,
                "response_type": "code",
                "scope": " ".join(scopes),
                "state": state,
                "code_challenge": code_challenge,
                "code_challenge_method": "S256",
                # Without `offline` Google issues no refresh token, and the connection
                # silently dies an hour later.
                "access_type": "offline",
                # Forces the consent screen even on a re-connect. Without it, a user who
                # has consented before gets no refresh token on the second grant — the
                # single most common way an OAuth integration breaks on re-authorization.
                "prompt": "consent",
                # Incremental consent: a later feature asking for another scope keeps the
                # ones already granted rather than replacing them.
                "include_granted_scopes": "true",
            }
        )
        return f"{self._authorization_endpoint}?{query}"

    def exchange_code(self, *, code: str, redirect_uri: str, code_verifier: str) -> TokenGrant:
        return self._token_request(
            {
                "client_id": self._client_id,
                "client_secret": self._client_secret,
                "code": code,
                "code_verifier": code_verifier,
                "grant_type": "authorization_code",
                "redirect_uri": redirect_uri,
            }
        )

    def refresh(self, *, refresh_token: str) -> TokenGrant:
        return self._token_request(
            {
                "client_id": self._client_id,
                "client_secret": self._client_secret,
                "refresh_token": refresh_token,
                "grant_type": "refresh_token",
            }
        )

    def _token_request(self, form: dict[str, str]) -> TokenGrant:
        response = self._post(self._token_endpoint, form)
        status = response.status_code
        if status == 400 or status == 401:
            # `invalid_grant` is the one that matters: the user revoked access, or the
            # refresh token expired after six months of disuse. Retrying cannot fix it and
            # only re-consent can, so it is a permanent failure rather than a retryable one.
            raise SourceAuthError(
                f"Google rejected the token request ({status}): {_error_detail(response)}. "
                "The mailbox must be reconnected."
            )
        if status >= 300:
            raise SourceError(f"Google's token endpoint returned {status}: {response.text[:300]}")

        body = response.json()
        access_token = body.get("access_token")
        if not isinstance(access_token, str) or not access_token:
            raise SourceError("Google's token response carried no access_token")
        raw_scopes = body.get("scope")
        return TokenGrant(
            access_token=access_token,
            # Present on the first exchange, absent on every refresh. Passed through as
            # None rather than as "" so a caller cannot store an empty grant by accident.
            refresh_token=body.get("refresh_token") or None,
            expires_in_seconds=int(body.get("expires_in") or 3600),
            scopes=tuple(raw_scopes.split()) if isinstance(raw_scopes, str) else (),
        )

    def _post(self, url: str, form: dict[str, str]) -> Any:
        if self._transport is not None:
            return self._transport.post(url, data=form)
        import httpx  # noqa: PLC0415  — fake mode never pulls in an HTTP client

        with httpx.Client(timeout=self._timeout) as client:
            return client.post(url, data=form)


class GmailMailClient:
    """List and fetch messages from one mailbox, with an already-resolved access token.

    **It is handed a token; it never resolves one.** Refreshing means reading a sealed
    credential out of the database and unsealing it, and only workers may do that
    (invariant 8). Keeping that out of here means this class holds no key material and no
    database handle — so the whole decrypt boundary stays in one place instead of being
    smeared across the adapter.
    """

    def __init__(
        self,
        access_token: str,
        *,
        timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
        base_url: str = GMAIL_API_BASE,
        transport: Any | None = None,
    ) -> None:
        if not access_token:
            raise GmailConfigError("GmailMailClient needs a resolved access token")
        self._token = access_token
        self._timeout = timeout_seconds
        self._base_url = base_url.rstrip("/")
        self._transport = transport

    def list_messages(self, *, query: str, cursor: str | None, limit: int) -> MessagePage:
        """One page of matching message ids, oldest first.

        ``cursor`` is a Gmail **history id**, and the history API is what makes an
        incremental poll cheap: it answers "what changed since" rather than "what matches",
        so a mailbox with ten thousand newsletters costs one small request per poll.

        A first sync has no history id, so it falls back to a search bounded to the last
        :data:`DEFAULT_FIRST_SYNC_DAYS` days — an unbounded first sync would ingest an
        archive and bill for deduping all of it.
        """
        if cursor:
            return self._history_page(cursor=cursor, limit=limit)
        return self._search_page(query=query, limit=limit)

    def _history_page(self, *, cursor: str, limit: int) -> MessagePage:
        response = self._get(
            f"{self._base_url}/users/me/history",
            {
                "startHistoryId": cursor,
                "historyTypes": "messageAdded",
                "maxResults": str(limit),
            },
        )
        if response.status_code == 404:
            # Gmail expires history ids past its retention window. Not an error — it means
            # "resync from scratch", which is a different repair from a retry, so it is
            # reported as a distinct fact rather than raised.
            logger.info("Gmail history id %s has expired; a full resync is needed", cursor)
            return MessagePage(messages=(), cursor=None, cursor_expired=True)
        body = self._json(response, "history")

        seen: list[str] = []
        for record in body.get("history") or []:
            for added in record.get("messagesAdded") or []:
                message = added.get("message") or {}
                message_id = message.get("id")
                # Deduplicated in order: one message can appear in several history records
                # (added, then labelled), and fetching it twice is a wasted request that
                # the unique index downstream would reject anyway.
                if isinstance(message_id, str) and message_id not in seen:
                    seen.append(message_id)

        return MessagePage(
            messages=tuple(MessageRef(id=message_id) for message_id in seen),
            # `historyId` is the mailbox's current watermark and is present even when
            # nothing matched — which is exactly when advancing it matters, or the next
            # poll re-reads the same empty window forever.
            cursor=str(body.get("historyId") or cursor),
        )

    def _search_page(self, *, query: str, limit: int) -> MessagePage:
        response = self._get(
            f"{self._base_url}/users/me/messages",
            {
                "q": f"{query} newer_than:{DEFAULT_FIRST_SYNC_DAYS}d",
                "maxResults": str(limit),
            },
        )
        body = self._json(response, "messages")
        messages = tuple(
            MessageRef(id=item["id"], thread_id=item.get("threadId"))
            for item in (body.get("messages") or [])
            if isinstance(item.get("id"), str)
        )
        # Reversed: Gmail returns newest first, and ingestion order decides what dedup
        # merges into what. Oldest first means a follow-up folds into the original story
        # rather than the original folding into the follow-up.
        messages = tuple(reversed(messages))

        # The watermark comes from the profile rather than from this response, because a
        # search result carries no history id. Fetching it *after* the search would risk
        # skipping a message that arrived in between; fetching it before means at worst
        # re-seeing one, which the unique index makes harmless.
        return MessagePage(messages=messages, cursor=self._current_history_id())

    def _current_history_id(self) -> str | None:
        body = self._json(self._get(f"{self._base_url}/users/me/profile", {}), "profile")
        history_id = body.get("historyId")
        return str(history_id) if history_id else None

    def fetch_message(self, message_id: str) -> RawMessage:
        """The message as RFC 822 bytes.

        ``format=raw`` rather than Gmail's parsed ``payload`` tree: the parsing is
        :mod:`motet_sources.extract`, and it must run on exactly the same input in real
        and fake mode or the fixtures stop proving anything.
        """
        response = self._get(f"{self._base_url}/users/me/messages/{message_id}", {"format": "raw"})
        body = self._json(response, f"message {message_id}")
        raw = body.get("raw")
        if not isinstance(raw, str):
            raise SourceError(f"Gmail returned no raw body for message {message_id}")
        # URL-safe base64, and Gmail omits the padding.
        return RawMessage(id=message_id, raw=base64.urlsafe_b64decode(raw + "=" * (-len(raw) % 4)))

    def _get(self, url: str, params: dict[str, str]) -> Any:
        headers = {"Authorization": f"Bearer {self._token}", "Accept": "application/json"}
        if self._transport is not None:
            return self._transport.get(url, params=params, headers=headers)
        import httpx  # noqa: PLC0415

        with httpx.Client(timeout=self._timeout) as client:
            return client.get(url, params=params, headers=headers)

    def _json(self, response: Any, what: str) -> dict[str, Any]:
        status = response.status_code
        if status in (401, 403):
            raise SourceAuthError(
                f"Gmail rejected the credential fetching {what} ({status}): "
                f"{_error_detail(response)}"
            )
        if status == 429 or status >= 500:
            # Rate limit or outage. Retryable, and the queue's backoff is the right place
            # for the waiting — a sleep here would hold a worker and its advisory lock.
            raise SourceError(f"Gmail is unavailable fetching {what} ({status}); retry later")
        if status >= 300:
            raise SourceError(f"Gmail returned {status} fetching {what}: {response.text[:300]}")
        body = response.json()
        if not isinstance(body, dict):
            raise SourceError(f"Gmail returned a non-object body fetching {what}")
        return body


def _error_detail(response: Any) -> str:
    """A short, safe rendering of an error body.

    Truncated and never logged wholesale: an OAuth error response can echo request
    parameters back, and this repo's logs go to a shared observability stack.
    """
    try:
        body = response.json()
    except Exception:  # noqa: BLE001 — an error body is frequently not JSON
        return str(response.text)[:200]
    if isinstance(body, dict):
        detail = body.get("error_description") or body.get("error") or ""
        if isinstance(detail, dict):
            detail = detail.get("message", "")
        return str(detail)[:200]
    return str(body)[:200]
