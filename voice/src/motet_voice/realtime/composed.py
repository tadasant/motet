"""The composed arm: VAD, STT, LLM and TTS as four separate legs.

**This is the arm that can actually run tonight**, which is why it is the default. Of its
four legs, two are already provisioned and wired to real vendors through seams that exist:

**Nothing checks this arm's replies against their material** — the advisory grounding
check was removed in motet#75. See :func:`_system_prompt` for the containment the prompt
provides, which is now the whole of it.

| Leg | Implementation | Provisioned? |
|---|---|---|
| VAD / turn detection | :mod:`motet_voice.vad` — ours, local, deterministic | n/a — no vendor |
| STT | none yet | **No** — dormant, see :class:`DormantSpeechRecognizer` |
| LLM | ``motet_inference.llm`` — OpenRouter, Claude Sonnet 5 | **Yes** |
| TTS | ``motet_inference`` — Cartesia Sonic | **Yes** |

The composed arm's case for existing is that each leg is separately swappable and
separately observable. Its case against is latency: four hops instead of one. The walk is
what decides whether the latency is a price worth paying, and the *turn detection* leg —
the one leg that needs no vendor at all — is the part being measured.

**Note what the missing STT leg does not block.** Barge-in detection does not need to know
*what* was said, only *that* someone is speaking. So the measurement runs at full fidelity
with STT dormant, and only the conversational half is degraded.
"""

from __future__ import annotations

import hashlib
import logging
import os
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from typing import Final

from motet_inference.accounting import record_budget_exhausted, record_usage
from motet_inference.interfaces import SpeechSynthesizer
from motet_inference.llm import (
    LlmBudgetExhaustedError,
    LlmClient,
    LlmConfig,
    LlmStage,
    Message,
    build_request,
)
from motet_inference.types import Audio

from ..bargein import BargeInPolicy, TurnDetector, VadTurnDetector
from ..config import COMPOSED_ARM, VoiceSettings
from ..vad import EnergyVad, Vad
from .interfaces import (
    ArmCapabilities,
    ArmDormant,
    AssistantTurn,
    ConversationModel,
    SpeechRecognizer,
    TurnRequest,
)

logger = logging.getLogger("motet.voice.composed")

#: A voice turn is short and latency-critical. Capping output is what stops a model from
#: monologuing at someone who asked a one-sentence question on a pavement.
MAX_REPLY_TOKENS: Final = 400


@dataclass
class DormantSpeechRecognizer:
    """No STT vendor is provisioned, and this says so rather than returning empty text.

    Deliberately not a silent no-op. An STT leg that returns ``""`` produces a session
    where the model politely answers a question nobody asked, and the cause is invisible.
    """

    reason: str = (
        "no speech-to-text vendor is provisioned for the composed arm, so it cannot "
        "transcribe the listener yet. Barge-in detection is unaffected."
    )

    @property
    def name(self) -> str:
        return "dormant"

    def transcribe(self, pcm: bytes) -> str:
        raise ArmDormant(self.reason)


@dataclass
class FakeSpeechRecognizer:
    """Deterministic STT: the transcript is a function of the audio, and only of the audio.

    Derived from a hash so that the same bytes always yield the same words and different
    bytes yield different words — which is what a test of "did the transcript flow through
    to the model" needs, and nothing more.
    """

    phrases: tuple[str, ...] = (
        "what was that funding number again",
        "save that bit",
        "skip this one",
        "who else reported it",
        "mark it read",
    )

    @property
    def name(self) -> str:
        return "fake"

    def transcribe(self, pcm: bytes) -> str:
        if not pcm:
            return ""
        index = int.from_bytes(hashlib.sha256(pcm).digest()[:2], "big") % len(self.phrases)
        return self.phrases[index]


@dataclass
class FakeConversationModel:
    """A deterministic reply, with no model behind it."""

    @property
    def name(self) -> str:
        return "fake"

    def reply(self, request: TurnRequest, user_text: str) -> str:
        return f"[fake-voice-reply] {user_text.strip() or 'go on'}"


