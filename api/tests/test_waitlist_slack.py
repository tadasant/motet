"""The Slack alert a waitlist signup posts: the payload, and every way it may not happen.

Two halves, on trial for different things.

The first drives the **real** :class:`WebhookSlackAlerter` over ``httpx.MockTransport``, so
what is asserted is the bytes this code puts on a socket — the JSON body, the absence of a
``channel`` field (an incoming webhook binds its own), and that the address is escaped for
Slack's markup rather than pasted in raw. A fake alerter could not make those claims.

The second goes through ``TestClient`` against a real Postgres and asserts the three
properties the feature is *for*: an unwired deployment neither calls nor complains, a
webhook that fails in any way still leaves the visitor with their 200 and the row in the
table, and an alert is sent only after the transaction that stored the address has
committed.

Nothing here reaches Slack. ``SLACK_WEBHOOK_URL`` is unset in CI, so the shipped default is
the inert alerter, and every enabled case injects its own transport.
"""

from __future__ import annotations

import json
import logging
from typing import Any

import httpx
import psycopg
import pytest
from fastapi.testclient import TestClient
from motet_api import app
from motet_api import slack as api_slack
from motet_api.auth import ADMIN_EMAILS_ENV, ALLOWED_EMAILS_ENV
from motet_api.deps import reset_slack_alerter, reset_store, slack_alerter
from motet_api.main import HEALTH_PATH
from motet_api.slack import (
    UNLABELLED,
    WEBHOOK_ENV,
    NullSlackAlerter,
    Signup,
    SlackAlerter,
    WaitlistAlert,
    WebhookSlackAlerter,
    build_alerter,
    compose,
    deployment_label,
)
from motet_db import repo

JOIN = "/v1/waitlist"
AS_SCRIPT = {
    "Content-Type": "application/x-www-form-urlencoded",
    "Accept": "application/json",
}

#: A webhook-shaped URL on a domain that cannot resolve. It is a *secret* in every test
#: here: the assertions about logging are that this string never appears.
WEBHOOK = "https://hooks.slack.invalid/services/T0/B0/xoxbSecretPath"


class Recorder:
    """An alerter that remembers what it was asked to announce. For the route-level cases."""

    def __init__(self, *, configured: bool = True, boom: Exception | None = None) -> None:
        self.sent: list[tuple[Signup, str | None]] = []
        self._configured = configured
        self._boom = boom

    @property
    def configured(self) -> bool:
        return self._configured

    def signup(self, signup: Signup, *, environment: str | None) -> None:
        self.sent.append((signup, environment))
        if self._boom is not None:
            raise self._boom


class MetricSpy:
    """Stands in for ``motet.api.waitlist_alerts``: every outcome records through it."""

    def __init__(self) -> None:
        self.adds: list[dict[str, str]] = []

    def add(self, amount: int, attributes: dict[str, str] | None = None) -> None:
        self.adds.append(dict(attributes or {}))

    @property
    def outcomes(self) -> list[str]:
        return [a["outcome"] for a in self.adds]


@pytest.fixture
def metric(monkeypatch: pytest.MonkeyPatch) -> MetricSpy:
    spy = MetricSpy()
    monkeypatch.setattr(api_slack, "_alerts", spy)
    return spy


