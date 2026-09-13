"""Agentic enrichment, from the worker's side: the rule, the credentials, the seal.

```
"Ingest now" → does this item link to a site the owner added?
    no  → integrate, exactly as before
    yes → enrich job → [motet-enrich runs the agent] → integrate (enriched)
```

**The decision is a rule, not a model call** (design option A2, motet#102). The prototype
asked Haiku on every ingested item whether the item was worth fetching; the owner picked
the deterministic version instead, and the rule is the whole of it: *the newsletter carries
a link whose host belongs to a ``site`` connector the owner created*. Adding that connector
is the opt-in (option B3), so there is no separate switch to forget, nothing is fetched from
a domain the owner has not named, and the decision costs nothing and cannot be wrong in an
interesting way.

**This module holds the credentials; the service holds none of them** (option D2). It opens
the site login, each MCP token set, and the sealed browser state — all three with the
worker's own :class:`~motet_vault.KeyManager`, which invariant 8 says is the only place any
of them may be decrypted — and sends them in one request. What comes back is the article, a
redacted transcript and the browser's new cookies, and this module seals and stores those.

**The caps are two-sided, and deliberately so.** The service enforces wall clock, tool calls
and the per-item dollar figure, because only it can see a run in progress. The *per-user
rolling daily* cap is here, because only the worker can see ``enrich_runs``. Hitting it is a
recorded skip: a run that did not happen, with a row saying so, and the preview integrates.

**Enrichment never fails the item.** Every outcome short of "the article arrived" ends the
same way — keep the newsletter's preview, record the run, queue integrate — because a
briefing made from a preview is better than no briefing. That is also why a failed
``enrich`` job that exhausts its retries has a failure recorder that does exactly this.
"""

from __future__ import annotations

import json
import logging
import os
import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Final, Protocol
from urllib.parse import urlsplit

import psycopg
from motet_db import SourceItemState, repo
from motet_db import connectors as connectors_repo
from motet_db import enrichment as enrichment_repo
from motet_db.enrichment import EnrichStatus, RunStatus
from motet_enrich.contract import (
    EnrichRequest,
    EnrichResult,
    McpServer,
    RunCaps,
    SiteCredential,
)
from motet_vault import KeyManager, VaultError, build_key_manager
from opentelemetry import metrics

from .queues import Queue

logger = logging.getLogger("motet.worker.enrich")

_meter = metrics.get_meter("motet.worker")
_runs = _meter.create_counter(
    "motet.enrich.runs",
    unit="{run}",
    description=(
        "Enrichment outcomes as they reached a source item, by outcome and site domain: "
        "`ok`, `blocked`, `capped`, `timeout`, `failed`, or `skipped` (a cap or a missing "
        "connector declined to spend). Counts what the pipeline *did*, where "
        "`motet.enrich.service_runs` counts what the service ran — a run whose answer never "
        "came back appears there and not here, which is the pair worth reading together."
    ),
)
_cost = _meter.create_counter(
    "motet.enrich.cost_usd",
    unit="USD",
    description=(
        "What enrichment cost, by outcome and site domain, as recorded against the item. "
        "`enrich_runs.cost_usd` is the per-item copy and is what the rolling daily cap is "
        "summed from; this is the fleet view."
    ),
)

#: How the payload says "the agent has already had its turn with this item".
#:
#: A replayed ``integrate`` job carrying it must not queue a second agent run, and the
#: source item's own ``enrich_status`` is the second guard — belt and braces on the one
#: decision in this pipeline that can spend half a dollar by accident.
ENRICHED_KEY: Final = "enriched"

#: Below this many characters an ``ok`` answer is not an article. A paywall stub, a cookie
#: banner and a "subscribe to continue" page all come back as a page with text on it, and
#: the failure mode worth avoiding is replacing a 1,400-character newsletter with 200
#: characters of consent notice.
MIN_ARTICLE_CHARS: Final = 400

#: Enrichment states that mean this item has already been through the stage.
#:
#: ``running`` is deliberately **not** here — it gets its own arm in ``handle_enrich``,
#: because it is the one state that is durable *before* the money is spent and it has to be
#: recorded as a failure rather than passed over silently.
FINISHED_STATES: Final = ("done", "failed", "skipped")

#: At most this many candidate links go to the agent. They are what the navigation lock is
#: built from, so an unbounded list is an unbounded allowlist; and a newsletter with twenty
#: links to one publisher is a digest, where the first few are the lead stories.
MAX_CANDIDATE_URLS: Final = 5

ENABLED_ENV: Final = "MOTET_ENRICH"
SERVICE_URL_ENV: Final = "MOTET_ENRICH_SERVICE_URL"
SERVICE_TOKEN_ENV: Final = "MOTET_ENRICH_SERVICE_TOKEN"
MAX_USD_PER_ITEM_ENV: Final = "MOTET_ENRICH_MAX_USD_PER_ITEM"
MAX_USD_PER_DAY_ENV: Final = "MOTET_ENRICH_MAX_USD_PER_DAY"
MAX_TOOL_CALLS_ENV: Final = "MOTET_ENRICH_MAX_TOOL_CALLS"
TIMEOUT_ENV: Final = "MOTET_ENRICH_TIMEOUT_SECONDS"

