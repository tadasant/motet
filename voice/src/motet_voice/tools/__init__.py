"""Platform tools: the only way the voice service touches anything outside itself."""

from .fakes import FailingToolTransport, RecordingToolTransport
from .mcp import McpToolTransport, build_motet_transport, motet_mcp_url
from .platform import McpTool, build_platform_tools, locate_claim
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
    "FailingToolTransport",
    "McpTool",
    "McpToolTransport",
    "RecordingToolTransport",
    "Tool",
    "ToolAvailability",
    "ToolRegistry",
    "ToolResponse",
    "ToolResult",
    "ToolState",
    "ToolTransport",
    "build_motet_transport",
    "build_platform_tools",
    "locate_claim",
    "motet_mcp_url",
]
