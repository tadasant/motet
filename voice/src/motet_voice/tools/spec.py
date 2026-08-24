"""What a tool is, what it answers, and how it reaches the outside world.

**Tools are how the voice service does anything at all** — that is invariant 3 restated as
a design. The service holds no database credential and no schema knowledge, so "what is
this story about" and "remember that bit" are not lookups, they are calls back to whoever
owns the data. If a change to this service seems to need a database handle, the answer is a
tool.

The transport is a seam with a fake, like every other vendor-facing thing in this repo:
``HttpToolTransport`` talks to Motet's API, ``RecordingToolTransport`` answers from a dict.
Tests use the second one, so a tool's argument handling, its defaults merging, and its
error mapping are all covered without a server.
"""

from __future__ import annotations

import logging
from collections.abc import Mapping
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any, Protocol, runtime_checkable

logger = logging.getLogger("motet.voice.tools")


class ToolState(StrEnum):
    """Whether a tool can actually be called right now."""

    AVAILABLE = "available"
    #: Implemented, but a credential or an upstream route it needs does not exist yet. The
    #: distinction matters to the persona: a dormant tool is described to the model as
    #: unavailable, so it says "I can't look that up right now" instead of hallucinating a
    #: result or apologizing for a failure it will retry.
    DORMANT = "dormant"


@dataclass(frozen=True)
class ToolAvailability:
    state: ToolState
    reason: str = ""

    @property
    def available(self) -> bool:
        return self.state is ToolState.AVAILABLE


AVAILABLE = ToolAvailability(ToolState.AVAILABLE)


@dataclass(frozen=True)
class ToolResult:
    """What a tool call produced. Errors are values, never exceptions.

    A raised exception in the middle of a realtime turn is a dropped conversation. A failed
    ``ToolResult`` is something the model can say out loud, which for a voice product is
    the difference between "I couldn't save that, sorry" and silence.
    """

    ok: bool
    result: dict[str, Any] = field(default_factory=dict)
    error: str | None = None

    @classmethod
    def failure(cls, error: str) -> ToolResult:
        return cls(ok=False, error=error)


@dataclass(frozen=True)
class ToolResponse:
    """A transport's answer: an HTTP-ish status and a decoded body."""

    status: int
    payload: dict[str, Any] = field(default_factory=dict)

    @property
    def ok(self) -> bool:
        return 200 <= self.status < 300


@runtime_checkable
class ToolTransport(Protocol):
    """How a tool reaches Motet's API. Never a database, never a vendor SDK."""

    async def request(
        self, method: str, path: str, *, json: Mapping[str, Any] | None = None
    ) -> ToolResponse: ...

    async def aclose(self) -> None: ...


@runtime_checkable
class Tool(Protocol):
    """One callable capability offered to a voice session."""

    @property
    def name(self) -> str: ...

    @property
    def description(self) -> str: ...

    @property
    def parameters(self) -> dict[str, Any]: ...

    def availability(self) -> ToolAvailability: ...

    async def invoke(self, arguments: Mapping[str, Any]) -> ToolResult: ...


class ToolRegistry:
    """The tools a session may call, resolved once at StartSession.

    Resolving at StartSession rather than at call time is deliberate: a persona is told what
    it can do in its system prompt, and a tool that appears or vanishes mid-session makes
    that prompt a lie.
    """

    def __init__(self, tools: Mapping[str, Tool]) -> None:
        self._tools = dict(tools)

    def __contains__(self, name: object) -> bool:
        return name in self._tools

    def names(self) -> tuple[str, ...]:
        return tuple(sorted(self._tools))

    def get(self, name: str) -> Tool | None:
        return self._tools.get(name)

    def describe(self) -> list[dict[str, Any]]:
        """The tool list in the shape a model is given, availability included."""
        described = []
        for name in self.names():
            tool = self._tools[name]
            availability = tool.availability()
            described.append(
                {
                    "name": name,
                    "description": tool.description,
                    "parameters": tool.parameters,
                    "state": availability.state.value,
                    "reason": availability.reason,
                }
            )
        return described

    async def invoke(self, name: str, arguments: Mapping[str, Any]) -> ToolResult:
        tool = self._tools.get(name)
        if tool is None:
            return ToolResult.failure(
                f"'{name}' is not a tool this session was granted; "
                f"granted: {', '.join(self.names()) or 'none'}"
            )
        availability = tool.availability()
        if not availability.available:
            return ToolResult.failure(availability.reason or f"'{name}' is unavailable")
        try:
            return await tool.invoke(arguments)
        except Exception as exc:  # noqa: BLE001 — see ToolResult: errors are values here
            logger.exception("tool %s raised", name)
            return ToolResult.failure(f"{name} failed: {exc}")
