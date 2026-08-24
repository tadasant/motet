"""The composed arm: VAD, STT, LLM and TTS as four separate legs.

**This is the arm that can actually run tonight**, which is why it is the default. Of its
four legs, two are already provisioned and wired to real vendors through seams that exist:

**Grounding does not run on this arm's reply path**, and that is a stated gap rather than a
design — see :func:`_system_prompt` for what stands in for it and why, and
https://github.com/tadasant/motet/issues/10 for the half that is not here.

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

from motet_inference.interfaces import SpeechSynthesizer
from motet_inference.llm import DEFAULT_MODEL, MODEL_ENV, LlmClient, LlmRequest, Message
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

#: Conversation gets its own model variable so a chat model can be cheap and fast while
#: script generation stays expensive and careful. Falls back to the seam's own global.
#:
#: **This should eventually be a fourth ``LlmStage``** alongside dedup/script/grounding —
#: that is where per-stage model and effort selection belongs. Adding one means editing
#: ``inference/``, which a parallel session is working in tonight, so the selection is done
#: here for now and the follow-up is filed rather than smuggled into this PR.
VOICE_MODEL_ENV: Final = "MOTET_VOICE_LLM_MODEL"

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

    Reasoning is deliberately **off**: this is a spoken turn, where a second of thinking is
    a second of a person standing in the street waiting. The stages that need thinking ask
    for it; a conversational reply is not one of them.
    """

    client: LlmClient
    model: str

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
        response = self.client.complete(
            LlmRequest(
                model=self.model,
                messages=tuple(messages),
                max_output_tokens=MAX_REPLY_TOKENS,
            )
        )
        return response.text


def _system_prompt(request: TurnRequest) -> str:
    """Persona plus context, and nothing fetched.

    Every fact the model may use arrives in :attr:`TurnRequest.context_notes`, placed there
    by the caller that owns the database. Invariant 2 is not a rule this prompt obeys; it
    is the reason the prompt is built this way.

    **Grounding — read this before extending the reply path.** Invariant 3 says every
    reported claim carries a source span validated *before* TTS, and the narration path
    enforces it as a pipeline gate. This path does not, and the gap is stated here rather
    than left for a reader to notice: a conversational reply is generated inside a spoken
    turn, and the grounding validator is a max-effort model call that cannot live there.

    What this path has instead is containment, and containment is a mitigation rather than
    a guarantee. The material is context the caller assembled from narration that was
    *already* grounded; the prompt below tells the model to answer from it and to reach for
    ``get_item_detail`` — which returns spans — instead of recalling. That narrows the
    failure to paraphrase and inference over grounded text. It does not eliminate it, and
    a spoken answer here can still assert something no span supports.

    **So do not treat this as settled, and do not widen the path on the strength of it.**
    Closing it properly is a design question with a latency budget at its centre, filed as
    https://github.com/tadasant/motet/issues/10. Anything that gives this path a *new* source
    of material — a research result, a second corpus, a longer memory — has to answer the
    grounding question first, because that is when paraphrase-over-grounded-text stops
    being the whole of the risk.
    """
    parts = [request.persona_instructions.strip()]
    if request.context_notes.strip():
        parts.append("What you already know about this episode:\n" + request.context_notes)
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
    from motet_inference.llm import build_client  # noqa: PLC0415

    model = (
        environ.get(VOICE_MODEL_ENV, "").strip()
        or environ.get(MODEL_ENV, "").strip()
        or DEFAULT_MODEL
    )
    return ComposedArm(
        vad_factory=EnergyVad,
        recognizer=DormantSpeechRecognizer(),
        model=LlmConversationModel(client=build_client(), model=model),
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
