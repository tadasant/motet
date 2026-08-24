"""Primary keys, generated in the application rather than by the database.

Prefixed and random rather than sequential. The prefix means an id is self-describing in
a log line or a URL — ``ep_1f4c…`` is obviously an episode — and randomness means an id
in a feed URL or an episode link leaks neither how many episodes exist nor what order
they were made in.

Not a UUID type in the schema: these ids travel through JSON, an RSS ``<guid>``, and a
TypeScript client, all of which want a string, and ``text`` keeps every layer honest
about that.
"""

from __future__ import annotations

import secrets

#: 12 hex characters — 48 bits. Collision risk is negligible at the scale this system
#: will ever see (one user, thousands of items), and the ids stay short enough to read
#: aloud when debugging.
_ENTROPY_BYTES = 6


def new_id(prefix: str) -> str:
    return f"{prefix}_{secrets.token_hex(_ENTROPY_BYTES)}"


def source_item_id() -> str:
    return new_id("si")


def news_item_id() -> str:
    return new_id("ni")


def episode_id() -> str:
    return new_id("ep")


def segment_id() -> str:
    return new_id("seg")


def claim_id() -> str:
    return new_id("cl")


def source_id() -> str:
    return new_id("src")


def highlight_id() -> str:
    return new_id("hl")


def feed_token() -> str:
    """A bearer secret that travels in a URL, so it needs real entropy.

    32 bytes, URL-safe. This is the only credential protecting the audio of everything
    the user reads, and it is handed to podcast clients that will store and re-request it
    forever — so it is sized as a secret, not as an identifier.
    """
    return secrets.token_urlsafe(32)
