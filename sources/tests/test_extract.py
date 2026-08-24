"""Newsletter extraction, against the shapes real newsletters actually arrive in.

This is the module with the most tests in the Gmail path, because it is the one that can
be *wrong* — everything above it is plumbing. The failures worth defending against are
not crashes; they are silent quality losses:

* a hidden preheader read aloud as the opening sentence,
* an unsubscribe footer becoming a claim in a briefing,
* a control character where an em dash should be, travelling into TTS,
* an attachment's text spliced into the middle of a story,
* a receipt ingested as a newsletter.
"""

from __future__ import annotations

import base64
import quopri

import pytest
from motet_sources import (
    MIN_TEXT_CHARS,
    ExtractionError,
    extract_newsletter,
    html_to_text,
    load_fixture_messages,
)

BODY = (
    "Northwind Ventures led a $20M round in Acme, the company said on Tuesday.\n\n"
    "The round values Acme at $180M post-money and brings total funding to $26M. "
    "Chief executive Dana Reyes said the money will go toward hiring.\n"
)


def message(
    *,
    subject: str = "A newsletter",
    body: str = BODY,
    content_type: str = 'text/plain; charset="utf-8"',
    encoding: str | None = None,
    extra_headers: str = "",
) -> bytes:
    payload = body.encode()
    if encoding == "quoted-printable":
        payload = quopri.encodestring(payload)
    elif encoding == "base64":
        payload = base64.b64encode(payload)
    header = (
        f"From: News <news@example.com>\r\n"
        f"To: reader@example.com\r\n"
        f"Subject: {subject}\r\n"
        f"Date: Tue, 18 Aug 2026 07:02:11 +0000\r\n"
        f"Message-ID: <test-1@example.com>\r\n"
        f"MIME-Version: 1.0\r\n"
        f"Content-Type: {content_type}\r\n"
        + (f"Content-Transfer-Encoding: {encoding}\r\n" if encoding else "")
        + extra_headers
        + "\r\n"
    )
    return header.encode() + payload


# --- the fixtures the fake mailbox serves --------------------------------------------


def test_every_fixture_message_parses_or_is_deliberately_refused() -> None:
    """The fake mailbox's fixtures are the contract, so they are asserted as a set.

    One of them is a receipt, which *must* be refused — a fixture set where everything
    succeeds would not cover the "this is not a newsletter" path that a real mailbox hits
    constantly.
    """
    refused = 0
    extracted = 0
    for raw in load_fixture_messages():
        try:
            item = extract_newsletter(raw.raw)
        except ExtractionError:
            refused += 1
            continue
        extracted += 1
        assert item.title
        assert len(item.text) >= MIN_TEXT_CHARS
    assert extracted >= 3, "the fixture mailbox should carry several real newsletters"
    assert refused == 1, "exactly one fixture is a receipt, and it must be refused"


def test_extraction_is_deterministic() -> None:
    """Same bytes, same characters — or every span into the result is a lottery."""
    for raw in load_fixture_messages():
        try:
            first = extract_newsletter(raw.raw)
        except ExtractionError:
            continue
        assert extract_newsletter(raw.raw) == first


def test_a_hidden_preheader_is_not_extracted() -> None:
    """Fixture 02's preheader is invisible on screen and must be invisible here.

    Without this the briefing opens with a sentence written for the inbox list view, which
    reads as a non-sequitur and is exactly the kind of thing nobody notices until they
    hear it.
    """
    wire = next(m for m in load_fixture_messages() if "weekly_wire" in m.id)
    text = extract_newsletter(wire.raw).text
    assert "Chip tariffs" not in text
    assert "Northbridge Semiconductor cut its full-year revenue outlook" in text


def test_an_unsubscribe_footer_is_cut() -> None:
    for raw in load_fixture_messages():
        try:
            text = extract_newsletter(raw.raw).text
        except ExtractionError:
            continue
        lowered = text.lower()
        assert "unsubscribe" not in lowered
        assert "all rights reserved" not in lowered
        assert "manage your preferences" not in lowered


