"""PROTOTYPE — the admin screen's LLM half: model configuration and spend.

Two routes and one fold, kept out of ``main.py`` so the prototype is one file to delete:

* ``GET /v1/admin/llm-config`` — per stage, the resolved model and effort *and* the chain
  they were resolved along (settings > stage env > global env > default), plus the
  catalogue so the dropdowns offer only slugs the worker would accept.
* ``PUT /v1/admin/llm-config/{stage}`` — set or clear the settings-table rows for one
  stage. Validated by running the very resolution the worker runs, so an unknown slug or an
  effort the slug does not take is a 400 here rather than a failed job later.
* :func:`fold_costs` — the ``llm_usage`` ledger, priced per model from the catalogue and
  summed per stage, per user and per queue, for ``GET /v1/admin/overview``.

The stage list is derived from :class:`~motet_inference.llm.LlmStage` everywhere; nothing
here names a stage, so a member added or removed upstream appears or disappears on its own.
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from typing import Annotated, Any, Final

import psycopg
from fastapi import APIRouter, Depends, HTTPException, Path, status
from motet_db import repo
from motet_inference.llm import (
    DEFAULT_EFFORTS,
    DEFAULT_MODEL,
    EFFORT_ENV,
    KNOWN_MODELS,
    MODEL_ENV,
    OFF_VALUE,
    ConfigSource,
    LlmConfigError,
    LlmStage,
    Usage,
    load_config,
    usage_cost_usd,
)
from motet_workers.queues import Queue

from .deps import connection, require_api_token
from .schemas import (
    AdminCostsResponse,
    LlmConfigResponse,
    LlmModelOption,
    LlmSpend,
    LlmStageConfigResponse,
    LlmStageConfigUpdate,
)

router = APIRouter(tags=["admin"])

# Same dependency, same scope as `main.py`'s aliases — the scope is part of FastAPI's
# dependency cache key, so a mismatch would hand one request two connections.
Conn = Annotated[psycopg.Connection[Any], Depends(connection, scope="function")]
User = Annotated[str, Depends(require_api_token)]

#: Highest precedence first, as the response spells it.
PRECEDENCE: Final = [
    ConfigSource.SETTINGS.value,
    ConfigSource.STAGE_ENV.value,
    ConfigSource.GLOBAL_ENV.value,
    ConfigSource.DEFAULT.value,
]

#: The worker reads the settings table once per job (``motet_workers.llm_context``), so a
#: change applies to the next job it claims. Reported rather than assumed by the UI.
APPLIES: Final = "next_job"

#: Which LLM stages a pipeline queue's spend is made of. ``script`` maps to the script stage
#: alone by decision; a stage not named here (voice) is on no queue.
QUEUE_STAGES: Final[Mapping[str, tuple[LlmStage, ...]]] = {
    Queue.INTEGRATE.value: (LlmStage.TRIAGE, LlmStage.DEDUP, LlmStage.DEDUP_CONFIRM),
    Queue.SCRIPT.value: (LlmStage.SCRIPT,),
}


def _stage(value: str) -> LlmStage:
    try:
        return LlmStage(value)
    except ValueError:
        raise HTTPException(
            status.HTTP_404_NOT_FOUND,
            f"unknown LLM stage {value!r}; one of: {', '.join(s.value for s in LlmStage)}",
        ) from None


def _effort_word(effort: str | None) -> str:
    return effort if effort is not None else OFF_VALUE


def _env(environ: Mapping[str, str], var: str) -> str | None:
    return environ.get(var, "").strip() or None


def describe_config(settings: Mapping[str, str], environ: Mapping[str, str]) -> LlmConfigResponse:
    """The resolved config plus every rung of the chain, for the screen to show."""
    config = load_config(environ, overrides=settings)
    stages = []
    for stage in LlmStage:
        resolved = config.for_stage(stage)
        stages.append(
            LlmStageConfigResponse(
                stage=stage.value,
                model=resolved.model,
                model_source=resolved.model_source.value,
                effort=_effort_word(resolved.effort),
                effort_source=resolved.effort_source.value,
                setting_model=settings.get(stage.model_setting) or None,
                stage_env_model=_env(environ, stage.model_env),
                global_env_model=_env(environ, MODEL_ENV),
                default_model=DEFAULT_MODEL,
                setting_effort=settings.get(stage.effort_setting) or None,
                stage_env_effort=_env(environ, stage.effort_env),
                global_env_effort=_env(environ, EFFORT_ENV),
                default_effort=_effort_word(DEFAULT_EFFORTS[stage]),
            )
        )
    return LlmConfigResponse(
        stages=stages,
        models=[
            LlmModelOption(
                slug=spec.slug,
                efforts=list(spec.efforts),
                adaptive_thinking=spec.adaptive_thinking,
                reasoning_on_by_default=spec.reasoning_on_by_default,
                input_usd_per_mtok=spec.input_usd_per_mtok,
                output_usd_per_mtok=spec.output_usd_per_mtok,
                cache_read_usd_per_mtok=spec.cache_read_usd_per_mtok,
                cache_write_usd_per_mtok=spec.cache_write_usd_per_mtok,
            )
            for spec in KNOWN_MODELS.values()
        ],
        precedence=PRECEDENCE,
        applies=APPLIES,
    )


@router.get("/v1/admin/llm-config", response_model=LlmConfigResponse)
def get_llm_config(conn: Conn, _caller: User) -> LlmConfigResponse:
    """Every LLM stage's resolved model and effort, and where each came from."""
    return describe_config(repo.load_settings(conn, prefix="llm."), os.environ)


