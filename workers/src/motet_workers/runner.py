"""Worker entry point — one Cloud Run job per queue.

**Scaffold.** The claim loop is not built. What is fixed here is the shape: a worker takes
a queue name, drains it, and exits; it never runs as a long-lived server, because Cloud Run
jobs are how the pipeline stages are scheduled.

Two constraints on whoever fills this in:

* **Claim with ``SELECT ... FOR UPDATE SKIP LOCKED``** against ``jobs`` — the reason there
  is no Redis (see the AGENTS.md tripwires). ``db/tests/test_migrate.py`` already pins that
  behaviour.
* **Serialize ingestion per user** (invariant 6). Two integrate runs for one user must
  never overlap, or dedup races and duplicates the story.
"""

from __future__ import annotations

import argparse
import logging

from motet_inference.llm import validate_startup as validate_llm_startup

from .queues import Queue

logger = logging.getLogger("motet.worker")

NOT_BUILT_YET = "Worker loop is not part of the factory scaffold — see AGENTS.md."


def drain(queue: Queue, database_url: str) -> int:
    """Claim and run every ready job on ``queue``. Returns the number processed."""
    raise NotImplementedError(NOT_BUILT_YET)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Drain one Motet pipeline queue.")
    parser.add_argument("queue", choices=[q.value for q in Queue])
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO)
    # Before claiming any job. A Cloud Run job that cannot reach a model should fail
    # immediately and visibly, rather than claim work it will then fail to finish.
    logger.info("llm: %s", validate_llm_startup().describe())
    raise NotImplementedError(f"{NOT_BUILT_YET} (queue={args.queue})")


if __name__ == "__main__":
    raise SystemExit(main())
