"""The admin screen's LLM half: which model each stage is on and why, and what it spent.

motet#92. The routes live in ``main.py`` beside ``/v1/admin/overview`` and take the same
``Admin`` guard (see the note there on why this repo does not use an ``APIRouter``); this
module is what they call, so ``main.py`` holds the HTTP surface and nothing more.

* :func:`describe_config` — per stage, the resolved model and effort *and* the chain they
  were resolved along, plus the catalogue, so the dropdowns offer only slugs the worker
  would accept and an operator can see which rung won. motet#85 was an afternoon of a
  shell export beating a ``.env`` line with nothing on any surface saying so.
* :func:`apply_update` — set or clear one stage's ``settings`` rows. Refused outright where
  ``MOTET_SETTINGS_WRITABLE`` is off, which is production; validated otherwise by the very
  function the worker installs rows through, so an unknown slug or an effort the slug does
  not take is a 400 here rather than an ERROR in a worker later.
* :func:`fold_spend` — the ``llm_usage`` ledger priced per cell and summed per stage, per
  user and per queue, over everything retained and over the last week.

Nothing here names a stage: the list is :class:`~motet_inference.llm.LlmStage`, so a member
added or removed upstream appears or disappears on its own.
"""

from __future__ import annotations

import logging
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, Final

import psycopg
from motet_db import llm_usage, repo
from motet_db import settings as settings_repo
from motet_inference.llm import (
    DEFAULT_EFFORTS,
    DEFAULT_MODEL,
    EFFORT_ENV,
    KNOWN_MODELS,
    MODEL_ENV,
    OFF_VALUE,
    SETTING_PREFIX,
    CacheTtl,
    ConfigSource,
    LlmConfigError,
    LlmStage,
    Usage,
    load_config,
    usage_cost_usd,
    validate_overrides,
)
from motet_workers.queues import Queue

from .schemas import (
    AdminLlmSpendResponse,
    AdminUserSpend,
    LlmConfigResponse,
    LlmModelOption,
    LlmSpend,
    LlmSpendBreakdown,
    LlmStageConfigResponse,
    LlmStageConfigUpdate,
)

logger = logging.getLogger("motet.api.admin_llm")

#: Highest precedence first, spelled as the ``*_source`` fields spell it.
PRECEDENCE: Final = [source.value for source in ConfigSource]

#: The worker reads ``settings`` once per job (``motet_workers.llm_context``), so a change
#: applies to the next job it claims. Reported rather than assumed by the UI.
APPLIES: Final = "next_job"

#: The spend column an operator reads. The total beside it is over whatever is retained.
SPEND_WINDOW_DAYS: Final = 7

#: Which LLM stages a pipeline queue's spend is made of. ``script`` is the script stage
#: alone, by decision: revisit if the script queue ever grows a second model call. Voice is
#: on no queue — and records no ledger row at all, because the voice service has no
#: database (invariant 2); its spend is the ``motet.llm.tokens`` metric and nothing else.
QUEUE_STAGES: Final[Mapping[str, tuple[LlmStage, ...]]] = {
    Queue.INTEGRATE.value: (LlmStage.DEDUP, LlmStage.DEDUP_CONFIRM),
    Queue.SCRIPT.value: (LlmStage.SCRIPT,),
}


class SettingsReadOnlyError(Exception):
    """This deployment does not honour ``settings`` rows, so it will not write one."""


def _effort_word(effort: str | None) -> str:
    return effort if effort is not None else OFF_VALUE


def _env(environ: Mapping[str, str], var: str) -> str | None:
    return environ.get(var, "").strip() or None


def honoured_rows(conn: psycopg.Connection[Any], environ: Mapping[str, str]) -> dict[str, str]:
    """The rows this deployment would apply — nothing where the switch is off."""
    if not settings_repo.settings_writable(environ):
        return {}
    return settings_repo.load(conn, SETTING_PREFIX)


def describe_config(rows: Mapping[str, str], environ: Mapping[str, str]) -> LlmConfigResponse:
    """The resolved config plus every rung of the chain, for the screen to show.

    Rows that no longer resolve — a slug a later deploy took out of the catalogue — are
    shown as the worker treats them: not applied, with the reason, rather than as a 500.
    """
    writable = settings_repo.settings_writable(environ)
    problem: str | None = None
    try:
        config = validate_overrides(rows, environ)
    except LlmConfigError as exc:
        problem = str(exc)
        config = load_config(environ, overrides={})
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
                setting_model=rows.get(stage.model_setting) or None,
                stage_env_model=_env(environ, stage.model_env),
                global_env_model=_env(environ, MODEL_ENV),
                default_model=DEFAULT_MODEL,
                setting_effort=rows.get(stage.effort_setting) or None,
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
                cache_write_1h_usd_per_mtok=spec.cache_write_1h_usd_per_mtok,
            )
            for spec in KNOWN_MODELS.values()
        ],
        precedence=PRECEDENCE,
        applies=APPLIES,
        writable=writable,
        writable_env=settings_repo.SETTINGS_WRITABLE_ENV,
        settings_error=problem,
    )


