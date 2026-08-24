"""Cartesia Sonic — the narration voice.

Invariant: **Sonic narrates, the realtime model converses.** Two voices on purpose, so
that voice identity is not welded to whichever realtime vendor Phase 2 settles on.

Everything a vendor could change out from under us is configuration rather than a
constant in code: the API version header, the model id, the voice id, and the output
format. A version bump or a voice change must be an environment change on a Cloud Run
revision, not a pull request — that is the same reasoning that makes the LLM model a
variable.

MP3 rather than WAV, deliberately. A twenty-minute WAV is about a hundred megabytes to
push through a phone's cellular connection on a dog walk; the same audio as MP3 is under
twenty. MP3 also concatenates frame-wise, which is what lets narration be synthesized one
segment at a time and joined — see ``motet_inference.audio``.
"""

from __future__ import annotations

import logging
import os
from collections.abc import Mapping
from typing import Any, Final

from .audio import MPEG_MEDIA_TYPE, AudioError, mpeg_duration_ms
from .types import Audio

logger = logging.getLogger("motet.tts.cartesia")

DEFAULT_BASE_URL: Final = "https://api.cartesia.ai"

API_KEY_ENV: Final = "CARTESIA_API_KEY"
VERSION_ENV: Final = "CARTESIA_VERSION"
MODEL_ENV: Final = "MOTET_TTS_MODEL"
VOICE_ENV: Final = "MOTET_TTS_VOICE_ID"
LANGUAGE_ENV: Final = "MOTET_TTS_LANGUAGE"
SAMPLE_RATE_ENV: Final = "MOTET_TTS_SAMPLE_RATE"
BIT_RATE_ENV: Final = "MOTET_TTS_BIT_RATE"
TIMEOUT_ENV: Final = "MOTET_TTS_TIMEOUT_SECONDS"

#: Defaults, every one of them overridable. They are a starting point that a deployed
#: environment is expected to pin explicitly — a vendor deprecating a version or a model
#: must never require a code change to survive.
DEFAULT_VERSION: Final = "2024-06-10"
DEFAULT_MODEL: Final = "sonic-2"
DEFAULT_LANGUAGE: Final = "en"
DEFAULT_SAMPLE_RATE: Final = 44_100
DEFAULT_BIT_RATE: Final = 128_000
DEFAULT_TIMEOUT_SECONDS: Final = 180.0


class TtsError(RuntimeError):
    """Synthesis failed, or came back as something that is not playable audio."""


class TtsConfigError(TtsError):
    """Synthesis was not attempted because the configuration could not support it."""


class CartesiaConfig:
    """Resolved TTS settings. Reading the environment happens here and nowhere else."""

    def __init__(self, env: Mapping[str, str] | None = None) -> None:
        environ = os.environ if env is None else env
        self.api_key = environ.get(API_KEY_ENV, "").strip()
        self.version = environ.get(VERSION_ENV, "").strip() or DEFAULT_VERSION
        self.model = environ.get(MODEL_ENV, "").strip() or DEFAULT_MODEL
        self.voice_id = environ.get(VOICE_ENV, "").strip()
        self.language = environ.get(LANGUAGE_ENV, "").strip() or DEFAULT_LANGUAGE
        self.sample_rate = _positive_int(environ, SAMPLE_RATE_ENV, DEFAULT_SAMPLE_RATE)
        self.bit_rate = _positive_int(environ, BIT_RATE_ENV, DEFAULT_BIT_RATE)
        self.timeout_seconds = _positive_float(environ, TIMEOUT_ENV, DEFAULT_TIMEOUT_SECONDS)
        self.base_url = DEFAULT_BASE_URL

    def validate(self) -> None:
        """Fail at startup rather than partway through rendering an episode.

        A missing voice id is the interesting one: the request would be rejected only
        after the script has been written and grounding-validated, which is the expensive
        part. Checking it on boot means a bad revision never takes traffic.
        """
        required = ((API_KEY_ENV, self.api_key), (VOICE_ENV, self.voice_id))
        missing = [name for name, value in required if not value]
        if missing:
            raise TtsConfigError(
                f"{', '.join(missing)} unset, so no narration can be synthesized. These are "
                "injected from Secret Manager and the service definition (private "
                "infrastructure repo); locally, put them in .env. To run with no TTS "
                "vendor at all, set MOTET_INFERENCE_MODE=fake."
            )


