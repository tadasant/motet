"""What this service reads from its environment, parsed once.

Every value here is set by the private infrastructure repo's service definition
(tadasant-internal#2837) and defaulted to something that is safe on a laptop. The defaults
are the ones the design session fixed (option C2) and the infra issue repeats, so a
deployment that names none of them still runs bounded.

**There is no database URL and no key path in this file, and that is the design.** See
:mod:`motet_enrich.contract`.
"""

from __future__ import annotations

import os
import shutil
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Final

from motet_inference.llm import DEFAULT_MODEL, KNOWN_MODELS
from motet_inference.mode import Mode, current_mode

#: The shared bearer the worker presents. Cloud Run's IAM check — a Google ID token in
#: ``X-Serverless-Authorization``, audience the service URL — is the *outer* door and is
#: consumed by the platform before the request reaches this process; this is the inner one,
#: and it is what makes a container reachable by accident still refuse to spend money.
SERVICE_TOKEN_ENV: Final = "MOTET_ENRICH_SERVICE_TOKEN"

MODEL_ENV: Final = "MOTET_ENRICH_MODEL"
THINKING_ENV: Final = "MOTET_ENRICH_THINKING"
MAX_USD_ENV: Final = "MOTET_ENRICH_MAX_USD_PER_ITEM"
MAX_TOOL_CALLS_ENV: Final = "MOTET_ENRICH_MAX_TOOL_CALLS"
TIMEOUT_ENV: Final = "MOTET_ENRICH_TIMEOUT_SECONDS"
TOOLCHAIN_DIR_ENV: Final = "MOTET_ENRICH_TOOLCHAIN_DIR"
OPENROUTER_KEY_ENV: Final = "OPENROUTER_API_KEY"

#: The caps the infra issue lists as the app's own defaults. A request may ask for less;
#: :func:`EnrichSettings.clamp` refuses to let one ask for more, because the caller is the
#: worker and a bug there must not become an unbounded agent run here.
DEFAULT_MAX_USD: Final = 0.50
DEFAULT_MAX_TOOL_CALLS: Final = 40
DEFAULT_TIMEOUT_SECONDS: Final = 600

#: Where the npm toolchain lives in the image. Overridable so a laptop can point at a
#: checkout's ``node_modules`` without rebuilding a container.
DEFAULT_TOOLCHAIN_DIR: Final = "/opt/motet-enrich"

#: Thinking level handed to the agent. ``low`` is the spike's, and the reason it is not
#: higher is that this agent's job is mechanical — open a link, find the article, read it
#: out — while every token it thinks is billed at the article's expense.
DEFAULT_THINKING: Final = "low"
_THINKING_LEVELS: Final = ("off", "minimal", "low", "medium", "high", "xhigh", "max")


class EnrichConfigError(RuntimeError):
    """The environment names something this process cannot honour."""


@dataclass(frozen=True)
class Toolchain:
    """Where each piece of the real runner is, and whether all of them are there.

    Resolved at startup rather than at the first run, for the ``motet-vault[kms]`` reason
    AGENTS.md gives: a missing dependency discovered inside a request is a 500 an hour
    after the deploy with nothing tying it to the change. Here it is a health field, a
    startup line, and an assertion ``bin/build-images`` can put to a real container.
    """

    root: Path
    pi: str | None
    adapter: Path | None
    browser_server: Path | None
    harness: Path | None
    chromium: bool
    detail: str | None

    @property
    def ready(self) -> bool:
        return self.detail is None


def resolve_toolchain(env: Mapping[str, str] | None = None) -> Toolchain:
    """Find the pi CLI, the MCP adapter, the stealth browser server and a Chromium.

    ``chromium`` is asked of Playwright's own browser directory rather than by running
    anything: launching a browser to prove a browser can launch costs a second and a
    hundred megabytes of memory on a health check the platform polls.
    """
    environ = os.environ if env is None else env
    root = Path(environ.get(TOOLCHAIN_DIR_ENV, DEFAULT_TOOLCHAIN_DIR))
    modules = root / "node_modules"
    pi = shutil.which("pi", path=str(modules / ".bin")) or shutil.which("pi")
    adapter = modules / "pi-mcp-adapter"
    browser_server = modules / "playwright-stealth-mcp-server"
    harness = root / "harness" / "browser-mcp.mjs"
    # Playwright installs browsers outside node_modules; PLAYWRIGHT_BROWSERS_PATH is what
    # the image sets so the non-root runtime user can read them.
    browsers = Path(environ.get("PLAYWRIGHT_BROWSERS_PATH", "/ms-playwright"))
    chromium = browsers.is_dir() and any(browsers.glob("chromium*"))

    missing = [
        name
        for name, present in (
            ("the pi CLI", pi is not None),
            ("pi-mcp-adapter", adapter.is_dir()),
            ("playwright-stealth-mcp-server", browser_server.is_dir()),
            ("the browser harness", harness.is_file()),
            (f"a Chromium under {browsers}", chromium),
        )
        if not present
    ]
    return Toolchain(
        root=root,
        pi=pi,
        adapter=adapter if adapter.is_dir() else None,
        browser_server=browser_server if browser_server.is_dir() else None,
        harness=harness if harness.is_file() else None,
        chromium=chromium,
        detail=None if not missing else "missing: " + ", ".join(missing),
    )


