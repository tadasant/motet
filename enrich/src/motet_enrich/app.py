"""`motet-enrich` as an HTTP service, built to run on Cloud Run.

```
POST /v1/enrich          one item, with the credentials that item needs -> the article
GET  /internal/health    what is wired, and what is dormant
```

**Why this is a service of its own at all** is design option D2 (motet#102, and the infra
half in tadasant-internal#2837), and the reason is one sentence: *any process in a Cloud
Run container can mint that container's service-account token from the metadata server*.
The enrichment run is third-party npm driving a browser over pages nobody at Motet wrote,
with the owner's mailbox in reach. Running it inside ``motet-worker`` would hand that code
KMS decrypt and Cloud SQL whatever its environment looked like. Here it runs under an
identity with no project roles at all, so the worst an injected instruction can reach is
the one run's own credentials.

**A service rather than a job**, which is the infra issue's refinement of the option: a
Cloud Run job takes per-run input only through execution overrides, which would record the
decrypted credentials in the execution's spec and need ``run.jobs.runWithOverrides``, a
permission the worker does not and should not hold. A request body carries them in transit
and nowhere else.

**Two doors, and they answer different questions.** Cloud Run's IAM check — a Google ID
token in ``X-Serverless-Authorization``, audience the service URL, granted to the worker's
service account alone — is consumed by the platform before a byte reaches this process.
:data:`~motet_enrich.config.SERVICE_TOKEN_ENV` is the inner one, checked here in constant
time, and it is what makes a container that becomes reachable some other way still refuse
to spend an OpenRouter key.
"""

from __future__ import annotations

import hmac
import logging
import re
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Annotated, Final

from fastapi import Depends, FastAPI, Header, HTTPException, status

from . import obs
from .config import SERVICE_TOKEN_ENV, EnrichSettings, load_settings
from .contract import EnrichHealth, EnrichRequest, EnrichResult
from .runner import Runner, build_runner

logger = logging.getLogger("motet.enrich.app")

#: Health, and deliberately **not** ``/healthz``: Cloud Run's frontend answers that path
#: with its own 404 before the request reaches the container (motet#16). See
#: ``motet_api.main`` for the full account.
HEALTH_PATH: Final = "/internal/health"

#: Namespaces the platform claims, prefix-matched. A third copy of
#: ``motet_api.main.PLATFORM_RESERVED_PATHS`` — like ``motet_voice.app``'s, and for the same
#: reason: this package depends on no Motet-side module that holds it, and a shared package
#: for one tuple would be a dependency edge bought for nothing. **Keep them in step.**
PLATFORM_RESERVED_PATHS: Final = ("/healthz", "/_ah")

#: The shape ``revision`` insists on before this unauthenticated route will repeat it. A
#: copy of ``motet_api.main.REVISION_PATTERN``, for that constant's disclosure argument:
#: this repo is public and ``service.version`` is set by the private one.
REVISION_PATTERN: Final = re.compile(r"[A-Za-z0-9_-]{1,64}")


def publishable_revision(service_version: str | None) -> str | None:
    if not service_version:
        return None
    return service_version if REVISION_PATTERN.fullmatch(service_version) else None


@asynccontextmanager
async def lifespan(target: FastAPI) -> AsyncIterator[None]:
    """Resolve the configuration once, loudly, and hold it for the process.

    Building the runner here rather than per request is what makes the toolchain's absence
    a startup line and a health field instead of a 500 inside the first enrichment — the
    ``motet-vault[kms]`` lesson AGENTS.md draws, on a toolchain instead of an SDK. It is
    still not fatal: a revision that cannot run must still serve health, or the platform
    reports "the container failed to start" and says nothing about why.
    """
    telemetry = obs.configure()
    settings: EnrichSettings = target.state.settings
    if telemetry.service_version and publishable_revision(telemetry.service_version) is None:
        obs.logger.error(
            "obs: service.version is not a shape %s may repeat, so it reports revision=null",
            HEALTH_PATH,
        )
    if not settings.authenticated:
        obs.logger.warning(
            "%s is unset: anyone who can reach this process can start an agent run and "
            "spend this deployment's OpenRouter key.",
            SERVICE_TOKEN_ENV,
        )
    dormant = settings.dormant_reason
    if dormant:
        obs.logger.error("enrich: real mode is dormant — %s", dormant)
    else:
        obs.logger.info(
            "enrich: mode=%s model=%s thinking=%s caps=$%.2f/%d calls/%ds",
            settings.mode,
            settings.model,
            settings.thinking,
            settings.max_usd,
            settings.max_tool_calls,
            settings.timeout_seconds,
        )
    try:
        yield
    finally:
        # Cloud Run stops a revision with SIGTERM and the SDK's atexit hook does not save
        # us: up to a batch interval of spans and logs is otherwise lost on every deploy.
        obs.shutdown()


