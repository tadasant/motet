"""The OpenAI Realtime arm — built in full, **dormant** on a key that does not exist.

``OPENAI_API_KEY`` is not provisioned. Tadas deferred it deliberately, so this arm ships
complete and switched off: the wire protocol is implemented, the transport is a seam with a
fake, and the tests drive the client against scripted vendor events. **Turning it on is a
credential and one environment variable, not a code change.**

Two things about this file need saying plainly, because getting either wrong would make the
walk produce a number that means nothing.

**1. Offline, this arm's turn detection is an *emulation*, not a measurement.**
The whole point of a hosted realtime provider is that turn detection happens server-side,
inside the vendor's socket, using a model we do not have. With no key we cannot run it. So
:class:`ServerVadEmulator` reproduces the *documented shape* of that server VAD — a
threshold on a speech probability, ``prefix_padding_ms`` before the onset, and
``silence_duration_ms`` of quiet to end a turn — over a local detector. That makes the two
arms comparable on the dials that are actually documented, and it does **not** tell you how
the vendor's own model behaves on wind. A report generated with no key says so on its face;
see :mod:`motet_voice.harness.report`.

**2. Live, the arm reports the vendor's decisions and does not second-guess them.**
When the key exists, :class:`ServerVadRelay` turns ``input_audio_buffer.speech_started``
events into barge-in decisions with ``trigger="openai_server_vad"``. It deliberately does
*not* also run a local VAD and merge: measuring a vendor means measuring the vendor.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
from collections.abc import AsyncIterator, Mapping
from dataclasses import dataclass, field, replace
from typing import Any, Final, Protocol, runtime_checkable

from ..audio import DEFAULT_FRAME_MS, PcmFrame, dbfs, zero_crossing_rate
from ..bargein import BargeInDecision, BargeInPolicy, TurnDetector, VadTurnDetector
from ..config import OPENAI_KEY_ENV, OPENAI_REALTIME_ARM, VoiceSettings
from ..vad import EnergyVad, Vad, VadReading
from .interfaces import (
    ArmCapabilities,
    ArmDormant,
    AssistantTurn,
    PendingToolCall,
    TurnRequest,
)

logger = logging.getLogger("motet.voice.openai_realtime")

REALTIME_URL: Final = "wss://api.openai.com/v1/realtime"

#: The vendor's documented server-VAD dials, at their documented defaults. Mirrored here
#: so the emulator and the live ``session.update`` are configured from **one** set of
#: numbers — two copies would drift, and the drift would look like a provider difference.
DEFAULT_SERVER_VAD: Final[Mapping[str, Any]] = {
    "type": "server_vad",
    "threshold": 0.5,
    "prefix_padding_ms": 300,
    "silence_duration_ms": 500,
}


class RealtimeProtocolError(RuntimeError):
    """The provider sent something this client cannot make sense of."""


@runtime_checkable
class RealtimeTransport(Protocol):
    """A duplex JSON channel to a realtime provider. Faked in tests; a socket in production."""

    async def send(self, event: Mapping[str, Any]) -> None: ...

    def events(self) -> AsyncIterator[dict[str, Any]]: ...

    async def aclose(self) -> None: ...


@dataclass
class ScriptedRealtimeTransport:
    """The fake: replays a canned list of provider events and records what was sent.

    This is what makes the arm testable with no key. The event names and payload shapes are
    the vendor's documented ones, so a test here fails if our client stops handling the
    protocol correctly — which is most of what can rot in an integration nobody can run.
    """

    scripted: list[dict[str, Any]] = field(default_factory=list)
    sent: list[dict[str, Any]] = field(default_factory=list)
    closed: bool = False

    async def send(self, event: Mapping[str, Any]) -> None:
        self.sent.append(dict(event))

    async def events(self) -> AsyncIterator[dict[str, Any]]:
        for event in self.scripted:
            yield event

    async def aclose(self) -> None:
        self.closed = True


class WebsocketRealtimeTransport:
    """The real socket. Never constructed without a key — see :func:`build_openai_arm`.

    ``websockets`` is imported lazily so that a fake-mode process never pays for it, the
    same shape the GCS backend and the OpenRouter adapter use elsewhere in this repo.
    """

    def __init__(self, api_key: str, model: str) -> None:
        if not api_key:  # pragma: no cover — build_openai_arm refuses first
            raise ArmDormant(f"{OPENAI_KEY_ENV} is not set")
        self._api_key = api_key
        self._model = model
        self._socket: Any = None
        self._lock = asyncio.Lock()

    async def _connect(self) -> Any:
        """Open the socket once, under a lock.

        **One transport is one conversation.** The socket is stateful on the vendor's side —
        a `session.update` applies to it, and responses interleave on it — so a caller that
        wants concurrent sessions must build a transport per session rather than sharing this
        one. The lock only stops two coroutines racing to open the *same* connection; it does
        not make the connection multi-session, and nothing here pretends otherwise.
        """
        async with self._lock:
            if self._socket is None:
                from websockets.asyncio.client import connect  # noqa: PLC0415

                self._socket = await connect(
                    f"{REALTIME_URL}?model={self._model}",
                    additional_headers={"Authorization": f"Bearer {self._api_key}"},
                )
            return self._socket

    async def send(self, event: Mapping[str, Any]) -> None:
        socket = await self._connect()
        await socket.send(json.dumps(dict(event)))

    async def events(self) -> AsyncIterator[dict[str, Any]]:
        socket = await self._connect()
        async for raw in socket:
            try:
                decoded = json.loads(raw)
            except ValueError as exc:
                raise RealtimeProtocolError(f"provider sent non-JSON: {exc}") from exc
            if isinstance(decoded, dict):
                yield decoded

    async def aclose(self) -> None:
        if self._socket is not None:
            await self._socket.close()
            self._socket = None


@dataclass
class ServerVadEmulator:
    """A local stand-in for the vendor's server VAD, on its documented dials.

    ``prefix_padding_ms`` becomes the onset back-off, ``silence_duration_ms`` becomes the
    refractory window, and ``threshold`` becomes the probability gate. The underlying
    speech probability comes from :class:`~motet_voice.vad.EnergyVad`, because that is the
    only detector available with no credential.

    **It is an emulation and it is labelled as one everywhere it appears.** Its value is
    that it makes the two arms comparable *on the dials*, so a sweep can answer "would
    300 ms of prefix padding have helped" without a vendor. It cannot answer "is the
    vendor's model better at wind than ours".
    """

    threshold: float = float(DEFAULT_SERVER_VAD["threshold"])
    prefix_padding_ms: int = int(DEFAULT_SERVER_VAD["prefix_padding_ms"])
    silence_duration_ms: int = int(DEFAULT_SERVER_VAD["silence_duration_ms"])
    inner: Vad = field(default_factory=EnergyVad)

    @property
    def name(self) -> str:
        return f"server_vad_emulation(t={self.threshold})"

    def reset(self) -> None:
        self.inner.reset()

    def observe(self, frame: PcmFrame) -> VadReading:
        reading = self.inner.observe(frame)
        # Rescale so that the vendor's `threshold` dial means what it means over there:
        # the documented parameter is a probability gate, and remapping ours onto it is
        # what lets one sweep vary that dial across both arms.
        scaled = min(1.0, reading.speech_probability / max(self.threshold, 1e-6) * 0.5)
        return VadReading(
            speech_probability=scaled,
            rms_dbfs=reading.rms_dbfs,
            noise_floor_dbfs=reading.noise_floor_dbfs,
            snr_db=reading.snr_db,
            zero_crossing_rate=reading.zero_crossing_rate,
        )


@dataclass
class ServerVadRelay:
    """Turns the provider's own ``speech_started`` events into barge-in decisions.

    Used only when the key exists. Frames are handed to the socket and the *provider*
    decides; this class holds the plumbing that turns its verdict into the same
    :class:`~motet_voice.bargein.BargeInDecision` shape the composed arm produces, so a
    report can put the two side by side.

    Local measurements (dBFS, zero-crossing rate) are still attached to each decision, and
    they are measurements rather than inputs: they are what makes a vendor false positive
    reviewable — "it fired on a −48 dBFS frame with a ZCR of 0.6" is a finding.
    """

    arm_name: str = OPENAI_REALTIME_ARM
    policy: BargeInPolicy = field(default_factory=BargeInPolicy)
    _last_frame: PcmFrame | None = field(default=None, init=False)
    _last_fired_ms: int | None = field(default=None, init=False)

    @property
    def arm(self) -> str:
        return self.arm_name

    @property
    def variant(self) -> str:
        return self.policy.name

    def reset(self) -> None:
        self._last_frame = None
        self._last_fired_ms = None

    def observe(
        self, frame: PcmFrame, *, narration_playing: bool, spoken_through_ms: int
    ) -> BargeInDecision | None:
        """Remember the frame. The provider, not this method, decides."""
        self._last_frame = frame
        return None

    def on_speech_started(
        self, *, audio_start_ms: int, narration_playing: bool, spoken_through_ms: int
    ) -> BargeInDecision | None:
        """The provider says the listener started talking. Record it, with local evidence."""
        if (
            self._last_fired_ms is not None
            and audio_start_ms - self._last_fired_ms < self.policy.refractory_ms
        ):
            return None
        self._last_fired_ms = audio_start_ms
        frame = self._last_frame
        samples = frame.samples if frame is not None else []
        return BargeInDecision(
            at_ms=audio_start_ms,
            onset_ms=max(0, audio_start_ms - int(DEFAULT_SERVER_VAD["prefix_padding_ms"])),
            arm=self.arm,
            variant=self.variant,
            trigger="openai_server_vad",
            spoken_through_ms=spoken_through_ms,
            narration_playing=narration_playing,
            consecutive_frames=0,
            speech_probability=1.0,
            rms_dbfs=dbfs(samples),
            noise_floor_dbfs=0.0,
            snr_db=0.0,
            zero_crossing_rate=zero_crossing_rate(samples),
        )


@dataclass
class OpenAiRealtimeArm:
    """The hosted-realtime arm of the comparison."""

    model: str
    transport: RealtimeTransport | None = None
    api_key_present: bool = False
    server_vad: Mapping[str, Any] = field(default_factory=lambda: dict(DEFAULT_SERVER_VAD))
    #: How long one conversational turn may take before it is abandoned. Generous for a
    #: model that thinks, far short of a listener standing in the street forever.
    turn_timeout_seconds: float = 30.0

    @property
    def name(self) -> str:
        return OPENAI_REALTIME_ARM

    def capabilities(self) -> ArmCapabilities:
        dormant = (
            ""
            if self.api_key_present
            else (
                f"{OPENAI_KEY_ENV} is not provisioned, so no vendor session can be opened. "
                "Turn detection falls back to an offline emulation of this provider's "
                "documented server-VAD parameters, which is not a measurement of the vendor."
            )
        )
        return ArmCapabilities(
            name=self.name,
            turn_detection="server",
            conversational=self.api_key_present,
            replayable=True,
            # Always True today, key or no key. The replayable detector is the emulation,
            # because the live relay has no path by which recorded audio could reach the
            # vendor — see `build_turn_detector`. It becomes False the day a live capture
            # path exists, and until then a report that said otherwise would be presenting
            # emulated numbers as a measurement.
            turn_detection_emulated=True,
            dormant_reason=dormant,
            notes=f"model={self.model} server_vad={json.dumps(dict(self.server_vad))}",
        )

    def build_turn_detector(self, policy: BargeInPolicy) -> TurnDetector:
        """The labelled emulation — **always**, key or no key.

        It is tempting to hand back :class:`ServerVadRelay` once a key exists, and that was
        the first version of this method. It is wrong, and wrong in the worst available
        direction: the relay decides nothing on its own — it reports what the vendor's socket
        says — and nothing feeds recorded audio to a socket, so a replay through it produces
        *zero decisions*, scores a perfect zero false positives per minute, and gets crowned
        the winner of the very comparison this harness exists to run. A silently wrong
        measurement is worse than no measurement.

        The relay is reachable through :meth:`build_live_turn_detector`, for the live session
        path that will use it. When that path exists, this method can return it for a replay
        that streams audio through the vendor, and ``turn_detection_emulated`` goes false.

        The vendor's documented dials are applied rather than merely stored:
        ``prefix_padding_ms`` becomes the onset back-off, and ``silence_duration_ms`` becomes
        a floor on the refractory window — the vendor needs that much quiet to end a turn, so
        it cannot start a second one sooner.
        """
        padding = int(self.server_vad.get("prefix_padding_ms", 300))
        silence = int(self.server_vad.get("silence_duration_ms", 500))
        return VadTurnDetector(
            vad=ServerVadEmulator(
                threshold=float(self.server_vad.get("threshold", 0.5)),
                prefix_padding_ms=padding,
                silence_duration_ms=silence,
            ),
            policy=replace(policy, refractory_ms=max(policy.refractory_ms, silence)),
            arm_name=self.name,
            trigger="openai_server_vad_emulated",
            frame_ms=DEFAULT_FRAME_MS,
            onset_back_off_ms=padding,
        )

    def build_live_turn_detector(self, policy: BargeInPolicy) -> ServerVadRelay:
        """The relay that reports the *vendor's* decisions, for a live session.

        Not reachable from :meth:`build_turn_detector` on purpose — see there. A caller using
        this is responsible for forwarding audio to the socket and calling
        :meth:`ServerVadRelay.on_speech_started` when the provider reports one; a relay that
        is merely fed frames produces nothing, which is exactly the trap being avoided.
        """
        return ServerVadRelay(arm_name=self.name, policy=policy)

    def session_update(self, request: TurnRequest) -> dict[str, Any]:
        """The ``session.update`` payload. Public so a test can assert its shape.

        Everything the session may know is in it: there is no second channel through which
        this arm could fetch anything, which is invariant 2 expressed as a wire format.
        """
        instructions = request.persona_instructions
        if request.context_notes.strip():
            instructions += "\n\nWhat you already know:\n" + request.context_notes
        return {
            "type": "session.update",
            "session": {
                "modalities": ["audio", "text"],
                "instructions": instructions,
                "voice": request.voice,
                "input_audio_format": "pcm16",
                "output_audio_format": "pcm16",
                "turn_detection": dict(self.server_vad),
                "tools": [
                    {
                        "type": "function",
                        "name": tool.get("name"),
                        "description": tool.get("description"),
                        "parameters": tool.get("parameters"),
                    }
                    for tool in request.tools
                ],
            },
        }

    async def respond(self, request: TurnRequest) -> AssistantTurn:
        if self.transport is None:
            raise ArmDormant(self.capabilities().dormant_reason or "no transport")
        await self.transport.send(self.session_update(request))
        if request.user_text:
            await self.transport.send(
                {
                    "type": "conversation.item.create",
                    "item": {
                        "type": "message",
                        "role": "user",
                        "content": [{"type": "input_text", "text": request.user_text}],
                    },
                }
            )
        await self.transport.send({"type": "response.create"})
        # Bounded. `_collect` reads until `response.done`, and a provider that stalls without
        # sending it would hang the turn *and* the listener's WebSocket behind it, with no
        # error anywhere. A voice product cannot wait indefinitely on a sentence.
        async with asyncio.timeout(self.turn_timeout_seconds):
            return await self._collect()

    async def _collect(self) -> AssistantTurn:  # noqa: D401
        """Fold the provider's event stream into one turn.

        Unknown event types are ignored rather than raised on: a realtime API adds events
        far more often than it changes the ones that exist, and a client that dies on an
        unrecognized type is a client that breaks on the vendor's next release.
        """
        assert self.transport is not None
        text_parts: list[str] = []
        user_transcript = ""
        calls: dict[str, PendingToolCall] = {}

        async for event in self.transport.events():
            kind = str(event.get("type", ""))
            if kind == "response.audio_transcript.delta":
                text_parts.append(str(event.get("delta", "")))
            elif kind == "conversation.item.input_audio_transcription.completed":
                user_transcript = str(event.get("transcript", ""))
            elif kind == "response.function_call_arguments.done":
                call_id = str(event.get("call_id", ""))
                calls[call_id] = PendingToolCall(
                    call_id=call_id,
                    name=str(event.get("name", "")),
                    arguments=_decode_arguments(event.get("arguments")),
                )
            elif kind == "error":
                raise RealtimeProtocolError(str(event.get("error", "provider error")))
            elif kind == "response.done":
                break

        return AssistantTurn(
            text="".join(text_parts),
            user_transcript=user_transcript,
            tool_calls=tuple(calls.values()),
        )

    async def aclose(self) -> None:
        if self.transport is not None:
            await self.transport.aclose()


def _decode_arguments(raw: Any) -> dict[str, Any]:
    if isinstance(raw, dict):
        return dict(raw)
    if isinstance(raw, str) and raw.strip():
        try:
            decoded = json.loads(raw)
        except ValueError:
            return {"_raw": raw}
        return decoded if isinstance(decoded, dict) else {"_raw": raw}
    return {}


def build_openai_arm(
    settings: VoiceSettings,
    *,
    api_key: str | None = None,
    transport: RealtimeTransport | None = None,
) -> OpenAiRealtimeArm:
    """Build the arm, dormant unless a key is genuinely present.

    Note what this does **not** do: it does not fall back to another vendor, and it does not
    refuse to construct. The arm exists either way, because its turn detector — the half the
    spike measures — needs no credential at all.
    """
    if transport is not None:
        return OpenAiRealtimeArm(
            model=settings.openai_realtime_model, transport=transport, api_key_present=True
        )
    if not settings.openai_api_key_present or not settings.real:
        return OpenAiRealtimeArm(model=settings.openai_realtime_model, api_key_present=False)

    resolved = api_key or os.environ.get(OPENAI_KEY_ENV, "")
    return OpenAiRealtimeArm(
        model=settings.openai_realtime_model,
        transport=WebsocketRealtimeTransport(resolved, settings.openai_realtime_model),
        api_key_present=True,
    )
