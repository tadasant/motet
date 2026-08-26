"""The provider-agnostic LLM contract: what a request is, what a response is.

Nothing in this module names a vendor. ``motet_inference.llm.openrouter`` translates
these types onto OpenRouter's OpenAI-compatible wire format; a future direct-Anthropic
adapter would translate the same types onto the Messages API. Stages depend on this
module, never on either adapter.

**Two absences are deliberate, and both are load-bearing.**

*No sampling parameters.* Sonnet 5 removed ``temperature``/``top_p``/``top_k`` — the
direct API rejects them with a 400. There is no field here to carry one, so a stage
cannot acquire the habit and a later direct adapter cannot inherit a broken request. The
same goes for ``budget_tokens``: thinking depth is :class:`Reasoning.effort`, not a token
ceiling.

*No free-form provider options.* An ``extra`` passthrough dict would let a stage smuggle
a vendor-shaped field past the seam, which is exactly the coupling the seam exists to
prevent. Widen these types instead.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Literal, Protocol, runtime_checkable

Role = Literal["system", "user", "assistant"]
CacheTtl = Literal["5m", "1h"]
Effort = Literal["low", "medium", "high", "xhigh", "max"]

#: Which mechanism ``reasoning.effort`` drives on a given model, and therefore what a
#: response with no reasoning in it is evidence of. See :class:`Reasoning`.
ThinkingMode = Literal["budget", "adaptive", "unknown"]

#: Anthropic accepts at most four cache breakpoints per request, and OpenRouter passes
#: them straight through. Exceeding it is a 400 from the vendor — caught here instead.
MAX_CACHE_BREAKPOINTS = 4


class LlmError(Exception):
    """Base class for every failure raised by the LLM seam."""


class LlmConfigError(LlmError):
    """We asked for something impossible: bad configuration, or a malformed request.

    Always our mistake rather than the provider's, and it is raised before anything is
    sent — at startup for configuration, at construction for a request. It never means a
    call failed; that is :class:`LlmTransportError`.
    """


class LlmTransportError(LlmError):
    """The provider could not be reached, or answered with something unusable."""


class LlmBudgetExhaustedError(LlmTransportError):
    """The answer hit ``max_output_tokens`` before it was finished.

    Its own class because it is the one transport failure that is **deterministic and
    about the request rather than the provider**: the same call will exhaust the same
    budget every time, so the job-queue retry ladder buys nothing and a caller that can
    make the work smaller should do that instead. That is motet#42, where grounding
    validation batched every claim in a 19-item episode into one call with a fixed 8k
    ceiling, spent all 8,000 tokens reasoning, returned no verdict at all, and then did it
    again on every retry until the episode gave up.

    It subclasses :class:`LlmTransportError` deliberately: a caller with nothing smaller
    to try keeps the behaviour it already had.
    """


class ReasoningNotAppliedError(LlmError):
    """Reasoning was requested and the response carries no evidence it happened.

    This exists because of a specific asymmetry: Anthropic's own API rejects an
    incompatible thinking config with a 400, but OpenRouter **silently drops** the
    ``reasoning`` field and runs the request anyway. The response looks entirely healthy;
    the only symptom is that the answer was produced without thinking. For grounding
    validation that is a quality regression with no error anywhere, which is far worse
    than a loud failure — so this is loud.

    **It applies only to budget-based models, and that scope is the whole of motet#31.**
    The inference above — *no reasoning in the response, therefore the field was dropped*
    — holds when ``reasoning.effort`` is converted into a thinking budget and reasoning is
    off until asked for. On a model with adaptive thinking (Claude 4.6 and later) effort
    sets Anthropic's ``output_config.effort`` and never a budget, so Claude decides per
    response whether to think and an unthought answer identifies nothing. On Sonnet 5 and
    Opus 5 it is stronger still: reasoning is on by *default* there, so a dropped field
    would raise thinking to ``high`` rather than remove it, and there is no response this
    error could correctly describe. That is the pair it fired on, which is why
    :class:`Reasoning.thinking` scopes the check to ``"budget"`` models instead of merely
    tolerating a failure everywhere. ``LlmResponse.reasoning_applied`` still carries the
    fact either way.
    """


@dataclass(frozen=True)
class CacheControl:
    """Marks the part it is attached to as the end of a cacheable prefix.

    Part-level control, which wins over message-level control on the providers that
    support both. ``ttl`` is passed through to Anthropic: ``5m`` is the default, ``1h``
    is worth its higher write price when the prefix is reused across a whole run.
    """

    ttl: CacheTtl = "5m"


@dataclass(frozen=True)
class TextPart:
    """One span of text, optionally ending a cacheable prefix."""

    text: str
    cache: CacheControl | None = None


@dataclass(frozen=True)
class Message:
    """One turn, as an ordered sequence of parts.

    Parts rather than a bare string because a cache breakpoint sits *between* parts: the
    stable window of news items and the volatile source item being integrated are two
    parts of one user message, with the breakpoint on the first.
    """

    role: Role
    parts: tuple[TextPart, ...]

    @classmethod
    def of(cls, role: Role, text: str, cache: CacheControl | None = None) -> Message:
        """A single-part message — the common case."""
        return cls(role=role, parts=(TextPart(text=text, cache=cache),))

    @property
    def text(self) -> str:
        return "".join(part.text for part in self.parts)


@dataclass(frozen=True)
class Reasoning:
    """Ask the model to think before answering.

    ``require_evidence`` decides what happens when the provider drops the request on the
    floor: the default raises :class:`ReasoningNotAppliedError` rather than returning an
    answer that only looks fine. Set it false for a stage where unthought output is
    merely worse rather than wrong.

    ``thinking`` is a fact about the *model*, not a preference, and it decides whether a
    response carrying no reasoning is a symptom or an answer. It is resolved from the
    catalog by :func:`~motet_inference.llm.registry.build_request`, which is why nothing
    else in this package constructs a :class:`Reasoning` by hand:

    * ``"budget"`` — effort becomes a thinking budget, so no reasoning means the field was
      dropped. :class:`ReasoningNotAppliedError`'s home ground, and ``require_evidence``
      decides whether it raises.
    * ``"adaptive"`` — the model decides for itself whether the task is worth thinking
      about, so no reasoning identifies nothing. The check does not run.
    * ``"unknown"`` — an unlisted model, reached through
      ``MOTET_LLM_ALLOW_UNLISTED_MODEL``. We cannot tell the two apart, so this does not
      raise either — a false positive here stops a pipeline, and the escape hatch's whole
      deal is that catalogue-derived checks are off — but it is logged as the open
      question it is rather than as the model's decision.
    """

    effort: Effort = "high"
    require_evidence: bool = True
    thinking: ThinkingMode = "budget"


@dataclass(frozen=True)
class JsonSchemaFormat:
    """Constrain the response to a JSON schema.

    Named after the concept rather than either vendor's spelling: OpenRouter takes
    ``response_format.json_schema``, Anthropic takes ``output_config.format``.
    """

    name: str
    schema: Mapping[str, object]


@dataclass(frozen=True)
class LlmRequest:
    """One completion request, fully resolved — model included.

    The model slug is already decided by the time a request exists: per-stage selection
    happens in ``motet_inference.llm.config``, so nothing downstream has to know which
    stage it is serving.
    """

    model: str
    messages: tuple[Message, ...]
    max_output_tokens: int
    reasoning: Reasoning | None = None
    response_format: JsonSchemaFormat | None = None

    def __post_init__(self) -> None:
        if not self.model:
            raise LlmConfigError("LlmRequest.model must not be empty")
        if not self.messages:
            raise LlmConfigError("LlmRequest.messages must not be empty")
        if self.max_output_tokens <= 0:
            raise LlmConfigError(
                f"LlmRequest.max_output_tokens must be positive, got {self.max_output_tokens}"
            )
        if self.cache_breakpoints > MAX_CACHE_BREAKPOINTS:
            raise LlmConfigError(
                f"{self.cache_breakpoints} cache breakpoints exceeds the "
                f"{MAX_CACHE_BREAKPOINTS} a request may carry"
            )

    @property
    def cache_breakpoints(self) -> int:
        return sum(1 for message in self.messages for part in message.parts if part.cache)


@dataclass(frozen=True)
class Usage:
    """Token accounting, normalized across providers.

    ``cache_read_tokens`` is the number that matters: if it stays zero across requests
    that share a prefix, a breakpoint is misplaced or something volatile crept in ahead
    of it. Assuming a cache hit is how the largest cost line in this system quietly
    fails to materialize.
    """

    input_tokens: int = 0
    output_tokens: int = 0
    reasoning_tokens: int = 0
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0


@dataclass(frozen=True)
class LlmResponse:
    """What came back, plus enough metadata to tell healthy from silently degraded."""

    text: str
    model: str
    usage: Usage = Usage()
    reasoning_applied: bool = False
    finish_reason: str | None = None


@runtime_checkable
class LlmClient(Protocol):
    """The whole provider seam: one method.

    Adding a provider means writing one class with this shape. Everything else — model
    selection, credential resolution, cache placement — is already provider-agnostic.
    """

    def complete(self, request: LlmRequest) -> LlmResponse: ...


def render_for_digest(messages: Sequence[Message]) -> str:
    """A canonical, stable rendering of messages, for fakes and cache-key debugging.

    Deterministic by construction: no dict ordering, no clock, no ids.
    """
    return "\n".join(
        f"{message.role}:" + "".join(part.text for part in message.parts) for message in messages
    )