@dataclass
class LlmConversationModel:
    """The LLM leg, through the provider seam — never a vendor SDK directly.

    **Which model, and how hard it thinks, come from ``LlmStage.VOICE``** — the same
    per-stage seam dedup and script use, rather than a variable of this
    module's own (motet#6). What that buys is the startup catalogue check: a slug typo in
    ``MOTET_LLM_MODEL_VOICE`` stops the process with a message naming
    ``bin/check-openrouter-models``, instead of surfacing as a vendor error inside
    somebody's spoken turn.

    Reasoning is **off by default** for this stage: a spoken turn is generated with a
    person standing in the street waiting for it, so a second of thinking is a second of
    silence. The stages that need thinking ask for it; a conversational reply is not one
    of them. ``MOTET_LLM_EFFORT_VOICE`` is there for a deployment that decides otherwise.

    The config is resolved **once**, when the arm is built, and held. Resolving it per
    turn would put an environment read and a catalogue validation inside the latency
    budget this whole arm exists to measure.

    **What the turn spent is recorded here, and this is the placement decision (motet#58).**
    AGENTS.md puts recording in the stage adapters rather than in the OpenRouter client
    because *stage* is what an operator splits cost by and an ``LlmRequest`` deliberately
    does not carry one. The load-bearing half of that is "the object that owns the call and
    names the stage is the object that records it" — and for ``LlmStage.VOICE`` that object
    is this one. Moving the leg into ``inference/`` to make it look like the pipeline stages
    would have to drag :class:`~motet_voice.realtime.interfaces.TurnRequest` and
    :func:`_system_prompt` — voice's own contract, and the only containment this path has —
    across the package boundary, and would point the dependency arrow the wrong way:
    ``motet-inference`` knows nothing about ``motet-voice``
    and must not start.

    Without this, a real voice session's completions were billed by OpenRouter and appeared
    in no metric and no log line — and a Grafana panel split by ``stage`` showed three
    series where the enum has four, so a voice fleet spending money and a voice fleet
    nobody has used looked identical.
    """

    client: LlmClient
    config: LlmConfig

    @property
    def model(self) -> str:
        return self.config.for_stage(LlmStage.VOICE).model

    @property
    def name(self) -> str:
        return f"llm:{self.model}"

    def reply(self, request: TurnRequest, user_text: str) -> str:
        messages = [Message.of("system", _system_prompt(request))]
        for turn in request.history:
            role = turn.get("role", "user")
            if role in ("user", "assistant"):
                messages.append(Message.of(role, turn.get("text", "")))  # type: ignore[arg-type]
        messages.append(Message.of("user", user_text))
        llm_request = build_request(
            LlmStage.VOICE,
            messages,
            max_output_tokens=MAX_REPLY_TOKENS,
            # The one path where the dropped-reasoning guard costs more than it buys.
            # It fires *after* a complete, billed answer has arrived, and raising here
            # would throw that answer away and propagate out of the turn — nothing
            # between this and :meth:`VoiceSession.respond_to_text` catches it, so the
            # listener gets silence and an error instead of the sentence that was
            # already generated. On a batch stage a lost completion is a retry; here it
            # is the turn. The adapter still logs the warning unconditionally, so the
            # quality drop is recorded rather than hidden, which is the half that matters
            # on a path where raising costs the listener the answer. Unreachable while
            # voice defaults to ``off``; it stops being unreachable the moment anyone sets
            # ``MOTET_LLM_EFFORT_VOICE``.
            require_reasoning_evidence=False,
            config=self.config,
        )
        try:
            response = self.client.complete(llm_request)
        except LlmBudgetExhaustedError as exc:
            # Billed and useless is still billed (motet#58). A voice turn sends no
            # ``response_format``, so this is the narrow case where reasoning ate the whole
            # of ``MAX_REPLY_TOKENS`` before the first word — reachable only once somebody
            # sets ``MOTET_LLM_EFFORT_VOICE``, and the most expensive turn there is. The
            # error still propagates: the turn genuinely failed, and swallowing it here
            # would hand :meth:`VoiceSession.respond_to_text` an empty reply to speak.
            record_budget_exhausted(LlmStage.VOICE, exc)
            raise
        # The one text call in the system a person waits on in real time, and until
        # motet#58 the only one that spent money without leaving a number behind. Recorded
        # here rather than inside the OpenRouter client for the reason the stage adapters
        # record where they do: *stage* is what an operator splits cost by, and this is the
        # object that owns the call and knows which stage it is.
        record_usage(LlmStage.VOICE, response)
        return response.text


def _system_prompt(request: TurnRequest) -> str:
    """Persona plus context, and nothing fetched.

    Every fact the model may use arrives in :attr:`TurnRequest.context_notes`, placed there
    by the caller that owns the database. Invariant 2 is not a rule this prompt obeys; it
    is the reason the prompt is built this way.

    **This prompt is the only containment on what gets said, and it is not a guarantee.**
    Nothing checks a reply against its material any more (motet#75). The material is
    context the caller assembled from an episode's own claims and their source spans, and
    the prompt below tells the model to answer from that and not from what it recalls. It
    used to also name ``get_item_detail`` as somewhere to reach for spans; that tool never
    worked and is gone (motet#120), so the whole of the sourced material a turn has is now
    what arrived in the session config — which, for this argument, is the stronger position
    rather than a weaker one. The failure it narrows to is still paraphrase and inference
    over text that came out of a source, and it still does not eliminate it: a spoken
    answer here can assert something no span supports, and nothing will say so.

    **Do not widen the path without reopening that.** Anything that gives this path a
    *new* source of material — a research result, a second corpus, a longer memory —
    changes the risk from "paraphrase over sourced text" to "assertion from unsourced
    text", and there is now no instrument behind the reply at all.
    """
    parts = [request.persona_instructions.strip()]
    if request.context_notes.strip():
        parts.append("What you already know about this episode:\n" + request.context_notes)
    if request.position_notes.strip():
        # Fresh per turn where the block above is fixed for the session: where the listener
        # interrupted, from our clock (:mod:`motet_voice.position`). Empty when the caller
        # sent no timed transcript, so the prompt is then exactly what it was.
        parts.append(request.position_notes.strip())
    if request.tools:
        names = ", ".join(str(tool.get("name", "?")) for tool in request.tools)
        parts.append(f"Tools available to you: {names}.")
    parts.append(
        "Answer only from what you have been given above, or from what a tool returns. If "
        "you are asked something it does not cover, say you do not have it and offer to look "
        "it up — do not fill the gap from memory. Numbers, names and dates especially: quote "
        "them from the material or fetch them, never recall them."
    )
    parts.append("Answer in one or two spoken sentences. You are being listened to, not read.")
    return "\n\n".join(parts)


