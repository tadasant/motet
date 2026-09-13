"""Per job: ``settings`` overrides in, one ``llm_usage`` row per completion out (motet#92).

Installed around each job by :func:`llm_job_context` and known to nothing else in this
package. Two halves, and each is best-effort in the direction that cannot cost a paste:

* **Model overrides, read once per job — and only where the deployment honours them.**
  ``build_request`` resolves ``load_config()`` on every completion, so the stage adapters
  already re-read the environment per call. Where ``MOTET_SETTINGS_WRITABLE`` is on (a
  laptop, staging) this reads the ``settings`` rows at the top of the job, validates them
  with the same :func:`~motet_inference.llm.validate_overrides` the admin route writes
  through, and installs them with :func:`~motet_inference.llm.llm_overrides`. Once per job
  rather than per completion, so a change landing mid-job cannot put dedup's first pass
  and its second look on different models. **Rows that do not resolve are not
  installed**: the job runs on the environment and an ERROR says why, because a bad row
  failing every job until somebody noticed would turn a dropdown into an outage — the one
  thing a runtime setting must never be able to do. Where the switch is off (production)
  nothing is read at all, and the environment ``validate_startup`` checked at boot is the
  whole of what the job runs on.

* **The ledger.** :func:`~motet_inference.accounting.usage_sink` hands each completion to a
  buffer, and the buffer is written **after** ``_execute``'s transactions have settled, on
  the loop's autocommit connection. Not inside the handler's transaction: a completion
  that was billed inside a job that then failed and rolled back is exactly the row that
  must not disappear with the rollback. A row that cannot be written is logged and dropped,
  never a job failure — the metric and the log line already carry it.

The deterministic stage fakes call no model, so a fake-mode job writes no row, correctly.
"""

from __future__ import annotations

import logging
import os
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

import psycopg
from motet_db import llm_usage
from motet_db import settings as settings_repo
from motet_inference.accounting import StageUsage, usage_sink
from motet_inference.llm import SETTING_PREFIX, LlmConfigError, llm_overrides, validate_overrides

from . import jobs

logger = logging.getLogger("motet.worker.llm")

#: Payload keys naming the domain object a completion is billed to, in the order tried.
#: ``integrate`` carries a source item and ``script`` an episode; no other queue calls a
#: model today.
_SUBJECT_KEYS = ("source_item_id", "episode_id")


def job_overrides(conn: psycopg.Connection[Any]) -> dict[str, str]:
    """The ``settings`` rows a job should run with, already validated — or nothing.

    Also the worker's boot check (``runner``), so that "the rows resolve against this
    environment" is asked by one function whether it is asked at boot or per job.
    """
    if not settings_repo.settings_writable(os.environ):
        return {}
    try:
        rows = settings_repo.load(conn, SETTING_PREFIX)
    except psycopg.Error:
        logger.exception("could not read the settings table; this job resolves LLM config from env")
        return {}
    if not rows:
        return {}
    try:
        validate_overrides(rows)
    except LlmConfigError as exc:
        logger.error(
            "settings rows do not resolve against this environment, so this job ignores all "
            "of them and runs on env: %s",
            exc,
        )
        return {}
    return rows


@contextmanager
def llm_job_context(conn: psycopg.Connection[Any], job: jobs.Job) -> Iterator[None]:
    """Resolve LLM config against ``settings`` and ledger every completion, for one job."""
    overrides = job_overrides(conn)
    buffered: list[StageUsage] = []
    try:
        with llm_overrides(overrides), usage_sink(buffered.append):
            yield
    finally:
        _flush(conn, job, buffered)


def _subject(job: jobs.Job) -> str | None:
    for key in _SUBJECT_KEYS:
        value = job.payload.get(key)
        if isinstance(value, str) and value:
            return value
    return None


def _flush(conn: psycopg.Connection[Any], job: jobs.Job, entries: list[StageUsage]) -> None:
    if not entries:
        return
    rows = [
        llm_usage.UsageRow(
            stage=entry.stage.value,
            model=entry.model,
            input_tokens=entry.usage.input_tokens,
            output_tokens=entry.usage.output_tokens,
            reasoning_tokens=entry.usage.reasoning_tokens,
            cache_read_tokens=entry.usage.cache_read_tokens,
            cache_write_tokens=entry.usage.cache_write_tokens,
            cache_ttl=entry.cache_ttl,
        )
        for entry in entries
    ]
    try:
        with conn.transaction():
            llm_usage.insert(conn, rows, subject=_subject(job), job_id=job.id)
    except psycopg.Error:
        logger.exception(
            "could not write %d llm_usage row(s) for job %d; the metric and the log line "
            "still carry them",
            len(rows),
            job.id,
        )
