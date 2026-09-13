"""Play Live: the API's half of a voice session (motet#93).

**Invariant 2 is why this module exists.** The voice service holds no database credential
and looks nothing up, so *somebody* has to assemble what a session knows about an episode —
its segments, every claim with the moment it is spoken, where the listener is — and that
somebody is the caller that has the database. In the prototype it was the browser, calling
the voice service directly, which worked only because the voice service's start token was
unset. Here the API builds the context, calls ``StartSession`` server-to-server with the
start token, and hands the browser back only what it needs to open the socket.

**The browser never learns the start token and never names a vendor** (invariant 1): it
gets a session token scoped to one config, a socket URL, and the ``authenticate`` frame to
send on it.

**Dormant unless configured, and that is the deployed state today.** No voice service is
deployed in staging or production, so both variables below are unset there and every route
here answers "not configured" — a 503 on the mint, ``configured: false`` on the status
route the SPA asks first — rather than trying a host that does not exist. Turning it on is
configuration, not code.
"""

from __future__ import annotations

import logging
import os
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Final, Protocol
from urllib.parse import urlsplit, urlunsplit

import httpx

from .schemas import EpisodeResponse

logger = logging.getLogger("motet.api.voice")

#: Where the voice service is, as a browser reaches it — the same URL the API calls
#: ``StartSession`` on, since on Cloud Run both are the service's public address.
VOICE_BASE_URL_ENV: Final = "MOTET_VOICE_BASE_URL"
#: The bearer the voice service requires to mint a session. Deliberately the same name the
#: voice service reads: it is one shared secret with two readers.
VOICE_START_TOKEN_ENV: Final = "MOTET_VOICE_START_SESSION_TOKEN"

#: How long the API waits on ``StartSession``. It is a signature and a JSON body, so a
#: slow answer is an unhealthy service, and the listener is waiting on a button.
START_TIMEOUT_SECONDS: Final = 10.0

#: What the SPA shows when there is no voice service. One sentence, owned here, so the
#: status route and the 503 cannot say two different things.
NOT_CONFIGURED: Final = "Live voice isn't configured in this environment."

#: The notes block is passed in-prompt on every turn; the voice contract caps it at 64k.
NOTES_CAP: Final = 60_000

#: Who the voice is. Server-side so that a persona change is a deploy, and so a client
#: cannot talk the model into being something else.
PERSONA_INSTRUCTIONS: Final = (
    "You are Motet, the voice of a news briefing the listener is hearing right now. They "
    "have just interrupted the narration to ask you something. Answer from the briefing "
    "material you have been given, in one or two spoken sentences, then stop so the "
    "narration can resume."
)

#: The tools a Play Live session may call. Only ``mark_read``: it is the one platform tool
#: whose API route exists and whose arguments match it today, and a persona told it can do
#: something that then fails spends the session apologising.
SESSION_TOOLS: Final = ("mark_read",)


class VoiceUnavailableError(RuntimeError):
    """The voice service could not mint a session. The message is safe to show."""


@dataclass(frozen=True)
class VoiceConfig:
    """The two variables, and whether both are there."""

    base_url: str | None
    start_token: str | None

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> VoiceConfig:
        environ = os.environ if env is None else env
        return cls(
            base_url=_clean(environ.get(VOICE_BASE_URL_ENV)),
            start_token=_clean(environ.get(VOICE_START_TOKEN_ENV)),
        )

    @property
    def configured(self) -> bool:
        """Both, or neither. A URL with no token would mint against an *open* voice service
        and would have to be refused there anyway; a token with no URL has nowhere to go."""
        return self.base_url is not None and self.start_token is not None

    @property
    def reason(self) -> str | None:
        """Why not, naming the variables — they are names, never values, so safe to show."""
        if self.configured:
            return None
        missing = [
            name
            for name, value in (
                (VOICE_BASE_URL_ENV, self.base_url),
                (VOICE_START_TOKEN_ENV, self.start_token),
            )
            if value is None
        ]
        return f"{NOT_CONFIGURED} ({' and '.join(missing)} unset)"


@dataclass(frozen=True)
class StartedVoiceSession:
    session_id: str
    session_token: str
    expires_at: str
    websocket_url: str
    arm: str
    conversational: bool


class VoiceStarter(Protocol):
    """Mints a voice session. Faked in tests at the HTTP transport."""

    def start(self, config: Mapping[str, Any]) -> StartedVoiceSession: ...


