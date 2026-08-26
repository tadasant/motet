"""Worker entry point — one process, one or every queue. Executed, never imported.

    python -m motet_workers.runner integrate            # drain once and exit
    python -m motet_workers.runner all --poll-seconds 2 # drain everything, forever

**Both shapes are supported because both are real deployments, and motet#38 is what
happens when only the first one exists.** A Cloud Run *job* drains a queue once and exits,
which is why ``--poll-seconds`` defaults to zero — but a job has to be *started* by
something, and until this repo grew the second shape the only thing that started one was a
human dispatching a workflow in the private infrastructure repo. The SPA meanwhile told
the user "a worker takes it off the queue within a few seconds". Nothing did.

``all`` is what makes an always-on worker one process rather than six. It sweeps
:data:`~motet_workers.queues.PIPELINE` in order on every pass, so a pasted item integrates
and an episode assembles, scripts and renders without waiting a poll interval per stage,
and a queue added to the enum is drained by whatever is already deployed.

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
import signal
import time
from dataclasses import dataclass
from types import FrameType

import motet_obs
from motet_inference import get_stages
from motet_inference.llm import validate_startup as validate_llm_startup
from motet_storage import build_store
from motet_vault import vault_status

from .loop import MAX_JOBS_PER_RUN, drain
from .queues import PIPELINE, Queue

logger = logging.getLogger("motet.worker")

#: The service name this process reports as. ``motet-api`` is the other one, and keeping
#: them distinct is the whole reason an operator can filter the two apart at all.
SERVICE_NAME = "motet-worker"

#: The queue argument that means "every queue, in pipeline order" — see :data:`PIPELINE`.
#:
#: Not a member of :class:`~motet_workers.queues.Queue`, deliberately: it is a thing to
#: *ask a worker for*, not a queue that exists in the ``jobs`` table, and putting it in the
#: enum would make it a legal value everywhere a queue name is accepted, including
#: ``enqueue``.
ALL_QUEUES = "all"


@dataclass
class _Stop:
    """Whether SIGTERM has arrived. Set from a signal handler, read between jobs."""

    requested: bool = False


def _install_sigterm(stop: _Stop) -> None:
    """Turn SIGTERM into "finish this pass and exit" rather than into a killed process.

    This matters only in ``--poll-seconds`` mode, and there it matters a lot: a long-lived
    worker is the thing Cloud Run *sends* SIGTERM to, on every deploy and every scale-in.
    The default disposition kills the process outright, which skips the ``finally`` below —
    so the last batch of spans and metrics, the ones describing the shutdown, are the ones
    that never leave. Cloud Run's grace period is seconds rather than minutes, which is
    also why nothing here waits for a job to finish that has not started.

    The flag is read between queues and between passes, so the worst case is one poll
    interval plus whatever job is in flight. SIGINT is deliberately left alone: Ctrl-C in
    development already unwinds through the ``finally`` as a ``KeyboardInterrupt``, and
    swallowing it would make a second Ctrl-C do nothing.
    """

    def _handler(signum: int, _frame: FrameType | None) -> None:
        # No logging call here: a handler runs between bytecodes and the logging module
        # takes a lock, so a signal arriving while the loop is mid-log deadlocks the
        # process it was meant to stop politely. The loop says so instead, once it looks.
        stop.requested = True

    signal.signal(signal.SIGTERM, _handler)


def main(argv: list[str] | None = None) -> int:
    # `prog` is set because argparse would otherwise take it from `sys.argv[0]`, which
    # under `python -m` is the file — so `--help` announced itself as `runner.py`, a name
    # that appears nowhere an operator could type it.
    parser = argparse.ArgumentParser(
        prog="motet-worker", description="Drain a Motet pipeline queue, or all of them."
    )
    parser.add_argument(
        "queue",
        choices=[*(q.value for q in Queue), ALL_QUEUES],
        help=(
            "Which queue to drain, or 'all' for every queue in pipeline order — one "
            "process that carries a pasted item the whole way through."
        ),
    )
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

    # The vault, said out loud, because this process is the one that cannot be asked. The
    # API reports the same thing on `/internal/health`; a Cloud Run job has no route, so a
    # log line at startup is the whole of what an operator can see. And the worker is the
    # *decrypt* side — a poll that cannot unwrap its credential fails per run, forever,
    # with nothing tying it to a missing SDK or a withdrawn IAM grant. Not fatal for the
    # same reason as in the API: every queue except `poll` is untouched by this.
    vault = vault_status()
    if vault.ready:
        logger.info("vault: backend=%s ready=true", vault.backend)
    else:
        logger.error(
            "vault: backend=%s ready=false — polling a mailbox will fail on every run: %s",
            vault.backend,
            vault.detail,
        )

    database_url = os.environ.get("DATABASE_URL")
    if not database_url:
        parser.error("DATABASE_URL is not set")

    queues = list(PIPELINE) if args.queue == ALL_QUEUES else [Queue(args.queue)]

    # Once for the life of the process, rather than once per drain. A poll loop that
    # rebuilt these would mint a fresh LLM client on every sweep, and OpenRouter's sticky
    # upstream routing — what keeps the dedup prompt cache warm — is per client. Building
    # them here also moves a misconfigured object store from "fails on the first job that
    # needs audio" to "fails at start-up", which is where this file puts everything else.
    stages = get_stages()
    store = build_store()

    stop = _Stop()
    try:
        if args.poll_seconds <= 0:
            for queue in queues:
                drain(queue, database_url, max_jobs=args.max_jobs, stages=stages, store=store)
            return 0

        _install_sigterm(stop)
        logger.info("polling %s every %.1fs", ", ".join(q.value for q in queues), args.poll_seconds)
        while not stop.requested:
            processed = 0
            for queue in queues:
                if stop.requested:
                    break
                processed += drain(
                    queue, database_url, max_jobs=args.max_jobs, stages=stages, store=store
                )
            # Only when the whole sweep found nothing. Sleeping after every pass would add
            # a poll interval to each stage; sleeping only when the pipeline is empty means
            # a busy one runs flat out and an idle one costs one query per queue per tick.
            if processed == 0 and not stop.requested:
                time.sleep(args.poll_seconds)
        logger.info("SIGTERM received; stopped cleanly")
        return 0
    finally:
        # A Cloud Run job drains and exits, and a batch processor that is not flushed
        # loses what it was holding — which is the end of the run, the most interesting
        # part. The SDK registers its own `atexit` hook, but a job killed after SIGTERM
        # may never reach it, so the flush is here where the exit is.
        motet_obs.shutdown()


if __name__ == "__main__":
    raise SystemExit(main())
