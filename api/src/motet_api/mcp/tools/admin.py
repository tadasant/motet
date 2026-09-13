"""Operator tools. Opt-in (``?tool_groups=admin``), and each is still its route's guard's call."""

from __future__ import annotations

from typing import Annotated

from mcp.server.mcpserver.exceptions import ToolError
from pydantic import Field

from ...deps import require_admin
from ...schemas import (
    AdminLlmSpendResponse,
    AdminOverviewResponse,
    AdminWaitlistResponse,
    LlmConfigResponse,
    LlmStageConfigUpdate,
    RevokedResponse,
)
from ..context import RouteCall, routes, run

_BEFORE = Field(description="The previous page's cursor (`*_next_before`). Omit for the newest.")
_LIMIT = Field(description="How many rows to list, 1 to 500. Omit for 200.")


def _limit(limit: int | None) -> int:
    """The route's own default and bounds, which FastAPI's ``Query`` enforces for a request."""
    main = routes
    if limit is None:
        return int(main.ADMIN_JOBS_DEFAULT_LIMIT)
    if not 1 <= limit <= main.ADMIN_JOBS_MAX_LIMIT:
        raise ToolError(f"422: limit must be between 1 and {main.ADMIN_JOBS_MAX_LIMIT}.")
    return limit


def logout_everywhere() -> RevokedResponse:
    """Revoke every signed-in session and every MCP client grant, including this connection's.

    The answer to a lost phone or a leaked token. This connection stops working too if it
    is a session or an MCP grant; the shared API token keeps working. Returns how many
    sessions were revoked.
    """
    return run(
        "logout_everywhere", lambda c: routes.logout_everywhere(conn=c.conn, caller=c.caller)
    )


def get_admin_overview(
    user_id: Annotated[
        str | None, Field(description="Only list jobs whose subject belongs to this user.")
    ] = None,
    before: Annotated[int | None, _BEFORE] = None,
    limit: Annotated[int | None, _LIMIT] = None,
) -> AdminOverviewResponse:
    """Get the operator view across every user: per-user counts, queue depths, recent jobs.

    Needs a signed-in person on this deployment's admin list; anyone else (the shared API
    token included) is a 403. The job list is paged newest first with `jobs_next_before`.
    """

    def call(c: RouteCall) -> AdminOverviewResponse:
        return routes.admin_overview(
            conn=c.conn,
            _admin=require_admin(caller=c.caller, config=c.config),
            user_id=user_id,
            before=before,
            limit=_limit(limit),
        )

    return run("get_admin_overview", call)


def list_waitlist(
    before: Annotated[int | None, _BEFORE] = None,
    limit: Annotated[int | None, _LIMIT] = None,
) -> AdminWaitlistResponse:
    """List who asked to join from the landing page's waitlist, newest first.

    Admins only, like `get_admin_overview`. Paged with `next_before`.
    """

    def call(c: RouteCall) -> AdminWaitlistResponse:
        return routes.admin_waitlist(
            conn=c.conn,
            _admin=require_admin(caller=c.caller, config=c.config),
            before=before,
            limit=_limit(limit),
        )

    return run("list_waitlist", call)


def get_llm_config() -> LlmConfigResponse:
    """Get each LLM stage's model and effort, where each came from, and the model catalogue.

    Admins only. `writable` says whether this deployment honours changes from
    `set_llm_config` at all; where it is false the environment is the whole configuration.
    """

    def call(c: RouteCall) -> LlmConfigResponse:
        return routes.get_llm_config(
            conn=c.conn, _admin=require_admin(caller=c.caller, config=c.config)
        )

    return run("get_llm_config", call)


def set_llm_config(
    stage: Annotated[
        str, Field(description="An LLM stage as get_llm_config lists it, e.g. dedup.")
    ],
    model: Annotated[
        str | None,
        Field(description="A catalogue slug to run the stage on. Omit to leave it unchanged."),
    ] = None,
    effort: Annotated[
        str | None,
        Field(description="A reasoning effort the slug accepts, or 'off'. Omit to leave it."),
    ] = None,
    clear: Annotated[
        bool, Field(description="True clears both overrides, back to the environment's value.")
    ] = False,
) -> LlmConfigResponse:
    """Set or clear which model and effort one LLM stage runs on. Applies to the next job.

    Admins only, and refused with a 409 where the deployment does not honour settings. An
    unknown slug, or an effort the slug does not take, is a 400 before anything is written.
    Changing a model changes what inference costs.
    """

    def call(c: RouteCall) -> LlmConfigResponse:
        fields: dict[str, str | None] = (
            {"model": None, "effort": None}
            if clear
            else {k: v for k, v in (("model", model), ("effort", effort)) if v is not None}
        )
        return routes.put_llm_config(
            conn=c.conn,
            admin=require_admin(caller=c.caller, config=c.config),
            body=LlmStageConfigUpdate.model_validate(fields),
            stage=stage,
        )

    return run("set_llm_config", call)


def get_llm_spend() -> AdminLlmSpendResponse:
    """Get what each LLM stage, user and pipeline queue has spent, from the usage ledger.

    Admins only. Tokens and US dollars, priced from the committed model catalogue.
    """

    def call(c: RouteCall) -> AdminLlmSpendResponse:
        return routes.get_llm_spend(
            conn=c.conn, _admin=require_admin(caller=c.caller, config=c.config)
        )

    return run("get_llm_spend", call)


TOOLS = (
    logout_everywhere,
    get_admin_overview,
    list_waitlist,
    get_llm_config,
    set_llm_config,
    get_llm_spend,
)
