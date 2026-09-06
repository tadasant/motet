"""Fixtures for the voice tests.

Nothing here touches a network, a vendor, or a database. The root ``conftest.py`` already
pins ``MOTET_INFERENCE_MODE=fake`` for the whole session (invariant 7); this file adds the
voice-specific settings so a test never depends on what happens to be in the developer's
environment.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import pytest
from motet_voice.config import VoiceSettings
from motet_voice.harness import synthesize_walk


@dataclass(frozen=True)
class MetricSink:
    """An in-memory OTLP-free reader, plus the one query a test makes of it."""

    reader: Any

    def points(self, metric_name: str) -> list[Any]:
        """Every data point exported under ``metric_name`` so far, attributes included.

        Cumulative for the whole test session, which is why a test filters on the
        attributes it cares about rather than assuming it is the only writer.
        """
        collected = self.reader.get_metrics_data()
        if collected is None:
            return []
        return [
            point
            for resource in collected.resource_metrics
            for scope in resource.scope_metrics
            for metric in scope.metrics
            if metric.name == metric_name
            for point in metric.data.data_points
        ]


@pytest.fixture(scope="session")
def metrics() -> MetricSink:
    """One real SDK ``MeterProvider`` for this process, shared by every test that reads it.

    OpenTelemetry's provider is process-global and may be set **once**, so this is a
    session-scoped fixture rather than a helper each test calls: two installers would leave
    whichever ran second reading a reader nothing is attached to. Nothing else in the suite
    installs one — ``motet_obs.configure`` installs nothing without an OTLP endpoint, and
    ``obs/tests`` does its exporting in subprocesses — so in practice this succeeds; the
    guard turns a future in-process installer into a skip rather than a confusing failure.

    **The guard compares identity, not class, and that is the whole of it working.**
    ``set_meter_provider`` is a silent no-op once it has fired, and the one installer this
    is realistically racing — ``motet_obs.runtime`` — installs the *same* ``MeterProvider``
    class. A class check would pass, leave ``reader`` attached to a provider nothing writes
    to, and fail later as "nothing was exported", which points at the accounting code
    instead of at the collision.

    Instruments created at import against the proxy meter resolve to real ones the moment
    the provider is set, which is what lets a module-level counter be read back here at all.
    """
    from opentelemetry import metrics as otel_metrics
    from opentelemetry.sdk.metrics import MeterProvider
    from opentelemetry.sdk.metrics.export import InMemoryMetricReader

    reader = InMemoryMetricReader()
    provider = MeterProvider(metric_readers=[reader])
    otel_metrics.set_meter_provider(provider)
    if otel_metrics.get_meter_provider() is not provider:
        pytest.skip("a MeterProvider is already installed in this process")
    return MetricSink(reader)


@pytest.fixture
def settings() -> VoiceSettings:
    """Deterministic settings: fake mode, no vendor keys, a fixed session secret."""
    return VoiceSettings.from_env(
        {
            "MOTET_INFERENCE_MODE": "fake",
            "MOTET_VOICE_SESSION_SECRET": "test-secret-not-a-real-one",
        }
    )


@pytest.fixture
def quiet_walk() -> bytes:
    """Twelve seconds of wind, traffic and footsteps, and nobody talking."""
    return synthesize_walk(duration_ms=12_000).pcm


@pytest.fixture
def spoken_walk() -> bytes:
    """The same conditions, with three clear utterances in it."""
    return synthesize_walk(duration_ms=12_000, speech_at_ms=(2_000, 6_000, 9_500)).pcm
