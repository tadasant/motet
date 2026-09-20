"""The real Gmail adapter.

Selected by configuration alone: set ``GOOGLE_OAUTH_CLIENT_ID`` and
``GOOGLE_OAUTH_CLIENT_SECRET``, flip ``MOTET_INFERENCE_MODE=real``, and the registry hands
out these classes instead of the fakes — see :mod:`motet_sources.registry`. CI covers it
against a stub transport (``sources/tests/test_gmail.py``); it has run against a real
mailbox in local real mode, which is where motet#94 and motet#95 were found.

Raw REST over ``httpx`` rather than ``google-api-python-client``: two endpoints are needed,
the SDK pulls in a discovery-document machine and its own auth stack, and its credential
object wants to hold and refresh tokens itself — which would put a plaintext refresh token
somewhere other than the vault. Refreshing is ours (see :meth:`GmailOAuthClient.refresh`),
so the token's whole lifetime stays inside invariant 8.

**Read-only by default, and incremental.** Connecting a mailbox asks for
``gmail.readonly`` and nothing else, so that the consent screen says exactly one true
thing. The one feature that needs more — moving a message between labels when its owner
ingests it (motet#96) — asks for ``gmail.modify`` *then*, for that one source, through a
second consent the owner starts by turning the feature on, which names both scopes itself.

**No authorization sends ``include_granted_scopes``, and that is what keeps the first half
true.** With it, Google folds every scope the account has ever granted this OAuth client
into the new token — so connecting a *second* mailbox on an account that had re-consented
another source for label sync would hand the new source ``gmail.modify`` with no consent
for it. Without it, a token carries what its own authorization asked for. The callback
also records only the granted scopes that authorization asked for, so a provider that
widened anyway still cannot make a source *look* writable.
"""

from __future__ import annotations

import base64
import json
import logging
import os
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Final
from urllib.parse import urlencode

from .interfaces import (
    Label,
    MessagePage,
    MessageRef,
    RawMessage,
    SourceAuthError,
    SourceError,
    StaleReferenceError,
    TokenGrant,
)

logger = logging.getLogger("motet.sources.gmail")

PROVIDER: Final = "gmail"

#: The scope every mailbox is connected with. Read-only, and the narrowest read-only scope
#: Gmail offers that can actually fetch a message body — `gmail.metadata` cannot.
GMAIL_READONLY_SCOPE: Final = "https://www.googleapis.com/auth/gmail.readonly"

#: The write scope, and only for a mailbox whose owner turned label sync on (motet#96).
#: The narrowest one that can change a message's labels: `gmail.labels` manages the labels
#: themselves but cannot apply one to a message. It is also a scope that could archive or
#: trash mail, which is why nothing asks for it on connect, and why the worker refuses to
#: touch any system label outside ``motet_sources.labels.WRITABLE_SYSTEM_LABELS``.
GMAIL_MODIFY_SCOPE: Final = "https://www.googleapis.com/auth/gmail.modify"

#: What a label-sync re-consent asks for. Read-only is named again because nothing asks the
#: provider to merge in an earlier grant: the token that comes back must still poll.
LABEL_SYNC_SCOPES: Final = (GMAIL_READONLY_SCOPE, GMAIL_MODIFY_SCOPE)

CLIENT_ID_ENV: Final = "GOOGLE_OAUTH_CLIENT_ID"
CLIENT_SECRET_ENV: Final = "GOOGLE_OAUTH_CLIENT_SECRET"
TIMEOUT_ENV: Final = "MOTET_GMAIL_TIMEOUT_SECONDS"

GMAIL_API_BASE: Final = "https://gmail.googleapis.com/gmail/v1"
GOOGLE_AUTH_ENDPOINT: Final = "https://accounts.google.com/o/oauth2/v2/auth"
GOOGLE_TOKEN_ENDPOINT: Final = "https://oauth2.googleapis.com/token"

DEFAULT_TIMEOUT_SECONDS: Final = 30.0

#: What a first sync pulls in when nobody chose. Days rather than a message count, because
#: "the last month of newsletters" is a thing a user can predict and "the last 50 messages"
#: is not.
#:
#: **It was 7, and 7 was too short to be useful** (motet#139). The first production connect
#: searched ``label:Newsletters`` over a week, found 55 messages, paged through them
#: correctly and reported "caught up" — and the owner, who expected the 200-odd in that
#: label, read the cap as a pagination bug. A month is the shortest window in which a
#: newsletter backlog looks like a backlog. Nothing here costs inference: extraction is
#: free and deterministic, and every message stops at the held gate (motet#91) until a
#: person picks it, so a wider window buys fetches rather than model calls.
DEFAULT_FIRST_SYNC_DAYS: Final = 30

