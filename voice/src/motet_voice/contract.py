"""The voice service's own protocol — the seam clients speak instead of a vendor's.

**Invariant 1: the client never speaks a vendor protocol.** Nothing in this module names
OpenAI, Cartesia, or anyone else. A client sends a session config and receives a stream of
events; which provider produced them is a service-side detail, so swapping providers is a
deploy, not an App Store release.

**Invariant 2: no database.** A session config arrives *complete* — the persona, the tools
the session may call, the MCP servers it may reach, the context it should already know, and
the turn policy. The voice service never looks anything up. Whoever starts the session
(Motet's API today; Zimmer tomorrow, which is the reason this shape exists at all) has the
database credential and does the reading.

```
StartSession(persona, tools, mcp_servers, context, turn_policy) -> session_token
```

then a socket that emits ``transcript``, ``audio_chunk``, ``tool_call``, ``tool_result``
and ``interrupted_at(offset)``.

**Why a config object and not a handful of arguments.** Because the config is what gets
signed into the session token, and a signature over a structured document is what lets a
stateless service on Cloud Run accept a WebSocket on a different instance than the one that
minted the token. There is no session store to consult; the token *is* the session.
"""

from __future__ import annotations

from typing import Annotated, Any, Literal

from pydantic import BaseModel, Field

#: The tools the platform supplies to every Motet voice session. A caller asks for these by
#: name; anything else in ``tools`` is rejected at StartSession rather than at call time,
#: because a persona that has been told it can do something it cannot will spend the whole
#: session apologizing.
PLATFORM_TOOLS: tuple[str, ...] = (
    "save_highlight",
    "get_item_detail",
    "start_research",
    "mark_read",
)


class Persona(BaseModel):
    """Who the voice is and how it behaves."""

    name: str = Field(min_length=1, max_length=80)
    instructions: str = Field(min_length=1, max_length=8_000)
    #: A provider-neutral voice label. Mapped to a vendor voice id by the arm, never here:
    #: a Cartesia voice uuid in a client request would be invariant 1 gone in one field.
    voice: str = Field(default="narrator", min_length=1, max_length=80)


class TurnPolicy(BaseModel):
    """When the service should consider the listener to have taken the floor.

    Mirrors :class:`~motet_voice.bargein.BargeInPolicy`, one layer out: this is the wire
    form a caller may set, and the arm turns it into the internal policy. Kept separate so
    that adding an internal dial does not change the client contract, and so that a client
    cannot reach past the sanity bounds below.
    """

    #: ``open_mic`` listens throughout, which is what a dog walk needs. ``push_to_talk``
    #: only listens when the client says so, and is the fallback if barge-in loses.
    mode: Literal["open_mic", "push_to_talk"] = "open_mic"
    speech_probability_threshold: Annotated[float, Field(ge=0.0, le=1.0)] = 0.6
    consecutive_speech_frames: Annotated[int, Field(ge=1, le=100)] = 6
    min_snr_db: Annotated[float, Field(ge=0.0, le=60.0)] = 12.0
    refractory_ms: Annotated[int, Field(ge=0, le=30_000)] = 2_000
    #: Interrupt only while narration is playing. The deployed default; the *measurement*
    #: runs with it off, so that an open mic over silence still produces decisions to count.
    require_narration_playing: bool = True


class ToolBinding(BaseModel):
    """A tool the session may call, named — never addressed.

    A binding carries a *name* and nothing about where the call goes: the endpoint comes
    from the service's own configuration. A client that could point a tool at an arbitrary
    host would have turned the voice service into an open proxy.
    """

    name: str = Field(min_length=1, max_length=64)
    #: Free-form arguments the caller wants bound into every invocation — the news item a
    #: session is about, for instance. Merged under the model's own arguments, never over
    #: them, so a caller cannot silently rewrite what the model asked for.
    defaults: dict[str, Any] = Field(default_factory=dict)


class McpServerBinding(BaseModel):
    """An MCP server the session may reach.

    Phase 2 ships the field and no servers: the platform tools below cover what a briefing
    conversation needs, and an MCP server reachable from the always-warm, internet-facing,
    vendor-connected component is a blast-radius decision rather than a feature. The field
    exists because the contract in the target design has it and because Zimmer will use it.
    """

    name: str = Field(min_length=1, max_length=64)
    #: A *slug*, resolved to a transport by service configuration — same reason as
    #: :class:`ToolBinding`: no client-supplied URLs.
    slug: str = Field(min_length=1, max_length=128)


