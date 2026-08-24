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
OPENAI_KEY_ENV: Final = "OPENAI_API_KEY"
OPENAI_REALTIME_MODEL_ENV: Final = "MOTET_VOICE_OPENAI_REALTIME_MODEL"
EXA_KEY_ENV: Final = "EXA_API_KEY"

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
    api_token: str | None
    openai_api_key_present: bool
    openai_realtime_model: str
    exa_api_key_present: bool

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
            # silently unauthenticated service. /healthz reports which of the two it is.
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
            openai_api_key_present=bool(_clean(environ.get(OPENAI_KEY_ENV))),
            openai_realtime_model=environ.get(
                OPENAI_REALTIME_MODEL_ENV, DEFAULT_OPENAI_REALTIME_MODEL
            ).strip()
            or DEFAULT_OPENAI_REALTIME_MODEL,
            exa_api_key_present=bool(_clean(environ.get(EXA_KEY_ENV))),
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
            f"openai_key={'present' if self.openai_api_key_present else 'absent'} "
            f"exa_key={'present' if self.exa_api_key_present else 'absent'}"
        )


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