#: The ceiling on a chosen window, and what the clients offer as "Everything". Ten years is
#: past the age of any mailbox this product is for, and the bound is real rather than
#: decorative: it is what stops a typo'd ``first_sync_days`` from turning one connect into
#: an unbounded archive crawl. The *run* is bounded separately and always was — a poll
#: queues at most ``POLL_PAGE_SIZE`` over ``MAX_PAGES_PER_POLL`` pages and re-arms itself —
#: so a wide window is a longer chain of short jobs, never one long one.
MAX_FIRST_SYNC_DAYS: Final = 3650

#: Overrides :data:`DEFAULT_FIRST_SYNC_DAYS` for one deployment. Read at the point of use
#: rather than at import, so a local session can widen the window without a restart of
#: anything but the worker. A window chosen on the source itself outranks it: the env is
#: the deployment's default, and the source's is the owner's decision about one mailbox.
FIRST_SYNC_DAYS_ENV: Final = "MOTET_GMAIL_FIRST_SYNC_DAYS"


def clamp_first_sync_days(days: int) -> int:
    """A window inside :data:`MAX_FIRST_SYNC_DAYS`, or the default for anything unusable."""
    if days <= 0:
        return DEFAULT_FIRST_SYNC_DAYS
    return min(days, MAX_FIRST_SYNC_DAYS)


def first_sync_days(configured: int | None = None) -> int:
    """How many days back a first sync reaches.

    Precedence is the source's own window, then the deployment's env override, then the
    default — narrowest scope first, which is the order every other configuration in this
    repo reads. ``configured`` is what ``sources.config['first_sync_days']`` holds, and it
    is clamped here rather than trusted, because the column outlives whatever validated it.
    """
    if configured is not None:
        return clamp_first_sync_days(configured)
    raw = os.environ.get(FIRST_SYNC_DAYS_ENV, "").strip()
    if not raw:
        return DEFAULT_FIRST_SYNC_DAYS
    try:
        days = int(raw)
    except ValueError:
        logger.warning(
            "%s=%r is not an integer; using %d", FIRST_SYNC_DAYS_ENV, raw, DEFAULT_FIRST_SYNC_DAYS
        )
        return DEFAULT_FIRST_SYNC_DAYS
    return clamp_first_sync_days(days)


#: The ``sources.config`` key a chosen first-sync window lives under. Config rather than a
#: column because it is the same kind of fact as ``query``: the owner's standing instruction
#: about one mailbox, read by the adapter and by nothing else.
FIRST_SYNC_DAYS_CONFIG_KEY: Final = "first_sync_days"


def config_first_sync_days(config: Mapping[str, Any]) -> int | None:
    """The window chosen for this source, or ``None`` when nobody chose one.

    Anything that is not a positive integer reads as "not chosen" rather than raising: this
    is a JSONB document, and a source connected before the key existed simply has none.
    ``bool`` is excluded explicitly because it is an ``int`` in Python and ``True`` days is
    not a window.
    """
    value = config.get(FIRST_SYNC_DAYS_CONFIG_KEY)
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        return None
    return value


#: How far behind the start of the last completed pass the next one begins. Gmail's search
#: index can lag a message's arrival by seconds to minutes, and the pass start is read off
#: this process's clock rather than Google's — so a watermark placed exactly at the pass
#: start could step over a message that arrived just before it and was not yet searchable.
#: An hour covers both with room to spare, and what it re-lists is dropped before a fetch:
#: the poll's pre-check skips a message that already has a row or an extract job.
WATERMARK_OVERLAP_SECONDS: Final = 3600

#: The default Gmail search. Category-based rather than label-based because it needs no
#: setup from the user — Gmail already sorts newsletters into `promotions` and `updates`.
#: Overridable per source in ``sources.config``, which is where a user's own label goes.
DEFAULT_QUERY: Final = "category:updates OR category:promotions"

_SECONDS_PER_DAY: Final = 86_400