class SessionContext(BaseModel):
    """What the session already knows, passed in whole because it cannot look anything up.

    This is invariant 2 in its most concrete form. Everything the conversation needs about
    the episode — the segments, the news items, where the listener is — arrives here, from
    a caller that does have the database.
    """

    episode_id: str | None = None
    #: Where narration had reached when the session opened. **Ours, not a provider's**
    #: (invariant 4): the caller reports it and :class:`~motet_voice.clock.PlaybackClock`
    #: takes it from here.
    spoken_through_ms: Annotated[int, Field(ge=0)] = 0
    #: Free-form briefing material — segment texts, item titles, the listener's own notes.
    #: Capped because it is passed in-prompt on every turn.
    notes: str = Field(default="", max_length=64_000)
    news_item_ids: list[str] = Field(default_factory=list)


class StartSessionRequest(BaseModel):
    """``StartSession(persona, tools, mcp_servers, context, turn_policy)``."""

    persona: Persona
    tools: list[ToolBinding] = Field(default_factory=list)
    mcp_servers: list[McpServerBinding] = Field(default_factory=list)
    context: SessionContext = Field(default_factory=SessionContext)
    turn_policy: TurnPolicy = Field(default_factory=TurnPolicy)


class StartSessionResponse(BaseModel):
    """``-> session_token``, plus what the client needs to use it.

    The token is the whole session. There is no server-side session record to look up,
    which is what lets the WebSocket land on any Cloud Run instance.
    """

    session_id: str
    session_token: str
    expires_at: str
    websocket_path: str
    #: Which provider arm this session will run on, echoed back so a client's logs say
    #: which one produced a transcript. Informational: the client's behaviour does not
    #: change, which is the point of invariant 1.
    arm: str
    #: ``False`` when the arm is configured but its credential is not provisioned. The
    #: session still opens and still runs turn detection — that is what the harness needs —
    #: but no model will answer. Stated rather than discovered on the first silence.
    conversational: bool


class SessionEvent(BaseModel):
    """Base for everything the service emits."""

    type: str
    #: Offset into the session, milliseconds. Ours; see :mod:`motet_voice.clock`.
    at_ms: int


class TranscriptEvent(SessionEvent):
    type: Literal["transcript"] = "transcript"
    speaker: Literal["user", "assistant"]
    text: str
    final: bool = True


class AudioChunkEvent(SessionEvent):
    type: Literal["audio_chunk"] = "audio_chunk"
    #: Base64 PCM. Binary WebSocket frames are the efficient path and are what the iOS
    #: client will use; the JSON form exists so the whole protocol is inspectable from a
    #: terminal, which matters more than bandwidth for a service being debugged outdoors.
    pcm_base64: str
    sample_rate: int
    duration_ms: int


class ToolCallEvent(SessionEvent):
    type: Literal["tool_call"] = "tool_call"
    call_id: str
    name: str
    arguments: dict[str, Any]


class ToolResultEvent(SessionEvent):
    type: Literal["tool_result"] = "tool_result"
    call_id: str
    name: str
    ok: bool
    result: dict[str, Any] = Field(default_factory=dict)
    error: str | None = None


class InterruptedAtEvent(SessionEvent):
    """``interrupted_at(offset)`` — the listener took the floor.

    ``offset_ms`` is :attr:`~motet_voice.clock.PlaybackClock.spoken_through_ms` frozen at
    the instant of the decision. It is what a highlight anchors to and where narration
    resumes, and it is emphatically not a number any provider gave us.
    """

    type: Literal["interrupted_at"] = "interrupted_at"
    offset_ms: int
    #: The evidence, inline. The same record the harness writes to its decision log — a
    #: barge-in a client cannot explain is a barge-in nobody can debug.
    decision: dict[str, Any] = Field(default_factory=dict)


class SessionStateEvent(SessionEvent):
    type: Literal["session_state"] = "session_state"
    state: Literal["ready", "listening", "speaking", "closed"]
    detail: str | None = None


class ErrorEvent(SessionEvent):
    type: Literal["error"] = "error"
    code: str
    message: str
