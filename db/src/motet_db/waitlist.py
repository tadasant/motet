"""The getmotet.com waitlist: addresses somebody typed into the landing page.

**Not an account and not a user.** Signup is still out of scope (AGENTS.md, "Signing in is a
second key to the same lock"); a row here is a request to be told when there is room, and
nothing in the system grants it anything.

Two properties are the design:

* **One row per address, held by the database.** :func:`join` is a single
  ``INSERT … ON CONFLICT`` on the normalized address, so a double-click, a second tab and a
  bot replaying one address all land on the same row. A read-then-write would race.
* **The caller cannot tell a new address from a known one.** :func:`join` reports which it
  was so the route can count it; the route answers both the same way, so the endpoint is
  not an oracle for who is already on the list.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Final

import psycopg

from .repo import _all, _one

#: RFC 5321's limit on a forward path, which is the longest address a mail server accepts.
MAX_EMAIL_LENGTH: Final = 254
#: RFC 5321's limit on the local part.
MAX_LOCAL_PART_LENGTH: Final = 64

#: Deliberately loose. The only reliable test of an address is sending it mail, which this
#: waitlist does not do; the job here is refusing what is plainly not one — a name, a URL,
#: an address with a space in it — without refusing an unusual real one. So: one ``@``,
#: no whitespace or control characters, and a dotted domain with no empty label.
_ATOM: Final = r"[^@\s\x00-\x1f\x7f]"
_LABEL: Final = r"[^@\s\x00-\x1f\x7f.]+"
_EMAIL: Final = re.compile(rf"^{_ATOM}+@(?:{_LABEL}\.)+{_LABEL}$")


def normalize_email(raw: str) -> str | None:
    """The stored form of ``raw``, or ``None`` when it is not plausibly an address.

    Lowercased whole. Strictly a local part is case-sensitive, but no mail provider anyone
    uses treats it so, and ``Ada@x.test`` and ``ada@x.test`` as two waitlist rows would be
    the duplicate this table exists not to have.
    """
    email = raw.strip().lower()
    if not email or len(email) > MAX_EMAIL_LENGTH or not _EMAIL.match(email):
        return None
    if len(email.split("@", 1)[0]) > MAX_LOCAL_PART_LENGTH:
        return None
    return email


@dataclass(frozen=True)
class WaitlistSignup:
    id: int
    email: str
    created_at: datetime
    last_submitted_at: datetime
    submissions: int


def join(conn: psycopg.Connection[Any], email: str) -> bool:
    """Put ``email`` on the list. True when it was not already there.

    ``email`` must already be :func:`normalize_email`'s output; the table's check
    constraint refuses anything else rather than storing a second spelling.
    ``submissions`` saturates instead of overflowing, because a script replaying one
    address two billion times should not turn into a 500.
    """
    row = _one(
        conn,
        """
        INSERT INTO waitlist_signups (email) VALUES (%s)
        ON CONFLICT (email) DO UPDATE
           SET last_submitted_at = now(),
               submissions = LEAST(waitlist_signups.submissions, 2147483646) + 1
        RETURNING (xmax = 0) AS inserted
        """,
        (email,),
    )
    return bool(row["inserted"])


def count(conn: psycopg.Connection[Any]) -> int:
    return int(_one(conn, "SELECT count(*) AS n FROM waitlist_signups", ())["n"])


def list_signups(
    conn: psycopg.Connection[Any], *, before: int | None, limit: int
) -> list[WaitlistSignup]:
    """Newest first, keyed on the id — the admin jobs list's paging, for the same reason."""
    rows = _all(
        conn,
        """
        SELECT id, email, created_at, last_submitted_at, submissions
        FROM waitlist_signups
        WHERE %(before)s::bigint IS NULL OR id < %(before)s::bigint
        ORDER BY id DESC
        LIMIT %(limit)s
        """,
        {"before": before, "limit": limit},
    )
    return [WaitlistSignup(**row) for row in rows]