def search_query(query: str, *, after: int) -> str:
    """The ``q`` a listing sends: the source's filter, bounded below by a watermark.

    The filter is parenthesised so that the bound applies to the whole of it rather than
    to its last term — a user's own query may carry an ``OR`` of its own. ``after`` is in
    epoch **seconds**: Gmail reads a bare date as midnight Pacific, and only a numeric
    value is exact. (``internalDate``, on a fetched message, is milliseconds; nothing here
    reads it.)
    """
    return f"({query}) after:{after}"


@dataclass(frozen=True)
class _SearchCursor:
    """Where a watermarked search got to — this adapter's half of ``sync_state.cursor``.

    ``after`` is the current pass's lower bound, in epoch seconds. ``started`` and
    ``page_token`` are set only mid-pass: when the pass began (which becomes the next
    watermark once it is exhausted) and the ``nextPageToken`` to continue it from. A cursor
    with neither is a finished pass, and the next poll searches from ``after``.

    JSON with a version, rather than a delimited string, because it is persisted and read
    back by a later release of this code; anything that does not decode — a Gmail history
    id from before listing moved to search, most obviously — is treated as no cursor.
    """

    after: int
    started: int | None = None
    page_token: str | None = None

    def encode(self) -> str:
        body: dict[str, Any] = {"v": 1, "after": self.after}
        if self.started is not None:
            body["started"] = self.started
        if self.page_token is not None:
            body["page_token"] = self.page_token
        return json.dumps(body, separators=(",", ":"), sort_keys=True)

    @classmethod
    def decode(cls, raw: str) -> _SearchCursor | None:
        try:
            body = json.loads(raw)
        except ValueError:
            return None
        if not isinstance(body, dict) or body.get("v") != 1:
            return None
        after, started, token = body.get("after"), body.get("started"), body.get("page_token")
        if not isinstance(after, int) or not _is_epoch(after):
            return None
        started = started if _is_epoch(started) else None
        # A page token belongs to a pass, and a pass without its start could not move the
        # watermark when it ends; dropping the token re-reads that pass from its top.
        token = token if isinstance(token, str) and token and started is not None else None
        return cls(after=after, started=started, page_token=token)


