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

from .config import LlmConfig, LlmStage, Provider, load_config
from .credentials import resolve_credential
from .fakes import FakeLlmClient
from .types import JsonSchemaFormat, LlmClient, LlmRequest, Message, Reasoning


def build_client(
    config: LlmConfig | None = None,
    env: Mapping[str, str] | None = None,
) -> LlmClient:
    """Construct the client the configuration asks for.

    Defaults to the fake, because :func:`~motet_inference.llm.config.load_config` derives
    the provider from ``MOTET_INFERENCE_MODE`` — a forgotten variable costs nothing.
    """
    resolved = config or load_config(env)
    if resolved.provider is Provider.FAKE:
        return FakeLlmClient()

    # Imported here rather than at module scope so that a fake-mode process — every test
    # and every laptop — never pulls in the HTTP client or its transitive imports.
    from .openrouter import OpenRouterClient

    return OpenRouterClient(
        resolve_credential(resolved.credential_kind, env),
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
    """Build a request for ``stage``, with that stage's configured model and effort."""
    stage_config = (config or load_config(env)).for_stage(stage)
    reasoning = (
        Reasoning(effort=stage_config.effort, require_evidence=require_reasoning_evidence)
        if stage_config.effort is not None
        else None
    )
    return LlmRequest(
        model=stage_config.model,
        messages=tuple(messages),
        max_output_tokens=max_output_tokens,
        reasoning=reasoning,
        response_format=response_format,
    )