def create_app(settings: EnrichSettings | None = None, runner: Runner | None = None) -> FastAPI:
    """Build the app. A factory, because building one reads the environment.

    ``settings`` and ``runner`` are injection points for tests, not a second deployment
    shape — the real process passes neither.
    """
    resolved = settings if settings is not None else load_settings()
    app = FastAPI(
        lifespan=lifespan,
        title="Motet enrichment service",
        version="0.1.0",
        # Not in `openapi.yaml`: that document is the seam between `motet-api` and the SPA,
        # and this service is reachable only by the worker, server to server.
        openapi_url=None,
    )
    app.state.settings = resolved
    app.state.runner = runner if runner is not None else build_runner(resolved)
    # Before the lifespan runs: instrumenting adds ASGI middleware and Starlette refuses
    # that once the middleware stack is built, which it is by the time a lifespan arrives.
    obs.instrument(app)

    def require_caller(
        authorization: Annotated[str | None, Header()] = None,
    ) -> None:
        """The inner door. Constant-time, and unset means open — for a laptop only.

        Unset is a *warning* at startup rather than a refusal because that is the shape
        every other service in this repo uses for its bearer, and because a deployment
        reaches this process only through an IAM grant the private repo makes to one
        service account.
        """
        expected = resolved.service_token
        if not expected:
            return
        presented = (authorization or "").removeprefix("Bearer ").strip()
        if not hmac.compare_digest(presented, expected):
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="this service needs the shared enrichment bearer",
            )

    @app.get(HEALTH_PATH, response_model=EnrichHealth, tags=["ops"])
    def health() -> EnrichHealth:
        """Liveness, plus what is actually wired.

        ``toolchain_ready`` is the field this route exists for. An image built without the
        npm half and one whose Chromium failed to install look identical from outside, and
        the failure would otherwise first appear as a failed enrichment for a real item.
        """
        current = obs.status()
        return EnrichHealth(
            status="ok",
            service=current.service_name,
            revision=publishable_revision(current.service_version),
            telemetry_configured=current.otlp_configured,
            telemetry_exporting=current.exporting,
            errors_configured=current.errors_configured,
            authenticated=resolved.authenticated,
            inference_mode=resolved.mode,
            toolchain_ready=resolved.toolchain.ready,
            # The missing piece's *name*, never its path: the toolchain root is a fact
            # about the image and this route is unauthenticated. `Toolchain.detail` is
            # built from the names alone for that reason.
            toolchain_detail=resolved.toolchain.detail,
            model=resolved.model,
            max_usd_per_item=resolved.max_usd,
            max_tool_calls=resolved.max_tool_calls,
            timeout_seconds=resolved.timeout_seconds,
        )

    @app.post(
        "/v1/enrich",
        response_model=EnrichResult,
        tags=["enrich"],
        # A route-level dependency rather than a parameter: the check takes nothing from
        # the caller and gives nothing back, and as a parameter FastAPI reads its `None`
        # annotation as a required query field.
        dependencies=[Depends(require_caller)],
    )
    def enrich(body: EnrichRequest) -> EnrichResult:
        """Run one enrichment and answer with what it produced.

        **A run that went badly is a 200 with a status, not an error code.** Every one of
        ``blocked``, ``capped``, ``timeout`` and ``failed`` means the same thing to the
        caller — keep the newsletter's preview, record the run — and turning them into 5xx
        would put them on the worker's retry ladder, where the second attempt spends the
        same money to meet the same wall. A 5xx from here means *this process* is broken.
        """
        max_usd, max_tool_calls, timeout_seconds = resolved.clamp(
            body.caps.max_usd, body.caps.max_tool_calls, body.caps.timeout_seconds
        )
        caps = body.caps.model_copy(
            update={
                "max_usd": max_usd,
                "max_tool_calls": max_tool_calls,
                "timeout_seconds": timeout_seconds,
            }
        )
        runner: Runner = app.state.runner
        result = runner.run(body, caps)
        obs.record_run(result, domain=body.site.domain)
        logger.info(
            "enrich %s: status=%s calls=%d cost=$%.4f login=%s chars=%d in %.1fs",
            body.item_id,
            result.status,
            result.tool_calls,
            result.cost_usd,
            result.login_performed,
            len(result.article_markdown or ""),
            result.duration_seconds,
        )
        return result

    return app