DEFAULT_MAX_USD_PER_ITEM: Final = 0.50
DEFAULT_MAX_USD_PER_DAY: Final = 5.00
DEFAULT_MAX_TOOL_CALLS: Final = 40
DEFAULT_TIMEOUT_SECONDS: Final = 600

#: How long to wait on the service. The run's own cap plus room for the round trip and the
#: agent's shutdown — Cloud Run's request timeout for this service is 900 s, so this must
#: sit between the run cap and that.
_HTTP_MARGIN_SECONDS: Final = 120


# --- configuration -------------------------------------------------------------------


@dataclass(frozen=True)
class EnrichConfig:
    """Whether this deployment enriches, where, and within what."""

    enabled: bool
    service_url: str
    service_token: str
    max_usd_per_item: float
    max_usd_per_day: float
    max_tool_calls: int
    timeout_seconds: int

    @property
    def caps(self) -> RunCaps:
        return RunCaps(
            max_usd=self.max_usd_per_item,
            max_tool_calls=self.max_tool_calls,
            timeout_seconds=self.timeout_seconds,
        )

    @property
    def usable(self) -> bool:
        """Enabled *and* pointed somewhere — what a **worker** needs to run anything.

        Deliberately **not** what the routing decision asks. The API decides whether an
        "Ingest now" goes to the ``enrich`` queue and never calls the service, so requiring
        the service's address there would put a fact about the private estate into the
        internet-facing service's configuration for nothing — and the infra issue's env list
        gives the API ``MOTET_ENRICH`` alone, correctly. :func:`plan_enrichment` therefore
        gates on :attr:`enabled`; this gates :func:`build_enrich_client`.

        The two can disagree, and the disagreement is safe in both directions: a worker with
        no URL records every queued item as ``skipped`` and integrates it on its preview,
        and an API with the switch off simply never queues one.
        """
        return self.enabled and bool(self.service_url)


def load_config(env: Mapping[str, str] | None = None) -> EnrichConfig:
    """Read the switch and the caps.

    Off by default, like the drain trigger and the scheduler drain before it: enrichment
    spends money per ingested item, so a deployment that names nothing must do nothing.
    The rollout the infra issue describes turns it on in staging first and leaves
    production unset until a human flips it.
    """
    environ = os.environ if env is None else env
    raw = environ.get(ENABLED_ENV, "").strip().lower()
    return EnrichConfig(
        enabled=raw in ("1", "on", "true", "yes"),
        service_url=environ.get(SERVICE_URL_ENV, "").strip().rstrip("/"),
        service_token=environ.get(SERVICE_TOKEN_ENV, "").strip(),
        max_usd_per_item=_number(environ, MAX_USD_PER_ITEM_ENV, DEFAULT_MAX_USD_PER_ITEM),
        max_usd_per_day=_number(environ, MAX_USD_PER_DAY_ENV, DEFAULT_MAX_USD_PER_DAY),
        max_tool_calls=int(_number(environ, MAX_TOOL_CALLS_ENV, DEFAULT_MAX_TOOL_CALLS)),
        timeout_seconds=int(_number(environ, TIMEOUT_ENV, DEFAULT_TIMEOUT_SECONDS)),
    )


def _number(environ: Mapping[str, str], name: str, default: float) -> float:
    """A positive number, or the default — never a crash and never a zero.

    A nonsense value here must not stop the pipeline: the caps are a bound on spending, and
    falling back to the documented default with a warning keeps them bounded, where raising
    would take ingestion down over a typo in an optional variable.
    """
    raw = environ.get(name, "").strip()
    if not raw:
        return default
    try:
        value = float(raw)
    except ValueError:
        value = 0.0
    if value <= 0:
        logger.error("%s=%r is not a positive number; using the default %s", name, raw, default)
        return default
    return value


# --- the rule ------------------------------------------------------------------------


@dataclass(frozen=True)
class EnrichTarget:
    """Which site an item is to be enriched from, and which of its links to start on."""

    connector_id: str
    domain: str
    urls: tuple[str, ...]

    @property
    def article_url(self) -> str:
        return self.urls[0]


def source_item_links(
    conn: psycopg.Connection[Any], item_ids: Sequence[str]
) -> dict[str, list[str]]:
    """The links each of these items carried, in one query. See :func:`plan_enrichment`."""
    return enrichment_repo.source_item_links_for(conn, item_ids)


def enrichment_sites(
    conn: psycopg.Connection[Any], *, user_id: str, config: EnrichConfig
) -> list[connectors_repo.StoredConnector]:
    """This user's ``site`` connectors, which are the allowlist (option B3).

    Read **once** per "ingest now" rather than once per item: the SPA's select-all sends up
    to ``repo.HELD_MAX_ITEMS`` ids in one request, and a connector query per id would be
    five hundred round trips inside one transaction to answer the same question five hundred
    times. Empty — and an empty list where enrichment is off — means nothing is enriched.
    """
    if not config.enabled:
        return []
    return [
        connector
        for connector in connectors_repo.list_connectors(conn, user_id)
        if connector.kind == connectors_repo.SITE
        and connector.domain
        and connector.status == "ready"
    ]


