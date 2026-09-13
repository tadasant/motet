"""Label sync's pure half, and the Gmail adapter's two label calls — motet#96.

The settings and the name-to-id resolution are plain functions over dicts, so every rule
about which labels may be touched is pinned here without a database or a mailbox. The
adapter's two calls are driven over a stub transport that records the request, because the
claim worth making about them is about the bytes sent — the endpoint, the body, which ids
went where — and a fake would only be testing itself.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any
from urllib.parse import parse_qs, urlparse

import pytest
from motet_sources import (
    FAKE_LABELS,
    GMAIL_MODIFY_SCOPE,
    GMAIL_READONLY_SCOPE,
    LABEL_SYNC_SCOPES,
    FakeMailClient,
    GmailMailClient,
    GmailOAuthClient,
    Label,
    LabelSettings,
    LabelSettingsError,
    SourceAuthError,
    SourceError,
    StaleReferenceError,
)
from motet_sources.labels import (
    CATALOG_KEY,
    CONFIG_KEY,
    catalog_fetched_at,
    catalog_from_sync_state,
    catalog_to_sync_state,
    pickable,
    resolve,
)

# --- settings --------------------------------------------------------------------------


def test_either_label_may_be_empty_but_not_both() -> None:
    assert LabelSettings.parse(remove="Newsletters", add=None) == LabelSettings(
        remove="Newsletters"
    )
    assert LabelSettings.parse(remove="  ", add=" Completed ") == LabelSettings(add="Completed")
    # Both empty is "turn it off", not an error.
    assert LabelSettings.parse(remove="", add=None) is None


def test_a_label_that_could_hide_mail_is_refused_at_input() -> None:
    """TRASH and SPAM are writable under `gmail.modify`; Motet never writes them."""
    for name in ("TRASH", "spam", "Sent", "CATEGORY_PROMOTIONS"):
        with pytest.raises(LabelSettingsError, match="system label"):
            LabelSettings.parse(remove=None, add=name)
    # INBOX is allowed, because removing it is archiving — the Inbox → Archive workflow.
    assert LabelSettings.parse(remove="INBOX", add=None) == LabelSettings(remove="INBOX")


def test_removing_and_adding_the_same_label_is_refused() -> None:
    with pytest.raises(LabelSettingsError, match="same"):
        LabelSettings.parse(remove="Completed", add="completed")


def test_a_name_longer_than_gmail_allows_is_refused() -> None:
    with pytest.raises(LabelSettingsError, match="225"):
        LabelSettings.parse(remove=None, add="x" * 226)


def test_settings_round_trip_through_config_and_read_leniently() -> None:
    chosen = LabelSettings(remove="Newsletters", add="Completed")
    assert LabelSettings.from_config({CONFIG_KEY: chosen.to_config()}) == chosen
    # A malformed row turns the feature off rather than crashing the poll or the list.
    assert LabelSettings.from_config({}) is None
    assert LabelSettings.from_config({CONFIG_KEY: "Completed"}) is None
    assert LabelSettings.from_config({CONFIG_KEY: {"add": 3, "remove": None}}) is None


# --- the catalog and resolution --------------------------------------------------------


def test_the_catalog_round_trips_through_sync_state() -> None:
    at = datetime(2026, 9, 13, 4, 0, tzinfo=UTC)
    cached = {CATALOG_KEY: catalog_to_sync_state(FAKE_LABELS, fetched_at=at)}
    assert catalog_from_sync_state(cached) == FAKE_LABELS
    assert catalog_fetched_at(cached) == at
    assert catalog_from_sync_state({}) == ()
    assert catalog_from_sync_state({CATALOG_KEY: {"labels": "nope"}}) == ()


def test_resolution_maps_names_to_this_mailboxs_ids() -> None:
    resolved = resolve(LabelSettings(remove="Newsletters", add="Completed"), FAKE_LABELS)
    assert resolved.remove == ("Label_101",)
    assert resolved.add == ("Label_102",)
    assert resolved.missing == () and resolved.refused == ()


def test_resolution_is_case_insensitive_like_gmail() -> None:
    resolved = resolve(LabelSettings(remove="Inbox", add="completed"), FAKE_LABELS)
    assert resolved.remove == ("INBOX",)
    assert resolved.add == ("Label_102",)


def test_a_name_the_mailbox_does_not_have_is_missing_not_guessed() -> None:
    resolved = resolve(LabelSettings(remove="Newsletters", add="Archive"), FAKE_LABELS)
    assert resolved.missing == ("Archive",)
    assert resolved.add == ()


def test_resolution_refuses_a_system_label_a_stored_row_names_anyway() -> None:
    """The second guard: a row written some other way still cannot move mail into TRASH."""
    resolved = resolve(LabelSettings(add="TRASH"), FAKE_LABELS)
    assert resolved.refused == ("TRASH",)
    assert resolved.add == ()


def test_trash_is_refused_even_when_the_catalog_calls_it_a_user_label() -> None:
    """A catalog is data a poll wrote; the id decides, not the cached flag."""
    catalog = (Label("TRASH", "TRASH", system=False), Label("SPAM", "Junk", system=False))
    assert resolve(LabelSettings(add="TRASH"), catalog).refused == ("TRASH",)
    assert resolve(LabelSettings(remove="Junk"), catalog).refused == ("Junk",)


def test_the_pickers_offer_user_labels_then_the_writable_system_ones() -> None:
    assert pickable(FAKE_LABELS) == [
        "Completed",
        "Newsletters",
        "Reading/Later",
        "IMPORTANT",
        "INBOX",
        "STARRED",
        "UNREAD",
    ]


# --- the fake mailbox ------------------------------------------------------------------


def test_the_fake_records_a_modify_and_answers_unknown_ids_like_gmail() -> None:
    mailbox = FakeMailClient()
    message_id = mailbox.messages[0].id
    mailbox.modify_labels(message_id, add=["Label_102"], remove=["Label_101"])
    assert mailbox.modify_calls == [(message_id, ("Label_102",), ("Label_101",))]

    with pytest.raises(StaleReferenceError, match="Invalid label"):
        mailbox.modify_labels(message_id, add=["Label_999"], remove=[])
    with pytest.raises(StaleReferenceError, match="not found"):
        mailbox.modify_labels("no-such-message", add=["Label_102"], remove=[])
    assert len(mailbox.modify_calls) == 1, "a refused call is not recorded as made"


# --- the real adapter, over a stub transport ------------------------------------------


@dataclass
class _Response:
    status_code: int
    body: Any = None

    def json(self) -> Any:
        return self.body

    @property
    def text(self) -> str:
        return str(self.body)


@dataclass
class _Transport:
    """Records every request and answers with the next canned response."""

    responses: list[_Response]
    requests: list[tuple[str, str, dict[str, Any]]] = field(default_factory=list)

    def get(self, url: str, **kwargs: Any) -> _Response:
        self.requests.append(("GET", url, kwargs))
        return self.responses.pop(0)

    def post(self, url: str, **kwargs: Any) -> _Response:
        self.requests.append(("POST", url, kwargs))
        return self.responses.pop(0)


def test_listing_labels_reads_names_and_types() -> None:
    transport = _Transport(
        [
            _Response(
                200,
                {
                    "labels": [
                        {"id": "INBOX", "name": "INBOX", "type": "system"},
                        {"id": "Label_7", "name": "Newsletters", "type": "user"},
                    ]
                },
            )
        ]
    )
    labels = GmailMailClient("tok", transport=transport).list_labels()
    assert labels == (Label("INBOX", "INBOX", system=True), Label("Label_7", "Newsletters"))
    method, url, kwargs = transport.requests[0]
    assert (method, url) == ("GET", "https://gmail.googleapis.com/gmail/v1/users/me/labels")
    assert kwargs["headers"]["Authorization"] == "Bearer tok"


def test_modify_posts_the_label_ids_to_the_message() -> None:
    transport = _Transport([_Response(200, {"id": "m1", "labelIds": ["Label_2"]})])
    GmailMailClient("tok", transport=transport).modify_labels(
        "m1", add=["Label_2"], remove=["Label_1"]
    )
    method, url, kwargs = transport.requests[0]
    assert method == "POST"
    assert url == "https://gmail.googleapis.com/gmail/v1/users/me/messages/m1/modify"
    assert kwargs["json"] == {"addLabelIds": ["Label_2"], "removeLabelIds": ["Label_1"]}


@pytest.mark.parametrize("status", [400, 404])
def test_an_unknown_message_or_label_is_a_stale_reference(status: int) -> None:
    transport = _Transport([_Response(status, {"error": {"message": "Invalid label"}})])
    with pytest.raises(StaleReferenceError):
        GmailMailClient("tok", transport=transport).modify_labels("m1", add=["X"], remove=[])


def test_a_grant_without_the_scope_is_an_auth_error() -> None:
    """Gmail's answer to a read-only token asking to modify: 403 insufficient permissions."""
    transport = _Transport([_Response(403, {"error": {"message": "Insufficient Permission"}})])
    with pytest.raises(SourceAuthError, match="403"):
        GmailMailClient("tok", transport=transport).modify_labels("m1", add=["X"], remove=[])


