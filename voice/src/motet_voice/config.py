"""Voice service configuration, read from the environment in one place.

Two rules this module exists to keep:

* **``MOTET_INFERENCE_MODE`` is not re-parsed here.** It is parsed in
  :mod:`motet_inference.mode` and nowhere else — AGENTS.md says so, and the reason is that
  two readings can disagree silently. This module *asks* that one, and a voice-only
  override does not exist.
* **No infrastructure facts.** No hostnames, no project ids, no bucket names. Every one of
  them arrives as a value in a variable whose *name* is the only thing this public repo
  knows.
"""

from __future__ import annotations

import logging
import os
import secrets
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Final

from motet_inference.mode import Mode, current_mode

logger = logging.getLogger("motet.voice.config")

ARM_ENV: Final = "MOTET_VOICE_ARM"
SESSION_SECRET_ENV: Final = "MOTET_VOICE_SESSION_SECRET"
SESSION_TTL_ENV: Final = "MOTET_VOICE_SESSION_TTL_SECONDS"
API_BASE_URL_ENV: Final = "MOTET_VOICE_API_BASE_URL"
API_TOKEN_ENV: Final = "MOTET_VOICE_API_TOKEN"
START_SESSION_TOKEN_ENV: Final = "MOTET_VOICE_START_SESSION_TOKEN"
OPENAI_KEY_ENV: Final = "OPENAI_API_KEY"
OPENAI_REALTIME_MODEL_ENV: Final = "MOTET_VOICE_OPENAI_REALTIME_MODEL"
#: Which of Motet's MCP tool groups this service's connection may list and call. A
#: deployment can narrow it; it can also widen it, which is why the default is the tight
#: selection rather than the server's own (every group but ``admin``).
MCP_TOOL_GROUPS_ENV: Final = "MOTET_VOICE_MCP_TOOL_GROUPS"
#: The credential presented to ``/mcp``. **Unset falls back to** ``MOTET_VOICE_API_TOKEN``,
#: which is the owner token — option (a) of motet#120, and the reason this change needs
#: nothing provisioned. Setting this one is the whole of option (b): a scoped credential
#: swaps in as a variable rather than as a rewrite.
MCP_TOKEN_ENV: Final = "MOTET_VOICE_MCP_TOKEN"
#: Comma-separated browser origins allowed to open a session socket — the SPA's origin in
#: a deployed environment. Unset allows any, which is right on a laptop.
ALLOWED_ORIGINS_ENV: Final = "MOTET_VOICE_ALLOWED_ORIGINS"

#: Long enough to survive a walk out of signal and a client reconnect; short enough that a
#: token scraped out of a log is not a durable handle on a live session.
DEFAULT_SESSION_TTL_SECONDS: Final = 3_600

#: The two arms of the comparison the barge-in spike exists to settle.
COMPOSED_ARM: Final = "composed"
OPENAI_REALTIME_ARM: Final = "openai_realtime"
ARMS: Final = (COMPOSED_ARM, OPENAI_REALTIME_ARM)

#: The composed arm is the default because it is the one that can actually run today: its
#: turn detection is local, its LLM leg is the provisioned OpenRouter seam, and its TTS leg
#: is the provisioned Cartesia adapter. The realtime arm is dormant on a key that does not
#: exist yet — see :mod:`motet_voice.realtime.openai_realtime`.
DEFAULT_ARM: Final = COMPOSED_ARM

DEFAULT_OPENAI_REALTIME_MODEL: Final = "gpt-realtime"

#: The slug a caller binds to reach Motet's own MCP server. A *slug*, never a URL — see
#: :class:`~motet_voice.contract.McpServerBinding`: where it points is this service's
#: configuration, so no client can name a host.
MOTET_MCP_SLUG: Final = "motet"

#: The tool groups the conversation genuinely needs, and no more (motet#120).
#:
#: ``backlog`` carries ``set_news_item_read``, which is ``mark_read``; ``highlights``
#: carries ``save_highlight``. Both are *write* groups because both platform tools write —
#: there is no read-only variant that contains a write. No read-only group is asked for:
#: everything a session reads arrives in its :class:`~motet_voice.contract.SessionContext`
#: (invariant 2), so a read group would be a surface nothing calls.
DEFAULT_MCP_TOOL_GROUPS: Final = "backlog,highlights"


