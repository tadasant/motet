"""Contract tests for the LLM provider seam.

Every test here runs offline, with no exceptions. The OpenRouter adapter is exercised
end to end through an ``httpx.MockTransport``, which drives the real translate-send-parse
path without a network and without a key — invariant 7 says no test in this repo may make
a real vendor call, and that is meant absolutely. Confirming a slug or a reasoning config
against the live vendor is a thing to do by hand, outside the suite.

Three of these are the ones worth understanding, because each pins a specific way this
integration is known to fail quietly:

* :func:`test_reasoning_silently_dropped_by_the_provider_raises` — OpenRouter drops an
  unsupported reasoning config instead of rejecting it, so a request that would 400 on
  Anthropic's own API returns a healthy-looking unthought answer here.
* :func:`test_payload_carries_no_sampling_parameters` — Sonnet 5 rejects
  ``temperature``/``top_p``/``top_k``/``budget_tokens``, and the seam's job is to make
  sending one impossible rather than merely discouraged.
* :func:`test_cache_breakpoint_lands_on_the_stable_prefix` — the dedup window is the
  largest cost line in the design, and it only pays off if the breakpoint sits after the
  stable text and before the volatile text.
"""

from __future__ import annotations

import json
import logging
from typing import Any

import httpx
import pytest
from motet_inference.llm import (
    DEFAULT_MODEL,
    CacheControl,
    Credential,
    CredentialKind,
    FakeLlmClient,
    JsonSchemaFormat,
    LlmClient,
    LlmConfigError,
    LlmRequest,
    LlmStage,
    LlmTransportError,
    Message,
    Provider,
    Reasoning,
    ReasoningNotAppliedError,
    TextPart,
    build_client,
    build_request,
    load_config,
    request_digest,
    resolve_credential,
    validate_startup,
)
from motet_inference.llm import credentials as credentials_module
from motet_inference.llm.openrouter import API_KEY_ENV, OpenRouterClient, build_payload
from motet_inference.mode import current_mode

MESSAGES = (
    Message.of("system", "You are Motet's dedup stage."),
    Message.of("user", "Does this belong to an existing story?"),
)

REAL = {"MOTET_INFERENCE_MODE": "real", "OPENROUTER_API_KEY": "sk-or-test"}


def a_request(**overrides: Any) -> LlmRequest:
    defaults: dict[str, Any] = {
        "model": DEFAULT_MODEL,
        "messages": MESSAGES,
        "max_output_tokens": 1024,
    }
    return LlmRequest(**{**defaults, **overrides})


def responder(
    payload: dict[str, Any] | None = None,
    *,
    status_code: int = 200,
    captured: list[dict[str, Any]] | None = None,
) -> httpx.MockTransport:
    """A transport that records the request body and replays a canned response."""

    def handle(request: httpx.Request) -> httpx.Response:
        if captured is not None:
            captured.append(json.loads(request.content))
        return httpx.Response(status_code, json=payload if payload is not None else {})

    return httpx.MockTransport(handle)


def a_client(transport: httpx.MockTransport) -> OpenRouterClient:
    return OpenRouterClient(
        Credential(kind=CredentialKind.API_KEY, secret="sk-or-test"),
        transport=transport,
    )


def completion(
    *,
    text: str = "an answer",
    reasoning_tokens: int = 0,
    reasoning: str | None = None,
    cached_tokens: int = 0,
) -> dict[str, Any]:
    message: dict[str, Any] = {"role": "assistant", "content": text}
    if reasoning is not None:
        message["reasoning"] = reasoning
    return {
        "id": "gen-1",
        "model": DEFAULT_MODEL,
        "choices": [{"finish_reason": "stop", "message": message}],
        "usage": {
            "prompt_tokens": 100,
            "completion_tokens": 20,
            "prompt_tokens_details": {"cached_tokens": cached_tokens},
            "completion_tokens_details": {"reasoning_tokens": reasoning_tokens},
        },
    }