def plan_enrichment(
    conn: psycopg.Connection[Any],
    *,
    user_id: str,
    item_id: str,
    config: EnrichConfig,
    sites: Sequence[connectors_repo.StoredConnector] | None = None,
    links: Sequence[str] | None = None,
) -> EnrichTarget | None:
    """The rule: does this item link to a site the owner has added?

    Returns the site and every link on it, in the order the newsletter carried them, capped
    at :data:`MAX_CANDIDATE_URLS`. ``None`` means integrate as before — which is the answer
    for an item with no links, an item whose links are all elsewhere, and every item at all
    where this deployment has enrichment switched off.

    **The first matching site wins when links reach two of them**, by that site's earliest
    link. A newsletter that links to two publishers the owner subscribes to is one article
    per item as far as the pipeline is concerned, and the first link is the lead story.

    **A tracking link on an unrelated host is invisible to this rule**, and that is a stated
    limitation rather than an oversight. A SendGrid wrapper on the publisher's own domain
    (``url3396.example.com``) matches, because it is a subdomain; a generic
    ``ct.sendgrid.net`` wrapper does not, because the only way to know where it lands is to
    follow it — which is a fetch, and a fetch from a domain the owner never named is exactly
    what option B3 says must not happen.

    Gated on :attr:`EnrichConfig.enabled` rather than ``usable``: the API decides this and
    is deliberately not told the enrichment service's address (see ``usable``).

    ``sites`` and ``links`` are both "the caller already read this" seams, for the same
    reason: "Ingest now" decides for up to :data:`motet_db.repo.HELD_MAX_ITEMS` items in one
    transaction holding that user's advisory lock, and a per-item read of either would be
    hundreds of round trips inside it. Passing neither is correct and is what a single-item
    caller does.
    """
    if not config.enabled:
        return None
    known = (
        list(sites) if sites is not None else enrichment_sites(conn, user_id=user_id, config=config)
    )
    if not known:
        return None
    carried = list(links) if links is not None else enrichment_repo.source_item_links(conn, item_id)
    if not carried:
        return None
    for link in carried:
        host = _host_of(link)
        if host is None:
            continue
        for site in known:
            assert site.domain is not None
            if connectors_repo.domain_matches(host, site.domain):
                matching = tuple(
                    candidate
                    for candidate in carried
                    if (candidate_host := _host_of(candidate)) is not None
                    and connectors_repo.domain_matches(candidate_host, site.domain)
                )[:MAX_CANDIDATE_URLS]
                # `matching` always holds at least `link`, which is the link that
                # matched: this is the same predicate over the same list.
                return EnrichTarget(connector_id=site.id, domain=site.domain, urls=matching)
    return None


def site_still_allowed(
    conn: psycopg.Connection[Any], *, user_id: str, domain: str, config: EnrichConfig
) -> bool:
    """Whether this account still has a ready ``site`` row for ``domain``.

    The payload carries the domain the enqueue decided on, and a job can sit in the queue
    across a deletion or a status change. Asking again is what makes "adding the site is the
    opt-in" true of the *fetch* rather than only of the decision to queue one.
    """
    return any(
        site.domain and connectors_repo.domain_matches(domain, site.domain)
        for site in enrichment_sites(conn, user_id=user_id, config=config)
    )


def _host_of(url: str) -> str | None:
    try:
        host = urlsplit(url).hostname
    except ValueError:
        return None
    return host.lower() if host else None


# --- the seam to the service ---------------------------------------------------------


class EnrichClient(Protocol):
    """Ask ``motet-enrich`` to run one enrichment.

    A Protocol with a fake behind it, exactly like every other vendor seam in this repo
    (invariant 7) — except that what is on the other side is ours. The reason it is a seam
    anyway is the same reason: no test may start a browser or spend a cent, and the whole
    of ``handle_enrich``'s interesting behaviour is what it does with an answer.
    """

    def enrich(self, request: EnrichRequest, caps: RunCaps) -> EnrichResult: ...


class FakeEnrichClient:
    """The in-process fake: runs :class:`motet_enrich.FakeRunner` and records the requests.

    Recording them is what lets a test assert the property that matters most on this seam —
    that a credential reached the agent, and that the transcript it came back with does not
    carry it.
    """

    def __init__(self, runner: Any | None = None) -> None:
        from motet_enrich.runner import FakeRunner  # noqa: PLC0415

        self.runner = runner if runner is not None else FakeRunner()
        self.requests: list[EnrichRequest] = []

    def enrich(self, request: EnrichRequest, caps: RunCaps) -> EnrichResult:
        self.requests.append(request)
        result: EnrichResult = self.runner.run(request, caps)
        return result


