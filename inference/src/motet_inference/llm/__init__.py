"""The LLM provider seam: one interface, one real adapter, one fake.

Motet's text callers — dedup/integrate, the second look it buys, script generation, and
the voice service's conversational turn — talk to a model through :class:`LlmClient` and
nothing else. OpenRouter is the provider Phase 1 ships, defaulting to Claude Sonnet 5
(``anthropic/claude-sonnet-5``); which model each stage uses is environment
configuration, not code.

Reach for :func:`build_client` and :func:`build_request` rather than constructing an
adapter directly — that is what keeps a stage from learning a vendor's name, and what
makes ``MOTET_INFERENCE_MODE=fake`` sufficient to guarantee no test ever spends money.

Call :func:`validate_startup` from every entry point. An unknown model slug or a missing
credential should stop the process on boot with a clear message, not surface as a 500 an
hour later.
"""

from .config import (
    ALLOW_UNLISTED_ENV,
    DEFAULT_EFFORTS,
    DEFAULT_MODEL,
    EFFORT_ENV,
    EFFORT_SETTING_PREFIX,
    KNOWN_MODELS,
    MODEL_ENV,
    MODEL_SETTING_PREFIX,
    OFF_VALUE,
    PROVIDER_ENV,
    TIMEOUT_ENV,
    ConfigSource,
    LlmConfig,
    LlmStage,
    ModelSpec,
    Provider,
    StageConfig,
    llm_overrides,
    load_config,
    usage_cost_usd,
    validate_startup,
)
from .credentials import Credential, CredentialKind, resolve_credential
from .fakes import FakeLlmClient, cache_prefix_digest, request_digest
from .registry import build_client, build_request
from .types import (
    MAX_CACHE_BREAKPOINTS,
    CacheControl,
    JsonSchemaFormat,
    LlmBudgetExhaustedError,
    LlmClient,
    LlmConfigError,
    LlmError,
    LlmRequest,
    LlmResponse,
    LlmTransportError,
    Message,
    Reasoning,
    ReasoningNotAppliedError,
    TextPart,
    Usage,
)

__all__ = [
    "ALLOW_UNLISTED_ENV",
    "DEFAULT_EFFORTS",
    "EFFORT_SETTING_PREFIX",
    "MODEL_SETTING_PREFIX",
    "OFF_VALUE",
    "ConfigSource",
    "llm_overrides",
    "usage_cost_usd",
    "DEFAULT_MODEL",
    "EFFORT_ENV",
    "KNOWN_MODELS",
    "MAX_CACHE_BREAKPOINTS",
    "MODEL_ENV",
    "PROVIDER_ENV",
    "TIMEOUT_ENV",
    "CacheControl",
    "Credential",
    "CredentialKind",
    "FakeLlmClient",
    "JsonSchemaFormat",
    "LlmBudgetExhaustedError",
    "LlmClient",
    "LlmConfig",
    "LlmConfigError",
    "LlmError",
    "LlmRequest",
    "LlmResponse",
    "LlmStage",
    "LlmTransportError",
    "Message",
    "ModelSpec",
    "Provider",
    "Reasoning",
    "ReasoningNotAppliedError",
    "StageConfig",
    "TextPart",
    "Usage",
    "build_client",
    "build_request",
    "cache_prefix_digest",
    "load_config",
    "request_digest",
    "resolve_credential",
    "validate_startup",
]