def apply_update(
    conn: psycopg.Connection[Any],
    stage: LlmStage,
    body: LlmStageConfigUpdate,
    environ: Mapping[str, str],
) -> LlmConfigResponse:
    """Set or clear one stage's rows, validated before anything is written.

    Raises :class:`SettingsReadOnlyError` where the switch is off and ``LlmConfigError``
    when the candidate does not resolve. The table is locked against other writers for the
    rest of the request's transaction — readers are unaffected — so two saves racing on one
    stage cannot each validate half of a pairing and together store one that does not
    resolve.
    """
    if not settings_repo.settings_writable(environ):
        raise SettingsReadOnlyError(
            "Model settings are read-only on this deployment: "
            f"{settings_repo.SETTINGS_WRITABLE_ENV} is not set, so the environment is the "
            "whole of the model configuration here."
        )
    conn.execute("LOCK TABLE settings IN SHARE ROW EXCLUSIVE MODE")
    current = settings_repo.load(conn, SETTING_PREFIX)
    changes: dict[str, str | None] = {}
    if "model" in body.model_fields_set:
        changes[stage.model_setting] = (body.model or "").strip() or None
    if "effort" in body.model_fields_set:
        changes[stage.effort_setting] = (body.effort or "").strip().lower() or None
    candidate = dict(current)
    for key, value in changes.items():
        if value is None:
            candidate.pop(key, None)
        else:
            candidate[key] = value
    validate_overrides(candidate, environ)
    for key, value in changes.items():
        settings_repo.put(conn, key, value)
    if changes:
        logger.warning(
            "llm settings changed for %s: %s — applies to the next job a worker claims",
            stage.value,
            ", ".join(
                f"{key}={value if value is not None else '<cleared>'}"
                for key, value in changes.items()
            ),
        )
    return describe_config(candidate, environ)


def overrides_in_force(database_url: str | None, environ: Mapping[str, str]) -> bool | None:
    """Whether a worker in this deployment would apply any row right now.

    For ``/internal/health``: ``False`` without touching the database where the switch is
    off — every production request — and otherwise one short query. ``None`` when that
    query could not be asked, which is not the same answer as "no".
    """
    if not settings_repo.settings_writable(environ):
        return False
    if not database_url:
        return None
    try:
        with repo.connect(database_url, connect_timeout=3) as conn:
            rows = settings_repo.load(conn, SETTING_PREFIX)
    except psycopg.Error:
        logger.warning("could not read the settings table for /internal/health", exc_info=True)
        return None
    if not rows:
        return False
    try:
        validate_overrides(rows, environ)
    except LlmConfigError:
        return False
    return True


#: The ledger's ``cache_ttl`` column back into the type the price function takes.
_TTLS: Final[Mapping[str, CacheTtl]] = {"5m": "5m", "1h": "1h"}


@dataclass
class _Bucket:
    completions: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    reasoning_tokens: int = 0
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0
    usd: float = 0.0
    unpriced_completions: int = 0

    def add(self, cell: llm_usage.Totals, usd: float | None) -> None:
        self.completions += cell.completions
        self.input_tokens += cell.input_tokens
        self.output_tokens += cell.output_tokens
        self.reasoning_tokens += cell.reasoning_tokens
        self.cache_read_tokens += cell.cache_read_tokens
        self.cache_write_tokens += cell.cache_write_tokens
        if usd is None:
            self.unpriced_completions += cell.completions
        else:
            self.usd += usd

    def response(self) -> LlmSpend:
        return LlmSpend(
            completions=self.completions,
            input_tokens=self.input_tokens,
            output_tokens=self.output_tokens,
            reasoning_tokens=self.reasoning_tokens,
            cache_read_tokens=self.cache_read_tokens,
            cache_write_tokens=self.cache_write_tokens,
            usd=round(self.usd, 6),
            unpriced_completions=self.unpriced_completions,
        )


@dataclass
class _User:
    email: str | None
    bucket: _Bucket = field(default_factory=_Bucket)


def _fold(cells: Iterable[llm_usage.Totals]) -> LlmSpendBreakdown:
    """Price every cell and sum it three ways.

    Priced per cell because a price is per model and a cache write's price is per TTL, and
    one stage may have run on several of each; a sum of per-cell costs is exact where a
    per-stage average would not be. Every stage in the enum and every mapped queue is
    present at zero, so "unused" and "not a stage" read differently.
    """
    stages = {stage.value: _Bucket() for stage in LlmStage}
    queues = {queue: _Bucket() for queue in QUEUE_STAGES}
    users: dict[str, _User] = {}
    queue_of = {stage.value: queue for queue, members in QUEUE_STAGES.items() for stage in members}
    for cell in cells:
        usd = usage_cost_usd(
            cell.model,
            Usage(
                input_tokens=cell.input_tokens,
                output_tokens=cell.output_tokens,
                reasoning_tokens=cell.reasoning_tokens,
                cache_read_tokens=cell.cache_read_tokens,
                cache_write_tokens=cell.cache_write_tokens,
            ),
            _TTLS.get(cell.cache_ttl or ""),
        )
        stages.setdefault(cell.stage, _Bucket()).add(cell, usd)
        if cell.user_id is not None:
            users.setdefault(cell.user_id, _User(email=cell.user_email)).bucket.add(cell, usd)
        queue = queue_of.get(cell.stage)
        if queue is not None:
            queues[queue].add(cell, usd)
    return LlmSpendBreakdown(
        stages={key: bucket.response() for key, bucket in stages.items()},
        queues={key: bucket.response() for key, bucket in queues.items()},
        users=[
            AdminUserSpend(user_id=user_id, email=user.email, spend=user.bucket.response())
            for user_id, user in sorted(users.items(), key=lambda item: -item[1].bucket.usd)
        ],
    )


def fold_spend(conn: psycopg.Connection[Any]) -> AdminLlmSpendResponse:
    """The ledger, over everything retained and over the last :data:`SPEND_WINDOW_DAYS`."""
    ledger = llm_usage.totals(conn, window_days=SPEND_WINDOW_DAYS)
    return AdminLlmSpendResponse(
        generated_at=datetime.now(UTC),
        since=ledger.since,
        window_days=ledger.window_days,
        retention_days=llm_usage.RETENTION_SECONDS // 86_400,
        total=_fold(ledger.total),
        window=_fold(ledger.window),
    )
