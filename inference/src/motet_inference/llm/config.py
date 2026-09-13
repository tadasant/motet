"""Which model each stage uses, resolved from the environment and checked at startup.

**Model selection is configuration, not code, and it is per stage.** The four text
callers have genuinely different cost and correctness profiles:

* **dedup/integrate** runs once per source item against the whole window of news items.
  It is the volume line and the reason prompt caching matters at all.
* **dedup/confirm** is the second look at a *single* pair, made only when the first pass
  answered ``related`` — the uncertain band motet#41 fell into. It is rare by
  construction, so it can afford the thinking the volume line cannot.
* **script** is a handful of calls per episode, and quality is user-visible prose.
* **voice** is a spoken conversational turn, and its currency is latency rather than
  depth. It defaults to no reasoning at all.

One global default, one override per stage. Nothing here is a code change.

**Above both, on a laptop and in staging only, a ``settings`` row** (motet#92). The
precedence is ``settings`` > ``MOTET_LLM_*_<STAGE>`` > ``MOTET_LLM_*`` > default, and
:class:`ConfigSource` says which rung won. This package still knows nothing about a
database: the rows reach :func:`load_config` as its ``overrides`` argument, or through
:func:`llm_overrides`, which the worker installs around each job. Whether rows are read at
all is decided by the deployment — ``MOTET_SETTINGS_WRITABLE``, parsed in
``motet_db.settings`` and never set in production — so production still resolves from the
environment alone, and there an unknown slug is still a startup crash.

**Why a committed catalog.** A slug typo is invisible until a request fails in
production, which is exactly the class of failure that should be caught at deploy time.
:data:`KNOWN_MODELS` is verified against OpenRouter's live model list by
``bin/check-openrouter-models`` — that script is not part of ``bin/ci``, because CI is
offline and free by design (invariant 7). A slug outside the catalog stops the process
with a message naming the script; ``MOTET_LLM_ALLOW_UNLISTED_MODEL=true`` is the escape
hatch for the hour between a vendor shipping a model and someone updating this file.
"""

from __future__ import annotations

import logging
import os
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from enum import StrEnum
from typing import Final, get_args

from ..mode import MODE_ENV_VAR, current_mode
from .credentials import CredentialKind
from .types import CacheTtl, Effort, LlmConfigError, Usage

logger = logging.getLogger("motet.llm.config")

#: The default everywhere. Verified live against OpenRouter's model list on 2026-08-23;
#: its canonical slug there is ``anthropic/claude-sonnet-5-20260630``.
DEFAULT_MODEL: Final = "anthropic/claude-sonnet-5"

PROVIDER_ENV: Final = "MOTET_LLM_PROVIDER"
MODEL_ENV: Final = "MOTET_LLM_MODEL"
EFFORT_ENV: Final = "MOTET_LLM_EFFORT"
TIMEOUT_ENV: Final = "MOTET_LLM_TIMEOUT_SECONDS"
ALLOW_UNLISTED_ENV: Final = "MOTET_LLM_ALLOW_UNLISTED_MODEL"

DEFAULT_TIMEOUT_SECONDS: Final = 120.0

#: The ``settings`` keys that sit above the environment: ``llm.model.dedup``,
#: ``llm.effort.script`` and so on, one per stage per axis. Values are spelled exactly as
#: the matching environment variable would spell them, ``off`` included.
SETTING_PREFIX: Final = "llm."
MODEL_SETTING_PREFIX: Final = f"{SETTING_PREFIX}model."
EFFORT_SETTING_PREFIX: Final = f"{SETTING_PREFIX}effort."

_EFFORTS: Final[tuple[Effort, ...]] = get_args(Effort)


class Provider(StrEnum):
    """Who serves the completion."""

    OPENROUTER = "openrouter"
    FAKE = "fake"


