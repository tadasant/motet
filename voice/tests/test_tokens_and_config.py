"""Session tokens and configuration — the stateless half of running on Cloud Run."""

from __future__ import annotations

import pytest
from motet_voice import tokens
from motet_voice.config import DEFAULT_ARM, VoiceConfigError, VoiceSettings, load_settings

SECRET = "a-secret-for-a-unit-test"


def _mint(digest: str = "digest", ttl: int = 60, now: float = 1_000.0) -> str:
    token, _ = tokens.mint(
        session_id="s1", secret=SECRET, ttl_seconds=ttl, digest=digest, now=lambda: now
    )
    return token


def test_a_valid_token_verifies_and_carries_its_claims() -> None:
    claims = tokens.verify(_mint(), secret=SECRET, digest="digest", now=lambda: 1_010.0)
    assert claims.session_id == "s1"
    assert claims.config_digest == "digest"


def test_a_token_signed_with_another_secret_does_not_verify() -> None:
    with pytest.raises(tokens.SessionTokenError, match="signature"):
        tokens.verify(_mint(), secret="different", digest="digest", now=lambda: 1_010.0)


def test_an_expired_token_is_refused() -> None:
    with pytest.raises(tokens.SessionTokenError, match="expired"):
        tokens.verify(_mint(ttl=10), secret=SECRET, digest="digest", now=lambda: 2_000.0)


def test_a_token_minted_for_a_different_config_is_refused() -> None:
    with pytest.raises(tokens.SessionTokenError, match="different session config"):
        tokens.verify(_mint(), secret=SECRET, digest="other", now=lambda: 1_010.0)


def test_a_malformed_token_is_refused_rather_than_crashing() -> None:
    for bad in ("", "nope", "v2.a.b.c.d", "v1.a.b.c"):
        with pytest.raises(tokens.SessionTokenError):
            tokens.verify(bad, secret=SECRET, now=lambda: 1_010.0)


def test_the_digest_does_not_depend_on_key_order() -> None:
    assert tokens.config_digest({"a": 1, "b": [2, 3]}) == tokens.config_digest(
        {"b": [2, 3], "a": 1}
    )


def test_settings_default_to_the_arm_that_can_actually_run() -> None:
    assert VoiceSettings.from_env({}).arm == DEFAULT_ARM


def test_an_unknown_arm_is_a_startup_error() -> None:
    with pytest.raises(VoiceConfigError, match="MOTET_VOICE_ARM"):
        VoiceSettings.from_env({"MOTET_VOICE_ARM": "telepathy"})


def test_a_nonsense_ttl_is_a_startup_error() -> None:
    with pytest.raises(VoiceConfigError):
        VoiceSettings.from_env({"MOTET_VOICE_SESSION_TTL_SECONDS": "soon"})
    with pytest.raises(VoiceConfigError):
        VoiceSettings.from_env({"MOTET_VOICE_SESSION_TTL_SECONDS": "-5"})


def test_the_inference_mode_is_read_from_the_one_parser() -> None:
    """AGENTS.md: MOTET_INFERENCE_MODE is parsed in exactly one place."""
    assert VoiceSettings.from_env({"MOTET_INFERENCE_MODE": " REAL "}).real is True
    with pytest.raises(ValueError):
        VoiceSettings.from_env({"MOTET_INFERENCE_MODE": "sortof"})


def test_a_missing_realtime_key_does_not_stop_the_service_from_starting() -> None:
    """The measurement needs no credential; refusing to boot would take it offline."""
    settings = load_settings({"MOTET_VOICE_ARM": "openai_realtime", "MOTET_INFERENCE_MODE": "fake"})
    assert settings.openai_api_key_present is False


def test_describe_never_leaks_a_secret() -> None:
    described = VoiceSettings.from_env(
        {"MOTET_VOICE_SESSION_SECRET": "hunter2", "OPENAI_API_KEY": "sk-not-real"}
    ).describe()
    assert "hunter2" not in described
    assert "sk-not-real" not in described
    assert "openai_key=present" in described