@dataclass(frozen=True)
class EnrichSettings:
    """This process's whole configuration."""

    mode: Mode
    service_token: str
    model: str
    thinking: str
    max_usd: float
    max_tool_calls: int
    timeout_seconds: int
    openrouter_key: str
    toolchain: Toolchain

    @property
    def authenticated(self) -> bool:
        return bool(self.service_token)

    @property
    def dormant_reason(self) -> str | None:
        """Why a real run would fail right now, or ``None``.

        Reported rather than raised, for ``vault_ready``'s reason: a service that cannot
        run and one nobody has asked to run look identical from outside, and refusing to
        boot would take the health route down with it.
        """
        if self.mode == "fake":
            return None
        if not self.openrouter_key:
            return f"{OPENROUTER_KEY_ENV} is unset"
        return self.toolchain.detail

    def clamp(
        self, max_usd: float, max_tool_calls: int, timeout_seconds: int
    ) -> tuple[float, int, int]:
        """A caller may ask for less than this deployment allows, never for more.

        The caller is the worker, which reads its own copy of the same variables — so in
        the ordinary case the two agree and this changes nothing. It exists for the case
        where they do not: a worker rolled out ahead of this service, or a bug in the
        payload, must not be able to turn a bounded run into an unbounded one.
        """
        return (
            min(max_usd, self.max_usd),
            min(max_tool_calls, self.max_tool_calls),
            min(timeout_seconds, self.timeout_seconds),
        )


def _positive_float(environ: Mapping[str, str], name: str, default: float) -> float:
    raw = environ.get(name, "").strip()
    if not raw:
        return default
    try:
        value = float(raw)
    except ValueError as exc:
        raise EnrichConfigError(f"{name} must be a number, got {raw!r}") from exc
    if value <= 0:
        raise EnrichConfigError(f"{name} must be greater than zero, got {raw!r}")
    return value


def _positive_int(environ: Mapping[str, str], name: str, default: int) -> int:
    raw = environ.get(name, "").strip()
    if not raw:
        return default
    try:
        value = int(raw)
    except ValueError as exc:
        raise EnrichConfigError(f"{name} must be a whole number, got {raw!r}") from exc
    if value <= 0:
        raise EnrichConfigError(f"{name} must be greater than zero, got {raw!r}")
    return value


def load_settings(env: Mapping[str, str] | None = None) -> EnrichSettings:
    """Read and validate the environment.

    Raises :class:`EnrichConfigError` for a value this process could not honour — an
    unknown model slug, a nonsense cap. That is the startup crash AGENTS.md asks for on the
    LLM seam, for the same reason: a slug nothing checked is a vendor refusal mid-run, and
    a run costs money before it gets there.
    """
    environ = os.environ if env is None else env
    model = environ.get(MODEL_ENV, "").strip() or DEFAULT_MODEL
    if model not in KNOWN_MODELS:
        raise EnrichConfigError(
            f"{MODEL_ENV}={model!r} is not in the model catalogue. Add it to "
            "motet_inference.llm.config.KNOWN_MODELS and verify it with "
            "bin/check-openrouter-models — this service prices the run from that row."
        )
    thinking = environ.get(THINKING_ENV, "").strip().lower() or DEFAULT_THINKING
    if thinking not in _THINKING_LEVELS:
        raise EnrichConfigError(
            f"{THINKING_ENV}={thinking!r} is not one of {', '.join(_THINKING_LEVELS)}"
        )
    return EnrichSettings(
        mode=current_mode(environ),
        service_token=environ.get(SERVICE_TOKEN_ENV, "").strip(),
        model=model,
        thinking=thinking,
        max_usd=_positive_float(environ, MAX_USD_ENV, DEFAULT_MAX_USD),
        max_tool_calls=_positive_int(environ, MAX_TOOL_CALLS_ENV, DEFAULT_MAX_TOOL_CALLS),
        timeout_seconds=_positive_int(environ, TIMEOUT_ENV, DEFAULT_TIMEOUT_SECONDS),
        openrouter_key=environ.get(OPENROUTER_KEY_ENV, "").strip(),
        toolchain=resolve_toolchain(environ),
    )
