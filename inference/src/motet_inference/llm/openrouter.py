"""OpenRouter — the one real LLM provider Phase 1 ships.

OpenRouter speaks an OpenAI-compatible chat-completions shape, so the translation from
:mod:`motet_inference.llm.types` is thin. Three things about it are not thin, and each
is a decision rather than an accident.

**1. Prompt caching passes through, and this system needs it to.** OpenRouter propagates
``cache_control`` to Anthropic, honours the ``5m``/``1h`` TTLs, and routes stickily to
keep hit rates up. Dedup/integrate sends the whole window of news items — roughly 4.5k
tokens — once per source item, so the prefix is reused across every call in an ingestion
run. That is the textbook caching case and the largest LLM cost line in the design.
Breakpoints are placed by the caller, on the last stable part; :class:`Usage` carries
``cache_read_tokens`` back so a hit is something you *check*, never something you assume.

Worth knowing before debugging a low hit rate: a slug on OpenRouter is served by several
upstreams (Sonnet 5 by Anthropic, Claude Platform on AWS, Azure, and Google), and a
prompt cache does not follow a request across them. OpenRouter routes stickily to keep
hits, so no routing is pinned here — but if ``cache_read_tokens`` is disappointing in
production, upstream bouncing is the first thing to look at, and an ``order`` preference
on the request body is the lever. Adding one before there is evidence would be guessing.

**2. Reasoning can be dropped silently, and that is dangerous — on the models where it
can happen.** Anthropic's own API rejects an incompatible thinking config with a 400.
OpenRouter instead drops the field and runs the request, so a misconfiguration that fails
loudly on the direct path fails *invisibly* here: a healthy-looking answer produced
without thinking. Every response is therefore checked for evidence that reasoning actually
happened, logged when it is missing, and — by default — raised on. See
:func:`_reasoning_evidence` for what counts as evidence and why token count is the
reliable signal rather than reasoning text.

That check is scoped to **budget-based** models, and the scope is motet#31. On a model
with adaptive thinking — Claude 4.6 and later, which is every Anthropic slug in the
catalog — ``reasoning.effort`` sets Anthropic's ``output_config.effort`` and never a
thinking budget, Claude decides per response whether the task warrants thinking, and
reasoning is on by *default*. So an answer with no reasoning in it is the model obeying
``effort='low'``, and a dropped field would raise thinking rather than remove it. The
guard fired 21 times in a row on the dedup stage against zero real faults and stopped
every item entering the pipeline; :class:`~motet_inference.llm.types.Reasoning` carries
which kind of model it is talking to so that it does not.

**3. No sampling parameters, ever.** Sonnet 5 removed ``temperature``/``top_p``/``top_k``
and ``budget_tokens``; :mod:`~motet_inference.llm.types` has no field for any of them, so
this module has nothing to send. Effort travels in ``reasoning.effort``, which is
OpenRouter's normalization of what Anthropic spells ``output_config.effort``.

**4. "No reasoning field" is not how you turn reasoning off** on an adaptive Anthropic
model, because reasoning is on by default there and an omitted field means adaptive
thinking at effort ``high`` — the most expensive setting, reached by asking for nothing.
:func:`build_payload` sends ``{"enabled": false}`` explicitly instead, so that the
``off`` value in :mod:`~motet_inference.llm.config` means what it says.
"""

from __future__ import annotations

import logging
from typing import Any, Final

import httpx

from .credentials import Credential
from .types import (
    JsonSchemaFormat,
    LlmRequest,
    LlmResponse,
    LlmTransportError,
    Message,
    ReasoningNotAppliedError,
    Usage,
)

logger = logging.getLogger("motet.llm.openrouter")

DEFAULT_BASE_URL: Final = "https://openrouter.ai/api/v1"

#: Where this provider's key is expected. Cloud Run injects it from Secret Manager under
#: exactly this name; nothing ever reads a key from a file or an image layer. It lives
#: here rather than in ``credentials`` because which variable holds the key, and how the
#: key is presented on the wire, are facts about the *provider* — Anthropic direct would
#: want a different variable and an ``x-api-key`` header for the very same kind of secret.
API_KEY_ENV: Final = "OPENROUTER_API_KEY"

#: Sent for attribution on OpenRouter's dashboards. Non-secret, and deliberately not a
#: hostname of ours: this repo is public and carries no topology.
_APP_TITLE: Final = "Motet"


