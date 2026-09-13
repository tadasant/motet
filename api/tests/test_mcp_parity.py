"""The route table and the MCP surface cannot drift (motet#111).

`bin/ci` already regenerates `openapi.yaml` and `schema.gen.ts` from the route table and
fails on a diff. The MCP surface is a fourth consumer of the same table, so it gets the same
treatment: every operation the app declares is a tool or a written exclusion, every registry
entry names an operation that exists, and every registered tool has exactly one function.

**A route added without a registry entry or an exclusion is a red run.** That is the rule
AGENTS.md records, and `test_every_operation_is_a_tool_or_a_written_exclusion` is it.
"""

from __future__ import annotations

import asyncio
from collections import Counter
from typing import Any

import pytest
from fastapi.routing import APIRoute
from motet_api import app
from motet_api.deps import Caller
from motet_api.main import MCP
from motet_api.mcp import registry
from motet_api.mcp.context import request_scope
from motet_api.mcp.registry import (
    ALL_TOOLS,
    DEFAULT_GROUPS,
    EXCLUDED,
    OPT_IN_GROUPS,
    Operation,
    ToolGroupsError,
    select_tools,
)
from motet_api.mcp.tools import IMPLEMENTATIONS
from starlette.requests import Request

ADD_A_ROUTE = (
    "Classify it in api/src/motet_api/mcp/registry.py: a tool's `covers` (with the tool in "
    "motet_api/mcp/tools/), or `EXCLUDED` with the reason it must never be a tool."
)


def declared_operations() -> set[Operation]:
    """Every ``(METHOD, path)`` the app serves as an API route.

    ``HEAD`` is dropped because Starlette adds it to every ``GET``. FastAPI's docs routes,
    ``/mcp`` itself and the OAuth endpoints are plain Starlette ``Route`` objects rather than
    ``APIRoute``: they are not operations of the API, and none of them is in ``openapi.yaml``.
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
    return [op for tool in ALL_TOOLS for op in tool.covers] + list(EXCLUDED)


# --- the route table --------------------------------------------------------------------


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


def test_the_mcp_mount_adds_nothing_to_the_openapi_document() -> None:
    """``/mcp`` is derived from the route table, not a part of the contract generated from it."""
    paths = app.openapi()["paths"]
    assert "/mcp" not in paths
    assert not [path for path in paths if path.startswith("/.well-known")]


# --- the registry and the tools -----------------------------------------------------------


def test_every_registered_tool_has_exactly_one_function() -> None:
    names = [tool.name for tool in ALL_TOOLS]
    assert len(names) == len(set(names)), sorted(n for n, c in Counter(names).items() if c > 1)
    assert set(names) == set(IMPLEMENTATIONS), (
        f"registered without a function: {sorted(set(names) - set(IMPLEMENTATIONS))}; "
        f"a function without a registration: {sorted(set(IMPLEMENTATIONS) - set(names))}"
    )


def test_tool_names_follow_the_naming_rule() -> None:
    for tool in ALL_TOOLS:
        assert tool.name.isidentifier() and tool.name == tool.name.lower(), tool.name
        assert len(tool.name) <= 25, f"{tool.name} is over 25 characters"


def test_every_tool_is_in_a_known_group_and_covers_something() -> None:
    groups = set(DEFAULT_GROUPS) | set(OPT_IN_GROUPS)
    assert not set(DEFAULT_GROUPS) & set(OPT_IN_GROUPS)
    for tool in ALL_TOOLS:
        assert tool.group in groups, f"{tool.name}: unknown group {tool.group!r}"
        assert tool.covers, f"{tool.name} covers no operation"


def test_a_tool_that_covers_a_mutation_is_a_write_tool() -> None:
    """The ``_readonly`` variants drop write tools, so a wrong flag leaks a write into one."""
    for tool in ALL_TOOLS:
        mutates = any(method != "GET" for method, _ in tool.covers)
        assert tool.write == mutates, f"{tool.name}: write={tool.write}, covers {tool.covers}"
        if not tool.write:
            assert not tool.destructive and not tool.idempotent, tool.name


def test_admin_routes_are_only_reachable_through_the_opt_in_group() -> None:
    for tool in ALL_TOOLS:
        if any(path.startswith("/v1/admin") for _, path in tool.covers):
            assert tool.group in OPT_IN_GROUPS, f"{tool.name} exposes an admin route by default"


# --- group selection -------------------------------------------------------------------------


def by_group(group: str) -> set[str]:
    return {tool.name for tool in ALL_TOOLS if tool.group == group}


def test_the_default_surface_is_every_default_group_and_no_admin() -> None:
    default = select_tools(None)
    assert default == select_tools("") == select_tools(" , ")
    assert default == {tool.name for tool in ALL_TOOLS if tool.group in DEFAULT_GROUPS}
    assert not default & by_group("admin")


def test_admin_is_granted_only_by_naming_it() -> None:
    assert select_tools("admin") == by_group("admin")
    assert by_group("admin") <= select_tools("backlog, admin")


def test_a_readonly_variant_carries_no_write_tool() -> None:
    for group in (*DEFAULT_GROUPS, *OPT_IN_GROUPS):
        chosen = select_tools(f"{group}_readonly")
        assert chosen == {t.name for t in ALL_TOOLS if t.group == group and not t.write}
        assert not [t.name for t in ALL_TOOLS if t.name in chosen and t.write]
    everything_readonly = ",".join(f"{g}_readonly" for g in DEFAULT_GROUPS)
    assert not {t.name for t in ALL_TOOLS if t.write} & select_tools(everything_readonly)


def test_naming_a_group_and_its_readonly_variant_gives_the_whole_group() -> None:
    assert select_tools("episodes,episodes_readonly") == by_group("episodes")


@pytest.mark.parametrize("raw", ["backlg", "admin_rw", "readonly", "ops,nope"])
def test_an_unknown_group_is_refused_rather_than_ignored(raw: str) -> None:
    with pytest.raises(ToolGroupsError, match="Unknown tool group"):
        select_tools(raw)


# --- what tools/list advertises ---------------------------------------------------------------


def listed(groups: str | None) -> list[Any]:
    """``tools/list`` for a connection that chose ``groups``, straight off the real server."""
    scope: dict[str, Any] = {"type": "http", "method": "POST", "path": "/mcp", "headers": []}

    async def go() -> list[Any]:
        caller = Caller(user_id="motet-owner", how="token")
        with request_scope(caller, registry.select_tools(groups), Request(scope)):
            return list(await MCP.server.list_tools())

    return asyncio.run(go())


def test_tools_list_advertises_a_name_description_and_object_schema_for_every_tool() -> None:
    tools = listed("backlog,ingestion,sources,episodes,feed,highlights,ops,admin")
    assert {tool.name for tool in tools} == {t.name for t in ALL_TOOLS}
    for tool in tools:
        assert tool.description and len(tool.description) > 40, tool.name
        assert tool.input_schema["type"] == "object", tool.name
        definition = next(t for t in ALL_TOOLS if t.name == tool.name)
        assert tool.annotations is not None
        assert tool.annotations.read_only_hint is (not definition.write), tool.name
        if definition.write:
            assert tool.annotations.destructive_hint is definition.destructive, tool.name


def test_tools_list_hides_what_the_connection_did_not_choose() -> None:
    assert {tool.name for tool in listed(None)} == select_tools(None)
    assert not {tool.name for tool in listed("episodes_readonly")} & {
        t.name for t in ALL_TOOLS if t.write
    }
