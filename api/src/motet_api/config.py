"""Process configuration, read from the environment.

The API never learns *where* it is deployed. Project ids, bucket names, hostnames, and
connection strings arrive as environment variables set by infrastructure that lives in the
private repo — none of them belong in this tree.
"""

from __future__ import annotations

import logging
import os
import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Final
from urllib.parse import urlsplit

from motet_db.api_tokens import LABEL_RE
from motet_inference.mode import current_mode
from motet_obs import resolve_deployment_environment

from .auth import CLIENT_ID_ENV, admin_emails, allowed_emails

logger = logging.getLogger("motet.api.config")

API_TOKEN_ENV: Final = "MOTET_API_TOKEN"
PUBLIC_BASE_URL_ENV: Final = "MOTET_PUBLIC_BASE_URL"

#: Where the SPA is served from. The API and the SPA are two different hostnames in every
#: deployed environment — ``api.`` and ``app.`` — which makes every call the SPA makes a
#: cross-origin one. A browser refuses those unless the API says otherwise, so this is
#: what the CORS policy is built from. Unset means no cross-origin access is granted,
#: which is right on a laptop, where the Vite dev server proxies and the origin is shared.
APP_BASE_URL_ENV: Final = "MOTET_APP_BASE_URL"

#: Whether this deployment's web app serves an Apple app-site-association file, so the iOS
#: app's sign-in sheet may wait for an https link instead of the `motet://` one.
#:
#: Off by default, and it has to be: the flag is a *claim about the web app*, and an app
#: told to wait for an https callback the web app does not advertise would sit in a sheet
#: that never closes. It is switched on per environment, beside MOTET_IOS_APP_ID on the web
#: service, once that file is served.
IOS_APP_LINK_ENV: Final = "MOTET_IOS_APP_LINK"

#: What a personal access token says it opens: the middle of ``mot_<label>_<secret>``.
#:
#: **Optional, and its fallback is a variable the deploy already sets.** A token's job is
#: to be identifiable at a glance in a paste, a log or a repository scan, and what makes
#: it identifiable is which *environment* it is a key to. ``deployment.environment.name``
#: in ``OTEL_RESOURCE_ATTRIBUTES`` is already that fact — it is what GlitchTip labels an
#: error with — so a deployment that sets nothing here still mints ``mot_staging_…`` and
#: ``mot_production_…`` rather than an undifferentiated string. This variable exists only
#: so a deployment can pick the shorter spelling (``stg``, ``live``) without renaming the
#: environment every span and every error report wears. Same shape as ``OTEL_INGEST_TOKEN``
#: beside ``OTEL_EXPORTER_OTLP_HEADERS``: one fact, and a second way to state it where the
#: first cannot be bent to the purpose.
TOKEN_LABEL_ENV: Final = "MOTET_TOKEN_LABEL"

#: What a token says when nothing names the environment. A laptop, and a ``docker run``.
DEFAULT_TOKEN_LABEL: Final = "dev"


#: What a podcast client shows for the feed. Configurable so an environment can tell itself
#: apart in a podcast app; not secret, and not infrastructure. The defaults are the brand's
#: copy (``brand/GUIDELINES.md``), which leads with the positioning line rather than with
#: citations — less "interactive", because nothing a podcast client can reach is.
FEED_TITLE_ENV: Final = "MOTET_FEED_TITLE"
FEED_DESCRIPTION_ENV: Final = "MOTET_FEED_DESCRIPTION"
FEED_AUTHOR_ENV: Final = "MOTET_FEED_AUTHOR"

DEFAULT_FEED_TITLE: Final = "Motet"
DEFAULT_FEED_DESCRIPTION: Final = (
    "Motet turns content you trust into a podcast you can listen to on the go."
)
DEFAULT_FEED_AUTHOR: Final = "Motet"


#: The one path the SPA hands back to after a provider redirect, for both signing in and
#: connecting a mailbox. It is registered on the Google OAuth client — three URIs, one per
#: environment, each that environment's own origin plus this path — and the registrations
#: live in the private infrastructure repo. Nothing in this repo can tell you it drifted.
CALLBACK_PATH: Final = "/oauth/callback"


