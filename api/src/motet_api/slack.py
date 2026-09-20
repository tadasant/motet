"""Telling a human a waitlist signup happened: the seam to a Slack incoming webhook.

**Asked for by Tadas on 2026-09-20**: every waitlist submission should post an alert into
the Tadasant Slack, in ``#motet-updates``, as an incoming webhook whose URL Cloud Run
injects from Secret Manager as ``SLACK_WEBHOOK_URL``. He named the mechanism, the
credential and its variable, so this module is the record of that choice rather than a
judgement made here; the invariant-12 reading is in AGENTS.md beside the waitlist section.

**The table is the record; this is the notification.** ``waitlist_signups`` and the
operator view are unchanged and remain where an address lives. The whole of what this adds
is that somebody hears about a row without going to look, which is why every failure below
is swallowed: an alert nobody received costs a notification, and a signup that 500s
because Slack was slow costs the signup.

**A channel is never sent.** An incoming webhook binds its own destination when it is
created, so a ``channel`` field in the payload is at best ignored and at worst a legacy
override; the deployment decides where this lands, and this repo does not name it.

Three things about it are decisions rather than details:

* **Off unless a URL is set**, like ``MOTET_DRAIN_TRIGGER`` and for a sharper reason: this
  code merges and runs in both environments *before* the secret exists, so the unset case
  is the normal one rather than an error. It is one DEBUG line per submission, nothing at
  WARNING, and no request.
* **Fired after the commit, inside the request.** The route arms it beside the write and
  :func:`motet_api.deps.connection` fires it once the transaction has committed, exactly
  as the drain nudge is armed and fired — so an alert is never sent for a row that then
  rolled back, and a request that fails on its way to the response sends nothing. It is
  deliberately **not** a background task, for the reason ``deps.connection`` documents:
  Cloud Run throttles a container's CPU between requests, so a task scheduled after the
  response may not run until the next request arrives, and a notification that fires
  unpredictably is worse than one that costs a bounded 3 seconds of latency.
* **The URL is never logged, never echoed and never in a repr.** It is a bearer credential
  in a URL — anyone holding it can post into the channel — so failures are reported by
  exception *type* and status code alone, never with ``logger.exception``: the error
  reporter captures frame locals, and a traceback out of httpx carries the request's URL
  with it. That is the rule ``motet_api.waitlist`` already keeps for the address, applied
  to the other secret in the same request.

**The address, by contrast, is the payload.** ``motet_api.waitlist`` promises no address
reaches a log line or a metric, and that promise is intact: the address goes to Slack, the
way it already goes to the table and the admin screen, and to nowhere else. Nothing here
logs it.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Final, Protocol, runtime_checkable
from urllib.parse import urlsplit

import httpx
from motet_obs import resolve_deployment_environment
from opentelemetry import metrics

logger = logging.getLogger("motet.api")

#: The webhook's URL, injected from Secret Manager under the same name. Unset or empty
#: means the feature is inactive; see the module docstring.
WEBHOOK_ENV: Final = "SLACK_WEBHOOK_URL"

#: Shorter than the drain trigger's five seconds, because this one buys less: a drain that
#: does not fire delays somebody's paste, while an alert that does not fire costs a message
#: about a row that is already safely stored. Three seconds is long enough for Slack's
#: p99 and short enough that a Slack outage cannot make the form feel broken.
#:
#: What it bounds: each phase of the POST (httpx applies it per connect/read/write rather
#: than as a total), so a pathological case can take a small multiple of it.
DEFAULT_TIMEOUT_SECONDS: Final = 3.0

#: How much of a rejection body to keep. Slack answers a bad webhook with a short reason
#: (``invalid_token``, ``no_service``); the rest is not worth a log line. The body never
#: contains the URL, but it is truncated anyway — a log line is not a place to paste a
#: vendor's whole response.
_ERROR_BODY_CHARS: Final = 200

#: What a deployment that has told nobody which one it is is called. Both environments can
#: post to a webhook, so an unlabelled message would be ambiguous — which is the one thing
#: the request for this feature asked the message to avoid.
UNLABELLED: Final = "an unlabelled deployment"

#: What stands in for the webhook wherever one could otherwise be written down.
_WITHHELD: Final = "<webhook withheld>"


_meter = metrics.get_meter("motet.api")

#: Whether the alert went out, and if not why not.
#:
#: ``disabled`` is counted alongside the real outcomes for the reason AGENTS.md gives as
#: never-infer-"no errors"-from-"no data": a series that only exists once the secret is
#: wired cannot tell "nobody has joined the waitlist" from "this deployment has never been
#: able to say so". It is the instrument that answers "is the webhook live yet", which is
#: the question this feature ships *before* knowing the answer to.
_alerts = _meter.create_counter(
    "motet.api.waitlist_alerts",
    unit="{alert}",
    description=(
        "Slack alerts for waitlist submissions, by whether the webhook accepted them. "
        "Carries no address."
    ),
)


@dataclass(frozen=True)
class Signup:
    """What a submission is worth saying, assembled by the route before it answers.

    ``email`` is out of the repr for ``motet_api.waitlist.Submission``'s reason: an error
    reporter captures frame locals by their repr, and this object is a local of the route
    that stores the address.
    """

    #: The normalized address, exactly as it was stored.
    email: str = field(repr=False)
    #: True when the address was already on the list — a resubmission, not a new signup.
    returning: bool
    #: How long the list is now, or ``None`` when the count could not be read. Read inside
    #: the route's own transaction, because by the time this is sent there is none.
    total: int | None


@runtime_checkable
class SlackAlerter(Protocol):
    """Post a waitlist alert, best-effort."""

    @property
    def configured(self) -> bool:
        """Whether this process has a webhook to post to — not whether Slack will accept it.

        Reported on ``/internal/health`` for ``vault_ready``'s reason: a deployment whose
        secret has not been wired yet and one whose webhook works look identical from
        outside, and this feature is expected to spend time in the first state. Whether the
        posts *succeed* is ``motet.api.waitlist_alerts{outcome}``'s question, not this
        flag's.
        """
        ...

    def signup(self, signup: Signup, *, environment: str | None) -> None:
        """Announce ``signup``. **Never raises**, whatever happens."""
        ...


@dataclass(frozen=True)
class NullSlackAlerter:
    """The off switch: count the alert that would have happened, and do nothing.

    What every laptop, every test and — until the secret is wired — both deployments get.
    It still records, so "addresses are arriving and nothing is announcing them" is a
    number rather than an absence.
    """

    @property
    def configured(self) -> bool:
        return False

    def signup(self, signup: Signup, *, environment: str | None) -> None:
        _alerts.add(1, {"outcome": "disabled"})
        logger.debug("not alerting Slack about a waitlist submission: %s is unset", WEBHOOK_ENV)


class WebhookSlackAlerter:
    """Post to a Slack incoming webhook.

    Constructed once per process. ``transport`` is an injection point for the tests rather
    than a second deployment shape: driving the *real* class over ``httpx.MockTransport``
    is what makes "this is the payload, and it carries no channel" a claim about the bytes
    this code puts on a socket rather than about a fake's bookkeeping.
    """

    def __init__(
        self,
        url: str,
        *,
        transport: httpx.BaseTransport | None = None,
        timeout: float = DEFAULT_TIMEOUT_SECONDS,
    ) -> None:
        # Bound to the instance under a private name and never in a repr: see the module
        # docstring on why this URL is treated as the credential it is.
        self._url = url
        self._client = httpx.Client(transport=transport, timeout=timeout)

    def __repr__(self) -> str:
        """Deliberately says nothing. The default would print ``_url`` at an object address."""
        return f"{type(self).__name__}({_WITHHELD})"

    @property
    def configured(self) -> bool:
        return True

    def _redacted(self, body: str) -> str:
        """A refusal body, bounded and with this webhook taken out of it.

        Slack does not echo the request URL today, and that is a promise about a vendor
        rather than a property of this code — a proxy, a WAF or a future Slack error page
        could, and the log line would then carry the credential into VictoriaLogs. The
        *secret* half is the path, so it is removed as well as the whole URL: a body
        quoting only ``/services/T…/B…/…`` would otherwise survive the first replacement.
        """
        redacted = body[:_ERROR_BODY_CHARS].replace(self._url, _WITHHELD)
        path = urlsplit(self._url).path
        return redacted.replace(path, _WITHHELD) if len(path) > 1 else redacted

    def signup(self, signup: Signup, *, environment: str | None) -> None:
        """Post the alert. Best-effort, and never raises.

        The row is committed before this runs and the operator view already shows it, so
        every failure here costs a notification and nothing else — which is requirement one
        of the change: a signup must not fail because Slack did.
        """
        payload = {"text": compose(signup, environment=environment)}
        try:
            response = self._client.post(self._url, json=payload)
        except Exception as exc:
            # By type, and never `logger.exception`: a traceback out of httpx carries the
            # request's URL, and the error reporter would carry it to GlitchTip. WARNING
            # rather than ERROR for the drain trigger's reason — only ERROR becomes an
            # event, and a Slack outage is not a Motet fault worth paging on.
            logger.warning(
                "could not tell Slack about a waitlist submission (%s); the address is "
                "stored and is on the admin screen",
                type(exc).__name__,
            )
            _alerts.add(1, {"outcome": "failed"})
            return

        if response.status_code >= 400:
            # Slack's own sentence — `invalid_token`, `no_service`, `channel_not_found` —
            # which is what tells a revoked webhook from a broken one.
            logger.warning(
                "Slack refused a waitlist alert: HTTP %d %s. The address is stored and is "
                "on the admin screen.",
                response.status_code,
                self._redacted(response.text),
            )
            _alerts.add(1, {"outcome": "refused"})
            return

        _alerts.add(1, {"outcome": "sent"})
        logger.info("told Slack about a waitlist submission")


def compose(signup: Signup, *, environment: str | None) -> str:
    """The message body. Nothing in it that the submission did not already carry.

    An address is user-supplied text going into a formatted message, so the three
    characters Slack's markup reserves are escaped — ``normalize_email`` is deliberately
    loose about what an address may contain (see ``motet_db.waitlist``) and does not
    exclude them. Backticks around it stop Slack turning it into a ``mailto:`` link, which
    on a phone is one mis-tap away from composing mail to a stranger.
    """
    where = environment or UNLABELLED
    headline = (
        "*Waitlist: an address already on the list submitted again*"
        if signup.returning
        else "*New Motet waitlist signup*"
    )
    lines = [f"{headline} · {escape(where)}", f"`{escape(signup.email)}`"]
    if signup.total is not None:
        lines.append(f"_{signup.total} on the list_")
    return "\n".join(lines)


def escape(text: str) -> str:
    """Slack's three reserved characters, per its own escaping rules. Ampersand first."""
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def deployment_label(public_base_url: str | None) -> str | None:
    """Which deployment this is, for the message. ``None`` when nothing says.

    Read from what the deploy already sets rather than from a variable of its own.
    ``deployment.environment`` in ``OTEL_RESOURCE_ATTRIBUTES`` is the estate's own label
    and the one every span and metric already wears, and AGENTS.md's rule against
    inventing a second pair of variables for a fact the deploy already states applies
    exactly here — so this feature asks the private repo for one secret and no more.

    It falls back to the **host** of ``MOTET_PUBLIC_BASE_URL``, which a deployed
    environment sets anyway (the RSS enclosures point at it, and the MCP issuer is built
    from it), so an environment that never set the resource attribute still says which one
    it is. Both are read from the environment at runtime and neither is named here; the
    caller passes the second in because it already holds a :class:`~motet_api.config.Settings`.
    """
    return resolve_deployment_environment() or _host(public_base_url)


