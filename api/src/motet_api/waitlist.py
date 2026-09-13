"""The landing page's waitlist form: parsing, answering, and counting a submission.

``POST /v1/waitlist`` is the one ``/v1`` route anybody on the internet may call, and it is
called from a *different site* — the static landing page on the apex domain, not the SPA —
so three things about it are decisions rather than details:

* **It is a CORS "simple request", so nothing needs to be configured.** The page sends
  ``application/x-www-form-urlencoded`` with no custom header, which a browser sends
  cross-origin without a preflight; the answer carries ``Access-Control-Allow-Origin: *``
  so the page's script may read it. A wildcard is right *here* and wrong everywhere else
  in ``/v1``: nothing about this route is credentialed, so there is nothing a hostile page
  could do through a visitor's browser that ``curl`` could not do directly. It also means
  the API needs no variable naming the landing page's origin, and a Cloudflare Pages
  preview deployment works against it unchanged. JSON is refused (415) rather than
  accepted, because a JSON body is *not* simple: it would be preflighted, and the SPA's
  CORS policy would refuse the preflight from any origin but the app's.
* **It works without JavaScript.** A native form post navigates to this route, so a
  caller that does not ask for JSON gets a small HTML page instead of a JSON document.
  Deliberately not a redirect back to the landing page: the only ways to know where that
  is are an origin variable, or echoing a caller-supplied URL into a ``Location`` header —
  and ``Settings.callback_uri_allowed`` already records that this API does not want that
  shape on an unauthenticated route, even when it is harmless.
* **An address never reaches a log line or a metric.** Outcomes are counted on
  ``motet.api.waitlist_submissions{outcome}`` and logged by outcome alone. The address is
  in the table and on the admin screen, and nowhere an operator did not go to look for it.

Abuse hygiene is proportionate to a list with no side effects — no mail is sent, nothing is
granted: a body cap, a loose address check, one row per address, and a honeypot field that
a person never sees and a form-filling bot fills. A bot that trips it is told it succeeded,
so it has no reason to adapt. There is no rate limit, because the stack has nowhere to keep
one that is not a new mechanism, and a flood of fresh addresses costs rows, not money.
"""

from __future__ import annotations

import html
import logging
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Final
from urllib.parse import parse_qs

from fastapi import Request
from fastapi.responses import HTMLResponse, JSONResponse, Response
from motet_db.waitlist import normalize_email
from opentelemetry import metrics

logger = logging.getLogger("motet.api.waitlist")

#: A form with one address and an empty honeypot is well under a kilobyte. Four is room for
#: a long address, percent-encoding and a browser's extras, and small enough that reading
#: it into memory before looking at it is not a thing worth defending against.
MAX_BODY_BYTES: Final = 4096

FORM_CONTENT_TYPE: Final = "application/x-www-form-urlencoded"
EMAIL_FIELD: Final = "email"
#: Hidden from people (off-screen, ``tabindex=-1``, ``autocomplete=off``) and filled by bots
#: that fill every input. Deliberately a name no autofill heuristic or password manager
#: recognises: ``website`` or ``url`` would be filled in for a real person now and then, who
#: would be told they had joined while nothing was stored.
HONEYPOT_FIELD: Final = "motet_hp"

#: The one header that lets the landing page read the answer. See the module docstring.
CORS_HEADERS: Final = {"Access-Control-Allow-Origin": "*", "Cache-Control": "no-store"}


class Outcome(StrEnum):
    JOINED = "joined"
    ALREADY_LISTED = "already_listed"
    HONEYPOT = "honeypot"
    INVALID = "invalid"
    TOO_LARGE = "too_large"
    UNSUPPORTED = "unsupported_media_type"
    #: The address was valid and the write did not happen. See ``join_waitlist``.
    STORE_FAILED = "store_failed"


_meter = metrics.get_meter("motet.api")

#: Every outcome, including the refusals. A waitlist with no signups and a waitlist whose
#: form has been posting to the wrong place look identical from the table; ``invalid`` and
#: ``unsupported_media_type`` climbing while ``joined`` does not is the second one.
_submissions = _meter.create_counter(
    "motet.api.waitlist_submissions",
    unit="{submission}",
    description="Landing-page waitlist submissions, by what became of them.",
)


@dataclass(frozen=True)
class Submission:
    """What the request said, read before the route touches the database."""

    #: The normalized address — ``None`` when the request was refused before one was read.
    #: Out of the repr because an error reporter captures frame locals by their repr, and this
    #: object is a local of the route that stores it.
    email: str | None = field(repr=False)
    #: Set when the request is answered without a write.
    refused: Outcome | None
    #: Whether to answer in JSON (the page's script) or HTML (a native form post).
    wants_json: bool