def _positive_int(environ: Mapping[str, str], var: str, default: int) -> int:
    raw = environ.get(var, "").strip()
    if not raw:
        return default
    try:
        value = int(raw)
    except ValueError:
        raise TtsConfigError(f"{var}={raw!r} is not an integer") from None
    if value <= 0:
        raise TtsConfigError(f"{var}={raw!r} must be positive")
    return value


def _positive_float(environ: Mapping[str, str], var: str, default: float) -> float:
    raw = environ.get(var, "").strip()
    if not raw:
        return default
    try:
        value = float(raw)
    except ValueError:
        raise TtsConfigError(f"{var}={raw!r} is not a number") from None
    if value <= 0:
        raise TtsConfigError(f"{var}={raw!r} must be positive")
    return value


def build_payload(text: str, config: CartesiaConfig) -> dict[str, Any]:
    """The request body. Pure and module-level, so a test can assert the exact bytes."""
    return {
        "model_id": config.model,
        "transcript": text,
        "voice": {"mode": "id", "id": config.voice_id},
        "language": config.language,
        "output_format": {
            "container": "mp3",
            "sample_rate": config.sample_rate,
            "bit_rate": config.bit_rate,
        },
    }


class CartesiaSpeechSynthesizer:
    """Turn validated copy into MP3 bytes.

    Nothing reaches this class that has not already passed grounding validation —
    invariant 3 puts the gate *before* synthesis, so a claim that failed validation is
    never paid for and never spoken.
    """

    def __init__(
        self,
        config: CartesiaConfig | None = None,
        *,
        transport: Any | None = None,
    ) -> None:
        self._config = config or CartesiaConfig()
        self._config.validate()
        self._transport = transport
        self._client: Any | None = None

    def _http(self) -> Any:
        # Imported lazily, like the OpenRouter adapter, so a fake-mode process never
        # pulls in the HTTP client.
        import httpx  # noqa: PLC0415

        if self._client is None:
            self._client = httpx.Client(
                timeout=self._config.timeout_seconds,
                transport=self._transport,
                headers={
                    "X-API-Key": self._config.api_key,
                    "Cartesia-Version": self._config.version,
                },
            )
        return self._client

    def synthesize(self, text: str) -> Audio:
        import httpx  # noqa: PLC0415

        if not text.strip():
            raise TtsError("refusing to synthesize empty text")
        try:
            response = self._http().post(
                f"{self._config.base_url}/tts/bytes",
                json=build_payload(text, self._config),
            )
        except httpx.HTTPError as exc:
            raise TtsError(f"Cartesia request failed: {exc}") from exc

        if response.status_code >= 400:
            raise TtsError(f"Cartesia returned {response.status_code}: {response.text[:500]}")
        data = response.content
        if not data:
            raise TtsError("Cartesia returned an empty body")

        # Measure what actually came back rather than trusting the bit rate we asked for.
        # The duration anchors every segment offset in the episode (invariant 4 — we own
        # playback position), so an assumed number here desynchronizes a transcript from
        # its audio for every listener, silently.
        try:
            duration_ms = mpeg_duration_ms(data)
        except AudioError as exc:
            # A 200 carrying something that is not audio — an error document, an HTML
            # error page from a proxy. Uploading it would produce an episode a player
            # refuses to open, with nothing anywhere having logged a failure.
            raise TtsError(
                f"Cartesia returned {len(data)} bytes that are not MPEG audio: {exc}. "
                f"First bytes: {data[:80]!r}"
            ) from exc
        logger.info(
            "synthesized %d characters to %d bytes of MPEG audio (%d ms)",
            len(text),
            len(data),
            duration_ms,
        )
        return Audio(media_type=MPEG_MEDIA_TYPE, data=data, duration_ms=duration_ms)

    def close(self) -> None:
        if self._client is not None:
            self._client.close()
            self._client = None

    def __enter__(self) -> CartesiaSpeechSynthesizer:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()
