"""Nudging the worker: the seam to Cloud Run's job admin API.

Every enqueue this API performs traces to a human doing something — pasting text, asking
for an episode, connecting a mailbox. The API therefore knows the exact moment there is
something to drain, and until now it did nothing with that: the ``motet-worker`` job was
started by a standing Cloud Scheduler sweep that fired whether or not any work existed.
About 21,900 executions a month per environment found nothing.

**This is a nudge, never a mechanism.** The job row is already committed to Postgres by
the time anything here runs, and the Cloud Scheduler drain stays in place as a backstop,
so a failed invoke costs *latency* and nothing else. That is why :meth:`DrainTrigger.fire`
swallows everything: a paste must not 500 because the drain trigger could not fire.

**Off unless an environment names a job.** ``MOTET_WORKER_JOB`` carries the Cloud Run v2
resource name of the worker job, which is exactly the string Terraform's
``google_cloud_run_v2_job`` resource already has — one variable, so there is no way to be
half-configured. Unset means inert, which is the right answer on a laptop, in CI, and in
any environment whose ``roles/run.invoker`` grant has not been applied yet.

**No request body, ever.** The Cloud Scheduler version of this call sent
``{"overrides":{"containerOverrides":[{"args":["all"]}]}}`` — mirroring
``gcloud run jobs execute --args=all`` — and Cloud Run rejected it, reporting the
rejection *only* to GCP Cloud Logging. Invariant 11 means nothing in this estate can read
that, so the job existed, every plan showed no drift, and every tick produced no
execution, no container and no log line anywhere; it took four applies to bisect. The
worker job declares ``args = ["all"]`` in its own definition, so an unmodified execution
already drains every queue. Keep it that way: :meth:`CloudRunJobTrigger.fire` posts with
no content at all, and ``api/tests/test_drain.py`` asserts the bytes on the wire.

The **regional** host (``{region}-run.googleapis.com``) rather than the global one is a
prefer-the-proven-shape choice: it is byte-for-byte what ``gcloud run jobs execute`` uses
and what has been starting this job since Phase 1. The global host was never shown to be
broken.
"""

from __future__ import annotations

import logging
import os
import threading
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from enum import StrEnum
from typing import Any, Final, Protocol, runtime_checkable

import httpx
from opentelemetry import metrics

from .config import ConfigError

logger = logging.getLogger("motet.api")

#: The Cloud Run v2 resource name of the worker job:
#: ``projects/{project}/locations/{region}/jobs/{job}``. Unset means the trigger is off.
#:
#: One variable rather than three, because three can be half-set. It is also the value the
#: infrastructure repo already has to hand — a ``google_cloud_run_v2_job``'s ``id`` is
#: this string — so wiring it up is an assignment rather than a template.
WORKER_JOB_ENV: Final = "MOTET_WORKER_JOB"

#: The scope Cloud Run's admin API wants. `run.invoker` is checked on the job itself; the
#: scope is what the ambient service-account credential is minted for.
CLOUD_PLATFORM_SCOPE: Final = "https://www.googleapis.com/auth/cloud-platform"

#: Short on purpose. The invoke happens inside the request that enqueued the work — see
#: `motet_api.deps.connection` for why it is there and not in a background task — so this
#: is latency a human is waiting through. A control-plane call that has not answered in
#: five seconds is not going to make the difference between this drain and the backstop
#: sweep, and giving up is cheaper than making somebody watch a spinner.
#:
#: What it bounds, precisely: each phase of the ``:run`` POST (httpx applies it per
#: connect/read/write, not as a total). It does **not** bound the credential refresh in
#: :class:`AdcAccessToken`, which on Cloud Run is a metadata-server read measured in
#: milliseconds and on a machine off GCP is google-auth's own, much longer, timeout — a
#: case that only arises with ``MOTET_WORKER_JOB`` set on a laptop, which nothing does.
DEFAULT_TIMEOUT_SECONDS: Final = 5.0

#: How much of a rejection body to keep. Google's errors put the useful sentence first;
#: the rest is a JSON envelope, and a log line is not the place for all of it.
_ERROR_BODY_CHARS: Final = 500


class DrainReason(StrEnum):
    """Which user action asked for the drain.

    A metric label, so the set is closed and small by construction: one member per
    enqueue *helper* the API calls. There are four helpers and five routes, because
    ``enqueue_source_poll`` is reached both by completing mailbox consent and by asking a
    source to poll — and those are the same user intent, so they share a label.
    """

    PASTE = "paste"
    EPISODE = "episode"
    SMART_EPISODE = "smart_episode"
    SOURCE_POLL = "source_poll"


_meter = metrics.get_meter("motet.api")

