"""Which model each stage uses, resolved from the environment and checked at startup.

**Model selection is configuration, not code, and it is per stage.** The three text
stages have genuinely different cost and correctness profiles:

* **dedup/integrate** runs once per source item against the whole window of news items.
  It is the volume line and the reason prompt caching matters at all.
* **script** is a handful of calls per episode, and quality is user-visible prose.
* **grounding** decides whether a claim is allowed to be spoken. Invariant 3 lives here,
  so it gets the most thinking and, when it matters, the strongest model.

One global default, one override per stage. Nothing here is a code change.

**Why a committed catalog.** A slug typo is invisible until a request fails in
production, which is exactly the class of failure that should be caught at deploy time.
:data:`KNOWN_MODELS` is verified against OpenRouter's live model list by
``bin/check-openrouter-models`` — that script is not part of ``bin/ci``, because CI is
offline and free by design (invariant 7). A slug outside the catalog stops the process
with a message naming the script; ``MOTET_LLM_ALLOW_UNLISTED_MODEL=true`` is the escape
hatch for the hour between a vendor shipping a model and someone updating this file.
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum
from typing import Final, get_args

from .credentials import CredentialKind
from .types import Effort, LlmConfigError

#: The default everywhere. Verified live against OpenRouter's model list on 2026-08-23;
#: its canonical slug there is ``anthropic/claude-sonnet-5-20260630``.
DEFAULT_MODEL: Final = "anthropic/claude-sonnet-5"

PROVIDER_ENV: Final = "MOTET_LLM_PROVIDER"
MODEL_ENV: Final = "MOTET_LLM_MODEL"
EFFORT_ENV: Final = "MOTET_LLM_EFFORT"
TIMEOUT_ENV: Final = "MOTET_LLM_TIMEOUT_SECONDS"
CREDENTIAL_KIND_ENV: Final = "MOTET_LLM_CREDENTIAL_KIND"
ALLOW_UNLISTED_ENV: Final = "MOTET_LLM_ALLOW_UNLISTED_MODEL"
#: Shared with ``motet_inference.registry``: ``fake`` everywhere except staging and prod.
INFERENCE_MODE_ENV: Final = "MOTET_INFERENCE_MODE"

DEFAULT_TIMEOUT_SECONDS: Final = 120.0

_EFFORTS: Final[tuple[Effort, ...]] = get_args(Effort)


class Provider(StrEnum):
    """Who serves the completion."""

    OPENROUTER = "openrouter"
    FAKE = "fake"


class LlmStage(StrEnum):
    """The inference stages that call a text model.

    TTS is not here: it is a different vendor with a different shape, and it sits behind
    its own stage Protocol in ``motet_inference.interfaces``.
    """

    DEDUP = "dedup"
    SCRIPT = "script"
    GROUNDING = "grounding"

    @property
    def model_env(self) -> str:
        return f"{MODEL_ENV}_{self.value.upper()}"

    @property
    def effort_env(self) -> str:
        return f"{EFFORT_ENV}_{self.value.upper()}"


@dataclass(frozen=True)
class ModelSpec:
    """What the catalog knows about a slug.

    ``efforts`` empty means the model has no selectable reasoning effort. That is not
    trivia: OpenRouter silently drops a ``reasoning`` field such a model cannot honour,
    so asking for effort on one is a misconfiguration that would otherwise never
    announce itself. :func:`load_config` rejects the pairing at startup instead.
    """

    slug: str
    context_tokens: int
    max_output_tokens: int
    efforts: tuple[Effort, ...] = ()
    supports_cache_ttl_1h: bool = False


def _spec(
    slug: str,
    context_tokens: int,
    max_output_tokens: int,
    efforts: tuple[Effort, ...] = (),
    supports_cache_ttl_1h: bool = False,
) -> tuple[str, ModelSpec]:
    return slug, ModelSpec(slug, context_tokens, max_output_tokens, efforts, supports_cache_ttl_1h)


_FULL_EFFORTS: Final[tuple[Effort, ...]] = ("low", "medium", "high", "xhigh", "max")

#: Slugs known to work, with the facts that affect how a request is built. Every row was
#: read off ``GET https://openrouter.ai/api/v1/models`` on 2026-08-23; re-verify with
#: ``bin/check-openrouter-models`` rather than by hand.
KNOWN_MODELS: Final[Mapping[str, ModelSpec]] = dict(
    (
        _spec(DEFAULT_MODEL, 1_000_000, 128_000, _FULL_EFFORTS, True),
        _spec("anthropic/claude-opus-5", 1_000_000, 128_000, _FULL_EFFORTS, True),
        _spec("anthropic/claude-opus-4.8", 1_000_000, 128_000, _FULL_EFFORTS, True),
        _spec(
            "anthropic/claude-sonnet-4.6",
            1_000_000,
            128_000,
            ("low", "medium", "high", "max"),
            True,
        ),
        # No selectable effort. Kept in the catalog because it is the obvious candidate
        # for the dedup volume line — and because pairing it with an effort override is
        # the misconfiguration this catalog is here to catch.
        _spec("anthropic/claude-haiku-4.5", 200_000, 64_000, (), True),
        _spec("openai/gpt-5.1", 400_000, 128_000, ("low", "medium", "high")),
    )
)

#: Per-stage defaults. Grounding gets the deepest thinking because a wrong verdict there
#: is a fabricated claim reaching audio; dedup gets the shallowest because it is the
#: volume line and its judgement is comparatively mechanical.
DEFAULT_EFFORTS: Final[Mapping[LlmStage, Effort]] = {
    LlmStage.DEDUP: "low",
    LlmStage.SCRIPT: "high",
    LlmStage.GROUNDING: "max",
}


@dataclass(frozen=True)
class StageConfig:
    """The resolved model and thinking depth for one stage."""

    stage: LlmStage
    model: str
    effort: Effort | None


@dataclass(frozen=True)
class LlmConfig:
    """Everything the seam needs, resolved once and validated once."""

    provider: Provider
    credential_kind: CredentialKind
    stages: Mapping[LlmStage, StageConfig]
    timeout_seconds: float
    allow_unlisted_model: bool

    def for_stage(self, stage: LlmStage) -> StageConfig:
        return self.stages[stage]

    def describe(self) -> str:
        """A one-line, secret-free summary for the startup log."""
        models = " ".join(f"{s.value}={self.stages[s].model}" for s in LlmStage)
        return f"provider={self.provider.value} credential={self.credential_kind.value} {models}"


def _default_provider(environ: Mapping[str, str]) -> Provider:
    """Inherit the inference mode unless told otherwise.

    ``MOTET_INFERENCE_MODE`` already decides whether this process may talk to a vendor.
    Deriving the provider from it means a test or a laptop cannot start spending money by
    forgetting a second variable, and a missing variable fails toward the free side.
    """
    return Provider.OPENROUTER if environ.get(INFERENCE_MODE_ENV) == "real" else Provider.FAKE


def _parse_enum[T: StrEnum](raw: str, enum: type[T], var: str) -> T:
    try:
        return enum(raw.strip().lower())
    except ValueError:
        allowed = ", ".join(member.value for member in enum)
        raise LlmConfigError(f"{var}={raw!r} is not one of: {allowed}") from None


#: Distinguishes "the variable is not set" from "the variable says: no reasoning".
_UNSET: Final = object()

#: What a caller writes to turn reasoning off for a stage rather than leaving it default.
#: An empty string cannot mean this: an unset and an empty variable are the same thing in
#: a Cloud Run service definition, so silence has to keep meaning "use the default".
OFF_VALUES: Final = ("off", "none")


def _parse_effort_setting(raw: str, var: str) -> Effort | None:
    value = raw.strip().lower()
    if value in OFF_VALUES:
        return None
    if value not in _EFFORTS:
        allowed = ", ".join((*_EFFORTS, *OFF_VALUES))
        raise LlmConfigError(f"{var}={raw!r} is not one of: {allowed}")
    return value


def _parse_timeout(raw: str) -> float:
    try:
        seconds = float(raw)
    except ValueError:
        raise LlmConfigError(f"{TIMEOUT_ENV}={raw!r} is not a number") from None
    if seconds <= 0:
        raise LlmConfigError(f"{TIMEOUT_ENV}={raw!r} must be positive")
    return seconds


def _parse_bool(raw: str, var: str) -> bool:
    value = raw.strip().lower()
    if value in ("1", "true", "yes"):
        return True
    if value in ("", "0", "false", "no"):
        return False
    raise LlmConfigError(f"{var}={raw!r} is not a boolean")


def load_config(env: Mapping[str, str] | None = None) -> LlmConfig:
    """Read and validate the configuration. Raises on anything it cannot make sense of.

    Does *not* touch credentials — that is :func:`validate_startup`, so that config can
    be inspected and tested without a key anywhere near it.
    """
    environ = os.environ if env is None else env

    provider = (
        _parse_enum(environ[PROVIDER_ENV], Provider, PROVIDER_ENV)
        if environ.get(PROVIDER_ENV, "").strip()
        else _default_provider(environ)
    )
    credential_kind = (
        _parse_enum(environ[CREDENTIAL_KIND_ENV], CredentialKind, CREDENTIAL_KIND_ENV)
        if environ.get(CREDENTIAL_KIND_ENV, "").strip()
        else CredentialKind.API_KEY
    )
    allow_unlisted = _parse_bool(environ.get(ALLOW_UNLISTED_ENV, ""), ALLOW_UNLISTED_ENV)
    timeout = (
        _parse_timeout(environ[TIMEOUT_ENV])
        if environ.get(TIMEOUT_ENV, "").strip()
        else DEFAULT_TIMEOUT_SECONDS
    )

    global_model = environ.get(MODEL_ENV, "").strip() or DEFAULT_MODEL
    global_effort: Effort | None | object = (
        _parse_effort_setting(environ[EFFORT_ENV], EFFORT_ENV)
        if environ.get(EFFORT_ENV, "").strip()
        else _UNSET
    )

    stages: dict[LlmStage, StageConfig] = {}
    for stage in LlmStage:
        # Which variable supplied the model, so an error can name the one to change
        # rather than the one that happens to be stage-shaped.
        model_source = stage.model_env if environ.get(stage.model_env, "").strip() else MODEL_ENV
        model = environ.get(stage.model_env, "").strip() or global_model
        effort: Effort | None
        if environ.get(stage.effort_env, "").strip():
            effort = _parse_effort_setting(environ[stage.effort_env], stage.effort_env)
        elif global_effort is not _UNSET:
            effort = global_effort  # type: ignore[assignment]
        else:
            effort = DEFAULT_EFFORTS[stage]
        _check_model(model, model_source, effort, stage, allow_unlisted)
        stages[stage] = StageConfig(stage=stage, model=model, effort=effort)

    return LlmConfig(
        provider=provider,
        credential_kind=credential_kind,
        stages=stages,
        timeout_seconds=timeout,
        allow_unlisted_model=allow_unlisted,
    )


def _check_model(
    model: str,
    model_source: str,
    effort: Effort | None,
    stage: LlmStage,
    allow_unlisted: bool,
) -> None:
    spec = KNOWN_MODELS.get(model)
    if spec is None:
        if allow_unlisted:
            return
        raise LlmConfigError(
            f"{model_source} sets stage {stage.value!r} to model {model!r}, which is not "
            f"in the catalog in motet_inference.llm.config. "
            f"Known: {', '.join(sorted(KNOWN_MODELS))}. "
            "Run bin/check-openrouter-models to verify a slug against OpenRouter's live "
            f"list and add it, or set {ALLOW_UNLISTED_ENV}=true to skip this check."
        )
    if effort is not None and not spec.efforts:
        raise LlmConfigError(
            f"stage {stage.value!r} asks for reasoning effort {effort!r} on {model!r}, "
            "which has no selectable effort. OpenRouter would drop the field silently and "
            f"answer without thinking. Set {stage.effort_env}=off to disable reasoning "
            "for this stage, or choose a model that supports it."
        )
    if effort is not None and effort not in spec.efforts:
        raise LlmConfigError(
            f"stage {stage.value!r} asks for effort {effort!r} on {model!r}, which "
            f"supports only: {', '.join(spec.efforts)}."
        )


def validate_startup(env: Mapping[str, str] | None = None) -> LlmConfig:
    """Fail the process now if it could not serve a request later.

    Called from the API's lifespan and from the worker entry point. Checks the config and
    — when a real provider is selected — that the credential actually resolves. A
    missing key is a startup crash with a clear message, never a 500 at 3am.
    """
    from .credentials import resolve_credential

    config = load_config(env)
    if config.provider is not Provider.FAKE:
        resolve_credential(config.credential_kind, env)
    return config