class HttpEnrichClient:
    """The real client: one POST, signed twice.

    ``Authorization`` carries the shared bearer this repo's own service checks;
    ``X-Serverless-Authorization`` carries a Google ID token whose audience is the service
    URL, which Cloud Run's frontend consumes before the request reaches the container. Two
    doors, and the outer one is the IAM grant the private repo makes to the worker's service
    account alone.

    **The ID token is minted at construction, not lazily**, for the ``motet-vault[kms]``
    reason AGENTS.md gives: a missing ``google-auth`` discovered inside the first enrichment
    is a failed item, where here it is a startup error and a switch that reads as off.
    """

    def __init__(
        self, config: EnrichConfig, token: Any | None = None, transport: Any = None
    ) -> None:
        import httpx  # noqa: PLC0415

        self._config = config
        self._httpx = httpx
        self._transport = transport
        self._token = token if token is not None else _AdcIdToken(config.service_url)

    def enrich(self, request: EnrichRequest, caps: RunCaps) -> EnrichResult:
        body = request.model_copy(update={"caps": caps})
        headers = {"Content-Type": "application/json"}
        if self._config.service_token:
            headers["Authorization"] = f"Bearer {self._config.service_token}"
        identity = self._token()
        if identity:
            headers["X-Serverless-Authorization"] = f"Bearer {identity}"
        with self._httpx.Client(
            timeout=caps.timeout_seconds + _HTTP_MARGIN_SECONDS, transport=self._transport
        ) as client:
            response = client.post(
                f"{self._config.service_url}/v1/enrich",
                content=body.model_dump_json(),
                headers=headers,
            )
            response.raise_for_status()
            return EnrichResult.model_validate(response.json())


class _AdcIdToken:
    """A Google ID token for the enrichment service's audience, from ambient credentials.

    Cached and refreshed in place, so the metadata server is asked about once an hour rather
    than once per enrichment. Returns ``""`` off a deployment — a laptop with no ambient
    credentials — because there the outer door does not exist and the shared bearer is the
    whole check.
    """

    def __init__(self, audience: str) -> None:
        import google.auth.transport.requests  # noqa: PLC0415
        import google.oauth2.id_token  # noqa: PLC0415

        self._audience = audience
        self._request = google.auth.transport.requests.Request
        self._fetch = google.oauth2.id_token.fetch_id_token
        self._token = ""
        self._expires = 0.0

    def __call__(self) -> str:
        if self._token and time.monotonic() < self._expires:
            return self._token
        try:
            self._token = str(self._fetch(self._request(), self._audience))  # type: ignore[no-untyped-call]
            self._expires = time.monotonic() + 1800
        except Exception as exc:  # noqa: BLE001 — no ADC on a laptop is not a failure here
            logger.warning("no ambient identity for %s: %s", self._audience, exc)
            self._token = ""
            self._expires = time.monotonic() + 60
        return self._token


def build_enrich_client(config: EnrichConfig) -> EnrichClient | None:
    """The real client where this deployment is configured for one, else ``None``.

    ``None`` rather than a fake, because "enrichment is off" and "enrichment runs against a
    fake" are different states and only one of them should ever reach a deployed
    environment. A worker with no client queues integrate directly, which is what every
    deployment did before this shipped.
    """
    if not config.usable:
        return None
    try:
        return HttpEnrichClient(config)
    except Exception:
        logger.exception(
            "%s is on but the enrichment client could not be built; items will integrate "
            "on their previews",
            ENABLED_ENV,
        )
        return None


# --- the handler ---------------------------------------------------------------------