#: Whether the nudge fired, split by what asked for it.
#:
#: ``disabled`` is counted as well as the two real outcomes, and that is the
#: never-infer-"no errors"-from-"no data" rule AGENTS.md states: a series that only exists
#: once an environment has opted in cannot tell "nothing has been enqueued" from "this
#: deployment never got the invoker grant". It is also the instrument that would answer
#: the fan-out question — one execution per enqueue is deliberate (see the PR), and
#: ``fired`` divided by wall-clock is what would say the burst rate had outgrown it.
_drain_triggers = _meter.create_counter(
    "motet.api.drain_triggers",
    unit="{trigger}",
    description=(
        "Worker-job executions this API asked Cloud Run to start when it enqueued work, "
        "by the user action that caused it and whether the invoke succeeded."
    ),
)


@runtime_checkable
class DrainTrigger(Protocol):
    """Ask the worker to drain now, best-effort."""

    @property
    def enabled(self) -> bool:
        """Whether firing would actually reach Cloud Run.

        Reported on ``/internal/health``, because an inert trigger and a working one look
        identical from outside — the same reason ``login_configured`` and ``vault_ready``
        are reported.
        """
        ...

    def fire(self, reason: DrainReason) -> None:
        """Start an execution. **Never raises**, whatever happens."""
        ...


@dataclass(frozen=True)
class NullDrainTrigger:
    """The off switch: count the nudge that would have happened, and do nothing.

    This is what every laptop, every test and every environment without the invoker grant
    gets, and it is the default. It still records, so "we are enqueueing work and nothing
    is nudging" is a number rather than an absence.
    """

    @property
    def enabled(self) -> bool:
        return False

    def fire(self, reason: DrainReason) -> None:
        _drain_triggers.add(1, {"reason": reason.value, "outcome": "disabled"})
        logger.debug(
            "not triggering a drain for %s: %s is unset or unusable, so the worker runs "
            "on its schedule",
            reason.value,
            WORKER_JOB_ENV,
        )


@dataclass(frozen=True)
class WorkerJob:
    """Where the worker job lives, parsed out of its Cloud Run v2 resource name."""

    project: str
    region: str
    name: str

    @property
    def resource(self) -> str:
        return f"projects/{self.project}/locations/{self.region}/jobs/{self.name}"

    @property
    def run_url(self) -> str:
        """The regional ``:run`` endpoint — see the module docstring on the host choice."""
        return f"https://{self.region}-run.googleapis.com/v2/{self.resource}:run"


def parse_job(value: str) -> WorkerJob:
    """Read a Cloud Run v2 job resource name, or say precisely what is wrong with it.

    Strict rather than lenient: a resource name that half-parses would produce a URL that
    404s at request time, in a call whose every failure is swallowed — so the wrong shape
    would be invisible forever. Refusing it at startup puts the mistake in the log line
    an operator reads once, next to the variable's name.
    """
    parts = value.strip().strip("/").split("/")
    if len(parts) != 6 or parts[0] != "projects" or parts[2] != "locations" or parts[4] != "jobs":
        raise ConfigError(
            f"{WORKER_JOB_ENV}={value!r} is not a Cloud Run v2 job resource name. "
            "Expected projects/{project}/locations/{region}/jobs/{job}."
        )
    if not all(parts[i] for i in (1, 3, 5)):
        raise ConfigError(f"{WORKER_JOB_ENV}={value!r} has an empty project, location or job name.")
    return WorkerJob(project=parts[1], region=parts[3], name=parts[5])


class AdcAccessToken:
    """A bearer token for the API's own service account, from ambient credentials.

    The API already runs on Cloud Run as its own identity, so there is no key to mount and
    nothing to rotate: ``google.auth.default`` finds the metadata server in a deployment
    and a developer's gcloud login on a laptop.

    **The SDK is imported here, at construction, and not lazily at the first call.** That
    is the ``motet-vault[kms]`` lesson from AGENTS.md — a lazy import is a statement about
    *when*, never about *whether*, and an import that first runs inside a swallowed
    best-effort call would make a missing dependency completely silent. Failing in the
    constructor instead means :func:`build_trigger` says so at ERROR on startup and
    ``/internal/health`` reports ``drain_trigger: false``, and it is what lets
    ``bin/build-images`` put the question to a real container.

    The credential is cached and refreshed in place, so the metadata server is hit about
    once an hour rather than once per paste. The lock is because a sync FastAPI route runs
    in a threadpool, so two enqueues really can arrive at once.
    """

    def __init__(self) -> None:
        import google.auth  # noqa: PLC0415
        import google.auth.transport.requests  # noqa: PLC0415

        self._auth = google.auth
        self._request = google.auth.transport.requests.Request
        self._lock = threading.Lock()
        self._credentials: Any | None = None

    def __call__(self) -> str:
        with self._lock:
            if self._credentials is None:
                self._credentials, _ = self._auth.default(scopes=[CLOUD_PLATFORM_SCOPE])
            if not self._credentials.valid:
                self._credentials.refresh(self._request())
            token = self._credentials.token
        if not token:
            raise RuntimeError("ambient credentials produced no access token")
        return str(token)