@router.put("/v1/admin/llm-config/{stage}", response_model=LlmConfigResponse)
def put_llm_config(
    conn: Conn,
    _caller: User,
    body: LlmStageConfigUpdate,
    stage: Annotated[str, Path()],
) -> LlmConfigResponse:
    """Set or clear one stage's settings-table overrides, then return the whole config.

    The candidate rows are resolved *before* they are written, through the same
    ``load_config`` the worker will run: an unknown slug, an effort the slug does not
    accept, or an effort on a model with none is a 400 with the resolver's own message.
    """
    target = _stage(stage)
    current = repo.load_settings(conn, prefix="llm.")
    candidate = dict(current)
    changes: dict[str, str | None] = {}
    if "model" in body.model_fields_set:
        changes[target.model_setting] = body.model.strip() if body.model else None
    if "effort" in body.model_fields_set:
        changes[target.effort_setting] = body.effort.strip().lower() if body.effort else None
    for key, value in changes.items():
        if value is None:
            candidate.pop(key, None)
        else:
            candidate[key] = value

    # A settings row must never be the escape hatch: the environment may allow an unlisted
    # slug for the hour between a vendor shipping a model and the catalogue catching up,
    # but a dropdown offers catalogue slugs and a write outside it is a typo.
    new_model = candidate.get(target.model_setting)
    if new_model is not None and new_model not in KNOWN_MODELS:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            f"{new_model!r} is not in the model catalogue; "
            f"known: {', '.join(sorted(KNOWN_MODELS))}",
        )
    try:
        load_config(os.environ, overrides=candidate)
    except LlmConfigError as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc)) from None

    for key, value in changes.items():
        repo.put_setting(conn, key, value)
    return describe_config(candidate, os.environ)


def _zero() -> dict[str, Any]:
    return {
        "completions": 0,
        "input_tokens": 0,
        "output_tokens": 0,
        "reasoning_tokens": 0,
        "cache_read_tokens": 0,
        "cache_write_tokens": 0,
        "usd": 0.0,
    }


def _add(bucket: dict[str, Any], cell: repo.LlmUsageTotals, usd: float) -> None:
    bucket["completions"] += cell.completions
    bucket["input_tokens"] += cell.input_tokens
    bucket["output_tokens"] += cell.output_tokens
    bucket["reasoning_tokens"] += cell.reasoning_tokens
    bucket["cache_read_tokens"] += cell.cache_read_tokens
    bucket["cache_write_tokens"] += cell.cache_write_tokens
    bucket["usd"] += usd


def fold_costs(ledger: repo.LlmUsageLedger) -> AdminCostsResponse:
    """Price every ``(user, stage, model)`` cell and sum it three ways.

    Priced per cell because the price is per model and one stage may have run on several;
    the sum of per-cell costs is exact where a per-stage average would not be. Every stage
    in the enum is present at zero, so "unused" and "not a stage" read differently.
    """
    stages: dict[str, dict[str, Any]] = {stage.value: _zero() for stage in LlmStage}
    users: dict[str, dict[str, Any]] = {}
    queues: dict[str, dict[str, Any]] = {queue: _zero() for queue in QUEUE_STAGES}
    stage_of_queue = {
        stage.value: queue for queue, members in QUEUE_STAGES.items() for stage in members
    }
    for cell in ledger.cells:
        usd = usage_cost_usd(
            cell.model,
            Usage(
                input_tokens=cell.input_tokens,
                output_tokens=cell.output_tokens,
                reasoning_tokens=cell.reasoning_tokens,
                cache_read_tokens=cell.cache_read_tokens,
                cache_write_tokens=cell.cache_write_tokens,
            ),
        )
        _add(stages.setdefault(cell.stage, _zero()), cell, usd)
        if cell.user_id is not None:
            _add(users.setdefault(cell.user_id, _zero()), cell, usd)
        queue = stage_of_queue.get(cell.stage)
        if queue is not None:
            _add(queues[queue], cell, usd)
    return AdminCostsResponse(
        since=ledger.since,
        stages={key: LlmSpend(**value) for key, value in stages.items()},
        users={key: LlmSpend(**value) for key, value in users.items()},
        queues={key: LlmSpend(**value) for key, value in queues.items()},
    )