class LlmStage(StrEnum):
    """Everything in Motet that calls a text model.

    A "stage" here is a *caller with its own cost and correctness profile*, not
    necessarily a step in the ingestion pipeline. :attr:`VOICE` is the conversational
    turn on the voice service's composed arm: it is not part of the pipeline, but it is a
    text-model call whose model and thinking depth a deployment wants to set separately —
    and, more to the point, one whose slug should be checked against the catalogue at
    startup rather than sent to a vendor unverified. It resolved its own model from a
    module-local variable until motet#6.

    TTS is not here: it is a different vendor with a different shape, and it sits behind
    its own stage Protocol in ``motet_inference.interfaces``.
    """

    DEDUP = "dedup"
    DEDUP_CONFIRM = "dedup_confirm"
    SCRIPT = "script"
    VOICE = "voice"

    @property
    def model_env(self) -> str:
        return f"{MODEL_ENV}_{self.value.upper()}"

    @property
    def effort_env(self) -> str:
        return f"{EFFORT_ENV}_{self.value.upper()}"

    @property
    def model_setting(self) -> str:
        return f"{MODEL_SETTING_PREFIX}{self.value}"

    @property
    def effort_setting(self) -> str:
        return f"{EFFORT_SETTING_PREFIX}{self.value}"


@dataclass(frozen=True)
class ModelSpec:
    """What the catalog knows about a slug.

    ``efforts`` empty means the model has no selectable reasoning effort. That is not
    trivia: OpenRouter silently drops a ``reasoning`` field such a model cannot honour,
    so asking for effort on one is a misconfiguration that would otherwise never
    announce itself. :func:`load_config` rejects the pairing at startup instead.

    ``adaptive_thinking`` records which side of a generational split the model sits on,
    and it decides whether an unthought answer is a fault or an answer (motet#31):

    * **Budget-based** (everything before Claude 4.6). ``reasoning.effort`` is converted
      by OpenRouter into a ``thinking.budget_tokens`` figure, so the model thinks iff the
      field survived — and a response with no reasoning in it means the field did not,
      which is exactly what
      :class:`~motet_inference.llm.types.ReasoningNotAppliedError` was written to catch.
    * **Adaptive** (Claude 4.6 and later, this catalog's whole Anthropic half).
      ``reasoning.effort`` sets Anthropic's ``output_config.effort`` and never a budget;
      *Claude* decides per response whether the task is worth thinking about. Zero
      reasoning tokens is then a thing the model is allowed to do, so it no longer
      identifies a dropped field, and the guard can only produce false positives —
      each of which costs a completion that was billed and then discarded.

    ``reasoning_on_by_default`` is a **second, narrower** fact and the two are easy to
    conflate, which is how the first draft of this got it wrong. It is not "Claude 4.6 and
    later": ``GET /api/v1/models`` reports ``reasoning.default_enabled`` true for Sonnet 5
    and Opus 5, false for Opus 4.8, and absent for Sonnet 4.6. Where it *is* true the
    argument above gets strictly stronger — a dropped ``reasoning`` field would leave
    thinking on at the default ``high`` rather than off, so an unthought answer cannot be
    a dropped field even in principle. That is the pair the guard actually fired on. Where
    it is false, an unthought answer is merely *ambiguous* between the two causes, which
    is reason enough not to raise but not the same claim.

    Both were read off ``GET https://openrouter.ai/api/v1/models`` on 2026-08-25 and cross
    -checked against OpenRouter's Sonnet 5 / Claude 4.7 / Claude 4.6 migration guides.
    ``reasoning_on_by_default`` is drift-checked by ``bin/check-openrouter-models``;
    ``adaptive_thinking`` cannot be, because the live list says which efforts a slug takes
    and never what an effort *does* to it.

    **Prices are USD per million tokens**, read off the same endpoint's ``pricing`` block
    (quoted per token there) and drift-checked by the same script (motet#92).
    ``cache_write`` is the 5-minute TTL's rate and ``cache_write_1h`` the hour's, because
    the pipeline asks for both — dedup's window is cached for an hour, the script prompt
    for five minutes — and the usage block does not say which one a write was. See
    :func:`usage_cost_usd`.

    ``canonical_slug`` is the dated snapshot OpenRouter reports in a response's ``model``
    field, which is what a usage record carries: ``anthropic/claude-sonnet-4.6`` answers as
    ``anthropic/claude-4.6-sonnet-20260217``, a shape no suffix rule recovers. Without it a
    real completion would be priced as an unknown model. Drift-checked too.
    """

    slug: str
    context_tokens: int
    max_output_tokens: int
    efforts: tuple[Effort, ...] = ()
    supports_cache_ttl_1h: bool = False
    adaptive_thinking: bool = False
    reasoning_on_by_default: bool = False
    canonical_slug: str = ""
    input_usd_per_mtok: float = 0.0
    output_usd_per_mtok: float = 0.0
    cache_read_usd_per_mtok: float = 0.0
    cache_write_usd_per_mtok: float = 0.0
    cache_write_1h_usd_per_mtok: float = 0.0


