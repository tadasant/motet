"""Platform tools: the only way the voice service touches anything outside itself."""

from .fakes import FailingToolTransport, RecordingToolTransport
from .platform import ApiTool, HttpToolTransport, build_platform_tools
from .spec import (
    AVAILABLE,
    Tool,
    ToolAvailability,
    ToolRegistry,
    ToolResponse,
    ToolResult,
    ToolState,
    ToolTransport,
)

__all__ = [
    "AVAILABLE",
    "ApiTool",
    "FailingToolTransport",
    "HttpToolTransport",
    "RecordingToolTransport",
    "Tool",
    "ToolAvailability",
    "ToolRegistry",
    "ToolResponse",
    "ToolResult",
    "ToolState",
    "ToolTransport",
    "build_platform_tools",
]