def _is_epoch(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value >= 0


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
        self,
        *,
        redirect_uri: str,
        state: str,
        code_challenge: str,
        scopes: Sequence[str],
        login_hint: str | None = None,
    ) -> str:
        params = {
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
            # Deliberately no `include_granted_scopes` — see the module docstring. It
            # would fold a label-sync grant on one source into every later connect.
        }
        if login_hint:
            # Preselects the mailbox a re-consent is for, so the account chooser does not
            # invite picking a different one. A hint, not a control: the callback checks.
            params["login_hint"] = login_hint
        return f"{self._authorization_endpoint}?{urlencode(params)}"

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
            },
            refreshing=True,
        )

    def _token_request(self, form: dict[str, str], *, refreshing: bool = False) -> TokenGrant:
        response = self._post(self._token_endpoint, form)
        status = response.status_code
        if status == 400 or status == 401:
            # `invalid_grant` is the one that matters: retrying cannot fix it and only
            # re-consent can, so it is a permanent failure rather than a retryable one.
            raise SourceAuthError(
                f"Google rejected the token request ({status}): {_error_detail(response)}. "
                + (
                    REFRESH_REJECTED
                    if refreshing and _error_code(response) == "invalid_grant"
                    else "The mailbox must be reconnected."
                )
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
        clock: Callable[[], float] = time.time,
    ) -> None:
        if not access_token:
            raise GmailConfigError("GmailMailClient needs a resolved access token")
        self._token = access_token
        self._timeout = timeout_seconds
        self._base_url = base_url.rstrip("/")
        self._transport = transport
        # Epoch seconds. Injected so a test can place a pass's start, which is what the
        # watermark is read from.
        self._clock = clock

    def list_messages(
        self, *, query: str, cursor: str | None, limit: int, window_days: int | None = None
    ) -> MessagePage:
        """One page of the source's search, oldest first within the page.

        **Every page is a ``messages.list`` search carrying the source's filter**, first
        sync or not. Incremental polls used to ask the history API instead — cheap, but it
        has no ``q``, so from the second poll on every message added to the mailbox was
        listed whatever the filter said (motet#95). The search is bounded below by a
        watermark, ``after:<epoch seconds>``, so a quiet mailbox still costs one small
        request per poll.

        **A pass is followed to its end, one page per call.** The cursor carries the
        pass's ``nextPageToken`` for as long as there is one, and ``more`` says so; only an
        exhausted pass moves the watermark, and only to where that pass *began*, less
        :data:`WATERMARK_OVERLAP_SECONDS`. The first sync used to read one page and then
        jump to the mailbox's live history id, so whatever that page missed was never
        listed again (motet#94). Nothing can be stepped over now: a page not yet read is a
        page the cursor still points at.

        A first sync — no cursor, or one this adapter did not write, such as a history id
        stored before listing moved to search — is the same search bounded to the last
        :func:`first_sync_days` days, and the page that starts it reports the window it
        chose. An unbounded first sync would ingest an archive.

        ``window_days`` is the window the *source* chose, passed down by the caller that
        read it off ``sources.config``; ``None`` falls back to the deployment's env override
        and then to the default. It is read only on the page that begins a first sync, so
        widening a source's window changes nothing until its cursor is cleared — which is
        exactly what ``POST /v1/sources/{id}/resync`` does (motet#139).
        """
        now = int(self._clock())
        state = _SearchCursor.decode(cursor) if cursor else None
        chosen_days: int | None = None
        if state is None:
            if cursor:
                # Not an error: a source connected before this adapter searched carries a
                # Gmail history id. Re-reading the window is the repair, and the unique
                # index makes whatever it re-lists harmless.
                logger.info(
                    "cursor %r is not a search watermark; starting a bounded first sync",
                    cursor[:40],
                )
            chosen_days = first_sync_days(window_days)
            logger.info("first sync: bounded to the last %d days", chosen_days)
            # Clamped at the epoch: a window wider than 1970 would otherwise write a negative
            # bound, which `decode` refuses — and a cursor that never decodes is a first sync
            # restarted on every poll, re-arming itself for ever.
            state = _SearchCursor(after=max(0, now - chosen_days * _SECONDS_PER_DAY))
        if state.page_token is None:
            state = _SearchCursor(after=state.after, started=now)

        response = self._search(query=query, state=state, limit=limit)
        if response.status_code == 400 and state.page_token is not None:
            # A page token Gmail no longer honours. Re-reading this pass from its first page
            # repeats what the earlier pages listed — which the poll's pre-check drops —
            # and loses nothing, where giving up on the pass would.
            logger.warning(
                "Gmail refused the stored page token (%s); restarting this pass from its "
                "first page",
                _error_detail(response),
            )
            state = _SearchCursor(after=state.after, started=state.started)
            response = self._search(query=query, state=state, limit=limit)
        body = self._json(response, "messages")

        # Reversed: Gmail returns newest first, and ingestion order decides what dedup
        # merges into what. Oldest first means a follow-up folds into the original story
        # rather than the original folding into the follow-up. That holds within a page;
        # across the pages of one long pass the newer page is read first, because Gmail
        # offers no oldest-first search.
        messages = tuple(
            reversed(
                [
                    MessageRef(id=item["id"], thread_id=item.get("threadId"))
                    for item in (body.get("messages") or [])
                    if isinstance(item.get("id"), str)
                ]
            )
        )

        # Whether another page exists is Gmail's `nextPageToken` and nothing else. A page
        # can come back *short* of `maxResults` with more behind it, so "fewer than asked
        # for" proves nothing.
        page_token = body.get("nextPageToken")
        started = state.started if state.started is not None else now
        if isinstance(page_token, str) and page_token:
            next_state = _SearchCursor(after=state.after, started=started, page_token=page_token)
        else:
            next_state = _SearchCursor(after=max(state.after, started - WATERMARK_OVERLAP_SECONDS))
        return MessagePage(
            messages=messages,
            cursor=next_state.encode(),
            more=next_state.page_token is not None,
            first_sync_days=chosen_days,
        )

    def _search(self, *, query: str, state: _SearchCursor, limit: int) -> Any:
        params = {"q": search_query(query, after=state.after), "maxResults": str(limit)}
        if state.page_token is not None:
            params["pageToken"] = state.page_token
        return self._get(f"{self._base_url}/users/me/messages", params)

    def mailbox_address(self) -> str | None:
        """``users.getProfile``'s ``emailAddress``: which mailbox this token reaches."""
        body = self._json(self._get(f"{self._base_url}/users/me/profile", {}), "profile")
        address = body.get("emailAddress")
        return str(address) if isinstance(address, str) and address else None

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

    def list_labels(self) -> tuple[Label, ...]:
        """Every label in the mailbox, system and user alike. One request, readonly scope."""
        body = self._json(self._get(f"{self._base_url}/users/me/labels", {}), "labels")
        return tuple(
            Label(
                id=item["id"],
                name=str(item.get("name") or item["id"]),
                system=item.get("type") == "system",
            )
            for item in (body.get("labels") or [])
            if isinstance(item, dict) and isinstance(item.get("id"), str)
        )

    def modify_labels(self, message_id: str, *, add: Sequence[str], remove: Sequence[str]) -> None:
        """``users.messages.modify`` on one message — the connector's only write.

        The *message*, not the thread: ``source_items.external_id`` is a message id, and
        Gmail shows a thread's labels as the union of its messages', which is what a person
        expects to see. 400 and 404 both become :class:`StaleReferenceError`: Gmail answers
        an unknown label id with the first and an unknown message with the second, and the
        error says which, because only the first is repaired by re-reading the labels.

        **The last guard on what may be written.** An id outside
        ``labels.WRITABLE_SYSTEM_LABELS`` among Gmail's system labels is refused here,
        before any request, whatever the caller resolved — so no bug upstream of this line
        can move a message into ``TRASH`` or ``SPAM``.
        """
        from .labels import FORBIDDEN_LABEL_IDS  # noqa: PLC0415 — labels imports interfaces only

        forbidden = sorted({*add, *remove} & FORBIDDEN_LABEL_IDS)
        if forbidden:
            raise SourceError(f"refusing to change system label(s) {', '.join(forbidden)}")
        response = self._post_json(
            f"{self._base_url}/users/me/messages/{message_id}/modify",
            {"addLabelIds": list(add), "removeLabelIds": list(remove)},
        )
        if response.status_code in (400, 404):
            raise StaleReferenceError(
                f"Gmail did not recognise message {message_id} or one of its label ids "
                f"({response.status_code}): {_error_detail(response)}",
                target="message" if response.status_code == 404 else "label",
            )
        self._json(response, f"a label change on message {message_id}")

    def _get(self, url: str, params: dict[str, str]) -> Any:
        headers = {"Authorization": f"Bearer {self._token}", "Accept": "application/json"}
        if self._transport is not None:
            return self._transport.get(url, params=params, headers=headers)
        import httpx  # noqa: PLC0415

        with httpx.Client(timeout=self._timeout) as client:
            return client.get(url, params=params, headers=headers)

    def _post_json(self, url: str, body: dict[str, Any]) -> Any:
        headers = {"Authorization": f"Bearer {self._token}", "Accept": "application/json"}
        if self._transport is not None:
            return self._transport.post(url, json=body, headers=headers)
        import httpx  # noqa: PLC0415

        with httpx.Client(timeout=self._timeout) as client:
            return client.post(url, json=body, headers=headers)

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


