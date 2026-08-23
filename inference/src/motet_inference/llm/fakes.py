"""A deterministic LLM client with no network. This is what CI runs.

Invariant 7 in one class: same request, same response, no clock, no randomness, no
vendor, no key. Because it is what the golden set runs behind, determinism here is not a
nicety — a fake whose output drifted would make the golden set assert nothing.

The fake is honest rather than empty: it records every request, honours a canned
response when one is supplied, and reports reasoning as applied when reasoning was asked
for. ``drop_reasoning`` reproduces the one provider behaviour worth being able to
simulate offline — OpenRouter accepting a request and silently answering without
thinking.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping

from .types import LlmRequest, LlmResponse, Usage, render_for_digest


def request_digest(request: LlmRequest) -> str:
    """A stable fingerprint of everything that would change the answer."""
    material = "\n".join(
        (
            request.model,
            render_for_digest(request.messages),
            str(request.max_output_tokens),
            request.reasoning.effort if request.reasoning else "-",
            request.response_format.name if request.response_format else "-",
        )
    )
    return hashlib.sha256(material.encode()).hexdigest()[:16]


def cache_prefix_digest(request: LlmRequest) -> str:
    """A fingerprint of the text up to and including the **last** cache breakpoint.

    What a provider would key its prompt cache on. Used by ``simulate_cache`` so that
    breakpoint placement can be exercised offline: move a volatile part ahead of the
    breakpoint and the simulated hit disappears, exactly as a real one would.

    The *last* breakpoint, not the first, and the distinction is not academic — the
    dedup shape is two breakpoints (a system prompt and the news-item window). Keying on
    the first would report a full hit whenever the system prompt matched, no matter what
    happened to the window, which is precisely the case this fake exists to catch.
    """
    seen: list[str] = []
    prefix: list[str] = []
    for message in request.messages:
        for part in message.parts:
            seen.append(part.text)
            if part.cache is not None:
                prefix = list(seen)
    if not prefix:
        return ""
    return hashlib.sha256("\n".join(prefix).encode()).hexdigest()[:16]


class FakeLlmClient:
    """Deterministic stand-in for any provider.

    ``responses`` maps a request digest, or a substring of the rendered prompt, to the
    text to return — how a stage test supplies a realistic answer. Anything unmatched
    gets a stable digest string (or a stable JSON object when a schema was requested), so
    an unconfigured call is obvious rather than plausible.
    """

    def __init__(
        self,
        *,
        responses: Mapping[str, str] | None = None,
        simulate_cache: bool = False,
        drop_reasoning: bool = False,
    ) -> None:
        self._responses = dict(responses or {})
        self._simulate_cache = simulate_cache
        self._drop_reasoning = drop_reasoning
        self._seen_prefixes: set[str] = set()
        self.calls: list[LlmRequest] = []

    def complete(self, request: LlmRequest) -> LlmResponse:
        self.calls.append(request)
        digest = request_digest(request)
        # Membership, not truthiness: a canned empty response is a legitimate thing to
        # want to test, and `or` would silently replace it with the fallback.
        text = (
            self._responses[digest]
            if digest in self._responses
            else self._match_by_substring(request)
        )
        if text is None:
            text = (
                json.dumps({"fake": digest}, sort_keys=True)
                if request.response_format is not None
                else f"fake-completion:{digest}"
            )

        reasoning_requested = request.reasoning is not None
        applied = reasoning_requested and not self._drop_reasoning
        return LlmResponse(
            text=text,
            model=request.model,
            usage=Usage(
                # Whole words rather than a real tokenizer: the point is a stable,
                # order-of-magnitude-right number, not an accurate bill.
                input_tokens=len(render_for_digest(request.messages).split()),
                output_tokens=len(text.split()),
                reasoning_tokens=64 if applied else 0,
                **self._cache_tokens(request),
            ),
            reasoning_applied=applied,
            finish_reason="stop",
        )

    def _match_by_substring(self, request: LlmRequest) -> str | None:
        rendered = render_for_digest(request.messages)
        for key, value in self._responses.items():
            if key in rendered:
                return value
        return None

    def _cache_tokens(self, request: LlmRequest) -> dict[str, int]:
        """Simulated cache accounting — off unless asked for.

        Off by default because it is the one stateful thing here: a second identical
        request reports a read where the first reported a write. Tests that care about
        caching opt in; everything else keeps a pure function.
        """
        if not self._simulate_cache:
            return {}
        prefix = cache_prefix_digest(request)
        if not prefix:
            return {}
        size = len(prefix) * 64  # stable, and unrelated to any real token count
        if prefix in self._seen_prefixes:
            return {"cache_read_tokens": size}
        self._seen_prefixes.add(prefix)
        return {"cache_write_tokens": size}
