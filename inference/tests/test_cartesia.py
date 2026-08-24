"""The Cartesia adapter, driven through a stub transport.

No network and no key: the whole translate-send-parse path runs against
``httpx.MockTransport``, which is the same shape the OpenRouter adapter's tests use.
Invariant 7 says no test in this repo may make a real vendor call, and an adapter that
could only be tested by calling one would be an adapter nobody tests.
"""

from __future__ import annotations

import httpx
import pytest
from motet_inference.audio import MPEG_MEDIA_TYPE, mpeg_duration_ms
from motet_inference.cartesia import (
    API_KEY_ENV,
    VOICE_ENV,
    CartesiaConfig,
    CartesiaSpeechSynthesizer,
    TtsConfigError,
    TtsError,
    build_payload,
    validate_tts_startup,
)

# One 128 kbps / 44.1 kHz MPEG-1 Layer III frame: 1152 samples, 417 bytes.
FRAME = bytes([0xFF, 0xFB, 0x90, 0x00]) + b"\x00" * 413


@pytest.fixture
def config(monkeypatch: pytest.MonkeyPatch) -> CartesiaConfig:
    monkeypatch.setenv(API_KEY_ENV, "sk-test-not-a-real-key")
    monkeypatch.setenv(VOICE_ENV, "voice-abc")
    return CartesiaConfig()


def transport(handler: object) -> httpx.MockTransport:
    return httpx.MockTransport(handler)  # type: ignore[arg-type]


class TestConfig:
    def test_a_missing_key_or_voice_is_a_startup_failure(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Both, checked together, and checked on boot.

        A missing voice id would otherwise only surface after the script has been written
        and grounding-validated — the expensive part — and would then fail every retry.
        """
        monkeypatch.delenv(API_KEY_ENV, raising=False)
        monkeypatch.delenv(VOICE_ENV, raising=False)
        with pytest.raises(TtsConfigError) as excinfo:
            validate_tts_startup()
        assert API_KEY_ENV in str(excinfo.value)
        assert VOICE_ENV in str(excinfo.value)

    def test_constructing_the_synthesizer_needs_no_credentials(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """``Stages`` is resolved as a bundle, so every worker builds every stage.

        Validating on construction would mean the dedup worker — which never speaks —
        refused to start without the TTS secrets mounted, the same blast-radius mistake
        AGENTS.md rejects for the LLM key in the API.
        """
        monkeypatch.delenv(API_KEY_ENV, raising=False)
        monkeypatch.delenv(VOICE_ENV, raising=False)
        CartesiaSpeechSynthesizer(CartesiaConfig())  # no raise

    def test_synthesizing_without_credentials_still_refuses(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Moving the check must not remove it: the last line of defence is the call."""
        monkeypatch.delenv(API_KEY_ENV, raising=False)
        monkeypatch.delenv(VOICE_ENV, raising=False)
        with pytest.raises(TtsConfigError):
            CartesiaSpeechSynthesizer(CartesiaConfig()).synthesize("Hello.")

    def test_every_vendor_fact_is_overridable(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """A vendor deprecating a version must not require a code change to survive."""
        monkeypatch.setenv(API_KEY_ENV, "k")
        monkeypatch.setenv(VOICE_ENV, "v")
        monkeypatch.setenv("CARTESIA_VERSION", "2099-01-01")
        monkeypatch.setenv("MOTET_TTS_MODEL", "sonic-9")
        monkeypatch.setenv("MOTET_TTS_LANGUAGE", "fr")
        monkeypatch.setenv("MOTET_TTS_BIT_RATE", "64000")
        resolved = CartesiaConfig()
        assert (resolved.version, resolved.model, resolved.language, resolved.bit_rate) == (
            "2099-01-01",
            "sonic-9",
            "fr",
            64000,
        )

    def test_a_nonsense_number_is_refused_rather_than_defaulted(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("MOTET_TTS_BIT_RATE", "loud")
        with pytest.raises(TtsConfigError):
            CartesiaConfig()


class TestPayload:
    def test_asks_for_mp3(self, config: CartesiaConfig) -> None:
        """MP3, not WAV — twenty minutes of WAV is ~100 MB over a phone's connection.

        It is also the format that concatenates frame-wise, which is what makes
        per-segment synthesis and joining possible at all.
        """
        payload = build_payload("Hello.", config)
        assert payload["output_format"] == {
            "container": "mp3",
            "sample_rate": 44_100,
            "bit_rate": 128_000,
        }
        assert payload["voice"] == {"mode": "id", "id": "voice-abc"}
        assert payload["transcript"] == "Hello."


class TestSynthesize:
    def test_returns_audio_with_a_measured_duration(self, config: CartesiaConfig) -> None:
        """Measured from the bytes, not assumed from the bit rate we asked for.

        Duration anchors every segment offset (invariant 4 — we own playback position), so
        an assumed number silently desynchronizes a transcript from its audio.
        """
        body = FRAME * 100
        captured: list[httpx.Request] = []

        def handler(request: httpx.Request) -> httpx.Response:
            captured.append(request)
            return httpx.Response(200, content=body)

        audio = CartesiaSpeechSynthesizer(config, transport=transport(handler)).synthesize("Hi.")

        assert audio.media_type == MPEG_MEDIA_TYPE
        assert audio.data == body
        assert audio.duration_ms == mpeg_duration_ms(body)
        assert captured[0].headers["x-api-key"] == "sk-test-not-a-real-key"
        assert captured[0].headers["cartesia-version"] == "2024-06-10"
        assert captured[0].url.path == "/tts/bytes"

    def test_an_error_status_is_raised_with_the_body(self, config: CartesiaConfig) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(429, text="rate limited")

        with pytest.raises(TtsError, match="429"):
            CartesiaSpeechSynthesizer(config, transport=transport(handler)).synthesize("Hi.")

    def test_an_empty_body_is_an_error_not_silent_audio(self, config: CartesiaConfig) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, content=b"")

        with pytest.raises(TtsError, match="empty body"):
            CartesiaSpeechSynthesizer(config, transport=transport(handler)).synthesize("Hi.")

    def test_a_json_error_served_as_200_does_not_become_audio(self, config: CartesiaConfig) -> None:
        """A 200 carrying an error document would otherwise be uploaded as an episode.

        The listener would get a file their player refuses to open, and nothing anywhere
        would have logged a failure.
        """

        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json={"error": "voice not found"})

        with pytest.raises(TtsError):
            CartesiaSpeechSynthesizer(config, transport=transport(handler)).synthesize("Hi.")

    def test_refuses_empty_text_without_calling_the_vendor(self, config: CartesiaConfig) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            raise AssertionError("should not have been called")

        with pytest.raises(TtsError, match="empty text"):
            CartesiaSpeechSynthesizer(config, transport=transport(handler)).synthesize("   ")