def test_an_attachment_is_not_spliced_into_the_body() -> None:
    """Fixture 03 carries a PDF; its bytes must not appear as prose."""
    ledger = next(m for m in load_fixture_messages() if "ledger" in m.id)
    assert "%PDF" not in extract_newsletter(ledger.raw).text


def test_plain_text_wins_even_when_it_comes_after_the_html_part() -> None:
    """Fixture 03 puts the plain alternative last, which real senders do.

    ``EmailMessage.get_body`` honours document order and would pick the HTML. Preference
    is by content type here, which is why the fuller plain-text body survives.
    """
    ledger = next(m for m in load_fixture_messages() if "ledger" in m.id)
    text = extract_newsletter(ledger.raw).text
    assert "Managing partner Ines Duarte" in text, "the richer plain-text part should win"


def test_a_cp1252_subject_mislabelled_as_latin1_recovers_its_punctuation() -> None:
    """Fixture 03's subject declares iso-8859-1 and is really windows-1252.

    Decoding as declared yields U+0097 — a control character — where an em dash belongs,
    and that character then travels into a news item title, an RSS document, and a TTS
    request. This is the single most common real-world encoding lie in email.
    """
    ledger = next(m for m in load_fixture_messages() if "ledger" in m.id)
    title = extract_newsletter(ledger.raw).title
    assert "—" in title
    assert not any("\u0080" <= ch <= "\u009f" for ch in title), "a C1 control survived"


# --- transfer encodings --------------------------------------------------------------


@pytest.mark.parametrize("encoding", [None, "quoted-printable", "base64"])
def test_every_transfer_encoding_yields_the_same_text(encoding: str | None) -> None:
    """The encoding is a wire detail and must not change a single character.

    It matters more than it looks: spans are character offsets into this text, so an
    encoding that shifted the body by one would move every claim's citation.
    """
    item = extract_newsletter(message(encoding=encoding))
    assert item.text.startswith("Northwind Ventures led a $20M round in Acme")


def test_an_rfc_2047_subject_is_decoded() -> None:
    encoded = base64.b64encode("Acme raises $20M — the details".encode()).decode()
    item = extract_newsletter(message(subject=f"=?UTF-8?B?{encoded}?="))
    assert item.title == "Acme raises $20M — the details"


def test_a_folded_subject_becomes_one_line() -> None:
    """A newline in a title would be read aloud as a pause and break an RSS document."""
    item = extract_newsletter(message(subject="Acme raises $20M\r\n and hires a CFO"))
    assert "\n" not in item.title
    assert item.title == "Acme raises $20M and hires a CFO"


def test_a_charset_lie_does_not_lose_the_message() -> None:
    """A part declaring us-ascii while carrying UTF-8 keeps its text.

    Failing the decode would drop a whole story over one em dash. A replacement character
    is a far better outcome than a missing newsletter.
    """
    raw = message(content_type='text/plain; charset="us-ascii"', body=BODY + "— and more.\n")
    assert "Northwind Ventures" in extract_newsletter(raw).text


def test_an_unknown_charset_falls_back_rather_than_failing() -> None:
    raw = message(content_type='text/plain; charset="x-not-a-charset"')
    assert "Northwind Ventures" in extract_newsletter(raw).text


# --- refusals ------------------------------------------------------------------------


def test_a_message_with_no_subject_is_refused() -> None:
    raw = (
        b"From: a@b.c\r\nTo: d@e.f\r\nDate: Tue, 18 Aug 2026 07:02:11 +0000\r\n"
        b'Content-Type: text/plain; charset="utf-8"\r\n\r\n' + BODY.encode()
    )
    with pytest.raises(ExtractionError, match="Subject"):
        extract_newsletter(raw)