# --------------------------------------------------------------------------- the fake


def test_the_fake_satisfies_the_protocol() -> None:
    assert isinstance(FakeLlmClient(), LlmClient)


def test_the_fake_is_deterministic() -> None:
    """Same request, same answer — the property the golden set depends on."""
    first = FakeLlmClient().complete(a_request())
    second = FakeLlmClient().complete(a_request())
    assert first == second


def test_the_fake_answers_differently_for_a_different_request() -> None:
    """Deterministic must not mean constant, or the fake would assert nothing."""
    other = a_request(messages=(Message.of("user", "a different question"),))
    assert FakeLlmClient().complete(a_request()).text != FakeLlmClient().complete(other).text


def test_the_fake_reports_reasoning_when_reasoning_was_requested() -> None:
    response = FakeLlmClient().complete(a_request(reasoning=Reasoning(effort="max")))
    assert response.reasoning_applied
    assert response.usage.reasoning_tokens > 0

    without = FakeLlmClient().complete(a_request())
    assert not without.reasoning_applied
    assert without.usage.reasoning_tokens == 0


def test_the_fake_can_reproduce_a_silently_dropped_reasoning_config() -> None:
    """The dangerous provider behaviour, reproducible offline."""
    client = FakeLlmClient(drop_reasoning=True)
    response = client.complete(a_request(reasoning=Reasoning(effort="high")))
    assert not response.reasoning_applied


def test_the_fake_records_calls_and_honours_canned_responses() -> None:
    client = FakeLlmClient(responses={"dedup stage": "MERGE ni_1"})
    assert client.complete(a_request()).text == "MERGE ni_1"
    assert len(client.calls) == 1


def test_the_fake_simulates_a_cache_hit_only_for_a_repeated_prefix() -> None:
    """Breakpoint placement is exercisable without a provider."""
    client = FakeLlmClient(simulate_cache=True)
    window = Message(
        role="user",
        parts=(
            TextPart("the stable window of news items", cache=CacheControl(ttl="1h")),
            TextPart("volatile source item A"),
        ),
    )
    first = client.complete(a_request(messages=(window,)))
    assert first.usage.cache_write_tokens > 0
    assert first.usage.cache_read_tokens == 0

    same_prefix = Message(
        role="user",
        parts=(
            TextPart("the stable window of news items", cache=CacheControl(ttl="1h")),
            TextPart("volatile source item B"),
        ),
    )
    second = client.complete(a_request(messages=(same_prefix,)))
    assert second.usage.cache_read_tokens > 0

    moved = Message(
        role="user",
        parts=(
            TextPart("the stable window of news items"),
            TextPart("volatile source item C", cache=CacheControl(ttl="1h")),
        ),
    )
    third = client.complete(a_request(messages=(moved,)))
    assert third.usage.cache_read_tokens == 0, "a breakpoint after volatile text cannot hit"


# ------------------------------------------------------------------- per-stage config


def test_every_stage_defaults_to_sonnet_5() -> None:
    config = load_config({})
    assert DEFAULT_MODEL == "anthropic/claude-sonnet-5"
    assert {config.for_stage(stage).model for stage in LlmStage} == {DEFAULT_MODEL}


def test_the_global_default_can_be_overridden_for_every_stage_at_once() -> None:
    config = load_config({"MOTET_LLM_MODEL": "anthropic/claude-opus-5"})
    assert {config.for_stage(stage).model for stage in LlmStage} == {"anthropic/claude-opus-5"}


def test_a_stage_override_beats_the_global_default() -> None:
    """The volume line runs cheap while grounding runs strong — the point of per-stage."""
    config = load_config(
        {
            "MOTET_LLM_MODEL": "anthropic/claude-sonnet-5",
            "MOTET_LLM_MODEL_DEDUP": "anthropic/claude-haiku-4.5",
            "MOTET_LLM_EFFORT_DEDUP": "off",
            "MOTET_LLM_MODEL_GROUNDING": "anthropic/claude-opus-5",
        }
    )
    assert config.for_stage(LlmStage.DEDUP).model == "anthropic/claude-haiku-4.5"
    assert config.for_stage(LlmStage.SCRIPT).model == "anthropic/claude-sonnet-5"
    assert config.for_stage(LlmStage.GROUNDING).model == "anthropic/claude-opus-5"