@dataclass
class ComposedArm:
    """VAD + STT + LLM + TTS, assembled.

    ``vad_factory`` rather than a VAD, because a VAD is **stateful**: it carries the adaptive
    noise floor for one stream of audio. The arm is process-wide and Cloud Run serves many
    sessions per instance, so a shared instance would have two listeners driving one floor
    and either one's ``reset()`` wiping the other's. Each detector gets its own.
    """

    vad_factory: Callable[[], Vad] = EnergyVad
    recognizer: SpeechRecognizer = field(default_factory=DormantSpeechRecognizer)
    model: ConversationModel = field(default_factory=FakeConversationModel)
    synthesizer: SpeechSynthesizer | None = None
    conversational: bool = False
    dormant_reason: str = ""

    @property
    def name(self) -> str:
        return COMPOSED_ARM

    def capabilities(self) -> ArmCapabilities:
        return ArmCapabilities(
            name=self.name,
            turn_detection="local",
            conversational=self.conversational,
            replayable=True,
            dormant_reason=self.dormant_reason,
            notes=(
                f"vad={self.vad_factory().name} stt={self.recognizer.name} "
                f"llm={self.model.name} "
                f"tts={'wired' if self.synthesizer is not None else 'none'}"
            ),
        )

    def build_turn_detector(self, policy: BargeInPolicy) -> TurnDetector:
        """No credential, no network, fully deterministic — the measurable half."""
        vad = self.vad_factory()
        return VadTurnDetector(
            vad=vad, policy=policy, arm_name=self.name, trigger=f"local_vad:{vad.name}"
        )

    async def respond(self, request: TurnRequest) -> AssistantTurn:
        user_text = request.user_text or ""
        if not user_text and request.user_pcm:
            user_text = self.recognizer.transcribe(request.user_pcm)

        reply = self.model.reply(request, user_text)
        audio: Audio | None = None
        if self.synthesizer is not None:
            audio = self.synthesizer.synthesize(reply)
        return AssistantTurn(text=reply, user_transcript=user_text, audio=audio)

    async def aclose(self) -> None:
        return None


def build_composed_arm(
    settings: VoiceSettings, *, env: Mapping[str, str] | None = None
) -> ComposedArm:
    """Assemble the arm for this process, honouring ``MOTET_INFERENCE_MODE``.

    In ``fake`` mode — every test, every laptop, all of CI — nothing here reaches a vendor.
    That is invariant 7 and it is why the mode is read from the one parser rather than from
    a second variable of this module's own.
    """
    environ = os.environ if env is None else env

    if not settings.real:
        return ComposedArm(
            vad_factory=EnergyVad,
            recognizer=FakeSpeechRecognizer(),
            model=FakeConversationModel(),
            synthesizer=_fake_synthesizer(),
            conversational=True,
            dormant_reason="",
        )

    from motet_inference.adapters import CartesiaSpeechSynthesizer  # noqa: PLC0415
    from motet_inference.llm import Provider, build_client, load_config  # noqa: PLC0415

    # Resolved once, here, and handed to both halves: the client is per process, and the
    # config it was built from is what every turn's request is built against. Loading it
    # twice would let a stale environment and a fresh one disagree about the model.
    config = load_config(environ)
    if config.provider is Provider.FAKE:
        # Two readings of "is this real" that can disagree, so say so rather than serving
        # fabricated replies from an arm that reports itself as real. `settings` came from
        # one mapping and `config` from `environ`; a caller passing a partial `env` — which
        # in practice means a test — gets a real-looking arm wired to `FakeLlmClient`.
        # `load_config`'s own version of this warning cannot fire here, because it reads
        # the same partial mapping and sees no mode at all.
        logger.warning(
            "the composed arm is being built in real mode but the LLM seam resolved "
            "provider=fake from the environment it was given, so every conversational "
            "reply will be fabricated. Nothing will reach a vendor."
        )
    return ComposedArm(
        vad_factory=EnergyVad,
        recognizer=DormantSpeechRecognizer(),
        model=LlmConversationModel(client=build_client(config, env=environ), config=config),
        synthesizer=CartesiaSpeechSynthesizer(),
        # Two of four legs are live, so the arm can speak but cannot listen. Reported as
        # not-conversational rather than half-conversational, because a session that can
        # talk and not hear is worse than one that says up front that it cannot.
        conversational=False,
        dormant_reason=DormantSpeechRecognizer().reason,
    )


def _fake_synthesizer() -> SpeechSynthesizer:
    from motet_inference.fakes import FakeSpeechSynthesizer  # noqa: PLC0415

    return FakeSpeechSynthesizer()