def test_an_outage_is_a_retryable_source_error_not_a_stale_one() -> None:
    transport = _Transport([_Response(503, "unavailable")])
    with pytest.raises(SourceError) as raised:
        GmailMailClient("tok", transport=transport).modify_labels("m1", add=["X"], remove=[])
    assert not isinstance(raised.value, StaleReferenceError | SourceAuthError)


# --- the scope: opt-in, and only for label sync ---------------------------------------


def _scopes_in(url: str) -> list[str]:
    return parse_qs(urlparse(url).query)["scope"][0].split()


def test_the_label_sync_consent_asks_for_modify_and_keeps_readonly() -> None:
    client = GmailOAuthClient(client_id="id", client_secret="secret")
    url = client.authorization_url(
        redirect_uri="https://app.example.invalid/oauth/callback",
        state="s",
        code_challenge="c",
        scopes=LABEL_SYNC_SCOPES,
        login_hint="owner@example.invalid",
    )
    query = parse_qs(urlparse(url).query)
    assert _scopes_in(url) == [GMAIL_READONLY_SCOPE, GMAIL_MODIFY_SCOPE]
    # Preselects the account the source already reads; the worker is what checks it.
    assert query["login_hint"] == ["owner@example.invalid"]


def test_no_authorization_folds_in_scopes_granted_to_another_source() -> None:
    """Without `include_granted_scopes`, a token carries what its own consent asked for.

    With it, re-consenting one mailbox for label sync would hand `gmail.modify` to the next
    mailbox connected on the same Google account, with no consent for that source.
    """
    client = GmailOAuthClient(client_id="id", client_secret="secret")
    for scopes in ((GMAIL_READONLY_SCOPE,), LABEL_SYNC_SCOPES):
        url = client.authorization_url(
            redirect_uri="https://app.example.invalid/oauth/callback",
            state="s",
            code_challenge="c",
            scopes=scopes,
        )
        query = parse_qs(urlparse(url).query)
        assert "include_granted_scopes" not in query
        assert "login_hint" not in query


