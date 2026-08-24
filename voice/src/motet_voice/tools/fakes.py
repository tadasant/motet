"""Deterministic tool transports — invariant 9, applied to the tool seam.

A voice session's tools are the only thing in the service that reaches outside the process,
so they are the only thing that needs a fake. These two cover both halves of what a test
wants to assert: what the tool *sent*, and what it does with what comes *back*.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

from .spec import ToolResponse


@dataclass
class RecordingToolTransport:
    """Answers from a canned table and records every call.

    Keyed by ``"METHOD /path"``. An unmapped call answers 404, which is exactly what the
    real API does for a route that has not shipped — so a test of the not-yet-merged tools
    exercises the same branch production will.
    """

    responses: dict[str, ToolResponse] = field(default_factory=dict)
    calls: list[tuple[str, str, dict[str, Any]]] = field(default_factory=list)
    closed: bool = False

    async def request(
        self, method: str, path: str, *, json: Mapping[str, Any] | None = None
    ) -> ToolResponse:
        self.calls.append((method, path, dict(json or {})))
        return self.responses.get(
            f"{method} {path}",
            ToolResponse(status=404, payload={"detail": "no such route in the fake transport"}),
        )

    async def aclose(self) -> None:
        self.closed = True


@dataclass
class FailingToolTransport:
    """Every call fails at the transport layer — the "API is unreachable" case."""

    detail: str = "connection refused"

    async def request(
        self, method: str, path: str, *, json: Mapping[str, Any] | None = None
    ) -> ToolResponse:
        return ToolResponse(status=599, payload={"detail": self.detail})

    async def aclose(self) -> None:
        return None
