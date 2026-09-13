"""The tools, one module per group. Each tool calls its route's own handler.

``IMPLEMENTATIONS`` is what :func:`motet_api.mcp.server.build_server` registers, and the
parity test holds its names equal to ``registry.ALL_TOOLS``: a registry entry with no
function, or a function with no registry entry, is a red run.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from . import admin, backlog, episodes, feed, highlights, ingestion, ops, sources

IMPLEMENTATIONS: dict[str, Callable[..., Any]] = {
    fn.__name__: fn
    for module in (backlog, ingestion, sources, episodes, feed, highlights, ops, admin)
    for fn in module.TOOLS
}
