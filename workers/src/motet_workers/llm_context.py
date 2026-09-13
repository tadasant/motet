"""PROTOTYPE — per-job LLM context: settings overrides in, usage rows out.

Two things the admin screen needs from the worker, installed around each job by
:func:`llm_job_context` and known to nothing else in this package:

* **Model configuration read per job.** ``build_request`` resolves ``load_config()`` on
  every completion, so the stage adapters already re-read the *environment* per call. This
  reads the ``settings`` rows once at the top of the job and installs them with
  :func:`~motet_inference.llm.llm_overrides`, so a change made on ``/admin`` applies to the
  next job a worker claims — no restart. Once per job rather than per completion so a
  change landing mid-job cannot put dedup's first pass and its second look on different
  models. ``validate_startup`` still checks the environment at boot and knows nothing of
  this; the settings are validated when they are written (400 on an unknown slug) and, as a
  last line, by ``load_config`` raising ``LlmConfigError`` inside the job.

* **One ``llm_usage`` row per completion.** :func:`~motet_inference.accounting.usage_sink`
  hands each entry to a buffer, and the buffer is written **after** the handler's
  transaction has settled rather than inside it — a completion that was billed and then
  rolled back with a failing job is exactly the row that must not disappear with the
  rollback. The loop runs its connection in autocommit with explicit transactions, so a
  flush after ``_execute`` returns is its own statement.

The voice service installs neither. It has no database (invariant 2), and its turns stay a
metric and a log line, which is what they were.
"""

from __future__ import annotations

import logging
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

import psycopg
from motet_db import repo
from motet_inference.accounting import StageUsage, usage_sink
from motet_inference.llm import llm_overrides

from . import jobs

logger = logging.getLogger("motet.worker.llm")

#: Payload keys that name the domain object a job is about, in the order to try them.
_SUBJECT_KEYS = ("source_item_id", "episode_id", "source_id")


def _subject(job: jobs.Job) -> str | None:
    for key in _SUBJECT_KEYS:
        value = job.payload.get(key)
        if isinstance(value, str) and value:
            return value
    return None


@contextmanager
def llm_job_context(conn: psycopg.Connection[Any], job: jobs.Job) -> Iterator[None]:
    """Resolve LLM config against the settings table and ledger every completion, for one job.

    Both halves are best-effort in the same direction: a settings table that cannot be
    read falls back to the environment (and says so), and a ledger row that cannot be
    written is logged and dropped. Neither may fail a job whose work is somebody's paste.
    """
    try:
        settings = repo.load_settings(conn, prefix="llm.")
    except psycopg.Error:
        logger.exception("could not read the settings table; resolving LLM config from env only")
        settings = {}

    buffered: list[StageUsage] = []
    try:
        with llm_overrides(settings), usage_sink(buffered.append):
            yield
    finally:
        _flush(conn, job, buffered)


def _flush(conn: psycopg.Connection[Any], job: jobs.Job, entries: list[StageUsage]) -> None:
    if not entries:
        return
    subject = _subject(job)
    try:
        for entry in entries:
            repo.insert_llm_usage(
                conn,
                stage=entry.stage.value,
                model=entry.model,
                input_tokens=entry.usage.input_tokens,
                output_tokens=entry.usage.output_tokens,
                reasoning_tokens=entry.usage.reasoning_tokens,
                cache_read_tokens=entry.usage.cache_read_tokens,
                cache_write_tokens=entry.usage.cache_write_tokens,
                subject=subject,
                job_id=job.id,
            )
    except psycopg.Error:
        logger.exception(
            "could not write %d llm_usage row(s) for job %d; the metric and the log line "
            "still carry them",
            len(entries),
            job.id,
        )
