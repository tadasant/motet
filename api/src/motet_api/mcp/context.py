"""What a tool call knows about the request it arrived on, and the one way it reaches a route.

**A tool is an argument validator, a caller and a formatter, and what it calls is the route's
own handler.** Not a copy of the handler's logic, and not an HTTP request back into this
process: the function FastAPI calls for ``POST /v1/sources/paste`` is the function
``paste_text`` calls, with the same connection lifecycle ``deps.connection`` gives a request.
So a tool cannot drift from its route, because there is nothing in it to drift — and the
post-commit drain nudge (motet#71) fires for a tool call exactly as it does for a request,
because it is the same generator doing the committing.

**The caller is established once, before the MCP transport sees the request**, by
:class:`motet_api.mcp.server.McpEndpoint` running ``deps.require_caller``. It rides a
``ContextVar`` into the tool, which the SDK carries into both async tools and the worker
thread a sync tool runs on.
"""

from __future__ import annotations

import contextlib
import contextvars
import logging
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

import psycopg
from fastapi import HTTPException
from mcp.server.mcpserver.exceptions import ToolError
from opentelemetry import metrics
from pydantic import BaseModel, Field, ValidationError
from starlette.requests import Request

from ..config import Settings
from ..deps import Caller, connection, drain_trigger, slack_alerter
from ..drain import DrainNudge
from ..slack import WaitlistAlert

__all__ = [
    "ActionResult",
    "RouteCall",
    "current_caller",
    "current_request",
    "request_scope",
    "routes",
    "run",
    "selected_tools",
]

logger = logging.getLogger("motet.api.mcp")

_caller: contextvars.ContextVar[Caller | None] = contextvars.ContextVar("mcp_caller", default=None)
_tools: contextvars.ContextVar[frozenset[str] | None] = contextvars.ContextVar(
    "mcp_tools", default=None
)
_request: contextvars.ContextVar[Request | None] = contextvars.ContextVar(
    "mcp_request", default=None
)

_meter = metrics.get_meter("motet.api")

#: ``outcome`` is ``ok``, ``refused`` (the route said no: a 4xx, or arguments its request
#: model rejected) or ``error`` (anything else). Counted per tool, because "no MCP calls"
#: and "nobody has connected" must not read the same (invariant 11's trap), and because a
#: tool refused on every call is a description that is lying to the model reading it.
tool_calls = _meter.create_counter(
    "motet.mcp.tool_calls",
    description="MCP tool calls, by tool and outcome.",
)


class ActionResult(BaseModel):
    """What a tool reports for a route that answers ``204 No Content``."""

    done: bool = Field(description="True: the route accepted the request.")
    detail: str = Field(description="What happened, in a sentence.")


@contextlib.contextmanager
def request_scope(caller: Caller, tools: frozenset[str], request: Request) -> Iterator[None]:
    """Bind one ``/mcp`` request's caller, tool selection and request for its tool calls."""
    tokens = (_caller.set(caller), _tools.set(tools), _request.set(request))
    try:
        yield
    finally:
        _request.reset(tokens[2])
        _tools.reset(tokens[1])
        _caller.reset(tokens[0])


def selected_tools() -> frozenset[str]:
    """The tools this connection may see. Empty outside a request: nothing leaks by default."""
    return _tools.get() or frozenset()


def current_caller() -> Caller:
    caller = _caller.get()
    if caller is None:
        # Unreachable through /mcp, which refuses before the transport runs. Raised rather
        # than defaulted so that a new entry point that forgot to authenticate fails closed.
        raise ToolError("This tool call was not authenticated.")
    return caller


def current_request() -> Request:
    request = _request.get()
    if request is None:
        raise ToolError("This tool needs the HTTP request it arrived on, and there is none.")
    return request


class _Routes:
    """``motet_api.main``, resolved when a tool runs rather than when tools are defined.

    ``main`` imports this package to mount it, so a module-level import here would be a
    cycle that works or not depending on which module something imports first. Type
    checkers see the module itself (below), so every handler call a tool makes is checked
    against the handler's real signature.
    """

    def __getattr__(self, name: str) -> Any:
        from .. import main

        return getattr(main, name)


if TYPE_CHECKING:
    from .. import main as routes
else:
    routes = _Routes()


@dataclass(frozen=True)
class RouteCall:
    """Everything a route handler's dependencies would have handed it for this request."""

    conn: psycopg.Connection[Any]
    nudge: DrainNudge
    config: Settings
    caller: Caller

    @property
    def user_id(self) -> str:
        return self.caller.user_id


def run[T](tool: str, call: Callable[[RouteCall], T]) -> T:
    """Call a route handler as a request would, and translate its answer for a tool result.

    The connection is ``deps.connection`` itself: committed when the handler returns, rolled
    back when it raises, and the drain nudge fired only after a commit. An ``HTTPException``
    becomes a tool error carrying the route's own status and sentence, so a model reads
    exactly what the SPA would have shown.
    """
    caller = current_caller()
    config = Settings.from_env()
    nudge = DrainNudge(drain_trigger())
    # No MCP tool reaches the waitlist route — it is public and unauthenticated, and
    # `EXCLUDED` in the registry says so — so this alert is never armed and never fires.
    # It is passed because `connection` takes it, not because anything here uses it.
    alert = WaitlistAlert(slack_alerter())
    try:
        with contextlib.contextmanager(connection)(config, nudge, alert) as conn:
            result = call(RouteCall(conn=conn, nudge=nudge, config=config, caller=caller))
    except HTTPException as exc:
        outcome = "refused" if exc.status_code < 500 else "error"
        tool_calls.add(1, {"tool": tool, "outcome": outcome})
        raise ToolError(f"{exc.status_code}: {exc.detail}") from None
    except ValidationError as exc:
        tool_calls.add(1, {"tool": tool, "outcome": "refused"})
        problems = "; ".join(
            f"{'.'.join(str(part) for part in err['loc']) or 'value'}: {err['msg']}"
            for err in exc.errors()
        )
        raise ToolError(f"422: {problems}") from None
    except ToolError:
        tool_calls.add(1, {"tool": tool, "outcome": "refused"})
        raise
    except Exception:
        tool_calls.add(1, {"tool": tool, "outcome": "error"})
        logger.exception("mcp tool %s failed", tool)
        raise
    tool_calls.add(1, {"tool": tool, "outcome": "ok"})
    return result
