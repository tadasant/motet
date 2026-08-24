"""Turn an RFC 822 newsletter into the text a source item holds.

This module is where Gmail ingestion actually earns its keep, and it is deliberately the
part with the most tests. Everything upstream of it is plumbing; everything downstream
assumes the text is clean. A newsletter is not a document — it is a table-based HTML
layout with a hidden preheader, a tracking pixel, three navigation bars, and an
unsubscribe footer, wrapped in quoted-printable and announced with an RFC 2047 subject.

**The output is what claims cite spans into.** ``source_items.text`` is the immutable
anchor for every claim (invariant 3) and for every highlight, so extraction has two
properties it must have and one it must not:

* **Deterministic.** The same message extracts to the same characters every time, or every
  span into it is a lottery. No clock, no randomness, no ordering that depends on dict
  iteration.
* **Stable.** Extraction runs once, at ingestion. Improving this module does not rewrite
  the text of already-ingested items, which is exactly right — an old highlight keeps
  pointing at what the user actually saw.
* **Not lossy in the middle.** Trimming boilerplate off the *ends* is safe; dropping a
  sentence from the body would make a claim uncitable, and the grounding validator would
  reject the story rather than tell you extraction ate it.

**text/plain wins where a newsletter offers both.** Not a performance choice: the plain
part is what the sender wrote, and the HTML part is what a layout tool generated from it.
Preferring the generated one means reconstructing prose that already exists.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from email import message_from_bytes, policy
from email.header import decode_header, make_header
from email.message import EmailMessage, Message
from email.utils import parsedate_to_datetime
from html.parser import HTMLParser
from typing import Final

logger = logging.getLogger("motet.sources.extract")

#: Longest body we keep. A newsletter that runs past this is a digest of a hundred links,
#: and the tail is navigation rather than content. Bounded because the whole item is
#: passed in-prompt to dedup, where an unbounded body is an unbounded bill.
MAX_TEXT_CHARS: Final = 60_000

#: Below this a "newsletter" is a receipt, a calendar invite, or a bounce. Ingesting it
#: produces a news item nobody wants and a claim with nothing to cite.
MIN_TEXT_CHARS: Final = 120

#: Elements whose content is never prose.
_DROPPED_TAGS: Final = frozenset({"script", "style", "head", "title", "noscript", "template"})

#: Elements after which a line break belongs. Tables are included because a newsletter's
#: paragraphs *are* table cells — treating `<td>` as inline runs an entire layout into one
#: unreadable line.
_BLOCK_TAGS: Final = frozenset(
    {
        "address",
        "article",
        "aside",
        "blockquote",
        "br",
        "div",
        "dd",
        "dl",
        "dt",
        "fieldset",
        "figcaption",
        "figure",
        "footer",
        "form",
        "h1",
        "h2",
        "h3",
        "h4",
        "h5",
        "h6",
        "header",
        "hr",
        "li",
        "main",
        "nav",
        "ol",
        "p",
        "pre",
        "section",
        "table",
        "tbody",
        "td",
        "tfoot",
        "th",
        "thead",
        "tr",
        "ul",
    }
)

#: A CSS declaration that hides an element. Newsletters put the *preheader* — the line a
#: mail client shows in the message list — inside one of these, so without this check the
#: extracted text opens with a sentence no human ever saw on screen.
_HIDDEN_STYLE_RE: Final = re.compile(
    r"(display\s*:\s*none)|(visibility\s*:\s*hidden)|(font-size\s*:\s*0)"
    r"|(max-height\s*:\s*0)|(mso-hide\s*:\s*all)|(opacity\s*:\s*0(\.0*)?\s*(;|$))",
    re.IGNORECASE,
)

#: Where the newsletter stops and the machinery starts. Matched against a whole stripped
#: line, anchored, so a sentence that merely mentions unsubscribing is not a cut point.
#:
#: Only ever cuts the TAIL, and only past a minimum of real content — a newsletter whose
#: third line says "view in browser" must not extract to three lines.
_FOOTER_RE: Final = re.compile(
    r"^\s*(?:"
    r"unsubscribe(?:\s+from\s+this\s+list)?"
    r"|manage\s+(?:your\s+)?(?:email\s+)?(?:preferences|subscription)s?"
    r"|update\s+your\s+preferences"
    r"|you(?:'re|\s+are)\s+receiving\s+this\s+(?:email|message)\b.*"
    r"|this\s+email\s+was\s+sent\s+to\b.*"
    r"|sent\s+to\s+you\s+by\b.*"
    r"|©\s*\d{4}\b.*"
    r"|copyright\s*©?\s*\d{4}\b.*"
    r"|all\s+rights\s+reserved\.?"
    r"|copyright\s*\(c\)\s*\d{4}\b.*"
    r"|add\s+us\s+to\s+your\s+address\s+book"
    r"|our\s+mailing\s+address\s+is:?"
    r"|no\s+longer\s+want\s+to\s+receive\s+these\s+emails\??"
    r")\s*[.!]?\s*$",
    re.IGNORECASE,
)

#: Boilerplate that shows up at the *top*: "view this email in your browser", a web-version
#: link, a nav bar. Dropped line-by-line rather than by cutting, because unlike a footer it
#: is interleaved with real content in some templates.
_PREAMBLE_RE: Final = re.compile(
    r"^\s*(?:"
    r"view\s+(?:this\s+)?(?:email|message|issue)?\s*(?:in\s+(?:your\s+)?browser|online|on\s+the\s+web)"
    r"|read\s+(?:this\s+)?(?:online|in\s+browser|on\s+the\s+web)"
    r"|open\s+in\s+browser"
    r"|web\s+version"
    r"|having\s+trouble\s+(?:viewing|reading)\s+this\s+email\??.*"
    r")\s*[.!]?\s*$",
    re.IGNORECASE,
)

#: How far into a message the tail begins, for the purposes of cutting a footer.
#: Footers live at the end; a marker earlier than this is a masthead link.
_FOOTER_TAIL_FRACTION: Final = 0.6

#: A line that is nothing but a URL, or a bare tracking redirect. Common in the plain-text
#: alternative, where every link becomes its own line of unspeakable characters.
_BARE_URL_RE: Final = re.compile(r"^\s*<?https?://\S+>?\s*$", re.IGNORECASE)

#: Charsets that senders declare and do not mean.
#:
#: A mail client that labels its output `iso-8859-1` has, in practice, emitted
#: windows-1252 — the em dash, the curly quotes, and the ellipsis a writer actually typed
#: all live in 0x80-0x9F, which is a *control* block in iso-8859-1 and printable
#: punctuation in cp1252. Decoding it as declared puts a C1 control character where an em
#: dash belongs, and that character then travels into a news item title, into an RSS
#: document, and into a text-to-speech request. Browsers have mandated this same
#: substitution since HTML5 for exactly this reason.
_CP1252_SUPERSETS: Final = frozenset({"iso-8859-1", "iso8859-1", "latin-1", "latin1", "cp819"})

#: Rules, ASCII art dividers, and the `=====` a plain-text newsletter separates sections
#: with. Kept as a single blank line rather than removed, so section boundaries survive.
_DIVIDER_RE: Final = re.compile(r"^\s*[-=_*~•·—–\s]{2,}\s*$")


class ExtractionError(ValueError):
    """A message could not be turned into a usable source item."""


@dataclass(frozen=True)
class ExtractedMessage:
    """One newsletter, ready to become a source item."""

    #: The message's Subject, decoded from RFC 2047 and whitespace-normalized. This is the
    #: source item's title and therefore what dedup compares first.
    title: str
    text: str
    sender: str
    #: RFC 3339, or an empty string if the message had no parsable Date. A string rather
    #: than a datetime because it travels through a JSON job payload.
    date: str
    message_id: str

    @property
    def usable(self) -> bool:
        return len(self.text) >= MIN_TEXT_CHARS and bool(self.title)


def extract_newsletter(raw: bytes) -> ExtractedMessage:
    """Parse RFC 822 bytes into a title and clean body text.

    Raises :class:`ExtractionError` when the result would not be a newsletter — too short,
    or with no subject to title it. That is a *permanent* condition for that message, and
    the caller treats it as such rather than retrying a calendar invite five times.
    """
    try:
        message = message_from_bytes(raw, policy=policy.default)
    except Exception as exc:  # noqa: BLE001 — the email package raises several types
        raise ExtractionError(f"could not parse the message: {exc}") from exc

    title = _decoded_header(message, "Subject")
    body = _clean(_body_text(message))

    if not title:
        # Titling from the first line would work, but a newsletter with no Subject is
        # almost always an automated notification rather than something to read aloud.
        raise ExtractionError("message has no Subject, so there is nothing to title it")
    if len(body) < MIN_TEXT_CHARS:
        raise ExtractionError(
            f"extracted body is {len(body)} characters, below the {MIN_TEXT_CHARS} "
            "minimum for a newsletter — this is a receipt or a notification"
        )

    return ExtractedMessage(
        title=title,
        text=body[:MAX_TEXT_CHARS],
        sender=_decoded_header(message, "From"),
        date=_date(message),
        message_id=_decoded_header(message, "Message-ID"),
    )


# --- headers -------------------------------------------------------------------------


def _decoded_header(message: Message, name: str) -> str:
    """One header, RFC 2047 decoded and collapsed to a single line.

    Subjects arrive as ``=?UTF-8?B?…?=`` more often than not, and a folded subject carries
    the newline that folded it. Both would end up in a news item title and then be read
    aloud, so both are dealt with here rather than downstream.
    """
    raw = message.get(name)
    if raw is None:
        return ""
    try:
        decoded = str(make_header(decode_header(str(raw))))
    except Exception:  # noqa: BLE001 — a malformed encoded-word raises several types
        logger.warning("could not decode the %s header; using it verbatim", name)
        decoded = str(raw)
    return re.sub(r"\s+", " ", _repair_c1(decoded)).strip()


def _repair_c1(text: str) -> str:
    """Reinterpret C1 control characters as the cp1252 punctuation they were meant to be.

    ``decode_header`` honours whatever charset an encoded-word declares, so a subject
    labelled ``iso-8859-1`` that is really cp1252 arrives here with U+0097 where an em
    dash belongs. Round-tripping just those code points through cp1252 recovers the
    punctuation; anything genuinely undecodable is dropped rather than left as a control
    character, because this string ends up in an RSS document and in spoken audio.
    """
    if not any("\u0080" <= ch <= "\u009f" for ch in text):
        return text
    return "".join(
        ch.encode("latin-1").decode("cp1252", errors="ignore") if "\u0080" <= ch <= "\u009f" else ch
        for ch in text
    )


def _date(message: Message) -> str:
    raw = message.get("Date")
    if raw is None:
        return ""
    try:
        return parsedate_to_datetime(str(raw)).isoformat()
    except (TypeError, ValueError):
        # A malformed Date is not worth failing an otherwise good newsletter over; the
        # ingestion timestamp is the one anything downstream actually orders by.
        logger.info("message has an unparsable Date header: %r", str(raw)[:80])
        return ""


# --- MIME ----------------------------------------------------------------------------


def _body_text(message: Message) -> str:
    """The best available body, plain text preferred over HTML.

    Walks the tree rather than trusting ``get_body()``: real newsletters nest
    ``multipart/mixed`` around ``multipart/alternative`` around the parts that matter, and
    some put the plain alternative *after* the HTML one in violation of the spec —
    ``get_body`` honours document order, which picks the wrong one. Preference is decided
    by content type here, and order only breaks ties.
    """
    plain: list[str] = []
    html: list[str] = []

    for part in _leaves(message):
        content_type = part.get_content_type()
        if content_type not in ("text/plain", "text/html"):
            continue
        if _is_attachment(part):
            continue
        decoded = _decode_part(part)
        if not decoded.strip():
            continue
        (plain if content_type == "text/plain" else html).append(decoded)

    if plain:
        return "\n\n".join(plain)
    if html:
        return "\n\n".join(html_to_text(chunk) for chunk in html)
    raise ExtractionError("message has no text/plain or text/html part")


def _leaves(message: Message) -> list[Message]:
    """Every non-multipart part, in document order."""
    if not message.is_multipart():
        return [message]
    parts: list[Message] = []
    for part in message.walk():
        if not part.is_multipart():
            parts.append(part)
    return parts


def _is_attachment(part: Message) -> bool:
    """Whether this part is a file rather than the message.

    A ``text/plain`` attachment is a file the sender attached, not the newsletter, and
    inlining it would splice an unrelated document into the middle of a briefing.
    """
    disposition = str(part.get("Content-Disposition") or "").lower()
    return disposition.startswith("attachment") or bool(part.get_filename())


def _decode_part(part: Message) -> str:
    """A part's bytes, transfer-decoded and charset-decoded.

    ``decode=True`` handles quoted-printable and base64, which is nearly every newsletter.
    Charset comes from the part when it declares one; when it lies — and a surprising
    number declare ``us-ascii`` while carrying a UTF-8 em dash — ``replace`` keeps the
    message rather than losing it to a decode error, because a stray replacement character
    is far better than a dropped story.
    """
    payload = part.get_payload(decode=True)
    if not isinstance(payload, bytes):
        text = part.get_payload()
        return text if isinstance(text, str) else ""
    charset = (part.get_content_charset() or "utf-8").lower()
    if charset in _CP1252_SUPERSETS:
        charset = "cp1252"
    try:
        return payload.decode(charset, errors="replace")
    except LookupError:
        logger.info("part declares unknown charset %r; decoding as UTF-8", charset)
        return payload.decode("utf-8", errors="replace")


# --- HTML ----------------------------------------------------------------------------


class _TextExtractor(HTMLParser):
    """Flatten newsletter HTML to readable lines.

    Not a general HTML-to-text converter, and not trying to be. It is tuned for one input
    — a marketing email — where the interesting failures are hidden preheaders, tracking
    pixels, and paragraphs implemented as table cells.

    **Anchor text is kept and the href is thrown away.** A briefing is going to be spoken;
    a 200-character tracking redirect is not a sentence, and the words in the link already
    say what it is.
    """

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self._chunks: list[str] = []
        self._suppress_depth = 0
        self._hidden_stack: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in _DROPPED_TAGS:
            self._suppress_depth += 1
            return
        if self._suppress_depth:
            return
        if _is_hidden(attrs):
            # Tracked by tag name so the matching end tag closes it. Void elements never
            # get here with content to hide, so an unbalanced hidden `<br>` cannot wedge
            # the parser into swallowing the rest of the message.
            if tag != "br":
                self._hidden_stack.append(tag)
                return
        if self._hidden_stack:
            return
        if tag in _BLOCK_TAGS:
            self._chunks.append("\n")

    def handle_endtag(self, tag: str) -> None:
        if tag in _DROPPED_TAGS:
            self._suppress_depth = max(0, self._suppress_depth - 1)
            return
        if self._hidden_stack and self._hidden_stack[-1] == tag:
            self._hidden_stack.pop()
            return
        if self._suppress_depth or self._hidden_stack:
            return
        if tag in _BLOCK_TAGS:
            self._chunks.append("\n")

    def handle_data(self, data: str) -> None:
        if self._suppress_depth or self._hidden_stack:
            return
        self._chunks.append(data)

    def text(self) -> str:
        return "".join(self._chunks)


def _is_hidden(attrs: list[tuple[str, str | None]]) -> bool:
    for name, value in attrs:
        if name.lower() == "hidden":
            return True
        if name.lower() == "style" and value and _HIDDEN_STYLE_RE.search(value):
            return True
    return False


def html_to_text(html: str) -> str:
    """Readable text from newsletter HTML. Exposed for the golden set."""
    parser = _TextExtractor()
    try:
        parser.feed(html)
        parser.close()
    except Exception as exc:  # noqa: BLE001 — html.parser raises on some malformed input
        # Malformed markup is normal in email. Whatever was parsed before the failure is
        # still worth having; losing the whole newsletter to one unbalanced tag is not.
        logger.warning("HTML parsing stopped early: %s", exc)
    return parser.text()


# --- cleanup -------------------------------------------------------------------------


def _clean(text: str) -> str:
    """Normalize whitespace and strip the machinery from the ends.

    The order matters. Line-level filters run before blank-line collapsing, so removing a
    nav line does not leave a hole; the footer cut runs last, on lines that have already
    been normalized, so a footer marker that arrived wrapped in ``&nbsp;`` still matches.
    """
    # Written as escapes rather than as the characters themselves: a reviewer cannot see
    # a literal zero-width space, and this file is the one place they are the subject.
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    text = re.sub(r"[\u2028\u2029]", "\n", text)  # unicode line/paragraph separators
    text = text.replace("\u00a0", " ")  # non-breaking space, ubiquitous in `&nbsp;` soup
    # Zero-width and bidi marks: how a sender fingerprints a send, and invisible inside a
    # span a claim cites, so a highlight would quote characters nobody can see.
    text = re.sub(r"[\u200b-\u200f\u2060\ufeff]", "", text)
    text = _repair_c1(text)
    lines: list[str] = []
    for raw_line in text.split("\n"):
        line = re.sub(r"[ \t]+", " ", raw_line).strip()
        if _PREAMBLE_RE.match(line) or _BARE_URL_RE.match(line):
            continue
        lines.append("" if _DIVIDER_RE.match(line) else line)

    lines = _cut_footer(lines)

    # Collapse runs of blank lines to one. A table-based layout emits dozens.
    collapsed: list[str] = []
    for line in lines:
        if not line and (not collapsed or not collapsed[-1]):
            continue
        collapsed.append(line)
    return "\n".join(collapsed).strip()


def _cut_footer(lines: list[str]) -> list[str]:
    """Drop everything from the first footer marker in the tail onwards.

    **The first marker, not the last, and only in the tail.** A footer is a block —
    "you're receiving this", then "unsubscribe", then a postal address, then a copyright
    line — so cutting at the *last* marker keeps most of the block. Cutting at the first
    one inside the tail removes all of it.

    "The tail" is the later part of the message, floored by a minimum of real content.
    Both bounds exist because both mistakes are real: some templates put a compact
    "unsubscribe" in the masthead, and cutting there would reduce a newsletter to its
    header — which looks ingested-and-empty rather than failed. Requiring the marker to
    sit past both bounds means the worst case is keeping some boilerplate, which costs a
    few tokens and nothing else.
    """
    floor = _content_floor(lines)
    if floor is None:
        return lines
    for index in range(floor, len(lines)):
        if _FOOTER_RE.match(lines[index]):
            return lines[:index]
    return lines


def _content_floor(lines: list[str]) -> int | None:
    """The first line index a footer cut is allowed at, or None if there is no tail.

    Past :data:`MIN_TEXT_CHARS` of accumulated content *and* past
    :data:`_FOOTER_TAIL_FRACTION` of the lines, whichever is later.
    """
    accumulated = 0
    by_content: int | None = None
    for index, line in enumerate(lines):
        accumulated += len(line)
        if accumulated >= MIN_TEXT_CHARS:
            by_content = index + 1
            break
    if by_content is None:
        return None
    return max(by_content, int(len(lines) * _FOOTER_TAIL_FRACTION))


def as_email_message(raw: bytes) -> EmailMessage:
    """Parse to an :class:`EmailMessage`, for tests and fixtures that want the headers."""
    parsed = message_from_bytes(raw, policy=policy.default)
    assert isinstance(parsed, EmailMessage)
    return parsed