_FULL_EFFORTS: Final[tuple[Effort, ...]] = ("low", "medium", "high", "xhigh", "max")

#: Slugs known to work, with the facts that affect how a request is built. Every row was
#: read off ``GET https://openrouter.ai/api/v1/models`` on 2026-08-23, and its prices and
#: canonical slug on 2026-09-13; re-verify with ``bin/check-openrouter-models`` rather than
#: by hand.
KNOWN_MODELS: Final[Mapping[str, ModelSpec]] = {
    DEFAULT_MODEL: ModelSpec(
        DEFAULT_MODEL,
        1_000_000,
        128_000,
        _FULL_EFFORTS,
        supports_cache_ttl_1h=True,
        adaptive_thinking=True,
        reasoning_on_by_default=True,
        canonical_slug="anthropic/claude-sonnet-5-20260630",
        input_usd_per_mtok=2.0,
        output_usd_per_mtok=10.0,
        cache_read_usd_per_mtok=0.2,
        cache_write_usd_per_mtok=2.5,
        cache_write_1h_usd_per_mtok=4.0,
    ),
    "anthropic/claude-opus-5": ModelSpec(
        "anthropic/claude-opus-5",
        1_000_000,
        128_000,
        _FULL_EFFORTS,
        supports_cache_ttl_1h=True,
        adaptive_thinking=True,
        reasoning_on_by_default=True,
        canonical_slug="anthropic/claude-opus-5-20260723",
        input_usd_per_mtok=5.0,
        output_usd_per_mtok=25.0,
        cache_read_usd_per_mtok=0.5,
        cache_write_usd_per_mtok=6.25,
        cache_write_1h_usd_per_mtok=10.0,
    ),
    # Adaptive like its neighbours, but reasoning is *off* until asked for — the pair of
    # facts is not one fact, and this row is the counterexample that says so.
    "anthropic/claude-opus-4.8": ModelSpec(
        "anthropic/claude-opus-4.8",
        1_000_000,
        128_000,
        _FULL_EFFORTS,
        supports_cache_ttl_1h=True,
        adaptive_thinking=True,
        canonical_slug="anthropic/claude-4.8-opus-20260528",
        input_usd_per_mtok=5.0,
        output_usd_per_mtok=25.0,
        cache_read_usd_per_mtok=0.5,
        cache_write_usd_per_mtok=6.25,
        cache_write_1h_usd_per_mtok=10.0,
    ),
    "anthropic/claude-sonnet-4.6": ModelSpec(
        "anthropic/claude-sonnet-4.6",
        1_000_000,
        128_000,
        ("low", "medium", "high", "max"),
        supports_cache_ttl_1h=True,
        adaptive_thinking=True,
        canonical_slug="anthropic/claude-4.6-sonnet-20260217",
        input_usd_per_mtok=3.0,
        output_usd_per_mtok=15.0,
        cache_read_usd_per_mtok=0.3,
        cache_write_usd_per_mtok=3.75,
        cache_write_1h_usd_per_mtok=6.0,
    ),
    # No selectable effort. Kept in the catalog because it is the obvious candidate for
    # the dedup volume line — and because pairing it with an effort override is the
    # misconfiguration this catalog is here to catch.
    "anthropic/claude-haiku-4.5": ModelSpec(
        "anthropic/claude-haiku-4.5",
        200_000,
        64_000,
        supports_cache_ttl_1h=True,
        canonical_slug="anthropic/claude-4.5-haiku-20251001",
        input_usd_per_mtok=1.0,
        output_usd_per_mtok=5.0,
        cache_read_usd_per_mtok=0.1,
        cache_write_usd_per_mtok=1.25,
        cache_write_1h_usd_per_mtok=2.0,
    ),
    # The one row in this catalog with selectable effort and no adaptive thinking, which
    # is why the dropped-config guard in the OpenRouter adapter is still load-bearing
    # rather than dead code: on a model like this, an answer with no reasoning in it can
    # only mean the reasoning config never reached the upstream.
    #
    # Both cache-write prices are zero because OpenRouter lists neither: OpenAI does not
    # charge for writing a prefix into its cache.
    "openai/gpt-5.1": ModelSpec(
        "openai/gpt-5.1",
        400_000,
        128_000,
        ("low", "medium", "high"),
        reasoning_on_by_default=True,
        canonical_slug="openai/gpt-5.1-20251113",
        input_usd_per_mtok=1.25,
        output_usd_per_mtok=10.0,
        cache_read_usd_per_mtok=0.125,
    ),
}