class HttpVoiceStarter:
    """``POST {base}/v1/voice/sessions`` with the start token."""

    def __init__(
        self, base_url: str, start_token: str, *, transport: httpx.BaseTransport | None = None
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._start_token = start_token
        self._transport = transport

    def start(self, config: Mapping[str, Any]) -> StartedVoiceSession:
        try:
            with httpx.Client(transport=self._transport, timeout=START_TIMEOUT_SECONDS) as client:
                response = client.post(
                    f"{self._base_url}/v1/voice/sessions",
                    json=dict(config),
                    headers={"Authorization": f"Bearer {self._start_token}"},
                )
        except httpx.HTTPError as exc:
            # The exception text can name the host, which is topology; the log has it, the
            # listener does not need it.
            logger.warning("voice StartSession did not answer: %s", exc)
            raise VoiceUnavailableError("The voice service did not answer.") from exc
        if response.status_code != httpx.codes.CREATED:
            logger.warning(
                "voice StartSession refused: status=%s body=%s",
                response.status_code,
                response.text[:500],
            )
            if response.status_code == httpx.codes.UNAUTHORIZED:
                raise VoiceUnavailableError(
                    "The voice service refused this deployment's start token."
                )
            raise VoiceUnavailableError(
                f"The voice service refused the session ({response.status_code})."
            )
        try:
            body = response.json()
            return StartedVoiceSession(
                session_id=str(body["session_id"]),
                session_token=str(body["session_token"]),
                expires_at=str(body["expires_at"]),
                websocket_url=websocket_url(self._base_url, str(body["websocket_path"])),
                arm=str(body.get("arm", "")),
                conversational=bool(body.get("conversational", False)),
            )
        except (ValueError, KeyError, TypeError, AttributeError) as exc:
            # A 201 this client cannot read is the voice service being wrong, not the API:
            # the same 503 as a refusal, never an unhandled 500.
            logger.warning("voice StartSession answered unreadably: %r", exc)
            raise VoiceUnavailableError(
                "The voice service answered with something unreadable."
            ) from exc


def websocket_url(base_url: str, path: str) -> str:
    """``https://voice.example`` + ``/v1/...`` → ``wss://voice.example/v1/...``."""
    parts = urlsplit(base_url)
    scheme = {"https": "wss", "http": "ws"}.get(parts.scheme, parts.scheme)
    return urlunsplit((scheme, parts.netloc, parts.path.rstrip("/") + path, "", ""))


def build_starter(config: VoiceConfig) -> VoiceStarter | None:
    if config.base_url is None or config.start_token is None:
        return None
    return HttpVoiceStarter(config.base_url, config.start_token)


def session_config(episode: EpisodeResponse, *, spoken_through_ms: int) -> dict[str, Any]:
    """``StartSessionRequest`` for one episode, in the voice service's wire shape.

    The timed transcript carries every claim with its apportioned timing
    (``segment_claims.start_ms`` / ``duration_ms``) — the voice service reads it against
    its own clock to say *what was being said* at the interruption (invariant 4) and looks
    nothing up (invariant 2). ``notes`` is the whole-episode backdrop, capped.
    """
    notes = f"Episode: {episode.title}\n\n"
    for segment in episode.segments:
        block = (
            f"## {segment.news_item_title} (news_item_id {segment.news_item_id}, "
            f"starts at {segment.start_ms // 1000}s)\n"
            + "\n".join(f"- {claim.text}" for claim in segment.claims)
            + "\n\n"
        )
        if len(notes) + len(block) > NOTES_CAP:
            break
        notes += block
    transcript = [
        {
            "title": segment.news_item_title,
            "start_ms": segment.start_ms,
            "end_ms": segment.start_ms + segment.duration_ms,
            "news_item_id": segment.news_item_id,
            "claims": [
                {
                    "start_ms": claim.start_ms,
                    "end_ms": claim.start_ms + claim.duration_ms,
                    "spoken_text": claim.text,
                }
                for claim in segment.claims
            ],
        }
        for segment in episode.segments
    ]
    return {
        "persona": {"name": "Motet", "instructions": PERSONA_INSTRUCTIONS, "voice": "narrator"},
        "tools": [{"name": name} for name in SESSION_TOOLS],
        "mcp_servers": [],
        "context": {
            "episode_id": episode.id,
            "transcript": transcript,
            "spoken_through_ms": max(0, min(spoken_through_ms, episode.duration_ms)),
            "notes": notes,
            "news_item_ids": [segment.news_item_id for segment in episode.segments],
        },
        "turn_policy": {"mode": "open_mic"},
    }


def _clean(value: str | None) -> str | None:
    cleaned = (value or "").strip()
    return cleaned or None