@pytest.fixture
def api(
    db: psycopg.Connection[Any],
    _migrated: str,
    object_store: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> Any:
    monkeypatch.setenv("DATABASE_URL", _migrated)
    monkeypatch.setenv(ALLOWED_EMAILS_ENV, "operator@motet.test")
    monkeypatch.setenv(ADMIN_EMAILS_ENV, "operator@motet.test")
    monkeypatch.delenv(WEBHOOK_ENV, raising=False)
    reset_store()
    reset_slack_alerter()
    with TestClient(app) as started:
        yield started
    app.dependency_overrides.pop(slack_alerter, None)
    reset_slack_alerter()
    reset_store()


def rows(db: psycopg.Connection[Any]) -> list[dict[str, Any]]:
    db.rollback()
    return db.execute("SELECT email, submissions FROM waitlist_signups ORDER BY id").fetchall()


def capturing(
    seen: list[httpx.Request], *, status_code: int = 200, body: str = "ok"
) -> WebhookSlackAlerter:
    def handle(request: httpx.Request) -> httpx.Response:
        # Read it here: `request.content` is what actually went out.
        seen.append(request)
        return httpx.Response(status_code, text=body)

    return WebhookSlackAlerter(WEBHOOK, transport=httpx.MockTransport(handle))


class TestTheGate:
    """Whether this process has a webhook at all."""

    def test_unset_is_off(self) -> None:
        assert isinstance(build_alerter(None), NullSlackAlerter)
        assert isinstance(build_alerter(""), NullSlackAlerter)
        assert isinstance(build_alerter("   "), NullSlackAlerter)

    def test_unset_says_nothing_above_debug(self, caplog: pytest.LogCaptureFixture) -> None:
        # This ships and runs in both environments before the secret exists, so the unset
        # case is the normal one: it must not warn on every signup.
        with caplog.at_level(logging.DEBUG, logger="motet.api"):
            alerter = build_alerter(None)
            alerter.signup(
                Signup(email="ada@example.com", returning=False, total=1), environment=None
            )
        assert [record.levelno for record in caplog.records] == [logging.DEBUG]

    def test_a_url_turns_it_on(self) -> None:
        alerter = build_alerter(WEBHOOK)
        assert isinstance(alerter, WebhookSlackAlerter)
        assert alerter.configured is True

    @pytest.mark.parametrize("bad", ["http://hooks.slack.invalid/x", "not-a-url", "https://"])
    def test_a_url_that_is_not_https_with_a_host_is_loud_and_still_off(
        self, bad: str, caplog: pytest.LogCaptureFixture
    ) -> None:
        # Loud, because somebody meant to wire this and the value is wrong — a different
        # thing from not having wired it at all.
        with caplog.at_level(logging.ERROR, logger="motet.api"):
            alerter = build_alerter(bad)
        assert isinstance(alerter, NullSlackAlerter)
        assert any(record.levelno == logging.ERROR for record in caplog.records)
        assert bad not in caplog.text

    def test_the_off_alerter_does_nothing_and_does_not_raise(self) -> None:
        NullSlackAlerter().signup(
            Signup(email="ada@example.com", returning=False, total=3), environment="staging"
        )


class TestThePayload:
    """The bytes on the socket."""

    def test_posts_json_to_the_webhook(self) -> None:
        seen: list[httpx.Request] = []
        capturing(seen).signup(
            Signup(email="ada@example.com", returning=False, total=7), environment="staging"
        )

        assert len(seen) == 1
        assert (seen[0].method, str(seen[0].url)) == ("POST", WEBHOOK)
        assert seen[0].headers["content-type"] == "application/json"

    def test_the_body_is_a_text_field_and_nothing_else(self) -> None:
        seen: list[httpx.Request] = []
        capturing(seen).signup(
            Signup(email="ada@example.com", returning=False, total=7), environment="staging"
        )

        body = json.loads(seen[0].content)
        # An incoming webhook binds its own destination when it is created, so a channel
        # here is at best ignored and at worst a legacy override of somebody's choice.
        assert set(body) == {"text"}
        assert "channel" not in seen[0].content.decode()

    def test_the_message_carries_the_address_the_environment_and_the_total(self) -> None:
        text = compose(
            Signup(email="ada@example.com", returning=False, total=42), environment="staging"
        )

        assert "ada@example.com" in text
        assert "staging" in text
        assert "42 on the list" in text
        assert "New Motet waitlist signup" in text

    def test_a_resubmission_says_so_rather_than_claiming_a_new_signup(self) -> None:
        text = compose(
            Signup(email="ada@example.com", returning=True, total=42), environment="production"
        )

        assert "already on the list" in text
        assert "New Motet waitlist signup" not in text

    def test_an_unlabelled_deployment_says_that_rather_than_nothing(self) -> None:
        # Both environments can post to a webhook, so an alert that names neither is the
        # one thing this message was asked not to be.
        assert UNLABELLED in compose(
            Signup(email="ada@example.com", returning=False, total=1), environment=None
        )

    def test_a_missing_total_drops_the_line_rather_than_guessing(self) -> None:
        text = compose(
            Signup(email="ada@example.com", returning=False, total=None), environment="staging"
        )

        assert "on the list" not in text
        assert "ada@example.com" in text

    def test_the_address_is_escaped_for_slacks_markup(self) -> None:
        # `normalize_email` is deliberately loose (see motet_db.waitlist) and does not
        # exclude the three characters Slack reserves.
        text = compose(
            Signup(email="a<b>&c@example.com", returning=False, total=1), environment="staging"
        )

        assert "a&lt;b&gt;&amp;c@example.com" in text
        assert "<b>" not in text


class TestWhenSlackDoesNotAnswer:
    """Every failure is swallowed, counted and logged without the URL."""

    def test_a_rejection_is_a_warning_and_does_not_raise(
        self, metric: MetricSpy, caplog: pytest.LogCaptureFixture
    ) -> None:
        seen: list[httpx.Request] = []
        with caplog.at_level(logging.DEBUG, logger="motet.api"):
            capturing(seen, status_code=404, body="no_service").signup(
                Signup(email="ada@example.com", returning=False, total=1), environment="staging"
            )

        assert metric.outcomes == ["refused"]
        assert "no_service" in caplog.text
        assert max(record.levelno for record in caplog.records) == logging.WARNING

    def test_a_transport_failure_is_swallowed(
        self, metric: MetricSpy, caplog: pytest.LogCaptureFixture
    ) -> None:
        def explode(request: httpx.Request) -> httpx.Response:
            raise httpx.ConnectError("nodename nor servname provided", request=request)

        alerter = WebhookSlackAlerter(WEBHOOK, transport=httpx.MockTransport(explode))
        with caplog.at_level(logging.DEBUG, logger="motet.api"):
            alerter.signup(
                Signup(email="ada@example.com", returning=False, total=1), environment="staging"
            )

        assert metric.outcomes == ["failed"]
        assert "ConnectError" in caplog.text

    def test_a_timeout_is_swallowed(self, metric: MetricSpy) -> None:
        def stall(request: httpx.Request) -> httpx.Response:
            raise httpx.ReadTimeout("timed out", request=request)

        WebhookSlackAlerter(WEBHOOK, transport=httpx.MockTransport(stall)).signup(
            Signup(email="ada@example.com", returning=False, total=1), environment="staging"
        )

        assert metric.outcomes == ["failed"]

    @pytest.mark.parametrize("status_code", [200, 404, 500])
    @pytest.mark.parametrize("body", [f"oh no: {WEBHOOK}", "posting to /services/T0/B0/x failed"])
    def test_the_url_never_reaches_a_log_line_even_if_slack_echoes_it(
        self, status_code: int, body: str, caplog: pytest.LogCaptureFixture
    ) -> None:
        # The URL *is* the credential: anyone holding it can post into the channel. The
        # refusal body is a vendor's text, so "Slack does not echo the URL" is a promise
        # about Slack rather than a property of this code — a proxy or an error page could.
        seen: list[httpx.Request] = []
        with caplog.at_level(logging.DEBUG, logger="motet.api"):
            capturing(seen, status_code=status_code, body=body).signup(
                Signup(email="ada@example.com", returning=False, total=1), environment="staging"
            )

        assert WEBHOOK not in caplog.text
        assert "xoxbSecretPath" not in caplog.text

    def test_the_url_is_not_in_a_repr(self) -> None:
        # An error reporter captures frame locals by their repr, and `self` is one.
        alerter = build_alerter(WEBHOOK)
        assert WEBHOOK not in repr(alerter)
        assert "xoxbSecretPath" not in repr(alerter)

    def test_the_address_never_reaches_a_log_line(self, caplog: pytest.LogCaptureFixture) -> None:
        # motet_api.waitlist's promise, kept on the one path that handles the address for
        # a second purpose.
        seen: list[httpx.Request] = []
        with caplog.at_level(logging.DEBUG, logger="motet.api"):
            capturing(seen, status_code=500, body="oh no").signup(
                Signup(email="ada@example.com", returning=False, total=1), environment="staging"
            )

        assert "ada@example.com" not in caplog.text


class TestTheArmedAlert:
    """``WaitlistAlert``: one arm, one send, and it never raises."""

    def test_an_unarmed_alert_sends_nothing(self) -> None:
        recorder = Recorder()
        WaitlistAlert(recorder).fire()
        assert recorder.sent == []

    def test_one_arm_is_one_send_however_often_it_fires(self) -> None:
        recorder = Recorder()
        alert = WaitlistAlert(recorder, environment="staging")
        alert.arm(Signup(email="ada@example.com", returning=False, total=1))
        alert.fire()
        alert.fire()

        assert len(recorder.sent) == 1
        assert recorder.sent[0][1] == "staging"

    def test_an_alerter_that_raises_is_counted_and_swallowed(self, metric: MetricSpy) -> None:
        # The Protocol promises never to raise; this is the caller that does not take its
        # word for it, because it sits between a committed row and the visitor's answer.
        alert = WaitlistAlert(Recorder(boom=RuntimeError("boom")))
        alert.arm(Signup(email="ada@example.com", returning=False, total=1))
        alert.fire()

        assert metric.outcomes == ["failed"]


class TestWhichDeploymentThisIs:
    def test_the_resource_attribute_wins(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("OTEL_RESOURCE_ATTRIBUTES", "deployment.environment=staging")
        assert deployment_label("https://api.example.test") == "staging"

    def test_the_newer_spelling_is_read_too(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("OTEL_RESOURCE_ATTRIBUTES", "deployment.environment.name=production")
        assert deployment_label(None) == "production"

    def test_it_falls_back_to_the_public_host(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("OTEL_RESOURCE_ATTRIBUTES", raising=False)
        assert deployment_label("https://api.example.test/") == "api.example.test"

    def test_nothing_set_is_none(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("OTEL_RESOURCE_ATTRIBUTES", raising=False)
        assert deployment_label(None) is None


class TestThroughTheRoute:
    """What a visitor gets, and what Slack is told, with a real database underneath."""

    def use(self, alerter: SlackAlerter) -> None:
        app.dependency_overrides[slack_alerter] = lambda: alerter

    def test_an_unwired_deployment_stores_the_address_and_calls_nobody(
        self, api: TestClient, db: psycopg.Connection[Any], metric: MetricSpy
    ) -> None:
        seen: list[httpx.Request] = []
        # No override: the process resolves its own alerter, and SLACK_WEBHOOK_URL is unset.
        response = api.post(JOIN, content="email=ada%40example.com", headers=AS_SCRIPT)

        assert response.status_code == 200
        assert response.json() == {"status": "joined"}
        assert rows(db) == [{"email": "ada@example.com", "submissions": 1}]
        assert seen == []
        assert metric.outcomes == ["disabled"]

    def test_a_signup_is_announced_with_the_address_and_the_total(
        self, api: TestClient, db: psycopg.Connection[Any]
    ) -> None:
        seen: list[httpx.Request] = []
        self.use(capturing(seen))

        response = api.post(JOIN, content="email=%20Ada%40Example.COM%20", headers=AS_SCRIPT)

        assert response.status_code == 200
        assert len(seen) == 1
        text = json.loads(seen[0].content)["text"]
        # The *stored* form, so the alert and the admin screen say the same thing.
        assert "ada@example.com" in text
        assert "1 on the list" in text

    def test_a_resubmission_is_announced_as_one(self, api: TestClient) -> None:
        seen: list[httpx.Request] = []
        self.use(capturing(seen))

        api.post(JOIN, content="email=ada%40example.com", headers=AS_SCRIPT)
        api.post(JOIN, content="email=ada%40example.com", headers=AS_SCRIPT)

        assert len(seen) == 2
        assert "already on the list" in json.loads(seen[1].content)["text"]
        # One row, two submissions, so the total does not climb with the resubmission.
        assert "1 on the list" in json.loads(seen[1].content)["text"]

    @pytest.mark.parametrize(
        ("body", "status_code"),
        [
            ("email=not-an-address", 422),
            ("email=ada%40example.com&motet_hp=bot", 200),
            ("", 415),
        ],
    )
    def test_nothing_without_a_stored_address_is_announced(
        self, api: TestClient, body: str, status_code: int
    ) -> None:
        # A refusal has no address to announce, and the honeypot's 200 is a lie told to a
        # bot on purpose — alerting on it would make the endpoint an oracle in a channel.
        seen: list[httpx.Request] = []
        self.use(capturing(seen))

        headers = AS_SCRIPT if body else {"Accept": "application/json"}
        response = api.post(JOIN, content=body, headers=headers)

        assert response.status_code == status_code
        assert seen == []

    @pytest.mark.parametrize("status_code", [400, 403, 404, 500])
    def test_a_webhook_that_refuses_still_leaves_the_visitor_with_a_200(
        self, api: TestClient, db: psycopg.Connection[Any], status_code: int
    ) -> None:
        seen: list[httpx.Request] = []
        self.use(capturing(seen, status_code=status_code, body="invalid_token"))

        response = api.post(JOIN, content="email=ada%40example.com", headers=AS_SCRIPT)

        assert response.status_code == 200
        assert response.json() == {"status": "joined"}
        assert rows(db) == [{"email": "ada@example.com", "submissions": 1}]

    @pytest.mark.parametrize(
        "boom",
        [
            httpx.ConnectError("dns"),
            httpx.ReadTimeout("slow"),
            httpx.ConnectTimeout("slow"),
            RuntimeError("a bug in the alerter"),
        ],
    )
    def test_a_webhook_that_explodes_still_leaves_the_visitor_with_a_200(
        self, api: TestClient, db: psycopg.Connection[Any], boom: Exception
    ) -> None:
        self.use(Recorder(boom=boom))

        response = api.post(JOIN, content="email=ada%40example.com", headers=AS_SCRIPT)

        assert response.status_code == 200
        assert rows(db) == [{"email": "ada@example.com", "submissions": 1}]

    def test_the_alert_is_sent_only_after_the_row_has_committed(
        self, api: TestClient, _migrated: str
    ) -> None:
        # The property, not the ordering of two lines: at the moment the alerter runs, a
        # *different* connection can already see the row. An alert for a row that then
        # rolled back would be an alert about nothing.
        visible: list[int] = []

        class CountsFromAnotherConnection(Recorder):
            def signup(self, signup: Signup, *, environment: str | None) -> None:
                super().signup(signup, environment=environment)
                with repo.connect(_migrated) as other:
                    row = other.execute("SELECT count(*) AS n FROM waitlist_signups").fetchone()
                    visible.append(int(row["n"]))  # type: ignore[index]

        recorder = CountsFromAnotherConnection()
        self.use(recorder)

        api.post(JOIN, content="email=ada%40example.com", headers=AS_SCRIPT)

        assert len(recorder.sent) == 1
        assert visible == [1]

    def test_health_reports_whether_a_signup_would_reach_slack(self, api: TestClient) -> None:
        # `vault_ready`'s argument: an unwired deployment and a revoked webhook look
        # identical from outside.
        assert api.get(HEALTH_PATH).json()["waitlist_alerts"] is False

        self.use(build_alerter(WEBHOOK))
        body = api.get(HEALTH_PATH).json()

        assert body["waitlist_alerts"] is True
        # And the URL itself is never on a public, unauthenticated route.
        assert WEBHOOK not in json.dumps(body)
