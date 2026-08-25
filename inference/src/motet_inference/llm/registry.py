"""Turn configuration into a client and a request. The only place a stage needs to look.

Two functions, because the two halves have different lifetimes: a client is per process
and per provider, a request is per call and per stage. Splitting them is what keeps model
selection out of every call site — a stage says which stage it is, and gets back a
request already carrying the right model and the right thinking depth.

    client = build_client()
    response = client.complete(
        build_request(LlmStage.DEDUP, messages, max_output_tokens=2_000)
    )
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence

from .config import KNOWN_MODELS, LlmConfig, LlmStage, Provider, api_key_env, load_config
from .credentials import resolve_credential
from .fakes import FakeLlmClient
from .types import (
    JsonSchemaFormat,
    LlmClient,
    LlmConfigError,
    LlmRequest,
    Message,
    Reasoning,
    ThinkingMode,
)


def build_client(
    config: LlmConfig | None = None,
    env: Mapping[str, str] | None = None,
) -> LlmClient:
    """Construct the client the configuration asks for.

    Defaults to the fake, because :func:`~motet_inference.llm.config.load_config` derives
    the provider from ``MOTET_INFERENCE_MODE`` — a forgotten variable costs nothing.

    **The caller owns the returned client and should build one per process**, not one per
    item. Each real client holds its own connection pool, so a per-item client leaks
    pools; it also throws away OpenRouter's sticky upstream routing, which is what keeps
    prompt-cache hit rates up on the dedup loop. Nothing is memoized here on purpose —
    a module-level cache keyed on a credential is its own footgun — so hold the client, or
    use it as a context manager when its lifetime really is scoped.
    """
    resolved = config or load_config(env)
    if resolved.provider is Provider.FAKE:
        return FakeLlmClient()

    # Imported here rather than at module scope so that a fake-mode process — every test
    # and every laptop — never pulls in the HTTP client or its transitive imports.
    from .openrouter import OpenRouterClient

    return OpenRouterClient(
        resolve_credential(api_key_env(resolved.provider), resolved.credential_kind, env),
        timeout_seconds=resolved.timeout_seconds,
    )


def build_request(
    stage: LlmStage,
    messages: Sequence[Message],
    *,
    max_output_tokens: int,
    response_format: JsonSchemaFormat | None = None,
    require_reasoning_evidence: bool = True,
    config: LlmConfig | None = None,
    env: Mapping[str, str] | None = None,
) -> LlmRequest:
    """Build a request for ``stage``, with that stage's configured model and effort.

    Also checks the request against what the catalogue says the model can do. Both checks
    guard the same failure shape as the reasoning guard: a field the model cannot honour
    is one a provider may quietly drop or clamp rather than reject.

    ``require_reasoning_evidence`` is what a *caller* wants; whether the evidence means
    anything is a fact about the model, and it is read from the catalogue here so that no
    stage has to know which generation it is talking to. A model with adaptive thinking
    chooses per response whether to think, so there is no evidence to require — see
    :class:`~motet_inference.llm.types.ReasoningNotAppliedError`.

    An **unlisted** model is ``"unknown"`` rather than either answer, which is the same
    deal the escape hatch already makes with :func:`_check_against_catalogue`: the guard
    is sound only on positive knowledge that a model is budget-based, and asserting a
    dropped config without that knowledge is precisely how motet#31 stopped ingestion for
    a day. It does not raise, and it says so in the log rather than claiming to know why.
    """
    stage_config = (config or load_config(env)).for_stage(stage)
    spec = KNOWN_MODELS.get(stage_config.model)
    thinking: ThinkingMode = (
        "unknown" if spec is None else "adaptive" if spec.adaptive_thinking else "budget"
    )
    reasoning = (
        Reasoning(
            effort=stage_config.effort,
            require_evidence=require_reasoning_evidence,
            thinking=thinking,
        )
        if stage_config.effort is not None
        else None
    )
    request = LlmRequest(
        model=stage_config.model,
        messages=tuple(messages),
        max_output_tokens=max_output_tokens,
        reasoning=reasoning,
        response_format=response_format,
    )
    _check_against_catalogue(request, stage)
    return request


def _check_against_catalogue(request: LlmRequest, stage: LlmStage) -> None:
    """Enforce the catalogue facts, so recording them means something.

    An unlisted model (the ``MOTET_LLM_ALLOW_UNLISTED_MODEL`` escape hatch) has no facts
    to check against, so it is skipped — that is the deal the escape hatch makes.
    """
    spec = KNOWN_MODELS.get(request.model)
    if spec is None:
        return
    if request.max_output_tokens > spec.max_output_tokens:
        raise LlmConfigError(
            f"stage {stage.value!r} asks for {request.max_output_tokens} output tokens "
            f"on {request.model!r}, which caps at {spec.max_output_tokens}."
        )
    if not spec.supports_cache_ttl_1h:
        for message in request.messages:
            for part in message.parts:
                if part.cache is not None and part.cache.ttl == "1h":
                    raise LlmConfigError(
                        f"stage {stage.value!r} asks for a 1h cache TTL on "
                        f"{request.model!r}, which does not support extended TTLs. The "
                        "provider would fall back to 5m without saying so; use "
                        'CacheControl(ttl="5m") or choose a model that supports 1h.'
                    )
