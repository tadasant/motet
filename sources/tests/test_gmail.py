"""The real Gmail adapter's listing, over a stub of Gmail's REST surface.

What these pin is the bytes the adapter puts on the wire and what it makes of the answer —
the half a :class:`~motet_sources.FakeMailClient` cannot cover, because the fake *is* the
adapter's shape rather than a test of it. Two defects live here: a first sync that read one
page and then jumped past the rest (motet#94), and incremental polls that asked the history
API, which has no ``q`` and so ignored the source's filter (motet#95).

Every message is synthesized: an id, a timestamp, and the filter terms it matches. This
repository is public, and no real mailbox content, address or id belongs in it.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any

import pytest
from motet_sources import GmailMailClient, SourceError
from motet_sources.gmail import (
    DEFAULT_FIRST_SYNC_DAYS,
    FIRST_SYNC_DAYS_ENV,
    MAX_FIRST_SYNC_DAYS,
    WATERMARK_OVERLAP_SECONDS,
)

NOW = 1_800_000_000  # epoch seconds; a fixed clock, so every bound is exact
DAY = 86_400
FILTER = "label:newsletters OR category:updates"


@dataclass
class _Response:
    status_code: int
    body: Any

    @property
    def text(self) -> str:
        return json.dumps(self.body)

    def json(self) -> Any:
        return self.body


@dataclass
class Mail:
    """One synthesized message: an id, when Gmail received it, and what it matches."""

    id: str
    at: int
    terms: frozenset[str] = frozenset({"label:newsletters"})


@dataclass
class StubGmail:
    """``users.messages.list`` over a synthesized mailbox, and nothing else.

    Evaluates ``(<a OR b ...>) after:<seconds>`` — the only shape the adapter sends — newest
    first, paged by an offset token. ``page_size`` below the caller's ``maxResults`` gives
    the short-page-with-more-behind-it case Gmail is documented to produce. Any other
    endpoint is a 404, so a request to the history API or the profile shows up as a failure
    rather than as a silent pass.
    """

    mail: list[Mail] = field(default_factory=list)
    page_size: int = 500
    refuse_page_tokens: bool = False
    requests: list[tuple[str, dict[str, str]]] = field(default_factory=list)

    def get(self, url: str, *, params: dict[str, str], headers: dict[str, str]) -> _Response:
        self.requests.append((url, dict(params)))
        if not url.endswith("/users/me/messages"):
            return _Response(404, {"error": {"message": f"not stubbed: {url}"}})
        token = params.get("pageToken")
        if token is not None and self.refuse_page_tokens:
            return _Response(400, {"error": {"message": "Invalid pageToken"}})
        match = re.fullmatch(r"\((?P<filter>.+)\) after:(?P<after>\d+)", params["q"])
        assert match, f"unexpected query shape: {params['q']!r}"
        terms = {term.strip() for term in match["filter"].split(" OR ")}
        after = int(match["after"])
        hits = sorted(
            (m for m in self.mail if m.at > after and m.terms & terms),
            key=lambda m: m.at,
            reverse=True,
        )
        offset = int(token) if token else 0
        size = min(int(params["maxResults"]), self.page_size)
        page = hits[offset : offset + size]
        body: dict[str, Any] = {
            "messages": [{"id": m.id, "threadId": m.id} for m in page],
            "resultSizeEstimate": len(hits),
        }
        if offset + size < len(hits):
            body["nextPageToken"] = str(offset + size)
        return _Response(200, body)

    @property
    def queries(self) -> list[str]:
        return [params["q"] for _, params in self.requests]


def client(stub: StubGmail, *, now: int = NOW) -> GmailMailClient:
    return GmailMailClient("token", transport=stub, clock=lambda: float(now))


def backlog(count: int, *, newest: int = NOW - 60) -> list[Mail]:
    """``count`` newsletters, one a minute, all inside the first-sync window."""
    return [Mail(id=f"nl_{i:04d}", at=newest - i * 60) for i in range(count)]


def drain(
    adapter: GmailMailClient, cursor: str | None, *, limit: int = 50
) -> tuple[list[str], str]:
    """Follow one search to its end the way a chain of polls does, and return what it listed."""
    listed: list[str] = []
    while True:
        page = adapter.list_messages(query=FILTER, cursor=cursor, limit=limit)
        listed.extend(message.id for message in page.messages)
        assert page.cursor is not None
        cursor = page.cursor
        if not page.more:
            return listed, cursor


# --- the first sync -----------------------------------------------------------------------


def test_a_first_sync_is_the_filter_bounded_to_the_window(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv(FIRST_SYNC_DAYS_ENV, raising=False)
    stub = StubGmail(mail=backlog(3))
    page = client(stub).list_messages(query=FILTER, cursor=None, limit=50)

    assert stub.queries == [f"({FILTER}) after:{NOW - DEFAULT_FIRST_SYNC_DAYS * DAY}"]
    assert stub.requests[0][1]["maxResults"] == "50"
    assert page.first_sync_days == DEFAULT_FIRST_SYNC_DAYS, "the window is reported"
    # Oldest first within the page, which is what dedup's merge direction wants.
    assert [m.id for m in page.messages] == ["nl_0002", "nl_0001", "nl_0000"]
    assert page.more is False


def test_the_window_is_a_setting(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(FIRST_SYNC_DAYS_ENV, "60")
    stub = StubGmail()
    page = client(stub).list_messages(query=FILTER, cursor=None, limit=50)
    assert stub.queries == [f"({FILTER}) after:{NOW - 60 * DAY}"]
    assert page.first_sync_days == 60


def test_a_window_wider_than_the_ceiling_is_clamped(monkeypatch: pytest.MonkeyPatch) -> None:
    """An absurd window is bounded rather than honoured, whoever asked for it.

    A typo'd env value or a stored ``first_sync_days`` is the realistic source of one, and
    an unbounded search is a crawl of the whole archive — so both go through the same
    ceiling, and the page reports what it actually used rather than what it was asked for.
    """
    monkeypatch.setenv(FIRST_SYNC_DAYS_ENV, "40000")
    stub = StubGmail()
    page = client(stub).list_messages(query=FILTER, cursor=None, limit=50)
    assert stub.queries == [f"({FILTER}) after:{NOW - MAX_FIRST_SYNC_DAYS * DAY}"]
    assert page.first_sync_days == MAX_FIRST_SYNC_DAYS

    stub = StubGmail()
    asked = client(stub).list_messages(
        query=FILTER, cursor=None, limit=50, window_days=MAX_FIRST_SYNC_DAYS * 10
    )
    assert asked.first_sync_days == MAX_FIRST_SYNC_DAYS


def test_a_window_reaching_past_the_epoch_is_clamped_and_its_cursor_still_resumes() -> None:
    """A negative bound would never decode, and a first sync would restart on every poll.

    The ceiling above makes this unreachable on any real clock — ten years before now is
    comfortably after 1970 — so it is driven by a clock young enough for the window to
    outrun it, which is the only way the floor can still be observed.
    """
    young = MAX_FIRST_SYNC_DAYS * DAY - 60  # the window is wider than this clock's "now"
    stub = StubGmail(mail=[Mail(id=f"nl_{i:04d}", at=young - 60 - i * 60) for i in range(80)])
    first = client(stub, now=young).list_messages(
        query=FILTER, cursor=None, limit=50, window_days=MAX_FIRST_SYNC_DAYS
    )
    assert stub.queries == [f"({FILTER}) after:0"]
    assert first.more is True

    second = client(stub, now=young).list_messages(query=FILTER, cursor=first.cursor, limit=50)
    assert second.first_sync_days is None, "resumed, not restarted"
    assert stub.requests[-1][1]["pageToken"] == "50"


def test_the_source_s_own_window_outranks_the_deployment_s(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """motet#139: the window is per mailbox, and the env is only the fallback.

    Narrowest scope wins — the owner chose this mailbox's window, and a deployment-wide
    variable must not quietly override a choice made on a connect screen.
    """
    monkeypatch.setenv(FIRST_SYNC_DAYS_ENV, "7")
    stub = StubGmail()
    page = client(stub).list_messages(query=FILTER, cursor=None, limit=50, window_days=90)
    assert stub.queries == [f"({FILTER}) after:{NOW - 90 * DAY}"]
    assert page.first_sync_days == 90


def test_the_window_is_read_only_where_a_search_begins() -> None:
    """A wider window mid-search changes nothing — which is why a resync drops the cursor.

    This is the property that makes ``POST /v1/sources/{id}/resync`` necessary rather than
    a convenience: without it, widening a source's window would be a setting that silently
    did nothing for ever.
    """
    stub = StubGmail(mail=backlog(80))
    first = client(stub).list_messages(query=FILTER, cursor=None, limit=50, window_days=7)
    assert first.more is True
    resumed = client(stub).list_messages(
        query=FILTER, cursor=first.cursor, limit=50, window_days=3650
    )
    assert resumed.first_sync_days is None, "mid-pass: still the window the pass began with"
    assert stub.queries[-1] == f"({FILTER}) after:{NOW - 7 * DAY}"


@pytest.mark.parametrize("raw", ["", "soon", "0", "-3"])
def test_an_unusable_window_falls_back_to_the_default(
    monkeypatch: pytest.MonkeyPatch, raw: str
) -> None:
    monkeypatch.setenv(FIRST_SYNC_DAYS_ENV, raw)
    page = client(StubGmail()).list_messages(query=FILTER, cursor=None, limit=50)
    assert page.first_sync_days == DEFAULT_FIRST_SYNC_DAYS


def test_the_watermark_is_in_seconds_not_milliseconds() -> None:
    """Gmail's ``after:`` takes epoch seconds; ``internalDate`` is milliseconds.

    A millisecond value would be a date some fifty thousand years out, and the search would
    match nothing — silently, which is the failure worth a test.
    """
    stub = StubGmail()
    client(stub).list_messages(query=FILTER, cursor=None, limit=50)
    after = int(stub.queries[0].rsplit("after:", 1)[1])
    assert after == NOW - DEFAULT_FIRST_SYNC_DAYS * DAY
    assert NOW - (DEFAULT_FIRST_SYNC_DAYS + 1) * DAY < after < NOW


# --- motet#94: a search longer than a page ---------------------------------------------


def test_every_page_of_a_long_search_is_listed_exactly_once() -> None:
    """The defect: 170 matched, one page of 50 was read, and the rest were never listed.

    Pages here come back *short* — 40 against a ``maxResults`` of 50 — with more behind
    them, so "fewer than asked for" is exercised as the non-proof it is.
    """
    stub = StubGmail(mail=backlog(170), page_size=40)
    listed, _ = drain(client(stub), None)

    assert len(listed) == 170
    assert set(listed) == {f"nl_{i:04d}" for i in range(170)}
    assert len(stub.requests) == 5, "170 at 40 a page"
    assert all(url.endswith("/users/me/messages") for url, _ in stub.requests), (
        "no history, no profile: nothing jumps the cursor"
    )


def test_a_cursor_mid_search_points_at_the_next_page_not_past_it() -> None:
    stub = StubGmail(mail=backlog(120))
    adapter = client(stub)
    first = adapter.list_messages(query=FILTER, cursor=None, limit=50)
    assert first.more is True
    assert first.cursor is not None
    assert json.loads(first.cursor)["page_token"] == "50"

    # A later poll, possibly minutes later, resumes the same pass rather than a new one.
    later = client(stub, now=NOW + 600)
    second = later.list_messages(query=FILTER, cursor=first.cursor, limit=50)
    assert stub.requests[-1][1]["pageToken"] == "50"
    assert stub.requests[-1][1]["q"] == stub.requests[0][1]["q"], "the same pass, same bound"
    assert not {m.id for m in first.messages} & {m.id for m in second.messages}


def test_an_exhausted_pass_moves_the_watermark_to_where_it_began_less_the_overlap() -> None:
    stub = StubGmail(mail=backlog(60))
    adapter = client(stub)
    first = adapter.list_messages(query=FILTER, cursor=None, limit=50)
    # The pass began at NOW; finishing it later must not move the watermark to the later time.
    last = client(stub, now=NOW + 900).list_messages(query=FILTER, cursor=first.cursor, limit=50)
    assert last.more is False
    assert last.cursor is not None
    assert json.loads(last.cursor) == {"after": NOW - WATERMARK_OVERLAP_SECONDS, "v": 1}


def test_a_refused_page_token_restarts_the_pass_rather_than_abandoning_it(
    caplog: pytest.LogCaptureFixture,
) -> None:
    stub = StubGmail(mail=backlog(80))
    first = client(stub).list_messages(query=FILTER, cursor=None, limit=50)
    stub.refuse_page_tokens = True

    page = client(stub).list_messages(query=FILTER, cursor=first.cursor, limit=50)

    assert "pageToken" not in stub.requests[-1][1], "re-read from the pass's first page"
    assert stub.requests[-1][1]["q"] == stub.requests[0][1]["q"], "with the pass's own bound"
    assert len(page.messages) == 50
    assert "restarting this pass" in caplog.text


def test_a_400_on_a_first_page_is_an_error_not_a_restart() -> None:
    class Rejecting(StubGmail):
        def get(self, url: str, *, params: dict[str, str], headers: dict[str, str]) -> _Response:
            self.requests.append((url, dict(params)))
            return _Response(400, {"error": {"message": "Invalid query"}})

    stub = Rejecting()
    with pytest.raises(SourceError, match="400"):
        client(stub).list_messages(query=FILTER, cursor=None, limit=50)
    assert len(stub.requests) == 1


# --- motet#95: the filter on every poll ------------------------------------------------


def test_an_incremental_poll_sends_the_filter_and_skips_what_it_excludes() -> None:
    """The defect: after the first sync, a billing receipt in the inbox was ingested.

    Incremental polls asked ``users.history.list``, which has no ``q``, so everything added
    to the mailbox was listed. Now the incremental poll is the same search, and the receipt
    does not match it.
    """
    stub = StubGmail(mail=backlog(2))
    _, caught_up = drain(client(stub), None)

    later = NOW + 3_600
    stub.mail += [
        Mail(id="receipt_0001", at=later - 30, terms=frozenset({"in:inbox"})),
        Mail(id="nl_new_0001", at=later - 20),
    ]
    page = client(stub, now=later).list_messages(query=FILTER, cursor=caught_up, limit=50)

    assert stub.queries[-1] == f"({FILTER}) after:{NOW - WATERMARK_OVERLAP_SECONDS}"
    listed = {m.id for m in page.messages}
    assert "nl_new_0001" in listed
    assert "receipt_0001" not in listed


def test_the_filter_is_parenthesised_so_the_bound_covers_all_of_it() -> None:
    stub = StubGmail()
    client(stub).list_messages(
        query="from:a@example.test OR from:b@example.test", cursor=None, limit=5
    )
    assert stub.queries[0].startswith("(from:a@example.test OR from:b@example.test) after:")


# --- cursors this adapter did not write ------------------------------------------------


@pytest.mark.parametrize(
    "stored",
    [
        "10892907",  # a Gmail history id, from before listing moved to search
        "{",  # unreadable
        '{"v": 2, "after": 1}',  # a version this code does not know
        '{"v": 1, "after": "yesterday"}',
    ],
)
def test_a_cursor_that_is_not_a_search_watermark_is_a_bounded_first_sync(
    stored: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A source connected before this change carries a history id. It is re-read, not lost.

    Re-reading the window re-lists what that source already has, which the poll's pre-check
    and the ``(source_id, external_id)`` index both absorb — and recovers what its first sync
    silently dropped, for as far back as the window reaches.
    """
    monkeypatch.delenv(FIRST_SYNC_DAYS_ENV, raising=False)
    stub = StubGmail(mail=backlog(3))
    page = client(stub).list_messages(query=FILTER, cursor=stored, limit=50)
    assert stub.queries == [f"({FILTER}) after:{NOW - DEFAULT_FIRST_SYNC_DAYS * DAY}"]
    assert page.first_sync_days == DEFAULT_FIRST_SYNC_DAYS
    assert len(page.messages) == 3


def test_a_page_token_without_its_pass_start_re_reads_the_pass() -> None:
    stub = StubGmail(mail=backlog(3))
    cursor = json.dumps({"v": 1, "after": NOW - DAY, "page_token": "50"})
    page = client(stub).list_messages(query=FILTER, cursor=cursor, limit=50)
    assert "pageToken" not in stub.requests[0][1]
    assert page.first_sync_days is None, "a known watermark, so not a first sync"
