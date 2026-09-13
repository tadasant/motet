"""Drive one held source item through triage → enrich → integrate, in-process, for real.

Spike helper for the live proof of the enrichment pipeline step. The running `runner all`
worker carries code from before the `enrich` queue existed, so instead of restarting it
this calls the three handlers directly on one connection with the real stages and the real
Pi runner — exactly what the worker would do, minus the queue claim.

    UV_ENV_FILE=.env GOOGLE_APPLICATION_CREDENTIALS=~/.config/motet/local-dev.json \\
        uv run python proto/enrich-spike/drive_pipeline.py <source_item_id>

Every job row the handlers enqueue (the enrich job, the post-enrich integrate job) is
marked `done` in the same transaction that writes it, so the old worker cannot race this
script for the integrate job and double-dedup the item. Prints no secret: only ids, the
triage decision, the run's numbers, and the news item that resulted.
"""

from __future__ import annotations

import logging
import os
import sys
import time
from typing import Any

import psycopg
from motet_db import enrichment, repo
from motet_inference import real_stages
from motet_inference.accounting import usage_sink
from motet_inference.llm import llm_overrides
from motet_storage import build_store
from motet_workers.enrich import handle_enrich
from motet_workers.handlers import Context, handle_integrate

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
log = logging.getLogger("drive")


def settle_jobs(conn: psycopg.Connection[Any], source_item_id: str) -> int:
    """Mark every ready job about this item done, so no other worker takes it."""
    result = conn.execute(
        """
        UPDATE jobs SET state = 'done', updated_at = now()
        WHERE state = 'ready' AND payload ->> 'source_item_id' = %s
        """,
        (source_item_id,),
    )
    return result.rowcount


def main(source_item_id: str) -> int:
    if os.environ.get("MOTET_INFERENCE_MODE", "").strip().lower() != "real":
        log.error("MOTET_INFERENCE_MODE must be real for the live proof")
        return 2
    url = os.environ["DATABASE_URL"]
    stages = real_stages()
    store = build_store()

    with repo.connect(url) as conn:
        # As the worker loop does: autocommit on, so that each `conn.transaction()` below
        # is a real transaction rather than a savepoint inside one that never commits —
        # and so that the handler's side connection (`enrich_status = running`) does not
        # block on a row this connection is still holding a lock on.
        conn.autocommit = True
        settings = repo.load_settings(conn, prefix="llm.")

        def ledger(entry: Any) -> None:
            repo.insert_llm_usage(
                conn,
                stage=entry.stage.value,
                model=entry.model,
                input_tokens=entry.usage.input_tokens,
                output_tokens=entry.usage.output_tokens,
                reasoning_tokens=entry.usage.reasoning_tokens,
                cache_read_tokens=entry.usage.cache_read_tokens,
                cache_write_tokens=entry.usage.cache_write_tokens,
                subject=source_item_id,
                job_id=None,
            )

        context = Context(conn=conn, stages=stages, store=store)
        with llm_overrides(settings), usage_sink(ledger):
            # 1. integrate → triage
            t0 = time.monotonic()
            with conn.transaction():
                handle_integrate(context, {"source_item_id": source_item_id})
                settled = settle_jobs(conn, source_item_id)
            current = enrichment.get_enrichment(conn, source_item_id)
            assert current is not None
            log.info(
                "TRIAGE %s: decision=%s status=%s url=%s reason=%r (%.1fs, %d job(s) settled)",
                source_item_id,
                current.triage_decision,
                current.enrich_status,
                current.article_url,
                current.triage_reason,
                time.monotonic() - t0,
                settled,
            )
            if current.enrich_status != "pending":
                log.info("no enrichment needed; the item integrated on its text")
                return 0

            # 2. enrich
            t1 = time.monotonic()
            with conn.transaction():
                handle_enrich(
                    context,
                    {
                        "source_item_id": source_item_id,
                        "article_url": current.article_url,
                        "domain": "",
                    },
                )
                settle_jobs(conn, source_item_id)
            run = enrichment.latest_enrich_run(conn, source_item_id, user_id=current.user_id)
            after = enrichment.get_enrichment(conn, source_item_id)
            assert run is not None and after is not None
            log.info(
                "ENRICH %s: status=%s tool_calls=%d cost_usd=%s login_performed=%s "
                "article_chars=%d error=%r (%.0fs)",
                source_item_id,
                after.enrich_status,
                run.tool_calls,
                f"{run.cost_usd:.4f}" if run.cost_usd is not None else "unknown",
                run.login_performed,
                run.article_chars,
                run.error,
                time.monotonic() - t1,
            )
            stored = repo.get_source_item(conn, source_item_id)
            assert stored is not None
            log.info("TEXT now %d chars; head: %r", len(stored.text), stored.text[:160])

            # 3. integrate again, enriched
            t2 = time.monotonic()
            with conn.transaction():
                handle_integrate(context, {"source_item_id": source_item_id, "enriched": True})
                settle_jobs(conn, source_item_id)
            row = conn.execute(
                """
                SELECT ni.id, ni.title, ni.summary, link.position
                FROM news_item_sources link JOIN news_items ni ON ni.id = link.news_item_id
                WHERE link.source_item_id = %s
                """,
                (source_item_id,),
            ).fetchone()
            log.info(
                "INTEGRATE %s (%.1fs): news item %r", source_item_id, time.monotonic() - t2, row
            )
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1]))