class VoiceConfigError(ValueError):
    """The voice service was asked for something it cannot do."""


@dataclass(frozen=True)
class VoiceSettings:
    """Everything the service reads from its environment, resolved once."""

    arm: str
    inference_mode: Mode
    session_secret: str
    session_secret_provided: bool
    session_ttl_seconds: int
    api_base_url: str | None
    #: Read only as the fallback for :attr:`mcp_token` — since motet#120 the one thing this
    #: service presents a bearer to is Motet's MCP server, and nothing else in this package
    #: makes an HTTP request to the API. Kept under its own name because that is the
    #: variable a deployment already sets, and because option (b) is the *other* name.
    api_token: str | None
    #: Bearer required to mint a session. Unset means anyone who can reach this service can
    #: open one — and a session's tools carry *our* ``/v1`` credential, so an open
    #: ``StartSession`` is a confused-deputy path into the corpus, not merely a cost risk.
    start_session_token: str | None
    openai_api_key_present: bool
    openai_realtime_model: str
    #: The ``?tool_groups=`` value this service's ``/mcp`` connection asks for, verbatim.
    #: Validated by the *server*, which refuses an unknown group with a 400 rather than
    #: quietly serving a smaller surface — so a typo here is a loud connection failure.
    mcp_tool_groups: str
    #: What to present to ``/mcp``. ``None`` when neither variable is set, which is the
    #: laptop case: an open API needs no bearer.
    mcp_token: str | None
    #: Whether :data:`MCP_TOKEN_ENV` supplied it, rather than the whole-API owner token.
    #: Reported on ``/internal/health`` so that "which credential is this?" is a question
    #: the deployment can answer without reading a secret.
    mcp_token_dedicated: bool
    #: Lowercased ``scheme://host[:port]`` origins, compared exactly against a socket's
    #: ``Origin`` header. Empty means unrestricted.
    allowed_origins: frozenset[str] = frozenset()

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> VoiceSettings:
        environ = os.environ if env is None else env

        arm = environ.get(ARM_ENV, DEFAULT_ARM).strip().lower() or DEFAULT_ARM
        if arm not in ARMS:
            raise VoiceConfigError(f"{ARM_ENV} must be one of {', '.join(ARMS)}, got {arm!r}")

        secret = environ.get(SESSION_SECRET_ENV, "").strip()
        provided = bool(secret)
        if not provided:
            # An ephemeral per-process secret keeps a laptop working with no setup, and
            # fails safely on Cloud Run: a second instance mints a different secret, so a
            # token from instance A is rejected by instance B and the misconfiguration
            # announces itself as "my socket keeps getting refused" rather than as a
            # silently unauthenticated service. /internal/health reports which of the two it is.
            secret = secrets.token_urlsafe(32)

        return cls(
            arm=arm,
            inference_mode=current_mode(environ),
            session_secret=secret,
            session_secret_provided=provided,
            session_ttl_seconds=_positive_int(
                environ, SESSION_TTL_ENV, DEFAULT_SESSION_TTL_SECONDS
            ),
            api_base_url=_clean(environ.get(API_BASE_URL_ENV)),
            api_token=_clean(environ.get(API_TOKEN_ENV)),
            start_session_token=_clean(environ.get(START_SESSION_TOKEN_ENV)),
            openai_api_key_present=bool(_clean(environ.get(OPENAI_KEY_ENV))),
            openai_realtime_model=environ.get(
                OPENAI_REALTIME_MODEL_ENV, DEFAULT_OPENAI_REALTIME_MODEL
            ).strip()
            or DEFAULT_OPENAI_REALTIME_MODEL,
            mcp_tool_groups=environ.get(MCP_TOOL_GROUPS_ENV, DEFAULT_MCP_TOOL_GROUPS).strip()
            or DEFAULT_MCP_TOOL_GROUPS,
            mcp_token=_clean(environ.get(MCP_TOKEN_ENV)) or _clean(environ.get(API_TOKEN_ENV)),
            mcp_token_dedicated=_clean(environ.get(MCP_TOKEN_ENV)) is not None,
            allowed_origins=frozenset(
                origin.strip().rstrip("/").lower()
                for origin in environ.get(ALLOWED_ORIGINS_ENV, "").split(",")
                if origin.strip()
            ),
        )

    @property
    def real(self) -> bool:
        return self.inference_mode == "real"

    def describe(self) -> str:
        """A one-line summary for the startup log. Never contains a secret."""
        return (
            f"arm={self.arm} mode={self.inference_mode} "
            f"session_secret={'set' if self.session_secret_provided else 'EPHEMERAL'} "
            f"api={'set' if self.api_base_url else 'unset'} "
            f"start_session_auth={'set' if self.start_session_token else 'OPEN'} "
            f"openai_key={'present' if self.openai_api_key_present else 'absent'} "
            f"origins={'restricted' if self.allowed_origins else 'ANY'} "
            f"mcp={self.describe_mcp()}"
        )

    def describe_mcp(self) -> str:
        """How this service reaches Motet's MCP server. Names, never values."""
        if not self.api_base_url:
            return "unset"
        # The same three words `/internal/health` reports, deliberately: an operator who
        # greps a boot log and an operator who reads the health route must not have to
        # notice that one says NONE and the other says none.
        credential = (
            "scoped" if self.mcp_token_dedicated else ("api_token" if self.mcp_token else "none")
        )
        return f"{self.mcp_tool_groups}/{credential}"