def test_a_short_message_is_refused() -> None:
    with pytest.raises(ExtractionError, match="below the"):
        extract_newsletter(message(body="Your receipt. $29.00.\n"))


def test_a_message_with_no_text_part_is_refused() -> None:
    raw = (
        b"From: a@b.c\r\nSubject: Photo\r\nTo: d@e.f\r\n"
        b"Date: Tue, 18 Aug 2026 07:02:11 +0000\r\nMIME-Version: 1.0\r\n"
        b"Content-Type: image/png\r\nContent-Transfer-Encoding: base64\r\n\r\n"
        + base64.b64encode(b"\x89PNG not really")
    )
    with pytest.raises(ExtractionError):
        extract_newsletter(raw)


def test_an_unparsable_date_does_not_fail_the_message() -> None:
    """A malformed Date is not worth losing a good newsletter over."""
    raw = message().replace(b"Date: Tue, 18 Aug 2026 07:02:11 +0000", b"Date: sometime last week")
    item = extract_newsletter(raw)
    assert item.date == ""
    assert "Northwind Ventures" in item.text


# --- the HTML converter --------------------------------------------------------------


def test_script_and_style_content_is_dropped() -> None:
    html = "<html><head><style>p{color:red}</style></head><body><p>Real prose.</p>"
    html += "<script>var x = 'code';</script></body></html>"
    text = html_to_text(html)
    assert "Real prose." in text
    assert "color:red" not in text
    assert "var x" not in text


@pytest.mark.parametrize(
    "style",
    [
        "display:none",
        "display: none;",
        "visibility:hidden",
        "font-size:0",
        "max-height:0",
        "mso-hide:all",
        "opacity:0",
    ],
)
def test_every_way_of_hiding_a_preheader_is_honoured(style: str) -> None:
    """Newsletter templates use all of these, and each one hides a real preheader."""
    html = f'<div style="{style}">Secret preheader.</div><p>Visible prose.</p>'
    text = html_to_text(html)
    assert "Secret preheader." not in text
    assert "Visible prose." in text


def test_the_hidden_attribute_is_honoured() -> None:
    text = html_to_text("<div hidden>Nope.</div><p>Yes.</p>")
    assert "Nope." not in text
    assert "Yes." in text


def test_table_cells_become_separate_lines() -> None:
    """A newsletter's paragraphs *are* table cells.

    Treating `<td>` as inline runs an entire layout into one unreadable line, which then
    becomes one enormous claim.
    """
    html = "<table><tr><td>First para.</td></tr><tr><td>Second para.</td></tr></table>"
    lines = [line for line in html_to_text(html).split("\n") if line.strip()]
    assert lines == ["First para.", "Second para."]


def test_anchor_text_survives_and_the_href_does_not() -> None:
    """A 200-character tracking redirect is not a sentence, and this gets spoken."""
    html = '<p>Read <a href="https://click.example.com/r/deadbeef?utm=1">the filing</a>.</p>'
    text = html_to_text(html)
    assert "the filing" in text
    assert "click.example.com" not in text


def test_entities_are_decoded() -> None:
    text = html_to_text("<p>Ben &amp; Jerry&rsquo;s &mdash; 20&nbsp;years</p>")
    assert "Ben & Jerry’s" in text
    assert "&amp;" not in text


def test_malformed_markup_keeps_what_was_parsed() -> None:
    """Email HTML is malformed constantly; one bad tag must not lose the newsletter."""
    text = html_to_text("<p>Before.<div><span>After.</p></div></span></unclosed")
    assert "Before." in text
    assert "After." in text


def test_a_hidden_br_does_not_swallow_the_rest_of_the_message() -> None:
    """A void element cannot close, so a naive hidden-depth counter would wedge open.

    This is the failure that would turn one styled `<br>` into an empty source item.
    """
    text = html_to_text('<p>Start.</p><br style="display:none"><p>Still here.</p>')
    assert "Start." in text
    assert "Still here." in text