async def read_submission(request: Request) -> Submission:
    """Parse the form, bounded, without raising — every refusal is answered by the route.

    A dependency rather than the route body so that the route itself can stay synchronous
    like every other ``/v1`` route, while the body is still read on the event loop.
    """
    wants_json = "application/json" in request.headers.get("accept", "").lower()

    content_type = request.headers.get("content-type", "").split(";", 1)[0].strip().lower()
    if content_type != FORM_CONTENT_TYPE:
        return Submission(email=None, refused=Outcome.UNSUPPORTED, wants_json=wants_json)

    declared = request.headers.get("content-length")
    if declared is not None and (
        not (declared.isascii() and declared.isdigit()) or int(declared) > MAX_BODY_BYTES
    ):
        return Submission(email=None, refused=Outcome.TOO_LARGE, wants_json=wants_json)

    body = bytearray()
    async for chunk in request.stream():
        body.extend(chunk)
        if len(body) > MAX_BODY_BYTES:
            return Submission(email=None, refused=Outcome.TOO_LARGE, wants_json=wants_json)

    try:
        fields = parse_qs(bytes(body).decode("ascii"), max_num_fields=8, errors="strict")
    except (UnicodeDecodeError, ValueError):
        return Submission(email=None, refused=Outcome.INVALID, wants_json=wants_json)

    if any(value.strip() for value in fields.get(HONEYPOT_FIELD, [])):
        return Submission(email=None, refused=Outcome.HONEYPOT, wants_json=wants_json)

    emails = fields.get(EMAIL_FIELD, [])
    email = normalize_email(emails[0]) if len(emails) == 1 else None
    if email is None:
        return Submission(email=None, refused=Outcome.INVALID, wants_json=wants_json)
    return Submission(email=email, refused=None, wants_json=wants_json)


#: What each outcome says, and with which status. The two successes and the honeypot share
#: one answer on purpose: see ``motet_db.waitlist``.
_ANSWERS: Final[dict[Outcome, tuple[int, str, str]]] = {
    Outcome.JOINED: (200, "You're on the list.", "We'll write when there's a place for you."),
    Outcome.ALREADY_LISTED: (
        200,
        "You're on the list.",
        "We'll write when there's a place for you.",
    ),
    Outcome.HONEYPOT: (200, "You're on the list.", "We'll write when there's a place for you."),
    Outcome.INVALID: (
        422,
        "That doesn't look like an email address.",
        "Go back and check it, then try again.",
    ),
    Outcome.TOO_LARGE: (413, "That was more than an email address.", "Go back and try again."),
    Outcome.UNSUPPORTED: (
        415,
        "That form didn't come from the Motet waitlist.",
        "Go back and try again.",
    ),
    Outcome.STORE_FAILED: (
        503,
        "We couldn't save that just now.",
        "Go back and try again in a moment.",
    ),
}


def answer(outcome: Outcome, *, wants_json: bool) -> Response:
    """Count ``outcome`` and render it for whichever caller asked."""
    _submissions.add(1, {"outcome": outcome.value})
    logger.info("waitlist: outcome=%s", outcome.value)

    status, headline, detail = _ANSWERS[outcome]
    if wants_json:
        body = {"status": "joined"} if status == 200 else {"detail": headline}
        return JSONResponse(body, status_code=status, headers=CORS_HEADERS)
    return HTMLResponse(_page(headline, detail), status_code=status, headers=CORS_HEADERS)


def _page(headline: str, detail: str) -> str:
    """The no-JavaScript answer: the brand's ground and ink, and nothing to load."""
    return f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="robots" content="noindex">
<title>{html.escape(headline)} · Motet</title>
<style>
  body {{ margin: 0; min-height: 100vh; display: grid; place-items: center; padding: 24px;
         background: #F4EFE6; color: #1B1A2E;
         font: 17px/1.6 "Helvetica Neue", Arial, sans-serif; }}
  main {{ max-width: 30em; }}
  .mark {{ font: italic 500 28px/1 "Iowan Old Style", Palatino, Georgia, serif;
          letter-spacing: -0.03em; margin: 0 0 32px; }}
  h1 {{ font: 400 40px/1.1 "Iowan Old Style", Palatino, Georgia, serif; margin: 0 0 16px; }}
  p {{ margin: 0; color: rgba(27, 26, 46, 0.66); }}
</style>
</head>
<body>
<main>
  <p class="mark">motet</p>
  <h1>{html.escape(headline)}</h1>
  <p>{html.escape(detail)}</p>
</main>
</body>
</html>
"""