def load_settings(env: Mapping[str, str] | None = None) -> VoiceSettings:
    """Resolve settings and say plainly what is dormant.

    Deliberately **not** a startup crash when a vendor key is missing, which is the
    opposite of what the LLM seam does — and the difference is the point. There, a missing
    key means the pipeline cannot do its job. Here, the whole barge-in harness runs with no
    realtime credential at all: turn detection is local, and it is what is being measured.
    Refusing to boot would take the measurement offline to protect a leg of the service
    that is not being used.
    """
    settings = VoiceSettings.from_env(env)
    logger.info("voice: %s", settings.describe())
    if settings.arm == OPENAI_REALTIME_ARM and not settings.openai_api_key_present:
        logger.warning(
            "%s is unset, so the %s arm cannot open a vendor session. Turn detection still "
            "runs, against the offline emulation of that provider's documented server-VAD "
            "parameters — which is an emulation, not a measurement of the vendor.",
            OPENAI_KEY_ENV,
            OPENAI_REALTIME_ARM,
        )
    if settings.api_base_url is None:
        logger.warning(
            "%s is unset, so this service resolves no MCP server and every platform tool is "
            "dormant: a session can converse, and it cannot mark a story read or save a "
            "highlight.",
            API_BASE_URL_ENV,
        )
    elif settings.mcp_token is None:
        logger.warning(
            "%s reaches %s with no credential (neither %s nor %s is set). That works only "
            "against an API whose own token is unset.",
            MOTET_MCP_SLUG,
            API_BASE_URL_ENV,
            MCP_TOKEN_ENV,
            API_TOKEN_ENV,
        )
    elif not settings.mcp_token_dedicated:
        logger.info(
            "%s presents %s to Motet's MCP server — the whole-API owner token (motet#120, "
            "option a). Set %s to swap in a scoped credential.",
            MOTET_MCP_SLUG,
            API_TOKEN_ENV,
            MCP_TOKEN_ENV,
        )
    if settings.start_session_token is None:
        logger.warning(
            "%s is unset: anyone who can reach this service can mint a voice session, and a "
            "session's tools carry this service's own API credential. Fine on a laptop; on a "
            "deployed environment it is a path into the corpus.",
            START_SESSION_TOKEN_ENV,
        )
    if not settings.session_secret_provided:
        logger.warning(
            "%s is unset: this process minted an ephemeral session secret, so tokens do "
            "not survive a restart and are not valid on another instance.",
            SESSION_SECRET_ENV,
        )
    return settings


def _clean(value: str | None) -> str | None:
    cleaned = (value or "").strip()
    return cleaned or None


def _positive_int(env: Mapping[str, str], name: str, default: int) -> int:
    raw = env.get(name, "").strip()
    if not raw:
        return default
    try:
        value = int(raw)
    except ValueError as exc:
        raise VoiceConfigError(f"{name} must be an integer, got {raw!r}") from exc
    if value <= 0:
        raise VoiceConfigError(f"{name} must be positive, got {value}")
    return value