@dataclass(frozen=True)
class Settings:
    #: Out of the repr, with ``api_token`` below, and that is a leak guard rather than
    #: tidiness. An error reporter captures frame locals **by their repr**, and a
    #: ``Settings`` is a local of `motet_api.deps.require_caller` — the one function every
    #: authenticated request passes through, and the one an unhandled exception on the
    #: auth path is raised inside (the `motet-vault[kms]` incident was exactly that).
    #: Without this the bearer is redacted by name and the *shared owner-equivalent
    #: secret* and the Cloud SQL URL, password and all, go to GlitchTip beside it.
    #: `sentry_sdk`'s scrubber works on names, and `config` is not a name it knows.
    database_url: str | None = field(repr=False)
    inference_mode: str
    api_token: str | None = field(repr=False)
    public_base_url: str | None
    app_base_url: str | None
    feed_title: str
    feed_description: str
    feed_author: str
    #: Who may sign in with Google. Empty means nobody — see `motet_db.allowlist`.
    #:
    #: Defaulted, unlike every field above it, and the default is the *closed* one. These
    #: two decide who gets in, so a `Settings` built for some other purpose — a test that
    #: only cares about the CORS policy, say — must land on "nobody signs in" rather than
    #: forcing every such caller to remember to say so.
    allowed_emails: frozenset[str] = frozenset()
    #: Who may open the operator view, ``/v1/admin/*``. Empty means nobody — see
    #: `motet_db.allowlist`. Defaulted closed for the same reason as the field above.
    admin_emails: frozenset[str] = frozenset()
    #: Whether the phone's sign-in handoff comes back on a verified https universal link
    #: rather than on the ``motet://`` scheme. Off unless the deployment also serves the
    #: app-site-association file naming the app, and defaulted to off for the reason the two
    #: fields above are defaulted closed: a `Settings` built to test something else must land
    #: on the fallback rather than on a link nothing would verify.
    ios_app_link: bool = False
    #: Present only so that "is sign-in actually wired" is answerable. The secret half is
    #: never read here: the API resolves it when it completes a sign-in, not at startup.
    google_client_id: str | None = None
    #: The label a personal access token minted here carries — see :data:`TOKEN_LABEL_ENV`.
    #: Defaulted, like the two allowlists, so a ``Settings`` built to test something else
    #: lands on the laptop's answer rather than on nothing.
    token_label: str = DEFAULT_TOKEN_LABEL

    @classmethod
    def from_env(cls) -> Settings:
        return cls(
            database_url=os.environ.get("DATABASE_URL"),
            inference_mode=os.environ.get("MOTET_INFERENCE_MODE", "fake"),
            api_token=_clean(os.environ.get(API_TOKEN_ENV)),
            public_base_url=_clean(os.environ.get(PUBLIC_BASE_URL_ENV)),
            app_base_url=_clean(os.environ.get(APP_BASE_URL_ENV)),
            ios_app_link=_truthy(os.environ.get(IOS_APP_LINK_ENV)),
            feed_title=_clean(os.environ.get(FEED_TITLE_ENV)) or DEFAULT_FEED_TITLE,
            feed_description=(
                _clean(os.environ.get(FEED_DESCRIPTION_ENV)) or DEFAULT_FEED_DESCRIPTION
            ),
            feed_author=_clean(os.environ.get(FEED_AUTHOR_ENV)) or DEFAULT_FEED_AUTHOR,
            allowed_emails=allowed_emails(os.environ),
            admin_emails=admin_emails(os.environ),
            google_client_id=_clean(os.environ.get(CLIENT_ID_ENV)),
            token_label=token_label(os.environ),
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
    def login_configured(self) -> bool:
        """Whether anybody can actually sign in with Google.

        Both halves are required and both fail closed. An empty allowlist means the
        deployment would deny every verified identity, and in real mode a missing OAuth
        client means the flow cannot start at all — so a deployment in either state must
        say so rather than offering a button that ends in a wall.

        Reported on ``/internal/health`` in the same spirit as ``authenticated``: an
        exporter that no-ops silently and a login that denies silently are the same class
        of problem, which is that nothing distinguishes them from working.
        """
        if not self.allowed_emails:
            return False
        # Through `current_mode` rather than comparing the stored string: AGENTS.md says
        # MOTET_INFERENCE_MODE is parsed in exactly one place, because two readings can
        # disagree silently. A second `== "real"` here would be one of them.
        try:
            mode = current_mode({"MOTET_INFERENCE_MODE": self.inference_mode})
        except ValueError:
            # An unrecognized mode is somebody else's crash — the worker entry point and
            # the LLM seam both raise on it. Here it only means "cannot claim this is
            # configured", which is the fail-closed answer.
            return False
        return mode != "real" or self.google_client_id is not None

    def callback_uri_allowed(self, redirect_uri: str) -> bool:
        """Whether the SPA may ask for a sign-in that returns to this URI.

        Google is the real check — it matches a redirect URI against the ones registered
        on the OAuth client, by exact string, and refuses anything else — so this cannot
        be the thing that stops a grant going somewhere it should not. It is here because
        *starting* a sign-in is necessarily unauthenticated, and an unauthenticated route
        that echoes an arbitrary caller-supplied URL into a redirect is a shape worth not
        having even when it is provably harmless.

        With ``MOTET_APP_BASE_URL`` unset the origin is not checked: that is a laptop,
        where the Vite dev server and the API share an origin and there is no configured
        answer to compare against.
        """
        try:
            path = urlsplit(redirect_uri).path
        except ValueError:
            return False
        if path.rstrip("/") != CALLBACK_PATH:
            return False
        origins = self.cors_origins
        if not origins:
            return True
        try:
            return _origin(redirect_uri) in origins
        except ConfigError:
            return False

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


def token_label(env: Mapping[str, str]) -> str:
    """Which environment a token minted here should say it opens.

    ``MOTET_TOKEN_LABEL`` first, then the deploy's own ``deployment.environment.name``,
    then ``dev``. Slugified rather than validated, because the fallback is a value set for
    somebody else's purpose — ``Staging (GCP)`` is a perfectly good GlitchTip environment
    and a terrible thing to put in the middle of a credential — and a *refusal* here would
    mean a deployment that could not mint a token at all because of how its telemetry was
    labelled. Anything that slugifies to nothing lands on the default.
    """
    raw = _clean(env.get(TOKEN_LABEL_ENV)) or resolve_deployment_environment(env) or ""
    slug = "".join(
        character for character in raw.lower() if character.isascii() and character.isalnum()
    )[:12]
    if not LABEL_RE.match(slug):
        if raw:
            logger.warning(
                "%r is not usable as a token label; minting %r tokens instead",
                raw,
                DEFAULT_TOKEN_LABEL,
            )
        return DEFAULT_TOKEN_LABEL
    return slug


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


#: What counts as on, and what counts as a deliberate off. Anything else is a typo.
_TRUTHY: Final = frozenset({"1", "true", "yes", "on"})
_FALSY: Final = frozenset({"", "0", "false", "no", "off"})


def _truthy(raw: str | None) -> bool:
    """A deployment flag, with an unrecognised value logged rather than read as off.

    ``MOTET_IOS_APP_LINK=enabled`` is somebody switching a feature on. Answering "off" and
    saying nothing is how a deployment sits in the fallback for weeks — the same argument
    the repo's other flags make by raising (``drain.resolve_job``) or warning
    (``motet_db.settings.settings_writable``) rather than shrugging.
    """
    value = (raw or "").strip().lower()
    if value in _TRUTHY:
        return True
    if value not in _FALSY:
        logger.error(
            "%r is not a recognised on/off value (%s); reading it as off",
            raw,
            ", ".join(sorted(_TRUTHY | (_FALSY - {""}))),
        )
    return False


def _clean(value: str | None) -> str | None:
    """Treat an empty variable as an unset one.

    Unset and empty are the same thing in a Cloud Run service definition, so a rule that
    distinguished them would be a rule nobody could actually express.
    """
    if value is None:
        return None
    stripped = value.strip()
    return stripped or None