def _content_parts(message: Message) -> list[dict[str, Any]]:
    parts: list[dict[str, Any]] = []
    for part in message.parts:
        block: dict[str, Any] = {"type": "text", "text": part.text}
        if part.cache is not None:
            # Part-level control, which wins over message-level control where both are
            # supported — so a breakpoint lands exactly where the caller put it.
            block["cache_control"] = {"type": "ephemeral", "ttl": part.cache.ttl}
        parts.append(block)
    return parts


def _response_format(fmt: JsonSchemaFormat) -> dict[str, Any]:
    return {
        "type": "json_schema",
        "json_schema": {"name": fmt.name, "strict": True, "schema": dict(fmt.schema)},
    }


def build_payload(request: LlmRequest) -> dict[str, Any]:
    """Translate a request onto OpenRouter's wire format.

    Module-level and pure, so a test can assert the exact bytes that would go out —
    including that no sampling parameter is among them — without a client or a network.
    """
    payload: dict[str, Any] = {
        "model": request.model,
        "messages": [
            {"role": message.role, "content": _content_parts(message)}
            for message in request.messages
        ],
        "max_tokens": request.max_output_tokens,
        # Ask for the full accounting block. Without it there is no cache-read count and
        # no reasoning-token count, which are the two numbers this adapter checks.
        "usage": {"include": True},
    }
    if request.reasoning is not None:
        payload["reasoning"] = {"enabled": True, "effort": request.reasoning.effort}
    else:
        # Off has to be said out loud. Reasoning is *on by default* on every adaptive
        # Anthropic model — a request that simply omits the field runs adaptive thinking
        # at effort `high`, so the obvious reading of "send nothing" turns the one lever
        # for disabling reasoning into the most expensive setting there is. `off` in
        # `motet_inference.llm.config` is what a stage sets when unthought output is
        # wanted; this is what makes it mean that.
        payload["reasoning"] = {"enabled": False}
    if request.response_format is not None:
        payload["response_format"] = _response_format(request.response_format)
    return payload


def _usage(raw: object) -> Usage:
    if not isinstance(raw, dict):
        return Usage()
    prompt_details = raw.get("prompt_tokens_details")
    prompt_details = prompt_details if isinstance(prompt_details, dict) else {}
    completion_details = raw.get("completion_tokens_details")
    completion_details = completion_details if isinstance(completion_details, dict) else {}
    return Usage(
        input_tokens=_int(raw.get("prompt_tokens")),
        output_tokens=_int(raw.get("completion_tokens")),
        reasoning_tokens=_int(completion_details.get("reasoning_tokens")),
        cache_read_tokens=_int(prompt_details.get("cached_tokens")),
        # Cache-*write* accounting is provider-specific and not always surfaced. Reads
        # are the number that tells you whether caching is working, so a missing write
        # count is a zero here rather than an error.
        cache_write_tokens=_int(prompt_details.get("cache_creation_tokens")),
    )


def _int(value: object) -> int:
    return value if isinstance(value, int) and not isinstance(value, bool) else 0


def _reasoning_evidence(message: dict[str, Any], usage: Usage) -> bool:
    """Did the model actually think?

    Three signals, any of which counts. Token count comes first because it is the only
    one that survives the common case: Sonnet 5 does not return its raw chain of thought,
    and thinking display defaults to omitted, so a *thought* response routinely carries
    empty reasoning text. Treating empty text as "no reasoning" would fire this check
    constantly and train everyone to switch it off — which would hand back exactly the
    silent failure it exists to catch.
    """
    if usage.reasoning_tokens > 0:
        return True
    if message.get("reasoning_details"):
        return True
    reasoning = message.get("reasoning")
    return isinstance(reasoning, str) and bool(reasoning.strip())