def test_effort_defaults_are_per_stage_and_overridable() -> None:
    default = load_config({})
    assert default.for_stage(LlmStage.DEDUP).effort == "low"
    assert default.for_stage(LlmStage.GROUNDING).effort == "max"

    overridden = load_config({"MOTET_LLM_EFFORT_GROUNDING": "xhigh"})
    assert overridden.for_stage(LlmStage.GROUNDING).effort == "xhigh"


def test_effort_off_disables_reasoning_for_one_stage() -> None:
    config = load_config({"MOTET_LLM_EFFORT_SCRIPT": "off"})
    assert config.for_stage(LlmStage.SCRIPT).effort is None
    request = build_request(LlmStage.SCRIPT, MESSAGES, max_output_tokens=100, config=config)
    assert request.reasoning is None


def test_build_request_carries_the_stage_model_and_effort() -> None:
    config = load_config({"MOTET_LLM_MODEL_GROUNDING": "anthropic/claude-opus-5"})
    request = build_request(LlmStage.GROUNDING, MESSAGES, max_output_tokens=512, config=config)
    assert request.model == "anthropic/claude-opus-5"
    assert request.reasoning is not None
    assert request.reasoning.effort == "max"


def test_the_provider_defaults_to_fake_and_follows_the_inference_mode() -> None:
    """A forgotten variable must fail toward the free side, as the stage registry does."""
    assert load_config({}).provider is Provider.FAKE
    assert load_config({"MOTET_INFERENCE_MODE": "real"}).provider is Provider.OPENROUTER
    assert isinstance(build_client(env={}), FakeLlmClient)


def test_an_explicit_provider_overrides_the_inference_mode() -> None:
    assert load_config({"MOTET_INFERENCE_MODE": "real", "MOTET_LLM_PROVIDER": "fake"}).provider is (
        Provider.FAKE
    )


# ------------------------------------------------------------ startup validation


def test_a_missing_credential_fails_at_startup() -> None:
    with pytest.raises(LlmConfigError, match="OPENROUTER_API_KEY"):
        validate_startup({"MOTET_INFERENCE_MODE": "real"})


def test_an_empty_credential_is_treated_as_missing() -> None:
    with pytest.raises(LlmConfigError, match="OPENROUTER_API_KEY"):
        validate_startup({"MOTET_INFERENCE_MODE": "real", "OPENROUTER_API_KEY": "   "})


def test_fake_mode_needs_no_credential() -> None:
    """CI has no key and must never need one."""
    assert validate_startup({}).provider is Provider.FAKE


def test_an_unknown_model_slug_fails_at_startup() -> None:
    with pytest.raises(LlmConfigError, match="not in the catalog"):
        load_config({"MOTET_LLM_MODEL": "anthropic/claude-sonnet-42"})


def test_an_unknown_slug_can_be_opted_into_deliberately() -> None:
    config = load_config(
        {
            "MOTET_LLM_MODEL": "anthropic/claude-sonnet-42",
            "MOTET_LLM_EFFORT": "off",
            "MOTET_LLM_ALLOW_UNLISTED_MODEL": "true",
        }
    )
    assert config.for_stage(LlmStage.SCRIPT).model == "anthropic/claude-sonnet-42"


def test_asking_for_effort_on_a_model_without_one_fails_at_startup() -> None:
    """The startup-time counterpart to OpenRouter's silent drop.

    ``claude-haiku-4.5`` has no selectable effort. Sent as-is, OpenRouter would drop the
    reasoning field and answer anyway; caught here, it is a boot failure with a fix in
    the message.
    """
    with pytest.raises(LlmConfigError, match="no selectable effort"):
        load_config({"MOTET_LLM_MODEL_DEDUP": "anthropic/claude-haiku-4.5"})