def handle_enrich(context: Any, payload: Mapping[str, Any]) -> None:
    """Fetch the article behind one newsletter, then hand the item on to integrate.

    Runs under the user's serialization key, which is what gives invariant 6 its second
    meaning here: one browser session per user at a time, so two runs cannot both be
    writing that user's storage state for one domain. **The cost is throughput**, and it is
    accepted rather than overlooked: ten items that each need an article are ten runs in
    series, at 50–250 seconds each, and this user's other integrate jobs wait behind them.
    Well inside ``MAX_LEASE_EXTENSION_SECONDS``, so the lease keeper covers it (motet#53).

    **Integrate is queued on every path that leaves the item waiting**, including the ones
    that raise their way out through the failure recorder. An item that reached this stage is
    an item the owner asked for; the article is an improvement on the preview and never a
    precondition for it. The two exceptions are the two where the item is not waiting: one
    that has already moved on (integrated or dismissed — something else queued it), and one
    that no longer exists.
    """
    item_id = _require(payload, "source_item_id")
    stored = repo.get_source_item(context.conn, item_id)
    if stored is None:
        raise _permanent(f"source item {item_id} no longer exists")
    if stored.state is not SourceItemState.PENDING:
        # Integrated or dismissed. Either way the decision has moved on without us, and
        # spending half a dollar to catch up is the one thing not to do.
        logger.info("source item %s is no longer pending; not enriching it", item_id)
        return

    state = enrichment_repo.enrichment_state(context.conn, item_id)
    if state is not None and state.status in FINISHED_STATES:
        # A replay: the work committed and the job's completion did not. The work fence
        # catches this first in the ordinary case; this is the guard for the case where the
        # enqueue itself was duplicated.
        logger.info(
            "source item %s has already been through enrichment; queueing integrate", item_id
        )
        _queue_integration(context.conn, stored.user_id, item_id, payload)
        return
    if state is not None and state.status == "running":
        # **The one case that costs money to get wrong, and the one the work fence cannot
        # see.** `running` is written on a side connection *before* the agent starts, so it
        # is durable while everything that records the cost is still inside this handler's
        # uncommitted transaction. A worker killed mid-run — a deploy, an OOM, a task
        # timeout — therefore leaves `running` with no `enrich_runs` row: the lease goes
        # stale, the row is reclaimed, `work_committed_attempt` is NULL, and without this
        # the agent runs again with the *daily cap seeing nothing spent*. Five attempts of
        # a $0.50 cap is $2.50 on one item, invisibly.
        #
        # So a second attempt never runs the agent. The preview integrates, which is what
        # every other unhappy path here does anyway.
        message = "a previous run was interrupted; not starting a second one for this item"
        logger.warning("source item %s: %s", item_id, message)
        # `failed`, not `skipped`: the agent tried and something killed it, which is not the
        # same thing as the budget saying no — and `_record_skip`'s docstring is the reason
        # those two words are kept apart.
        _record_skip(context.conn, stored, str(state.domain or "unknown"), message, status="failed")
        _finish(context, stored, "failed", message)
        _queue_integration(context.conn, stored.user_id, item_id, payload)
        return

    domain = str(payload.get("domain") or (state.domain if state else "") or "")
    config = load_config()
    client = getattr(context, "enrich_client", None) or build_enrich_client(config)
    if client is None or not domain:
        # Two different faults, named apart: this deployment has no service to call, or the
        # job row does not say which site it is for. Both end the same way, and both leave a
        # row — a skip that recorded nothing would make `motet.enrich.runs` a series that
        # exists only when something is wrong, which cannot tell "nothing happened" from
        # "nothing is running".
        message = (
            "enrichment is not configured on this worker"
            if client is None
            else "the enrich job names no site domain"
        )
        _record_skip(context.conn, stored, domain or "unknown", message)
        _finish(context, stored, "skipped", message)
        _queue_integration(context.conn, stored.user_id, item_id, payload)
        return

    if not site_still_allowed(context.conn, user_id=stored.user_id, domain=domain, config=config):
        # Option B3 re-checked at run time, not only at enqueue time. A site row is the
        # permission, and a job can sit in the queue across a deletion — so an owner who
        # removes a site between "Ingest now" and the run has removed it, rather than
        # having removed it for everything except the jobs already written.
        message = f"{domain} is no longer a site this account has added"
        logger.warning("source item %s: %s", item_id, message)
        _record_skip(context.conn, stored, domain, message)
        _finish(context, stored, "skipped", message)
        _queue_integration(context.conn, stored.user_id, item_id, payload)
        return

    spent = enrichment_repo.spend_since(context.conn, stored.user_id)
    if spent >= config.max_usd_per_day:
        # A recorded skip, not a failure and not a retry: the cap will still be spent in
        # ten minutes, and the preview is what the briefing gets. Design option C2.
        message = (
            f"the rolling 24h enrichment cap is spent (${spent:.2f} of "
            f"${config.max_usd_per_day:.2f})"
        )
        logger.warning("source item %s: %s", item_id, message)
        _record_skip(context.conn, stored, domain, message)
        _finish(context, stored, "skipped", message)
        _queue_integration(context.conn, stored.user_id, item_id, payload)
        return

    request = _build_request(context, stored, payload, domain, config)
    started = datetime.now(UTC)
    if not _announce_running(context, item_id):
        # The announce is the replay guard, not a status light — see `_announce_running`.
        # Without it landing, a worker killed mid-run would have nothing saying an agent had
        # been started, and the next claim would start another one.
        message = "could not record that a run had started, so no run was started"
        _record_skip(context.conn, stored, domain, message, status="failed")
        _finish(context, stored, "failed", message)
        _queue_integration(context.conn, stored.user_id, item_id, payload)
        return
    try:
        result = client.enrich(request, config.caps)
    except Exception as exc:  # noqa: BLE001 — see below; this must not reach the ladder
        # **Deliberately not retried**, which is the opposite of what a transport error
        # usually deserves. The call may have *already started an agent run*: the client's
        # timeout is shorter than the service's own request timeout, so a read timeout is
        # more likely to mean "the run is still going" than "nothing happened". Putting
        # that on the retry ladder buys up to five more billed runs for one item, and none
        # of them would be visible to the daily cap, because a run whose answer never came
        # back writes no `enrich_runs` row. One attempt, then the preview.
        message = f"the enrichment service did not answer: {type(exc).__name__}: {exc}"
        logger.exception("source item %s: the enrichment call failed", item_id)
        _record_skip(context.conn, stored, domain, message, status="failed")
        _finish(context, stored, "failed", message)
        _queue_integration(context.conn, stored.user_id, item_id, payload)
        return
    _apply(context, stored, domain, result, started)
    _queue_integration(context.conn, stored.user_id, item_id, payload)