#: A usage record's model as the catalogue row it belongs to — slug or dated snapshot.
_BY_ANY_SLUG: Final[Mapping[str, ModelSpec]] = {
    **{spec.canonical_slug: spec for spec in KNOWN_MODELS.values() if spec.canonical_slug},
    **KNOWN_MODELS,
}


def find_model(model: str) -> ModelSpec | None:
    """The catalogue row for ``model``, whether it is spelled as configured or as served."""
    return _BY_ANY_SLUG.get(model)


#: Per-stage defaults. Script gets the deepest thinking because its output is prose a
#: listener hears; dedup gets the shallowest because it is the volume line and its
#: judgement is comparatively mechanical.
#:
#: ``None`` is a default in its own right and is spelled the same way a deployment spells
#: it — :data:`OFF_VALUE`, which :func:`_parse_effort_setting` maps to ``None``. Voice is
#: the stage that wants it: a spoken turn is generated with somebody standing on a
#: pavement waiting for it, so a second of thinking is a second of silence.
DEFAULT_EFFORTS: Final[Mapping[LlmStage, Effort | None]] = {
    LlmStage.DEDUP: "low",
    # Deeper than the pass it second-guesses, and that asymmetry is the whole cost
    # argument: the volume line stays cheap, and depth is spent only on the pairs the
    # volume line said it was unsure about. motet#41 is a story that was compared against
    # a whole backlog, at `low`, while also being asked to write a headline and a summary.
    LlmStage.DEDUP_CONFIRM: "medium",
    LlmStage.SCRIPT: "high",
    LlmStage.VOICE: None,
}


class ConfigSource(StrEnum):
    """Which rung of the precedence chain supplied a resolved value.

    Highest first: ``settings`` > the stage's variable > the global variable > the
    committed default. Reported per stage, per axis, so that "why is dedup on Opus" has an
    answer that names the thing to change — motet#85 was an afternoon of a shell export
    winning over a ``.env`` line with nothing on any surface saying so.
    """

    SETTINGS = "settings"
    STAGE_ENV = "stage_env"
    GLOBAL_ENV = "global_env"
    DEFAULT = "default"


@dataclass(frozen=True)
class StageConfig:
    """The resolved model and thinking depth for one stage, and where each came from."""

    stage: LlmStage
    model: str
    effort: Effort | None
    model_source: ConfigSource = ConfigSource.DEFAULT
    effort_source: ConfigSource = ConfigSource.DEFAULT


