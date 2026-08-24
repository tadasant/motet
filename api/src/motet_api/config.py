"""Process configuration, read from the environment.

The API never learns *where* it is deployed. Project ids, bucket names, hostnames, and
connection strings arrive as environment variables set by infrastructure that lives in the
private repo — none of them belong in this tree.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Final

API_TOKEN_ENV: Final = "MOTET_API_TOKEN"
PUBLIC_BASE_URL_ENV: Final = "MOTET_PUBLIC_BASE_URL"

#: What a podcast client shows for the feed. Configurable because "Motet" is a working
#: name for one user's briefing and Phase 3 gives the product a brand; not secret, and not
#: infrastructure.
FEED_TITLE_ENV: Final = "MOTET_FEED_TITLE"
FEED_DESCRIPTION_ENV: Final = "MOTET_FEED_DESCRIPTION"
FEED_AUTHOR_ENV: Final = "MOTET_FEED_AUTHOR"

DEFAULT_FEED_TITLE: Final = "Motet"
DEFAULT_FEED_DESCRIPTION: Final = (
    "Your reading backlog, read aloud. Every claim traces to the source it came from."
)
DEFAULT_FEED_AUTHOR: Final = "Motet"


@dataclass(frozen=True)
class Settings:
    database_url: str | None
    inference_mode: str
    api_token: str | None
    public_base_url: str | None
    feed_title: str
    feed_description: str
    feed_author: str

    @classmethod
    def from_env(cls) -> Settings:
        return cls(
            database_url=os.environ.get("DATABASE_URL"),
            inference_mode=os.environ.get("MOTET_INFERENCE_MODE", "fake"),
            api_token=_clean(os.environ.get(API_TOKEN_ENV)),
            public_base_url=_clean(os.environ.get(PUBLIC_BASE_URL_ENV)),
            feed_title=_clean(os.environ.get(FEED_TITLE_ENV)) or DEFAULT_FEED_TITLE,
            feed_description=(
                _clean(os.environ.get(FEED_DESCRIPTION_ENV)) or DEFAULT_FEED_DESCRIPTION
            ),
            feed_author=_clean(os.environ.get(FEED_AUTHOR_ENV)) or DEFAULT_FEED_AUTHOR,
        )

    @property
    def authenticated(self) -> bool:
        """Whether ``/v1`` requires a bearer token.

        False is legitimate on a laptop and a mistake anywhere else, which is why
        ``/healthz`` reports it rather than leaving it to be discovered: an unauthenticated
        deployment is one paste away from spending real money on someone else's text, and
        it looks exactly like a working one.
        """
        return self.api_token is not None


def _clean(value: str | None) -> str | None:
    """Treat an empty variable as an unset one.

    Unset and empty are the same thing in a Cloud Run service definition, so a rule that
    distinguished them would be a rule nobody could actually express.
    """
    if value is None:
        return None
    stripped = value.strip()
    return stripped or None
