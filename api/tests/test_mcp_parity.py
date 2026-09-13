"""The route table and the MCP registry cannot drift (motet#111).

`bin/ci` already regenerates `openapi.yaml` and `schema.gen.ts` from the route table and
fails on a diff. The MCP surface is a fourth consumer of the same table, so it gets the
same treatment: every operation the app declares is a tool, a planned tool, or a written
exclusion, and every registry entry names an operation that exists.
"""

from __future__ import annotations

from collections import Counter

from fastapi.routing import APIRoute
from motet_api import app
from motet_api.mcp.registry import (
    ALL_TOOLS,
    DEFAULT_GROUPS,
    EXCLUDED,
    OPT_IN_GROUPS,
    PLANNED,
    Operation,
)

ADD_A_ROUTE = (
    "Classify it in api/src/motet_api/mcp/registry.py: a tool's `covers`, `PLANNED`, or "
    "`EXCLUDED` with the reason it must never be a tool."
)


def declared_operations() -> set[Operation]:
    """Every ``(METHOD, path)`` the app serves as an API route.

    ``HEAD`` is dropped because Starlette adds it to every ``GET``. FastAPI's own docs
    routes are plain ``Route`` objects, not ``APIRoute``, and are not operations.
    """
    return {
        (method, route.path)
        for route in app.routes
        if isinstance(route, APIRoute)
        for method in route.methods or ()
        if method != "HEAD"
    }


def classified() -> list[Operation]:
    """Every classification, with repeats kept so a double entry is visible."""
    ops = [op for tool in (*ALL_TOOLS, *PLANNED) for op in tool.covers]
    return ops + list(EXCLUDED)


def test_every_operation_is_a_tool_or_a_written_exclusion() -> None:
    missing = sorted(declared_operations() - set(classified()))
    assert not missing, f"Unclassified API operations: {missing}. {ADD_A_ROUTE}"


def test_no_registry_entry_names_an_operation_that_does_not_exist() -> None:
    dangling = sorted(set(classified()) - declared_operations())
    assert not dangling, (
        f"The MCP registry names operations the app does not declare: {dangling}. A renamed "
        "or removed route takes its registry entry with it."
    )


def test_each_operation_is_classified_exactly_once() -> None:
    twice = sorted(op for op, n in Counter(classified()).items() if n > 1)
    assert not twice, f"Classified more than once (a tool and an exclusion, or two tools): {twice}"


def test_every_exclusion_says_why() -> None:
    unexplained = sorted(op for op, reason in EXCLUDED.items() if len(reason.strip()) < 40)
    assert not unexplained, f"An exclusion needs a written reason: {unexplained}"


def test_tool_names_are_unique_and_follow_the_naming_rule() -> None:
    tools = (*ALL_TOOLS, *PLANNED)
    names = [tool.name for tool in tools]
    assert len(names) == len(set(names)), sorted(n for n, c in Counter(names).items() if c > 1)
    for name in names:
        assert name.isidentifier() and name == name.lower(), name
        assert len(name) <= 25, f"{name} is over 25 characters"


def test_every_tool_is_in_a_known_group_and_covers_something() -> None:
    groups = set(DEFAULT_GROUPS) | set(OPT_IN_GROUPS)
    assert not set(DEFAULT_GROUPS) & set(OPT_IN_GROUPS)
    for tool in (*ALL_TOOLS, *PLANNED):
        assert tool.group in groups, f"{tool.name}: unknown group {tool.group!r}"
        assert tool.covers, f"{tool.name} covers no operation"


def test_a_tool_that_covers_a_mutation_is_a_write_tool() -> None:
    """The ``_readonly`` variant drops write tools, so a wrong flag leaks a write into it."""
    for tool in (*ALL_TOOLS, *PLANNED):
        mutates = any(method != "GET" for method, _ in tool.covers)
        assert tool.write == mutates, f"{tool.name}: write={tool.write}, covers {tool.covers}"


def test_admin_routes_are_only_reachable_through_the_opt_in_group() -> None:
    for tool in (*ALL_TOOLS, *PLANNED):
        if any(path.startswith("/v1/admin") for _, path in tool.covers):
            assert tool.group in OPT_IN_GROUPS, f"{tool.name} exposes an admin route by default"