#: ``settings`` rows for :func:`load_config` to resolve against when its caller passed
#: none. A ``ContextVar`` for the reason the usage ledger is one: the stage adapters call
#: ``build_request`` → ``load_config()`` with no arguments, and threading a mapping through
#: them would put a settings table in every ``Protocol`` in ``interfaces``. Unset — which is
#: every process that never calls :func:`llm_overrides` — means the environment alone.
_overrides: ContextVar[Mapping[str, str] | None] = ContextVar("motet_llm_overrides", default=None)


@contextmanager
def llm_overrides(settings: Mapping[str, str]) -> Iterator[None]:
    """Resolve every :func:`load_config` call inside the block against ``settings`` too.

    The caller is expected to have run :func:`validate_overrides` first. Nested blocks
    shadow rather than merge, like :func:`~motet_inference.accounting.collect_usage`.
    """
    token = _overrides.set(dict(settings))
    try:
        yield
    finally:
        _overrides.reset(token)


def usage_cost_usd(model: str, usage: Usage, cache_ttl: CacheTtl | None = None) -> float | None:
    """What one completion — or a sum of them on one model — cost, in USD.

    OpenRouter's ``prompt_tokens`` *includes* both cache figures and ``completion_tokens``
    includes the reasoning tokens; that is how the adapter decodes them into
    :class:`~.types.Usage`. So uncached input is what is left of ``input_tokens`` after the
    cache figures, each cache figure is billed at its own rate, and reasoning is already
    inside ``output_tokens`` and is not added twice. Cache writes are billed at the
    ``1h`` rate when the request asked for it and the ``5m`` rate otherwise.

    ``None`` for a model the catalogue does not know — reachable only through
    ``MOTET_LLM_ALLOW_UNLISTED_MODEL`` — rather than a zero that would read as free, or an
    exception that would take an admin route down with it.
    """
    spec = find_model(model)
    if spec is None:
        return None
    write_rate = (
        spec.cache_write_1h_usd_per_mtok if cache_ttl == "1h" else spec.cache_write_usd_per_mtok
    )
    uncached = max(0, usage.input_tokens - usage.cache_read_tokens - usage.cache_write_tokens)
    return (
        uncached * spec.input_usd_per_mtok
        + usage.cache_read_tokens * spec.cache_read_usd_per_mtok
        + usage.cache_write_tokens * write_rate
        + usage.output_tokens * spec.output_usd_per_mtok
    ) / 1_000_000


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
        """A one-line, secret-free summary for the startup log.

        The **effort** is here as well as the model, and that is not decoration: a stage's
        thinking depth has no other visible surface, and the global ``MOTET_LLM_EFFORT``
        overrides a per-stage default without saying so. Voice is the stage that makes
        this bite — its default is ``off`` precisely so a spoken turn stays fast, and an
        operator who raised the global for pipeline quality would otherwise have no way to
        see that they had also made every conversational turn think.
        """
        stages = " ".join(
            f"{s.value}={self.stages[s].model}@{self.stages[s].effort or OFF_VALUE}"
            for s in LlmStage
        )
        return f"provider={self.provider.value} credential={self.credential_kind.value} {stages}"


def _default_provider(environ: Mapping[str, str]) -> Provider:
    """Inherit the inference mode unless told otherwise.

    ``MOTET_INFERENCE_MODE`` already decides whether this process may talk to a vendor.
    Deriving the provider from it means a test or a laptop cannot start spending money by
    forgetting a second variable, and a missing variable fails toward the free side.

    **The mode is parsed by exactly one function, and it must stay that way.** The stage
    registry normalizes case and whitespace and rejects anything it does not recognize.
    A second, stricter reading here — an exact ``== "real"``, say — would make
    ``MOTET_INFERENCE_MODE=Real`` mean *real stage adapters wired to a fake model*: a
    revision that boots clean, skips the credential check, and feeds
    ``fake-completion:...`` into the script stage and then into audio. "Fails toward the
    free side" is no comfort there; the failure is not free, it is fabricated output that
    looks fine.
    """
    return Provider.OPENROUTER if current_mode(environ) == "real" else Provider.FAKE


