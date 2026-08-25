"""Worker entry point — one Cloud Run job per queue. Executed, never imported.

    python -m motet_workers.runner integrate

**Nothing in this package may import this module**, and that is the entire reason this
file now holds the CLI and nothing else. Until motet#21 the queue-draining loop lived
here too, at the top of this same file. ``python -m motet_workers.runner`` imports the
package ``motet_workers`` first and only then executes ``runner.py`` as ``__main__``. If
the package has already pulled ``runner`` into ``sys.modules`` on the way past — which
``from .runner import drain`` in ``__init__`` did, to re-export that loop — ``runpy``
executes the same file a *second* time under a second name, and says so:

    RuntimeWarning: 'motet_workers.runner' found in sys.modules after import of package
    'motet_workers', but prior to execution of 'motet_workers.runner'; this may result in
    unpredictable behaviour

Two copies of one module do not share module-level state. Nothing was stateful enough for
that to have hurt, but the metric instruments that now live in :mod:`motet_workers.loop`
were being built twice while they were still in this file, and a connection pool, a
client, or a class used with ``isinstance`` would not have survived it. So the rule is
structural rather than a thing to remember: the loop is importable and lives next door,
this file is the executable, and ``workers/tests/test_entrypoint.py`` fails if the two
ever merge back.

The queue name is the container's argument, so the image's ``ENTRYPOINT`` is this module
and a Cloud Run job supplies ``args``. The name is validated against the ``Queue`` enum,
which makes a typo a failed job rather than a silently idle one.
"""

from __future__ import annotations

import argparse
import logging
import os
import time

import motet_obs
from motet_inference.llm import validate_startup as validate_llm_startup

from .loop import MAX_JOBS_PER_RUN, drain
from .queues import Queue

logger = logging.getLogger("motet.worker")

#: The service name this process reports as. ``motet-api`` is the other one, and keeping
#: them distinct is the whole reason an operator can filter the two apart at all.
SERVICE_NAME = "motet-worker"


def main(argv: list[str] | None = None) -> int:
    # `prog` is set because argparse would otherwise take it from `sys.argv[0]`, which
    # under `python -m` is the file — so `--help` announced itself as `runner.py`, a name
    # that appears nowhere an operator could type it.
    parser = argparse.ArgumentParser(
        prog="motet-worker", description="Drain one Motet pipeline queue."
    )
    parser.add_argument("queue", choices=[q.value for q in Queue])
    parser.add_argument(
        "--poll-seconds",
        type=float,
        default=0.0,
        help=(
            "Keep draining, sleeping this long between empty passes. Default 0 drains "
            "once and exits, which is what a Cloud Run job wants."
        ),
    )
    parser.add_argument("--max-jobs", type=int, default=MAX_JOBS_PER_RUN)
    args = parser.parse_args(argv)

    # First, so that every line below is exported rather than only printed. This is the
    # worker's half of invariant 11 and it is the half that was missing: `motet_api` calls
    # the same function, but it depends on `motet_workers`, so until the wiring moved into
    # its own package the process that makes every vendor call had no way to reach it.
    # `configure` logs its own one-line summary of what it installed, so nothing is
    # logged here — a second line would be the same fact in a less readable shape.
    motet_obs.configure(SERVICE_NAME)

    # Before anything else, and before claiming any job. A Cloud Run job that cannot reach
    # a model should fail immediately and visibly, rather than claim work it will then fail
    # to finish — and it should report *that* rather than whichever check happens to run
    # first. Ordering this ahead of the database check keeps the credential failure the one
    # you see when both are wrong.
    logger.info("llm: %s", validate_llm_startup().describe())

    database_url = os.environ.get("DATABASE_URL")
    if not database_url:
        parser.error("DATABASE_URL is not set")

    queue = Queue(args.queue)
    try:
        if args.poll_seconds <= 0:
            drain(queue, database_url, max_jobs=args.max_jobs)
            return 0

        while True:
            if drain(queue, database_url, max_jobs=args.max_jobs) == 0:
                time.sleep(args.poll_seconds)
    finally:
        # A Cloud Run job drains and exits, and a batch processor that is not flushed
        # loses what it was holding — which is the end of the run, the most interesting
        # part. The SDK registers its own `atexit` hook, but a job killed after SIGTERM
        # may never reach it, so the flush is here where the exit is.
        motet_obs.shutdown()


if __name__ == "__main__":
    raise SystemExit(main())