class OpenRouterClient:
    """An :class:`~motet_inference.llm.types.LlmClient` backed by OpenRouter.

    The credential is resolved once, by the caller, and never read from disk here.
    ``transport`` exists so tests can drive the full translate-send-parse path against a
    stub without a network — CI never reaches a vendor (invariant 7).
    """

    def __init__(
        self,
        credential: Credential,
        *,
        base_url: str = DEFAULT_BASE_URL,
        timeout_seconds: float = 120.0,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        self._credential = credential
        self._base_url = base_url.rstrip("/")
        self._client = httpx.Client(
            timeout=timeout_seconds,
            transport=transport,
            headers={"X-Title": _APP_TITLE},
        )

    def _auth_headers(self) -> dict[str, str]:
        """OpenRouter's wire shape for the credential. Anthropic direct would differ."""
        return {"Authorization": f"Bearer {self._credential.token()}"}

    def complete(self, request: LlmRequest) -> LlmResponse:
        payload = build_payload(request)
        try:
            http_response = self._client.post(
                f"{self._base_url}/chat/completions",
                json=payload,
                headers=self._auth_headers(),
            )
        except httpx.HTTPError as exc:
            raise LlmTransportError(f"OpenRouter request failed: {exc}") from exc

        if http_response.status_code >= 400:
            raise LlmTransportError(
                f"OpenRouter returned {http_response.status_code}: {http_response.text[:500]}"
            )
        try:
            data = http_response.json()
        except ValueError as exc:
            raise LlmTransportError(f"OpenRouter returned non-JSON: {exc}") from exc
        if not isinstance(data, dict):
            raise LlmTransportError(f"OpenRouter returned a {type(data).__name__}, not an object")
        return self._parse(request, data)

    def _parse(self, request: LlmRequest, data: dict[str, Any]) -> LlmResponse:
        # OpenRouter reports some upstream failures as a 200 carrying an error object.
        error = data.get("error")
        if error:
            raise LlmTransportError(f"OpenRouter reported an error: {error}")

        choices = data.get("choices")
        if not isinstance(choices, list) or not choices:
            raise LlmTransportError("OpenRouter returned no choices")
        choice = choices[0]
        message = choice.get("message") if isinstance(choice, dict) else None
        if not isinstance(message, dict):
            raise LlmTransportError("OpenRouter returned a choice with no message")

        usage = _usage(data.get("usage"))
        content = message.get("content")
        applied = _reasoning_evidence(message, usage)
        finish_reason_raw = choice.get("finish_reason")
        finish_reason = finish_reason_raw if isinstance(finish_reason_raw, str) else None

        # An empty completion is never useful, and it arrives looking like a success.
        # The most likely cause is reasoning consuming the whole max_tokens budget --
        # very plausible at effort=max -- which passes the reasoning check above and then
        # hands "" to a caller that will fail parsing it several frames away, with
        # nothing pointing back at truncation.
        if not isinstance(content, str) or not content.strip():
            raise LlmTransportError(
                f"OpenRouter returned an empty completion (finish_reason="
                f"{finish_reason!r}, output_tokens={usage.output_tokens}, "
                f"reasoning_tokens={usage.reasoning_tokens}). If finish_reason is "
                "'length', the token budget was spent before any answer was produced -- "
                "raise max_output_tokens or lower the stage's effort."
            )
        if finish_reason == "length":
            logger.warning(
                "response truncated at max_output_tokens on model=%s: the answer is "
                "incomplete, not merely short",
                data.get("model") or request.model,
            )
        # OpenRouter echoes the model it actually served, which can be more specific than
        # what was asked for (a dated snapshot). Prefer it, fall back to the request.
        served_model = data.get("model")
        response = LlmResponse(
            text=content,
            model=served_model if isinstance(served_model, str) else request.model,
            usage=usage,
            reasoning_applied=applied,
            finish_reason=finish_reason,
        )

        if request.reasoning is not None and not applied:
            if request.reasoning.adaptive:
                # Not a fault, and not tolerated-with-a-warning either: on an adaptive
                # model this is Claude having decided the task was not worth thinking
                # about, which at effort='low' is the whole point of asking for 'low'.
                # Recorded rather than merely swallowed — `reasoning_applied` rides on
                # every response, and this line carries the pair that explains it — but at
                # info, because a warning per dedup call would be noise that trains
                # everyone to stop reading warnings.
                logger.info(
                    "model=%s answered without thinking at effort=%s; adaptive thinking "
                    "makes that the model's call, not a dropped reasoning config",
                    response.model,
                    request.reasoning.effort,
                )
                return response
            # Logged unconditionally, because even when the caller has chosen to tolerate
            # it this is the only trace that quality silently dropped.
            logger.warning(
                "reasoning requested at effort=%s on model=%s but the response carries no "
                "evidence of it: OpenRouter drops an unsupported reasoning config instead "
                "of rejecting it, so this answer was probably produced without thinking",
                request.reasoning.effort,
                response.model,
            )
            if request.reasoning.require_evidence:
                raise ReasoningNotAppliedError(
                    f"reasoning was requested at effort={request.reasoning.effort!r} on "
                    f"model={response.model!r}, but the response has no reasoning tokens, "
                    "no reasoning_details, and no reasoning text. Check that the model "
                    "supports selectable effort, or set Reasoning(require_evidence=False) "
                    "if unthought output is acceptable for this stage."
                )
        return response

    def close(self) -> None:
        """Release the connection pool. Also reachable as a context manager."""
        self._client.close()

    def __enter__(self) -> OpenRouterClient:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()
