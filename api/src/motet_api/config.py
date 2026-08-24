"""Process configuration, read from the environment.

The API never learns *where* it is deployed. Project ids, bucket names, hostnames, and
connection strings arrive as environment variables set by infrastructure that lives in the
private repo — none of them belong in this tree.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from typing import Final
from urllib.parse import urlsplit

API_TOKEN_ENV: Final = "MOTET_API_TOKEN"
PUBLIC_BASE_URL_ENV: Final = "MOTET_PUBLIC_BASE_URL"

#: Where the SPA is served from. The API and the SPA are two different hostnames in every
#: deployed environment — ``api.`` and ``app.`` — which makes every call the SPA makes a
#: cross-origin one. A browser refuses those unless the API says otherwise, so this is
#: what the CORS policy is built from. Unset means no cross-origin access is granted,
#: which is right on a laptop, where the Vite dev server proxies and the origin is shared.
APP_BASE_URL_ENV: Final = "MOTET_APP_BASE_URL"

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
    app_base_url: str | None
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
            app_base_url=_clean(os.environ.get(APP_BASE_URL_ENV)),
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
        ``/internal/health`` reports it rather than leaving it to be discovered: an unauthenticated
        deployment is one paste away from spending real money on someone else's text, and
        it looks exactly like a working one.
        """
        return self.api_token is not None

    @property
    def cors_origins(self) -> list[str]:
        """The exact origins allowed to call ``/v1`` from a browser.

        A list of one, and never ``["*"]``. The SPA sends ``Authorization``, and a
        wildcard origin cannot carry credentialed requests — but more to the point, a
        wildcard would let any page on the internet drive this API with a token it
        tricked out of a browser. One known origin is the whole requirement.

        Only the origin is kept: a browser compares scheme, host and port, and a trailing
        path in the configured value would never match an ``Origin`` header.
        """
        if self.app_base_url is None:
            return []
        return [_origin(self.app_base_url)]


class ConfigError(ValueError):
    """A configuration value cannot be used, and the process should not start.

    The same rule the LLM seam applies to an unknown model slug: fail at startup, where
    Cloud Run reports a failed revision and never shifts traffic to it. The alternative
    here is worse than a 500 an hour later — it is a CORS policy that installs cleanly,
    refuses every preflight, and reports nothing wrong anywhere.
    """


def _origin(url: str) -> str:
    """Reduce a URL to the ``scheme://host[:port]`` a browser puts in ``Origin``.

    Built from ``hostname`` and ``port`` rather than from ``netloc``, which matters for
    two reasons that are invisible until a browser refuses a request:

    * ``urlsplit`` lowercases the scheme but **not** the host, and Starlette compares
      origins with ``in`` — an exact, case-sensitive match against an ``Origin`` header a
      browser always sends lowercased. ``HTTPS://App.Example.COM`` would never match.
    * ``netloc`` keeps userinfo, so ``https://user:pw@app.example.com`` would both fail to
      match and put a credential into the startup log line.

    Anything that does not resolve to a plausible origin raises rather than returning a
    string nothing will ever match. The case that motivates this is a one-slash typo —
    ``https:/app.example.com`` has no ``//``, so the lenient reading turns it into the
    netloc ``https:`` and yields ``https://https:``. That installs a middleware which
    refuses everything, while the "no origin configured" warning stays silent because an
    origin *was* configured.
    """
    # A scheme followed by a *single* slash is the one-slash typo, and it has to be caught
    # before parsing rather than after. `urlsplit("//https:/app.example.com")` reads
    # `https:` as the netloc and hands back the hostname `https`, which is a perfectly
    # well-formed origin that happens to match nothing — so no check downstream of here
    # can tell it apart from a host genuinely called `https`.
    #
    # The `//` exclusion is what keeps `https://…` out of this branch, and requiring a
    # slash after the colon is what keeps `localhost:5173` out of it: a bare host:port
    # has a digit there, not a separator.
    if re.match(r"^[a-zA-Z][a-zA-Z0-9+.\-]*:/", url) and "://" not in url:
        raise ConfigError(
            f"{APP_BASE_URL_ENV}={url!r} looks like a scheme followed by one slash. "
            "A browser's Origin header is scheme://host[:port] — check for a missing "
            "slash, as in 'https:/example.com'."
        )
    parsed = urlsplit(url if "//" in url else f"//{url}", scheme="https")
    if parsed.scheme not in ("http", "https"):
        raise ConfigError(
            f"{APP_BASE_URL_ENV}={url!r} has scheme {parsed.scheme!r}; expected http or https."
        )
    try:
        host, port = parsed.hostname, parsed.port
    except ValueError as exc:  # a non-numeric port
        raise ConfigError(f"{APP_BASE_URL_ENV}={url!r} has an unusable port: {exc}") from exc
    if not host:
        raise ConfigError(
            f"{APP_BASE_URL_ENV}={url!r} has no hostname. A browser's Origin header is "
            "scheme://host[:port] — check for a missing slash, as in 'https:/example.com'."
        )
    # An IPv6 literal has to keep its brackets: the Origin header carries `http://[::1]`.
    if ":" in host:
        host = f"[{host}]"
    return f"{parsed.scheme}://{host}" + (f":{port}" if port is not None else "")


def _clean(value: str | None) -> str | None:
    """Treat an empty variable as an unset one.

    Unset and empty are the same thing in a Cloud Run service definition, so a rule that
    distinguished them would be a rule nobody could actually express.
    """
    if value is None:
        return None
    stripped = value.strip()
    return stripped or None
