"""Deterministic tool transports — invariant 7, applied to the tool seam.

A voice session's tools are the only thing in the service that reaches outside the process,
so they are the only thing that needs a fake. These two cover both halves of what a test
wants to assert: what the tool *sent*, and what it does with what comes *back*.

**A fake cannot tell you the wire shape is right**, which is the whole lesson of the
templates these replaced. ``voice/tests/test_mcp_binding.py`` drives the real client against
a real in-process MCP server, and ``api/tests/test_mcp_voice_binding.py`` drives it against
Motet's own; these two are for the branches neither can reach cheaply.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

from .spec import ToolResponse


@dataclass
class RecordingToolTransport:
    """Answers from a canned table and records every call.

    Keyed by the tool's name on the server. An unmapped call answers 404, which is roughly
    what Motet answers for an id that is not there — and an *unknown tool* is a different
    thing the real server refuses, which is why the binding tests use the real one.
    """

    responses: dict[str, ToolResponse] = field(default_factory=dict)
    calls: list[tuple[str, dict[str, Any]]] = field(default_factory=list)
    closed: bool = False

    async def call_tool(self, name: str, arguments: Mapping[str, Any]) -> ToolResponse:
        self.calls.append((name, dict(arguments)))
        return self.responses.get(
            name,
            ToolResponse(status=404, payload={"detail": "no such tool in the fake transport"}),
        )

    async def aclose(self) -> None:
        self.closed = True


@dataclass
class FailingToolTransport:
    """Every call fails at the transport layer — the "Motet is unreachable" case."""

    detail: str = "connection refused"

    async def call_tool(self, name: str, arguments: Mapping[str, Any]) -> ToolResponse:
        return ToolResponse(status=599, payload={"detail": self.detail})

    async def aclose(self) -> None:
        return None