class CloudRunJobTrigger:
    """Start a ``motet-worker`` execution over the Cloud Run v2 admin API.

    Constructed once per process. ``token`` and ``transport`` are injection points for the
    tests rather than for a second deployment shape: driving the *real* class over
    ``httpx.MockTransport`` is what makes "no request body is sent" a claim about the
    bytes this code puts on a socket, rather than about a fake's bookkeeping.
    """

    def __init__(
        self,
        job: WorkerJob,
        *,
        token: Callable[[], str] | None = None,
        transport: httpx.BaseTransport | None = None,
        timeout: float = DEFAULT_TIMEOUT_SECONDS,
    ) -> None:
        self._job = job
        self._token = token if token is not None else AdcAccessToken()
        self._client = httpx.Client(transport=transport, timeout=timeout)

    @property
    def enabled(self) -> bool:
        return True

    @property
    def job(self) -> WorkerJob:
        return self._job

    def fire(self, reason: DrainReason) -> None:
        """Ask Cloud Run to run the job once. Best-effort, and never raises.

        The work is already committed and the scheduled sweep is still there, so every
        failure below costs latency and nothing else. Requirement one of the issue this
        implements: a paste must not 500 because the drain trigger could not fire.
        """
        try:
            response = self._client.post(
                self._job.run_url,
                headers={"Authorization": f"Bearer {self._token()}"},
                # NO BODY. Not `json={}`, not an `overrides` block — see the module
                # docstring. The job's own definition carries `args = ["all"]`.
            )
        except Exception:
            # `exception`, not `error`: this is the only place a broken credential chain,
            # a DNS failure and a bug in this module can be told apart, and without a
            # traceback they arrive in GlitchTip looking identical.
            logger.exception(
                "could not ask Cloud Run to drain after %s; the work is queued and the "
                "scheduled sweep will still take it",
                reason.value,
            )
            _drain_triggers.add(1, {"reason": reason.value, "outcome": "failed"})
            return

        if response.status_code >= 400:
            logger.error(
                "Cloud Run refused to run the worker job after %s: HTTP %d %s. The work "
                "is queued and the scheduled sweep will still take it.",
                reason.value,
                response.status_code,
                response.text[:_ERROR_BODY_CHARS],
            )
            _drain_triggers.add(1, {"reason": reason.value, "outcome": "failed"})
            return

        _drain_triggers.add(1, {"reason": reason.value, "outcome": "fired"})
        logger.info("asked Cloud Run to drain the queues after %s", reason.value)


def build_trigger(env: Mapping[str, str] | None = None) -> DrainTrigger:
    """Resolve the trigger from the environment. Off unless a job is named.

    Nothing here raises. Three different states collapse to the same inert trigger, and
    only the first one is silent — the other two say so at ERROR, because a deployment
    that *meant* to nudge and cannot is a different thing from a laptop:

    * ``MOTET_WORKER_JOB`` unset — a laptop, CI, or an environment that has not opted in.
    * A value that is not a Cloud Run v2 job resource name.
    * ``google-auth`` missing from the image, which is the shape of the Gmail-connect
      outage AGENTS.md documents: an SDK behind an extra nobody depended on, discovered
      inside a request months later.
    """
    environ: Mapping[str, str] = os.environ if env is None else env
    raw = (environ.get(WORKER_JOB_ENV) or "").strip()
    if not raw:
        return NullDrainTrigger()
    try:
        job = parse_job(raw)
    except ConfigError as exc:
        logger.error("no drain will be triggered: %s", exc)
        return NullDrainTrigger()
    try:
        return CloudRunJobTrigger(job)
    except Exception:
        logger.exception(
            "no drain will be triggered: %s names %s but this process cannot build a "
            "credential for it",
            WORKER_JOB_ENV,
            job.resource,
        )
        return NullDrainTrigger()


@dataclass
class DrainNudge:
    """One request's intent to nudge, held until its transaction commits.

    **A nudge for work that has not committed is a nudge for nothing**, so the routes
    *arm* this next to the enqueue and ``motet_api.deps.connection`` fires it after
    ``conn.commit()`` — which is also what makes a request that fails on its way to the
    response fire nothing at all.

    One per request and at most one invoke, so a route that enqueued twice would still
    start one execution: a single sweep drains every queue.
    """

    trigger: DrainTrigger
    reason: DrainReason | None = None

    def arm(self, reason: DrainReason) -> None:
        self.reason = reason

    def fire(self) -> None:
        """Fire the armed nudge, if any. **Never raises**, whatever the trigger does.

        ``DrainTrigger.fire`` promises the same, and ``CloudRunJobTrigger`` keeps the
        promise — but this is the one caller that sits between a committed transaction
        and the user's response, so it does not take a Protocol's word for it. A bug in a
        trigger must cost a log line, not a 500 for a paste that has already succeeded.
        """
        reason, self.reason = self.reason, None
        if reason is None:
            return
        try:
            self.trigger.fire(reason)
        except Exception:
            logger.exception(
                "the drain trigger raised after %s; the work is committed and the "
                "scheduled sweep will still take it",
                reason.value,
            )
            _drain_triggers.add(1, {"reason": reason.value, "outcome": "failed"})
