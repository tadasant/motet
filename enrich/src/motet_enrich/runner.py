"""The seam to the agent, with a fake in front of it (invariant 7).

One :class:`Runner` Protocol, one deterministic fake, and one real implementation that
shells out to the Pi CLI. ``MOTET_INFERENCE_MODE`` picks, in exactly the way it picks for
every other vendor in this repo — so no test in this package, and no ``bin/ci`` run, can
start a browser, reach OpenRouter, or spend a cent.

The fake is not a stub. It answers with a plausible article, a transcript that exercises
every redaction rule, and a browser state, because what the worker does with a result is
most of what there is to test on the other side of this seam.
"""

from __future__ import annotations

import json
from typing import Protocol

from .config import EnrichSettings
from .contract import EnrichRequest, EnrichResult, RunCaps, TranscriptEntry


class Runner(Protocol):
    """Run one enrichment and answer with what it produced.

    **Never raises for a run that went badly** — a wall, a timeout, a crashed toolchain and
    a cap all come back as an :class:`~motet_enrich.contract.EnrichResult` with a status,
    because the caller's answer is the same for all of them: keep the preview, record the
    run. It may raise for a fault in *this process* (an unusable configuration), which is a
    500 and a retry.
    """

    def run(self, request: EnrichRequest, caps: RunCaps) -> EnrichResult: ...


class FakeRunner:
    """A deterministic run. What every test and every fake-mode deployment gets.

    ``blocked_domains`` and ``failing_domains`` are how a test asks for the other outcomes
    without a second class: the worker's handling of a failed run is the interesting half.
    """

    #: Long enough to pass the worker's ``MIN_ARTICLE_CHARS``, so a fake-mode end-to-end
    #: run actually replaces the preview rather than being discarded as a stub.
    BODY: str = "The full article, as the fake runner always tells it. " * 12

    def __init__(
        self,
        *,
        blocked_domains: frozenset[str] = frozenset(),
        failing_domains: frozenset[str] = frozenset(),
    ) -> None:
        self._blocked = blocked_domains
        self._failing = failing_domains

    def run(self, request: EnrichRequest, caps: RunCaps) -> EnrichResult:
        url = request.candidate_urls[0]
        transcript = [
            TranscriptEntry(
                seq=1,
                kind="tool_call",
                tool="browser__browser_execute",
                args=f"page.goto({url!r}, {{timeout: 10000}})",
            ),
            TranscriptEntry(
                seq=2,
                kind="tool_result",
                tool="browser__browser_execute",
                ok=True,
                result="innerText: the article",
            ),
        ]
        if request.site.domain in self._failing:
            return EnrichResult(
                status="failed",
                tool_calls=2,
                cost_usd=0.01,
                duration_seconds=1.0,
                transcript=transcript,
                error="the fake runner was asked to fail for this domain",
            )
        if request.site.domain in self._blocked:
            return EnrichResult(
                status="blocked",
                article_url=url,
                tool_calls=2,
                cost_usd=0.02,
                duration_seconds=1.0,
                transcript=transcript,
                error="a wall this fake has no credential for",
            )
        return EnrichResult(
            status="ok",
            article_url=url,
            article_markdown=f"# {request.title}\n\n{self.BODY}",
            login_performed=request.site.username is not None and request.browser_state is None,
            tool_calls=2,
            cost_usd=0.03,
            duration_seconds=1.0,
            browser_state=json.dumps(
                {"cookies": [{"name": "fake_session", "value": "x", "domain": request.site.domain}]}
            ),
            transcript=transcript,
        )


def build_runner(settings: EnrichSettings) -> Runner:
    """Fake or real, decided by ``MOTET_INFERENCE_MODE`` and nothing else.

    Imported here rather than at module scope for the one reason a lazy import is ever
    right: :mod:`motet_enrich.pi` reaches for ``subprocess`` and the toolchain's paths, and
    a fake-mode process — every test, every laptop — has no business resolving any of it.
    Whether the toolchain is *present* is answered eagerly, at startup, by
    :func:`~motet_enrich.config.resolve_toolchain`, which is the half that must not be lazy.
    """
    if settings.mode == "fake":
        return FakeRunner()
    from .pi import PiRunner  # noqa: PLC0415

    return PiRunner(settings)