def _host(base_url: str | None) -> str | None:
    if not base_url:
        return None
    return urlsplit(base_url).hostname or None


def build_alerter(webhook_url: str | None) -> SlackAlerter:
    """Resolve the alerter from the webhook URL. Off unless one is set and usable.

    Nothing here raises. An unset URL is silent — it is the state both deployments are in
    until the secret is wired, and the state every laptop is in forever. A URL that is
    *set* and unusable says so at ERROR, once, at startup: somebody meant to wire this and
    the value is wrong, and that is a different thing from not having wired it.

    **The refusal never repeats the value.** A webhook URL is a bearer credential, and an
    error message naming it would put it in the startup log of a public-facing service.
    """
    url = (webhook_url or "").strip()
    if not url:
        return NullSlackAlerter()
    split = urlsplit(url)
    if split.scheme != "https" or not split.hostname:
        logger.error(
            "%s is set but is not an https URL with a host, so no waitlist alerts will be "
            "sent. (The value is not logged.)",
            WEBHOOK_ENV,
        )
        return NullSlackAlerter()
    try:
        return WebhookSlackAlerter(url)
    except Exception as exc:
        # By type, for the module docstring's reason: the constructor is handed the URL,
        # so a traceback here is the one place it could escape into the error reporter.
        logger.error(
            "%s is set but a Slack client could not be built (%s), so no waitlist alerts "
            "will be sent.",
            WEBHOOK_ENV,
            type(exc).__name__,
        )
        return NullSlackAlerter()