def _build_request(
    context: Any, stored: Any, payload: Mapping[str, Any], domain: str, config: EnrichConfig
) -> EnrichRequest:
    """Open exactly the credentials this run needs, and nothing else.

    The order is deliberate: the site first, because without it there is nothing to log in
    as; then the MCP servers the owner scoped to this domain (or to every domain); then the
    browser state. Every one of them is opened with the worker's ``KeyManager``, which is
    the decrypt half invariant 8 scopes to this service account.
    """
    manager = _key_manager(context)
    urls = [
        str(url).strip()
        for url in payload.get("candidate_urls", [])
        if isinstance(url, str) and str(url).strip()
    ]
    if not urls:
        # The payload is written by `enqueue_enrichment` from a target that always has one,
        # so an empty list is a malformed row rather than a run that could go ahead: there
        # is nothing to open. Permanent, because retrying will find the same payload.
        raise _permanent(f"enrich job for {stored.id} names no candidate URL: {payload!r}")
    site = _site_credential(context.conn, manager, payload, domain, user_id=stored.user_id)
    return EnrichRequest(
        item_id=stored.id,
        title=stored.title,
        preview_text=(stored.text or "")[:4_000],
        candidate_urls=urls,
        site=site,
        mcp_servers=_mcp_servers(context.conn, manager, user_id=stored.user_id, domain=domain),
        browser_state=_browser_state(context.conn, manager, user_id=stored.user_id, domain=domain),
        caps=config.caps,
    )


def _site_credential(
    conn: psycopg.Connection[Any],
    manager: KeyManager,
    payload: Mapping[str, Any],
    domain: str,
    *,
    user_id: str,
) -> SiteCredential:
    """The one site login. A site with no password is a legal, ordinary row.

    A connector that no longer exists — deleted between the enqueue and the run — yields a
    credential-free one, and :func:`site_still_allowed` is what stops the run instead. The
    two are apart because "no password" and "no permission" are different answers.
    """
    connector_id = str(payload.get("connector_id") or "")
    if not connector_id:
        return SiteCredential(domain=domain)
    connector = connectors_repo.get_connector(conn, connector_id, user_id=user_id)
    if connector is None:
        return SiteCredential(domain=domain)
    password: str | None = None
    if connector.has_secret:
        try:
            password = connectors_repo.load_connector_secret(
                conn, manager, connector_id=connector_id
            )
        except VaultError:
            # A site whose password will not open is a site to try without one: many need
            # no password at all, and failing the run here would lose a fetch that might
            # have worked. Logged rather than swallowed silently.
            logger.exception("could not open the site password for connector %s", connector_id)
    return SiteCredential(domain=domain, username=connector.username, password=password)


def _mcp_servers(
    conn: psycopg.Connection[Any], manager: KeyManager, *, user_id: str, domain: str
) -> list[McpServer]:
    """Every ready MCP connector this domain may use, with a bearer good for the run.

    ``domains`` empty means "every site the owner has added" — never an arbitrary one,
    because nothing is fetched anywhere else. A server scoped to a list is handed over only
    for the sites on it.

    **A token that will not open, or will not refresh, drops the server rather than failing
    the run.** The mailbox is how a magic-link site is logged into, so losing it costs the
    login path — but a site whose ``eu=`` link reads without one still works, and the spike
    found that to be the common case.
    """
    servers: list[McpServer] = []
    for connector in connectors_repo.list_connectors(conn, user_id):
        if connector.kind != connectors_repo.MCP or connector.status != "ready":
            continue
        if connector.domains and not any(
            connectors_repo.domain_matches(domain, scoped) for scoped in connector.domains
        ):
            continue
        try:
            token = _mcp_access_token(conn, manager, connector_id=connector.id)
        except (VaultError, ValueError, KeyError):
            logger.exception("could not open the token set for MCP connector %s", connector.id)
            continue
        if not token or not connector.url:
            continue
        servers.append(
            McpServer(name=_server_slug(connector.id), url=connector.url, access_token=token)
        )
    return servers


def _mcp_access_token(
    conn: psycopg.Connection[Any], manager: KeyManager, *, connector_id: str
) -> str | None:
    """The connector's access token, as stored.

    **Refreshing is deliberately not done here**, and the reason is worth stating: a refresh
    is an HTTP round trip to the server's token endpoint *and* a re-seal, so it belongs with
    the OAuth client in ``motet_sources.mcp_oauth`` rather than inside a job handler. Until
    that is wired, a connector whose access token has expired hands the agent a bearer the
    server will refuse, which shows up as the mailbox tool failing and the run reporting
    ``blocked`` — recoverable by re-authorizing on the Credentials screen. The alternative,
    refreshing from here, would put a vendor call inside the transaction that holds this
    user's serialization lock.
    """
    sealed = connectors_repo.load_connector_secret(conn, manager, connector_id=connector_id)
    if sealed is None:
        return None
    try:
        token_set = json.loads(sealed)
    except json.JSONDecodeError:
        # An `mcp` secret is a JSON token set by construction. Anything else is a row
        # written by something that is not this system.
        return None
    access = token_set.get("access_token") if isinstance(token_set, dict) else None
    return str(access) if access else None


def _browser_state(
    conn: psycopg.Connection[Any], manager: KeyManager, *, user_id: str, domain: str
) -> str | None:
    """Last run's cookies for this domain, or ``None`` for a fresh browser.

    A state that will not open is a fresh browser, not a failed run: the agent logs in
    again, and the run that follows replaces the unreadable row.
    """
    try:
        return enrichment_repo.load_browser_state(conn, manager, user_id=user_id, domain=domain)
    except VaultError:
        logger.exception("could not open the saved browser state for %s", domain)
        return None


