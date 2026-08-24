"""The two provider arms, behind one interface, each with a fake."""

from .composed import (
    ComposedArm,
    DormantSpeechRecognizer,
    FakeConversationModel,
    FakeSpeechRecognizer,
    LlmConversationModel,
    build_composed_arm,
)
from .interfaces import (
    ArmCapabilities,
    ArmDormant,
    AssistantTurn,
    ConversationModel,
    PendingToolCall,
    RealtimeArm,
    SpeechRecognizer,
    TurnRequest,
)
from .openai_realtime import (
    DEFAULT_SERVER_VAD,
    OpenAiRealtimeArm,
    RealtimeProtocolError,
    RealtimeTransport,
    ScriptedRealtimeTransport,
    ServerVadEmulator,
    ServerVadRelay,
    WebsocketRealtimeTransport,
    build_openai_arm,
)
from .registry import build_all_arms, build_arm

__all__ = [
    "DEFAULT_SERVER_VAD",
    "ArmCapabilities",
    "ArmDormant",
    "AssistantTurn",
    "ComposedArm",
    "ConversationModel",
    "DormantSpeechRecognizer",
    "FakeConversationModel",
    "FakeSpeechRecognizer",
    "LlmConversationModel",
    "OpenAiRealtimeArm",
    "PendingToolCall",
    "RealtimeArm",
    "RealtimeProtocolError",
    "RealtimeTransport",
    "ScriptedRealtimeTransport",
    "ServerVadEmulator",
    "ServerVadRelay",
    "SpeechRecognizer",
    "TurnRequest",
    "WebsocketRealtimeTransport",
    "build_all_arms",
    "build_arm",
    "build_composed_arm",
    "build_openai_arm",
]