def test_an_effort_the_model_does_not_support_fails_at_startup() -> None:
    with pytest.raises(LlmConfigError, match="supports only"):
        load_config(
            {
                "MOTET_LLM_MODEL_SCRIPT": "anthropic/claude-sonnet-4.6",
                "MOTET_LLM_EFFORT_SCRIPT": "xhigh",
            }
        )


def test_nonsense_configuration_values_fail_at_startup() -> None:
    with pytest.raises(LlmConfigError, match="MOTET_LLM_PROVIDER"):
        load_config({"MOTET_LLM_PROVIDER": "anthropic-direct"})
    with pytest.raises(LlmConfigError, match="MOTET_LLM_EFFORT"):
        load_config({"MOTET_LLM_EFFORT": "maximum"})
    with pytest.raises(LlmConfigError, match="MOTET_LLM_TIMEOUT_SECONDS"):
        load_config({"MOTET_LLM_TIMEOUT_SECONDS": "-1"})


def test_a_resolved_credential_never_reveals_its_secret() -> None:
    """A key that reaches a log or a traceback is a key that has to be rotated."""
    credential = resolve_credential(API_KEY_ENV, env={API_KEY_ENV: "sk-or-supersecret"})
    assert "supersecret" not in repr(credential)
    assert "supersecret" not in str(credential)
    assert credential.token() == "sk-or-supersecret"


def test_the_credential_module_knows_nothing_about_wire_shape() -> None:
    """The header is the provider's business, not the credential kind's.

    An API key we own travels as ``Authorization: Bearer`` to OpenRouter and as
    ``x-api-key`` to Anthropic direct. If the credential built the header, that provider
    distinction would have to masquerade as a second *kind* of credential, which it is
    not — and adding the second provider would mean reshaping this seam rather than
    extending it.
    """
    credential = resolve_credential(API_KEY_ENV, env={API_KEY_ENV: "sk-or-test"})
    assert not hasattr(credential, "auth_headers")
    assert API_KEY_ENV not in dir(credentials_module)


def test_the_startup_summary_carries_no_secret() -> None:
    summary = validate_startup(REAL).describe()
    assert "sk-or-test" not in summary
    assert DEFAULT_MODEL in summary


# ------------------------------------------------------------------- the wire payload


def test_payload_carries_no_sampling_parameters() -> None:
    """Sonnet 5 rejects them, so the seam must make sending one impossible."""
    payload = build_payload(a_request(reasoning=Reasoning(effort="high")))
    for banned in ("temperature", "top_p", "top_k", "budget_tokens", "thinking"):
        assert banned not in payload
    assert not hasattr(LlmRequest, "temperature")


def test_payload_asks_for_usage_accounting() -> None:
    """Without it there is no cache-read count and no reasoning-token count."""
    assert build_payload(a_request())["usage"] == {"include": True}


def test_effort_travels_in_the_reasoning_field() -> None:
    payload = build_payload(a_request(reasoning=Reasoning(effort="xhigh")))
    assert payload["reasoning"] == {"enabled": True, "effort": "xhigh"}


def test_no_reasoning_field_when_none_was_asked_for() -> None:
    assert "reasoning" not in build_payload(a_request())


def test_cache_breakpoint_lands_on_the_stable_prefix() -> None:
    """The dedup shape: stable window cached, volatile item after the breakpoint."""
    payload = build_payload(
        a_request(
            messages=(
                Message.of("system", "instructions"),
                Message(
                    role="user",
                    parts=(
                        TextPart("<window>...4.5k tokens...</window>", cache=CacheControl("1h")),
                        TextPart("<item>the source item being integrated</item>"),
                    ),
                ),
            )
        )
    )
    window, item = payload["messages"][1]["content"]
    assert window["cache_control"] == {"type": "ephemeral", "ttl": "1h"}
    assert "cache_control" not in item
    assert "cache_control" not in payload["messages"][0]["content"][0]


