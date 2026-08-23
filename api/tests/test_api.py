"""Tests for the API surface.

These check the *contract* — routes exist, health reports telemetry wiring honestly,
unimplemented routes say so — not behaviour that does not exist yet.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from motet_api import app
from motet_api.obs import ERROR_DSN_ENV, OTLP_ENDPOINT_ENV

client = TestClient(app)


def test_healthz_is_ok(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(OTLP_ENDPOINT_ENV, raising=False)
    monkeypatch.delenv(ERROR_DSN_ENV, raising=False)
    response = client.get("/healthz")
    assert response.status_code == 200
    assert response.json()["status"] == "ok"


def test_healthz_reports_unconfigured_telemetry_as_unconfigured(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Guards the "no data is not no errors" trap in obs.py."""
    monkeypatch.delenv(OTLP_ENDPOINT_ENV, raising=False)
    monkeypatch.delenv(ERROR_DSN_ENV, raising=False)
    body = client.get("/healthz").json()
    assert body["telemetry_configured"] is False
    assert body["errors_configured"] is False


def test_healthz_reports_configured_telemetry(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(OTLP_ENDPOINT_ENV, "https://obs.example.invalid/otel/v1")
    monkeypatch.setenv(ERROR_DSN_ENV, "https://public@glitchtip.example.invalid/1")
    body = client.get("/healthz").json()
    assert body["telemetry_configured"] is True
    assert body["errors_configured"] is True


@pytest.mark.parametrize(
    ("method", "path", "payload"),
    [
        ("post", "/v1/sources/paste", {"title": "t", "text": "body"}),
        ("get", "/v1/news-items", None),
        ("post", "/v1/episodes", {"title": "t", "max_duration_ms": 1000}),
        ("get", "/v1/episodes/ep_1", None),
        ("get", "/feed.xml", None),
    ],
)
def test_scaffolded_routes_answer_501(method: str, path: str, payload: dict | None) -> None:
    """Declared but not built. A 404 here would mean the contract lost a route."""
    response = getattr(client, method)(path, **({"json": payload} if payload else {}))
    assert response.status_code == 501


def test_request_validation_still_applies() -> None:
    """The models are real even though the handlers are not."""
    assert client.post("/v1/sources/paste", json={"title": "", "text": ""}).status_code == 422
    assert client.post("/v1/episodes", json={"title": "t", "max_duration_ms": 0}).status_code == 422
