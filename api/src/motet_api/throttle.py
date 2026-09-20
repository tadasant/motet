"""A bound on how fast this process will do work for requests that fail to authenticate.

**Read what this buys before relying on it, because the obvious reading is wrong.** It is
not what stops a bearer token being guessed — a 256-bit URL-safe secret is what stops
that, and no rate at all makes the search tractable. What this bounds is the *work* a
stranger can make this process do: a database probe per attempt, on an API whose
connection is per request.

**The bucket is the process, not the caller, and that is a correction rather than a
simplification.** The first version keyed on ``request.client.host`` — and the API is
served by ``uvicorn --forwarded-allow-ips='*'``, because Cloud Run's front end is the peer
(see the ``Dockerfile``). In that mode uvicorn rewrites ``request.client`` from the
*left-most* ``X-Forwarded-For`` entry, which is whatever the outermost caller typed. So
the key was attacker-chosen: a new value per request bought a fresh budget and the limiter
bounded nothing at all, while a value copied from somebody else spent theirs. There is no
unspoofable per-caller key available here — the right-most entry is Google's front end,
which is one value for the whole internet — so the honest shape is one counter for the
process and no key at all.

Three properties keep that from being a liability:

* **It is consulted only after a request has already failed to authenticate, and only
  when the request actually presented a bearer.** A caller holding a valid credential
  never touches it, so no amount of hammering can lock the owner, an agent, or a podcast
  client out. A request carrying *no* credential is not counted either — it costs no
  database probe, so there is nothing to bound — which is also what keeps an MCP client's
  unauthenticated discovery probe answering 401 with its RFC 9728 pointer rather than 429.
* **The worst an attacker can do to somebody else is turn their 401 into a 429**, which is
  a more informative answer to a request that was going to be refused anyway. The SPA
  treats both as "not signed in" for exactly this reason (``web/src/App.tsx``).
* **It is in-process and per-instance**, so the real budget is this one times the instance
  count, and it **fails open** on anything unexpected. A cross-instance limiter would be a
  row and a lock on the one path whose whole job is to be cheap, which is the shape
  ``motet_api.waitlist`` already declines for the same reason; it would be a new mechanism
  and would need invariant 12's session.
"""

from __future__ import annotations

import threading
import time
from collections.abc import Callable
from typing import Final

from opentelemetry import metrics

_meter = metrics.get_meter("motet.api")

#: Every refused bearer, by what the refusal was. ``throttled`` climbing is the signal
#: this module exists to produce: a queue of 401s and a queue of 429s look identical in
#: the access log, and "no errors" must never be inferred from "no data".
auth_failures = _meter.create_counter(
    "motet.api.auth_failures",
    unit="{request}",
    description="Requests that failed to authenticate against /v1, by outcome.",
)

#: How many credential-bearing failures this process will serve in a window before the
#: answer becomes a 429.
#:
#: Generous, because the bucket is the whole process and the cost of being wrong lands on
#: a real person: a browser whose session expired overnight spends five or six refusals in
#: a second, since the SPA fires several calls on boot. A scripted attempt still drops from
#: thousands a second to this, which is the whole of what this is for.
MAX_FAILURES: Final = 60

#: The window the budget is spent over, and the ``Retry-After`` a throttled caller is told.
#:
#: A **fixed** window, not a sliding one, so the honest ceiling is twice
#: :data:`MAX_FAILURES` in a span straddling a boundary — 120 refusals inside ~1.5s is
#: reachable. That is accepted rather than overlooked: a sliding window costs a deque per
#: bucket to shave a factor of two off a number whose only job is to be finite.
WINDOW_SECONDS: Final = 60


class FailureThrottle:
    """A fixed window over one counter. No key, deliberately — see the module docstring."""

    def __init__(
        self,
        *,
        max_failures: int = MAX_FAILURES,
        window_seconds: float = WINDOW_SECONDS,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._max_failures = max_failures
        self._window = window_seconds
        # Injectable only so a test can pin the arithmetic. The property worth pinning is
        # that a failure every `window + ε` never accumulates, and the only way to assert
        # it without a seam is `window_seconds=0` — which makes the reset branch fire
        # unconditionally and would pass whether the reset worked or not.
        self._clock = clock
        # Locked because FastAPI runs sync routes in a threadpool: two refusals arriving
        # together would otherwise race on the counter.
        self._lock = threading.Lock()
        self._started = clock()
        self._failures = 0

    def record_failure(self) -> bool:
        """Count one failed authentication. True when this process is now over budget.

        Called *after* the decision to refuse and only for a request that presented a
        bearer, so a caller holding a valid credential is never counted and never refused
        by this.
        """
        now = self._clock()
        with self._lock:
            if now - self._started >= self._window:
                self._started = now
                self._failures = 0
            self._failures += 1
            return self._failures > self._max_failures

    def retry_after_seconds(self) -> int:
        """What to tell a throttled caller. The whole window, rounded up."""
        return int(self._window)

    def reset(self) -> None:
        """Forget the window. For tests, and for nothing else."""
        with self._lock:
            self._started = self._clock()
            self._failures = 0


#: One throttle per process, for the same reason the object store is one per process:
#: it holds state that is only meaningful across requests.
failed_auth = FailureThrottle()