# --- recording what happened ---------------------------------------------------------


def _apply(context: Any, stored: Any, domain: str, result: EnrichResult, started: datetime) -> None:
    """Store the article if there is one, the run always, and the cookies if any came back."""
    article = (result.article_markdown or "").strip()
    ok = result.status == "ok" and len(article) >= MIN_ARTICLE_CHARS
    if result.status == "ok" and not ok:
        logger.warning(
            "source item %s: the agent reported ok with %d characters, below the %d minimum "
            "— keeping the preview",
            stored.id,
            len(article),
            MIN_ARTICLE_CHARS,
        )
    if ok:
        enrichment_repo.apply_enriched_article(
            context.conn,
            stored.id,
            article_url=result.article_url or "",
            article=article,
        )
    else:
        _finish(context, stored, "failed", result.error or "the article was not retrieved")

    _record(context.conn, stored, domain, result, started=started, effective_ok=ok)
    _store_browser_state(context, stored.user_id, domain, result)
    logger.info(
        "enrich %s on %s: status=%s calls=%d cost=$%.4f login=%s chars=%d in %.1fs",
        stored.id,
        domain,
        result.status if ok or result.status != "ok" else "stub",
        result.tool_calls,
        result.cost_usd,
        result.login_performed,
        len(article),
        result.duration_seconds,
    )


def _record(
    conn: psycopg.Connection[Any],
    stored: Any,
    domain: str,
    result: EnrichResult,
    *,
    started: datetime | None = None,
    effective_ok: bool | None = None,
) -> None:
    """Append the run row and count it.

    ``effective_ok`` is how a stub is recorded: the service said ``ok``, the article was too
    short to use, and the row has to say ``failed`` or the daily spend view would report a
    success that produced nothing.
    """
    outcome = result.status
    if effective_ok is False and outcome == "ok":
        outcome = "failed"
    enrichment_repo.record_enrich_run(
        conn,
        source_item_id=stored.id,
        user_id=stored.user_id,
        domain=domain,
        status=outcome,
        tool_calls=result.tool_calls,
        cost_usd=result.cost_usd,
        article_chars=len(result.article_markdown or ""),
        login_performed=result.login_performed,
        transcript=[entry.model_dump(exclude_none=True) for entry in result.transcript],
        error=result.error,
        started_at=started,
    )
    attributes = {"outcome": outcome, "domain": domain}
    _runs.add(1, attributes)
    _cost.add(result.cost_usd, attributes)


def _store_browser_state(context: Any, user_id: str, domain: str, result: EnrichResult) -> None:
    """Seal the cookies the run left, on every outcome that produced any.

    Including a timeout: the harness writes the storage state after every browser call, so a
    run that logged in and then ran out of clock has bought a login the next run can use.
    Sealing is the *wrapper* half of the vault, which the worker also holds.
    """
    if not result.browser_state:
        return
    try:
        enrichment_repo.store_browser_state(
            context.conn,
            # `KeyManager` extends `DekWrapper`, so the worker's one object is both halves.
            # The *split* invariant 8 rests on is between processes — the API holds only a
            # wrapper — not between two objects here.
            _key_manager(context),
            user_id=user_id,
            domain=domain,
            state=result.browser_state,
            cookies=_count_cookies(result.browser_state),
        )
    except VaultError:
        # A session that could not be sealed costs the next run a login. Never the item.
        logger.exception("could not seal the browser state for %s", domain)


def _count_cookies(state: str) -> int:
    try:
        parsed = json.loads(state)
    except json.JSONDecodeError:
        return 0
    cookies = parsed.get("cookies") if isinstance(parsed, dict) else None
    return len(cookies) if isinstance(cookies, list) else 0


def _finish(context: Any, stored: Any, status: EnrichStatus, error: str | None) -> None:
    enrichment_repo.mark_enrichment_finished(context.conn, stored.id, status=status, error=error)


def _record_skip(
    conn: psycopg.Connection[Any],
    stored: Any,
    domain: str,
    message: str,
    *,
    status: RunStatus = "skipped",
) -> None:
    """A run that produced no answer still gets a row, with what it was.

    ``skipped`` and ``failed`` are kept apart because a person reading the spend view has to
    be able to tell "the budget said no" from "the agent tried and could not" — only one of
    them is worth changing a prompt over. Either way the row exists, so a run whose cost
    nobody could measure is at least visible as a run.
    """
    enrichment_repo.record_enrich_run(
        conn,
        source_item_id=stored.id,
        user_id=stored.user_id,
        domain=domain,
        status=status,
        error=message,
    )
    _runs.add(1, {"outcome": status, "domain": domain})