def test_the_default_cache_ttl_is_five_minutes() -> None:
    payload = build_payload(a_request(messages=(Message.of("user", "x", CacheControl()),)))
    assert payload["messages"][0]["content"][0]["cache_control"]["ttl"] == "5m"


def test_too_many_cache_breakpoints_is_rejected_before_the_vendor_sees_it() -> None:
    cached = [Message.of("user", f"part {index}", CacheControl()) for index in range(5)]
    with pytest.raises(LlmConfigError, match="cache breakpoints"):
        a_request(messages=tuple(cached))


def test_structured_output_uses_a_strict_json_schema() -> None:
    schema = {"type": "object", "properties": {"merged": {"type": "boolean"}}}
    payload = build_payload(
        a_request(response_format=JsonSchemaFormat(name="integration", schema=schema))
    )
    assert payload["response_format"]["type"] == "json_schema"
    assert payload["response_format"]["json_schema"]["strict"] is True
    assert payload["response_format"]["json_schema"]["schema"] == schema


def test_a_request_with_no_messages_or_no_budget_is_rejected() -> None:
    with pytest.raises(LlmConfigError, match="messages"):
        a_request(messages=())
    with pytest.raises(LlmConfigError, match="max_output_tokens"):
        a_request(max_output_tokens=0)


# ----------------------------------------------------------- the adapter, end to end


def test_the_adapter_sends_the_payload_and_parses_the_answer() -> None:
    captured: list[dict[str, Any]] = []
    client = a_client(responder(completion(text="hello", cached_tokens=4096), captured=captured))
    response = client.complete(a_request())

    assert captured[0]["model"] == DEFAULT_MODEL
    assert response.text == "hello"
    assert response.finish_reason == "stop"
    assert response.usage.input_tokens == 100
    assert response.usage.cache_read_tokens == 4096


def test_the_adapter_authorizes_with_the_credential() -> None:
    seen: list[str] = []

    def handle(request: httpx.Request) -> httpx.Response:
        seen.append(request.headers["authorization"])
        return httpx.Response(200, json=completion())

    a_client(httpx.MockTransport(handle)).complete(a_request())
    assert seen == ["Bearer sk-or-test"]


def test_reasoning_that_took_effect_is_reported_as_applied() -> None:
    client = a_client(responder(completion(reasoning_tokens=512)))
    response = client.complete(a_request(reasoning=Reasoning(effort="max")))
    assert response.reasoning_applied
    assert response.usage.reasoning_tokens == 512