@dataclass
class WaitlistAlert:
    """One request's intent to alert, held until its transaction commits.

    The same shape as :class:`motet_api.drain.DrainNudge` and for the same reason: an alert
    about a row no other process can see yet is an alert about nothing, and a request that
    fails on its way to the response should send none at all. The route arms it;
    ``motet_api.deps.connection`` fires it after ``conn.commit()``.
    """

    alerter: SlackAlerter
    #: Which deployment this is, resolved once per request by ``deps.waitlist_alert``.
    environment: str | None = None
    signup: Signup | None = None

    def arm(self, signup: Signup) -> None:
        self.signup = signup

    def fire(self) -> None:
        """Send the armed alert, if any. **Never raises**, whatever the alerter does.

        ``SlackAlerter.signup`` promises the same and the shipped implementations keep the
        promise — but this is the one caller that sits between a committed row and the
        visitor's answer, so it does not take a Protocol's word for it. A bug in an alerter
        must cost a log line, not a 500 for a signup that has already succeeded.
        """
        signup, self.signup = self.signup, None
        if signup is None:
            return
        try:
            self.alerter.signup(signup, environment=self.environment)
        except Exception as exc:
            logger.warning(
                "the Slack alerter raised for a waitlist submission (%s); the address is "
                "stored and is on the admin screen",
                type(exc).__name__,
            )
            _alerts.add(1, {"outcome": "failed"})