def _announce_running(context: Any, item_id: str) -> bool:
    """Say the run has started, on a connection of this function's own. **Not best effort.**

    The handler's transaction stays open for as long as the agent runs, so a write on it is
    invisible for the ten minutes somebody is most likely to be watching — which is why this
    goes out on a side connection. But that write is doing a second, much larger job: it is
    the **only** durable record that an agent was started for this item, because everything
    that records a run's cost commits with the handler. ``handle_enrich``'s ``running`` arm
    is what reads it back, and a killed worker whose flag never landed is an item that runs
    the agent again with the daily cap seeing nothing spent.

    So a failed announce returns ``False`` and the caller does **not** start the agent. Half
    a dollar unspent is the cheap side of that trade; the alternative is spending it up to
    five times over, invisibly.

    The URL comes off the context rather than out of ``DATABASE_URL``, so a ``drain()``
    called with an explicit one cannot write the flag to a different database and leave the
    guard quietly absent.
    """
    announce = getattr(context, "announce_running", None)
    if announce is not None:
        announce(item_id)
        return True
    database_url = getattr(context, "database_url", "") or os.environ.get("DATABASE_URL", "")
    if not database_url:
        logger.error("no database URL to mark %s as enriching; not starting the agent", item_id)
        return False
    try:
        with psycopg.connect(database_url, autocommit=True) as side:
            enrichment_repo.mark_enrichment_running(side, item_id)
    except psycopg.Error:
        logger.exception("could not mark %s as enriching; not starting the agent", item_id)
        return False
    return True


def _queue_integration(
    conn: psycopg.Connection[Any], user_id: str, item_id: str, payload: Mapping[str, Any]
) -> None:
    """Hand the item on to dedup, carrying whatever the original enqueue decided.

    ``labels.DELIBERATE_KEY`` rides through untouched, because the label write-back keys on
    it and an item that went through enrichment was still a deliberate ingest — dropping it
    here would silently stop a mailbox's `Newsletters → Completed` move for exactly the
    items the owner cared most about.
    """
    from .handlers import enqueue_integrate_job  # noqa: PLC0415

    enqueue_integrate_job(conn, user_id=user_id, item_id=item_id, payload=payload, enriched=True)


def record_enrich_failure(
    conn: psycopg.Connection[Any], payload: Mapping[str, Any], message: str
) -> None:
    """The failure recorder: a run that exhausted its retries still integrates.

    Written here rather than by the handler because the handler's transaction is the one
    that rolled back. It does what every other path out of enrichment does — record the
    outcome, keep the preview, queue integrate — so an agent that cannot be reached at all
    costs the article and never the item.
    """
    item_id = payload.get("source_item_id")
    if not isinstance(item_id, str) or not item_id:
        return
    stored = repo.get_source_item(conn, item_id)
    if stored is None or stored.state is not SourceItemState.PENDING:
        return
    enrichment_repo.mark_enrichment_finished(conn, item_id, status="failed", error=message)
    domain = str(payload.get("domain") or "unknown")
    _runs.add(1, {"outcome": "failed", "domain": domain})
    enrichment_repo.record_enrich_run(
        conn,
        source_item_id=item_id,
        user_id=stored.user_id,
        domain=domain,
        status="failed",
        error=message,
    )
    _queue_integration(conn, stored.user_id, item_id, payload)


# --- small shared helpers ------------------------------------------------------------


def _server_slug(connector_id: str) -> str:
    """A tool namespace from a connector id: ``cn_9f2a…`` → ``cn-9f2a…``.

    The id rather than the owner's label, because a label is free text and a tool namespace
    is matched against :data:`motet_enrich.redact.BROWSER_SERVER` — a connector labelled
    "browser" must not be able to buy itself transcript retention.
    """
    return connector_id.replace("_", "-")[:32]


def _key_manager(context: Any) -> KeyManager:
    manager = getattr(context, "key_manager", None)
    return manager if manager is not None else build_key_manager()


def _require(payload: Mapping[str, Any], key: str) -> str:
    value = payload.get(key)
    if not isinstance(value, str) or not value:
        raise _permanent(f"job payload is missing a usable {key!r}: {payload!r}")
    return value


def _permanent(message: str) -> Exception:
    from .handlers import PermanentFailure  # noqa: PLC0415

    return PermanentFailure(message)


def enrich_payload(item_id: str, target: EnrichTarget, extra: Mapping[str, Any]) -> dict[str, Any]:
    """The job payload one enrichment needs — and what it deliberately does not carry.

    No credential and no token: the handler opens those at run time, from rows, so a job
    sitting in the queue for an hour holds nothing worth stealing and a rotated credential
    is picked up by the run rather than pinned at enqueue time.
    """
    payload = {key: value for key, value in extra.items() if key != "source_item_id"}
    payload.update(
        {
            "source_item_id": item_id,
            "connector_id": target.connector_id,
            "domain": target.domain,
            "article_url": target.article_url,
            "candidate_urls": list(target.urls),
        }
    )
    return payload


def enqueue_enrichment(
    conn: psycopg.Connection[Any],
    *,
    user_id: str,
    item_id: str,
    target: EnrichTarget,
    extra: Mapping[str, Any],
) -> None:
    """Write the enrich job and record on the item what it is for, in one transaction."""
    from .jobs import enqueue  # noqa: PLC0415

    enrichment_repo.mark_enrichment_queued(
        conn, item_id, article_url=target.article_url, domain=target.domain
    )
    enqueue(conn, Queue.ENRICH, enrich_payload(item_id, target, extra), serialize_key=user_id)