#: What to say when Google refuses a *refresh* with ``invalid_grant``.
#:
#: The error means one grant is dead and says nothing about which of three things killed
#: it, so the message names all three rather than asserting the likeliest. The third is
#: the one that cost an afternoon and is invisible from inside this repo: **an OAuth
#: client left in Google's "Testing" publishing status expires every refresh token it has
#: issued after roughly seven days.** A deployment driven by an automated loop therefore
#: breaks on a weekly cadence, with a `400 invalid_grant` and nothing anywhere pointing at
#: the console setting that caused it — the publishing status is a fact about the OAuth
#: client, which lives in the private infrastructure repo and which nothing here can read.
#:
#: The carve-out that makes people discount this does not apply to Motet: Google exempts
#: clients requesting only name, email and profile, and Motet asks for ``gmail.readonly``.
REFRESH_REJECTED: Final = (
    "The mailbox must be reconnected. Google refuses a refresh with 'invalid_grant' for "
    "three different reasons and does not say which: access was revoked, the token went "
    "unused for six months, or this OAuth client is still in 'Testing' publishing status, "
    "which expires every refresh token it issued after about seven days. Check the "
    "publishing status first if this recurs on a weekly cadence."
)


def _error_code(response: Any) -> str:
    """The OAuth 2 ``error`` field, which :func:`_error_detail` deliberately passes over.

    That function prefers ``error_description`` because it is what a person reads; this one
    wants the machine-readable code, because ``invalid_grant`` is the one value worth
    branching on and the description is free-form prose Google has changed before.
    """
    try:
        body = response.json()
    except Exception:  # noqa: BLE001 — an error body is frequently not JSON
        return ""
    return str(body.get("error", "")) if isinstance(body, dict) else ""


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