def test_reasoning_silently_dropped_by_the_provider_raises(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The whole reason this guard exists.

    Anthropic's own API 400s on an incompatible thinking config. OpenRouter drops the
    field and answers anyway, so the response below is a *success* that was generated
    without thinking — for grounding validation, quality degradation with no error
    anywhere. The adapter refuses to pass it off as healthy.
    """
    client = a_client(responder(completion(text="an unthought answer", reasoning_tokens=0)))
    with caplog.at_level(logging.WARNING, logger="motet.llm.openrouter"):
        with pytest.raises(ReasoningNotAppliedError, match="no reasoning tokens"):
            client.complete(a_request(reasoning=Reasoning(effort="high")))
    assert "no evidence of it" in caplog.text


def test_a_dropped_reasoning_config_is_logged_even_when_tolerated(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Tolerating it must still leave a trace — it is the only sign quality dropped."""
    client = a_client(responder(completion(reasoning_tokens=0)))
    with caplog.at_level(logging.WARNING, logger="motet.llm.openrouter"):
        response = client.complete(
            a_request(reasoning=Reasoning(effort="low", require_evidence=False))
        )
    assert not response.reasoning_applied
    assert "no evidence of it" in caplog.text


def test_reasoning_text_counts_as_evidence_when_tokens_are_not_reported() -> None:
    """Sonnet 5 omits its chain of thought, so no single signal is sufficient alone."""
    client = a_client(responder(completion(reasoning="let me think about this")))
    assert client.complete(a_request(reasoning=Reasoning(effort="high"))).reasoning_applied


def test_no_guard_fires_when_reasoning_was_never_requested() -> None:
    client = a_client(responder(completion(reasoning_tokens=0)))
    assert not client.complete(a_request()).reasoning_applied


def test_an_http_error_becomes_a_transport_error() -> None:
    client = a_client(responder({"error": {"message": "no credit"}}, status_code=402))
    with pytest.raises(LlmTransportError, match="402"):
        client.complete(a_request())


def test_an_error_object_in_a_200_becomes_a_transport_error() -> None:
    """OpenRouter reports some upstream failures with a 200 and an error body."""
    client = a_client(responder({"error": {"message": "upstream is down"}}))
    with pytest.raises(LlmTransportError, match="upstream is down"):
        client.complete(a_request())


def test_a_response_with_no_choices_becomes_a_transport_error() -> None:
    client = a_client(responder({"id": "gen-1", "choices": []}))
    with pytest.raises(LlmTransportError, match="no choices"):
        client.complete(a_request())


def test_a_connection_failure_becomes_a_transport_error() -> None:
    def fail(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused")

    with pytest.raises(LlmTransportError, match="request failed"):
        a_client(httpx.MockTransport(fail)).complete(a_request())


# ------------------------------------------------- regressions from the fresh-eyes review


@pytest.mark.parametrize("raw", ["real", "REAL", "Real", " real\n"])
def test_the_inference_mode_is_parsed_the_same_way_everywhere(raw: str) -> None:
    """One parser, or a capital letter silently swaps the model for a fake.

    The stage registry normalizes this variable; if the LLM seam read it more strictly,
    ``MOTET_INFERENCE_MODE=Real`` would mean real stage adapters wired to
    ``FakeLlmClient`` — a revision that boots clean, skips the credential check, and
    feeds fabricated text into grounding validation and then into audio.
    """
    env = {"MOTET_INFERENCE_MODE": raw}
    assert current_mode(env) == "real"
    assert load_config(env).provider is Provider.OPENROUTER


def test_an_unrecognized_inference_mode_is_rejected_rather_than_guessed() -> None:
    with pytest.raises(ValueError, match="must be 'fake' or 'real'"):
        load_config({"MOTET_INFERENCE_MODE": "production"})


def test_forcing_the_fake_in_real_mode_announces_itself(caplog: pytest.LogCaptureFixture) -> None:
    """A legitimate escape hatch, but a silent one looks like the bug it resembles."""
    with caplog.at_level(logging.WARNING, logger="motet.llm.config"):
        config = load_config({"MOTET_INFERENCE_MODE": "real", "MOTET_LLM_PROVIDER": "fake"})
    assert config.provider is Provider.FAKE
    assert "fabricated" in caplog.text


def test_an_empty_completion_is_an_error_rather_than_an_empty_string() -> None:
    """Reasoning can eat the whole budget, and the result looks like a success.

    At ``effort=max`` the model can spend every output token thinking and return no
    content at all. ``reasoning_tokens`` is non-zero so the reasoning guard is satisfied;
    without this check the caller gets ``""`` and fails parsing it several frames away,
    with nothing pointing back at truncation.
    """
    truncated = completion(text="", reasoning_tokens=4096)
    truncated["choices"][0]["finish_reason"] = "length"
    truncated["choices"][0]["message"]["content"] = None
    client = a_client(responder(truncated))
    with pytest.raises(LlmTransportError, match="empty completion"):
        client.complete(a_request(reasoning=Reasoning(effort="max")))


def test_a_whitespace_only_completion_is_also_an_error() -> None:
    client = a_client(responder(completion(text="   \n  ")))
    with pytest.raises(LlmTransportError, match="empty completion"):
        client.complete(a_request())


def test_a_truncated_but_non_empty_completion_warns(caplog: pytest.LogCaptureFixture) -> None:
    """Incomplete, not merely short — the caller should know which."""
    truncated = completion(text="a partial ans")
    truncated["choices"][0]["finish_reason"] = "length"
    with caplog.at_level(logging.WARNING, logger="motet.llm.openrouter"):
        response = a_client(responder(truncated)).complete(a_request())
    assert response.finish_reason == "length"
    assert "truncated" in caplog.text


def test_the_fake_keys_its_cache_on_the_last_breakpoint_not_the_first() -> None:
    """The dedup shape has two breakpoints, so first-vs-last is the whole question.

    Keying on the first would report a full hit whenever the system prompt matched, no
    matter what happened to the news-item window after it — a hit the real provider
    would not deliver, asserted by a fake that exists to catch exactly that.
    """
    client = FakeLlmClient(simulate_cache=True)

    def request_with(window: str) -> LlmRequest:
        return a_request(
            messages=(
                Message.of("system", "stable instructions", CacheControl()),
                Message.of("user", window, CacheControl()),
            )
        )

    client.complete(request_with("window A"))
    changed = client.complete(request_with("window B"))
    assert changed.usage.cache_read_tokens == 0, "a changed window cannot be a cache hit"
    repeated = client.complete(request_with("window B"))
    assert repeated.usage.cache_read_tokens > 0


def test_the_fake_honours_a_canned_empty_response() -> None:
    """Membership, not truthiness: an empty answer is a legitimate thing to script."""
    client = FakeLlmClient()
    digest = request_digest(a_request())
    assert FakeLlmClient(responses={digest: ""}).complete(a_request()).text == ""
    assert client.complete(a_request()).text.startswith("fake-completion:")


def test_asking_for_more_output_tokens_than_the_model_allows_is_rejected() -> None:
    """A catalogue fact nothing enforced would be worse than not recording it."""
    config = load_config(
        {"MOTET_LLM_MODEL": "anthropic/claude-haiku-4.5", "MOTET_LLM_EFFORT": "off"}
    )
    with pytest.raises(LlmConfigError, match="caps at 64000"):
        build_request(LlmStage.SCRIPT, MESSAGES, max_output_tokens=128_000, config=config)


def test_a_1h_cache_ttl_on_a_model_without_extended_ttls_is_rejected() -> None:
    """Another field a provider would quietly downgrade rather than reject."""
    # gpt-5.1 caps at `high`, so the global effort comes down with the model — which is
    # itself the startup guard doing its job.
    config = load_config({"MOTET_LLM_MODEL": "openai/gpt-5.1", "MOTET_LLM_EFFORT": "high"})
    with pytest.raises(LlmConfigError, match="1h cache TTL"):
        build_request(
            LlmStage.SCRIPT,
            (Message.of("user", "x", CacheControl(ttl="1h")),),
            max_output_tokens=100,
            config=config,
        )


def test_build_client_constructs_the_real_adapter_without_touching_the_network() -> None:
    """Covers the lazy import and the credential wiring, which nothing else exercises."""
    client = build_client(env=REAL)
    assert isinstance(client, OpenRouterClient)
    assert isinstance(client, LlmClient)
    client.close()


def test_the_real_client_is_usable_as_a_context_manager() -> None:
    with a_client(responder(completion())) as client:
        assert client.complete(a_request()).text == "an answer"


def test_cache_write_tokens_are_parsed_when_the_provider_reports_them() -> None:
    payload = completion()
    payload["usage"]["prompt_tokens_details"]["cache_creation_tokens"] = 2048
    response = a_client(responder(payload)).complete(a_request())
    assert response.usage.cache_write_tokens == 2048


def test_a_boolean_is_not_mistaken_for_a_token_count() -> None:
    """`isinstance(True, int)` is True in Python, so the guard is not decoration."""
    payload = completion()
    payload["usage"]["prompt_tokens"] = True
    assert a_client(responder(payload)).complete(a_request()).usage.input_tokens == 0