def test_the_adapter_refuses_a_forbidden_system_label_before_any_request() -> None:
    """The last of three guards: no bug upstream of the adapter can reach TRASH or SPAM."""
    transport = _Transport([])
    client = GmailMailClient("tok", transport=transport)
    for add, remove in ((["TRASH"], []), ([], ["SPAM"]), (["Label_1", "SENT"], [])):
        with pytest.raises(SourceError, match="refusing"):
            client.modify_labels("m1", add=add, remove=remove)
    assert transport.requests == []


def test_a_404_names_the_message_and_a_400_the_label() -> None:
    """Only the second is repaired by re-reading the labels, so the caller has to know."""
    missing = _Transport([_Response(404, {"error": {"message": "Not Found"}})])
    with pytest.raises(StaleReferenceError) as raised:
        GmailMailClient("tok", transport=missing).modify_labels("m1", add=["X"], remove=[])
    assert raised.value.target == "message"
    invalid = _Transport([_Response(400, {"error": {"message": "Invalid label"}})])
    with pytest.raises(StaleReferenceError) as raised:
        GmailMailClient("tok", transport=invalid).modify_labels("m1", add=["X"], remove=[])
    assert raised.value.target == "label"


def test_the_mailbox_address_is_read_from_the_profile() -> None:
    transport = _Transport([_Response(200, {"emailAddress": "owner@example.invalid"})])
    assert GmailMailClient("tok", transport=transport).mailbox_address() == "owner@example.invalid"
    assert transport.requests[0][1].endswith("/users/me/profile")