def _parse_enum[T: StrEnum](raw: str, enum: type[T], var: str) -> T:
    try:
        return enum(raw.strip().lower())
    except ValueError:
        allowed = ", ".join(member.value for member in enum)
        raise LlmConfigError(f"{var}={raw!r} is not one of: {allowed}") from None


#: What a caller writes to turn reasoning off for a stage rather than leaving it default.
#: An empty string cannot mean this: an unset and an empty variable are the same thing in
#: a Cloud Run service definition, so silence has to keep meaning "use the default".
OFF_VALUE: Final = "off"


def _parse_effort_setting(raw: str, var: str) -> Effort | None:
    value = raw.strip().lower()
    if value == OFF_VALUE:
        return None
    if value not in _EFFORTS:
        allowed = ", ".join((*_EFFORTS, OFF_VALUE))
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


def load_config(
    env: Mapping[str, str] | None = None, overrides: Mapping[str, str] | None = None
) -> LlmConfig:
    """Read and validate the configuration. Raises on anything it cannot make sense of.

    Does *not* touch credentials — that is :func:`validate_startup`, so that config can
    be inspected and tested without a key anywhere near it.

    ``overrides`` is the ``settings`` rows as a mapping; ``None`` means whatever
    :func:`llm_overrides` installed for this context, which outside the worker's per-job
    block is nothing. A value there outranks every environment variable and is validated
    exactly as one would be — though :func:`validate_overrides` is the stricter gate, and
    the one every path that *feeds* this a mapping goes through first.
    """
    environ = os.environ if env is None else env
    settings = overrides if overrides is not None else (_overrides.get() or {})

    provider = (
        _parse_enum(environ[PROVIDER_ENV], Provider, PROVIDER_ENV)
        if environ.get(PROVIDER_ENV, "").strip()
        else _default_provider(environ)
    )
    if provider is Provider.FAKE and current_mode(environ) == "real":
        # A legitimate escape hatch — run real stages against a fake model to shake out
        # wiring without spending — but a silent one would be indistinguishable from the
        # misconfiguration it resembles, so it announces itself.
        logger.warning(
            "%s=fake while %s=real: stages are real but every completion will be "
            "fabricated by the deterministic fake. Nothing will reach a vendor.",
            PROVIDER_ENV,
            MODE_ENV_VAR,
        )
    allow_unlisted = _parse_bool(environ.get(ALLOW_UNLISTED_ENV, ""), ALLOW_UNLISTED_ENV)
    timeout = (
        _parse_timeout(environ[TIMEOUT_ENV])
        if environ.get(TIMEOUT_ENV, "").strip()
        else DEFAULT_TIMEOUT_SECONDS
    )

    global_model = environ.get(MODEL_ENV, "").strip() or DEFAULT_MODEL
    # Presence is checked directly rather than through a sentinel, because "unset" and
    # "set to off" are genuinely different answers and both are legitimate.
    global_effort_set = bool(environ.get(EFFORT_ENV, "").strip())
    global_effort = (
        _parse_effort_setting(environ[EFFORT_ENV], EFFORT_ENV) if global_effort_set else None
    )

    stages: dict[LlmStage, StageConfig] = {}
    for stage in LlmStage:
        # Which name supplied the model, so an error can name the one to change rather
        # than the one that happens to be stage-shaped.
        model_var: str
        model_from: ConfigSource
        if settings.get(stage.model_setting, "").strip():
            model = settings[stage.model_setting].strip()
            model_var, model_from = stage.model_setting, ConfigSource.SETTINGS
        elif environ.get(stage.model_env, "").strip():
            model = environ[stage.model_env].strip()
            model_var, model_from = stage.model_env, ConfigSource.STAGE_ENV
        else:
            model, model_var = global_model, MODEL_ENV
            model_from = (
                ConfigSource.GLOBAL_ENV
                if environ.get(MODEL_ENV, "").strip()
                else ConfigSource.DEFAULT
            )
        effort: Effort | None
        if settings.get(stage.effort_setting, "").strip():
            effort = _parse_effort_setting(settings[stage.effort_setting], stage.effort_setting)
            effort_from = ConfigSource.SETTINGS
        elif environ.get(stage.effort_env, "").strip():
            effort = _parse_effort_setting(environ[stage.effort_env], stage.effort_env)
            effort_from = ConfigSource.STAGE_ENV
        elif global_effort_set:
            effort = global_effort
            effort_from = ConfigSource.GLOBAL_ENV
        else:
            effort = DEFAULT_EFFORTS[stage]
            effort_from = ConfigSource.DEFAULT
        _check_model(model, model_var, effort, stage, allow_unlisted)
        stages[stage] = StageConfig(
            stage=stage,
            model=model,
            effort=effort,
            model_source=model_from,
            effort_source=effort_from,
        )

    return LlmConfig(
        provider=provider,
        # There is exactly one kind, so nothing reads this from the environment yet.
        # Adding the variable before the second kind exists would be config nobody can
        # set to anything useful.
        credential_kind=CredentialKind.API_KEY,
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


def validate_overrides(
    settings: Mapping[str, str], env: Mapping[str, str] | None = None
) -> LlmConfig:
    """Resolve ``settings`` against ``env`` exactly as a job would, or raise.

    **The one gate every ``settings`` mapping passes before anything acts on it** — the
    admin route before it writes a row, the worker before it installs a job's rows, and
    the worker's boot check. One function, so the three cannot disagree about what a
    valid row is.

    Stricter than :func:`load_config` in exactly one way: a model row must be in
    :data:`KNOWN_MODELS` even when ``MOTET_LLM_ALLOW_UNLISTED_MODEL`` is set. That escape
    hatch is for the hour between a vendor shipping a model and this file catching up, and
    it is a property of a *deployment*; a row outside the catalogue is a typo, and a
    ``settings`` row is never the way around the catalogue. A key under ``llm.`` that
    names no stage and no axis is refused for the same reason — nothing would read it.
    """
    known_keys = {key for stage in LlmStage for key in (stage.model_setting, stage.effort_setting)}
    for key, value in settings.items():
        if not key.startswith(SETTING_PREFIX):
            continue
        if key not in known_keys:
            raise LlmConfigError(
                f"settings key {key!r} names no LLM stage; known: {', '.join(sorted(known_keys))}"
            )
        if key.startswith(MODEL_SETTING_PREFIX) and value.strip() not in KNOWN_MODELS:
            raise LlmConfigError(
                f"settings key {key!r} is {value!r}, which is not in the model catalogue — "
                f"and a settings row may not use {ALLOW_UNLISTED_ENV}. "
                f"Known: {', '.join(sorted(KNOWN_MODELS))}."
            )
    return load_config(env, overrides=settings)


def validate_startup(env: Mapping[str, str] | None = None) -> LlmConfig:
    """Fail the process now if it could not serve a request later.

    Called from the API's lifespan and from the worker entry point. Checks the config and
    — when a real provider is selected — that the credential actually resolves. A
    missing key is a startup crash with a clear message, never a 500 at 3am.
    """
    from .credentials import resolve_credential

    config = load_config(env)
    if config.provider is not Provider.FAKE:
        resolve_credential(api_key_env(config.provider), config.credential_kind, env)
    return config


def api_key_env(provider: Provider) -> str:
    """Which environment variable holds ``provider``'s key.

    Which variable, and what header it ends up in, are facts about the provider rather
    than about the kind of credential — so both live with the adapter, and this is the
    lookup that connects them. Imported lazily to keep a fake-mode process from pulling
    in the HTTP client.
    """
    if provider is Provider.OPENROUTER:
        from .openrouter import API_KEY_ENV

        return API_KEY_ENV
    raise LlmConfigError(f"provider {provider.value!r} has no API key variable")
