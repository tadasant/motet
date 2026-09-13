"""The Motet HTTP API.

Routes are versioned under ``/v1`` so the contract can move without breaking a shipped
client — invariant 1 means clients only ever speak *this* protocol, so it is the one that
has to stay stable. The feed is deliberately outside ``/v1``: ``/feed.xml`` is a URL a
human pastes into a podcast client, and a version number in it would be a version number
in something that has to keep working for years.

**The API never runs inference.** It writes rows and enqueues jobs; workers call models.
That is why it validates LLM *configuration* at startup but never resolves the key —
mounting the one vendor secret in the system into the internet-facing service would widen
the blast radius for no functional gain.
"""

from __future__ import annotations

import importlib
import logging
import os
import re
from collections.abc import AsyncIterator, Mapping, Sequence
from contextlib import asynccontextmanager, suppress
from datetime import UTC, datetime
from typing import Annotated, Any, Final
from urllib.parse import urlencode, urlsplit

import psycopg
from fastapi import Depends, FastAPI, HTTPException, Path, Query, Request, Response, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, RedirectResponse
from motet_db import (
    CredentialPurpose,
    Highlight,
    IngestionStatus,
    RuleError,
    SmartRule,
    SourceItemState,
    SourceKind,
    StoredEpisode,
    StoredNewsItem,
    StoredSource,
    phase2,
    repo,
)
from motet_db import auth as auth_repo
from motet_db import connectors as connector_repo
from motet_db import settings as settings_repo
from motet_db import waitlist as waitlist_repo
from motet_inference.llm import LlmConfigError, LlmStage
from motet_inference.llm import load_config as load_llm_config
from motet_sources import (
    GMAIL_MODIFY_SCOPE,
    GMAIL_READONLY_SCOPE,
    LABEL_SYNC_SCOPES,
    PROVIDER,
    LabelSettings,
    LabelSettingsError,
    SourceError,
    build_oauth_client,
    new_oauth_state,
    new_pkce_pair,
)
from motet_sources.labels import (
    CONFIG_KEY as LABEL_SYNC_CONFIG_KEY,
)
from motet_sources.labels import (
    MAILBOX_ADDRESS_KEY,
    catalog_fetched_at,
    catalog_from_sync_state,
    pickable,
)
from motet_sources.mcp_oauth import PROVIDER as MCP_PROVIDER
from motet_sources.mcp_oauth import (
    AuthorizationServer,
    McpOAuthError,
    RegistrationUnsupportedError,
    UnsafeUrlError,
    authorization_url,
    build_mcp_oauth_client,
)
from motet_storage import ObjectStore, StorageError
from motet_vault import DekWrapper, VaultError, vault_status
from motet_workers import (
    DEFAULT_MAX_ATTEMPTS,
    enqueue_episode,
    enqueue_integration,
    enqueue_paste,
    enqueue_smart_episode,
    enqueue_source_poll,
    queue_readiness,
    source_query,
)
from motet_workers.queues import PIPELINE
from pydantic import ValidationError
from starlette.requests import ClientDisconnect

from . import admin_llm, obs
from .auth import (
    ALLOWED_EMAILS_ENV,
    LOGIN_SCOPES,
    IdentityConfigError,
    IdentityError,
    IdentityUnavailableError,
    build_identity_provider,
    is_allowed,
    is_login_state,
    new_login_state,
    new_nonce,
)
from .auth import PROVIDER as GOOGLE_PROVIDER
from .config import APP_BASE_URL_ENV, CALLBACK_PATH, Settings
from .connectors import (
    ConnectorInputError,
    connector_response,
    connector_spec,
    is_connector_state,
    new_connector_state,
)
from .deps import (
    Caller,
    connection,
    dek_wrapper,
    drain_nudge,
    drain_trigger,
    is_admin,
    public_base_url,
    require_admin,
    require_api_token,
    require_caller,
    require_feed_token,
    settings,
    store,
)
from .drain import ENABLED_ENV, DrainNudge, DrainReason, DrainTrigger
from .feed import (
    ARTWORK_MEDIA_TYPE,
    ARTWORK_PATH,
    FeedMetadata,
    artwork_bytes,
    artwork_version,
    feed_url,
    render_feed,
)
from .mcp import registry as mcp_registry
from .mcp.oauth import complete_authorization as complete_mcp_oauth
from .mcp.oauth import oauth_setup as mcp_oauth_setup
from .schemas import (
    AdminEpisodeCounts,
    AdminJobCounts,
    AdminJobResponse,
    AdminLlmSpendResponse,
    AdminNewsItemCounts,
    AdminOverviewResponse,
    AdminQueueResponse,
    AdminSourceItemCounts,
    AdminUserResponse,
    AdminWaitlistResponse,
    AdminWaitlistSignupResponse,
    AuthorizeConnectorRequest,
    AuthorizeConnectorResponse,
    ClaimModel,
    CompleteLoginRequest,
    ConnectorOAuthCallbackRequest,
    ConnectorResponse,
    ConnectSourceRequest,
    ConnectSourceResponse,
    CreateConnectorRequest,
    CreateEpisodeRequest,
    CreateSmartEpisodeRequest,
    DedupDecisionResponse,
    DismissResponse,
    EpisodeResponse,
    FeedInfoResponse,
    HealthResponse,
    HeldSourceItemResponse,
    HighlightResponse,
    IngestionItemResponse,
    IntegrateResponse,
    LabelSyncRequest,
    LabelSyncResponse,
    ListenProgressRequest,
    ListenProgressResponse,
    LlmConfigResponse,
    LlmStageConfigUpdate,
    LoginResponse,
    MarkListenedResponse,
    McpAuthorizationResponse,
    NewsItemResponse,
    NewsItemSourceRef,
    OAuthCallbackRequest,
    PasteRequest,
    ProcessingStatusResponse,
    ProcessingStepResponse,
    QueueHeartbeatResponse,
    QueueReadinessResponse,
    ReadStateRequest,
    ReauthorizeSourceRequest,
    RedeemNativeLoginRequest,
    RevokedResponse,
    SaveHighlightRequest,
    SegmentResponse,
    SessionResponse,
    SourceItemDetailResponse,
    SourceItemIdsRequest,
    SourceItemJobResponse,
    SourceItemNewsItemResponse,
    SourceItemPulledStage,
    SourceItemResponse,
    SourceResponse,
    SourceSpanModel,
    SourceSyncResult,
    StartLoginRequest,
    StartLoginResponse,
    StartNativeLoginRequest,
    StartNativeLoginResponse,
    StartVoiceSessionRequest,
    VoiceSessionResponse,
    VoiceStatusResponse,
    WaitlistJoinResponse,
)
from .shownotes import SourceExcerpt, chapters_json, transcript_vtt
from .voice import (
    VoiceConfig,
    VoiceStarter,
    VoiceUnavailableError,
    build_starter,
    session_config,
)
from .waitlist import Outcome as WaitlistOutcome
from .waitlist import Submission, read_submission
from .waitlist import answer as waitlist_answer

logger = logging.getLogger("motet.api")

#: ``scope="function"`` is load-bearing: it commits — and fires the drain nudge — before the
#: response starts rather than after it is sent. See ``deps.connection``; every
#: ``Depends(connection)`` must say the same, or a request gets two connections.
Conn = Annotated[psycopg.Connection[Any], Depends(connection, scope="function")]
User = Annotated[str, Depends(require_api_token)]
#: The caller *and how they proved it* — a signed-in browser, the shared API token, or an
#: unlocked deployment. Only the sign-in routes need the distinction; every other route
#: takes ``User``, because there is one account and the answer is always the same row.
Who = Annotated[Caller, Depends(require_caller)]
#: A caller who may read every user's data: a signed-in session on MOTET_ADMIN_EMAILS. Every
#: route under ``/v1/admin`` takes it, and a test walks the app to hold that true.
Admin = Annotated[Caller, Depends(require_admin)]
FeedUser = Annotated[str, Depends(require_feed_token)]
Config = Annotated[Settings, Depends(settings)]
Store = Annotated[ObjectStore, Depends(store)]
#: The **encrypt-only** half of the credential vault. The API seals third-party tokens
#: because the OAuth callback lands on an HTTP route; it must never hold anything that can
#: unseal one (invariant 8). `DekWrapper` has no `unwrap`, and the deployed service
#: account has no `useToDecrypt` — the type is the reminder, IAM is the control.
Wrapper = Annotated[DekWrapper, Depends(dek_wrapper)]
#: Whether this deployment starts a worker execution when it enqueues work. Reported on
#: ``/internal/health``; off unless ``MOTET_DRAIN_TRIGGER`` opts in.
Trigger = Annotated[DrainTrigger, Depends(drain_trigger)]
#: This request's intent to nudge the worker. A route **arms** it beside its enqueue and
#: `deps.connection` fires it after the commit — see `DrainNudge` for why the two are
#: split. Every route taking this also takes `Conn`, which is what guarantees a fire.
Nudge = Annotated[DrainNudge, Depends(drain_nudge)]


@asynccontextmanager
async def lifespan(target: FastAPI) -> AsyncIterator[None]:
    """Refuse to serve at all rather than serve a request we cannot fulfil.

    An unknown model slug or a nonsense effort stops the process here, where Cloud Run
    reports a failed revision and never shifts traffic to it. Discovering the same fact on
    the first inference request means a 500 an hour after the deploy, with nothing tying it
    to the change that caused it.

    **Config only — deliberately not the credential.** ``validate_startup`` also resolves
    the API key, and the worker entry point calls it for exactly that reason. Requiring it
    here would mean mounting the one vendor secret in the system into the *internet-facing*
    service, which in Phase 1 never calls a model at all: inference runs in workers. The
    day the API calls a model directly, this becomes ``validate_startup`` and the key
    becomes its business.
    """
    # First, so that everything below is logged through the configured handler rather
    # than through whatever `logging` falls back to.
    telemetry = obs.configure()
    if telemetry.service_version and publishable_revision(telemetry.service_version) is None:
        obs.logger.error(
            "obs: service.version is not a shape %s may repeat, so it reports "
            "revision=null. It must be letters, digits, '_' and '-' — a commit SHA, or "
            "the deploy's bootstrap sentinel. An image reference or a hostname is "
            "refused because that route is unauthenticated and this repo is public.",
            HEALTH_PATH,
        )
    config = load_llm_config()
    obs.logger.info("llm: %s", config.describe())
    current = Settings.from_env()
    if not current.cors_origins:
        obs.logger.warning(
            "%s is unset: no browser origin is allowed to call /v1. The SPA is served "
            "from a different hostname than this API in every deployed environment, so "
            "this means the web app cannot reach it at all.",
            APP_BASE_URL_ENV,
        )
    if not current.authenticated:
        obs.logger.warning(
            "MOTET_API_TOKEN is unset: /v1 is open to anyone who can reach this process. "
            "Fine on a laptop; on a deployed environment it means anyone can ingest text "
            "and spend inference budget."
        )
    # Said at startup, not only on the health route, because the vault is exercised
    # exactly once per mailbox — by a human, at the end of a consent flow — and a
    # deployment that cannot seal has no other occasion to mention it. Not fatal: the
    # rest of the API works, and refusing to boot over a dormant Phase 2 path would
    # take the whole product down for a feature nobody was using.
    vault = vault_status()
    if vault.ready:
        obs.logger.info("vault: backend=%s ready=true", vault.backend)
    else:
        obs.logger.error(
            "vault: backend=%s ready=false — connecting a mailbox will fail after the "
            "provider has already issued a token: %s",
            vault.backend,
            vault.detail,
        )
    # Built here rather than on the first paste, for the same reason as the vault line
    # above: an inert trigger and a working one are indistinguishable from outside, and a
    # value that will not parse — or an image missing `google-auth` — should be said once,
    # loudly, at startup rather than swallowed inside the best-effort call it breaks.
    # `build_trigger` logs its own ERROR for those two; this line covers the quiet case.
    if drain_trigger().enabled:
        obs.logger.info("drain: enqueuing work will start a worker execution immediately")
    else:
        obs.logger.info(
            "drain: %s is off or unusable, so enqueued work waits for the next worker "
            "run rather than starting one immediately",
            ENABLED_ENV,
        )
    # Said once at startup for the reason every line above is: a mount that registered
    # nothing and an authorization server that is switched off both look, from outside,
    # exactly like an MCP server nobody has connected to yet (invariant 11's trap).
    obs.logger.info(
        "mcp: /mcp serves %d tools; OAuth for MCP clients is %s",
        len(mcp_registry.ALL_TOOLS),
        "on" if mcp_oauth_setup(current) else "off (the /v1 bearer only)",
    )
    try:
        # The MCP transport's task group. Without it the first /mcp request fails with
        # "Task group is not initialized" — the step every mounted MCP server forgets.
        async with target.state.mcp.running():
            yield
    finally:
        # Cloud Run stops a revision with SIGTERM, and the OTel SDK's own `atexit` hook
        # does not save us: measured locally, a terminate immediately after a request
        # exported *nothing at all*, because the batch processors were still holding it.
        # Up to a batch interval of spans and logs is therefore lost on every deploy and
        # every scale-down — including, for a revision that is failing to start, the only
        # records that would say why.
        obs.shutdown()


app = FastAPI(
    lifespan=lifespan,
    title="Motet API",
    version="0.1.0",
    description=(
        "Motet turns a reading backlog into an interactive podcast.\n\n"
        "This document is generated from the FastAPI app and committed as `openapi.yaml`; "
        "the TypeScript client is generated from it in turn. Do not hand-edit either."
    ),
)

#: The MCP server at `/mcp`, and the OAuth endpoints MCP clients authorize with (motet#111).
#: Plain Starlette routes rather than `APIRoute`s, so they add nothing to `openapi.yaml`;
#: `api/tests/test_mcp_parity.py` is what holds the tools to the routes below.
# Imported here rather than at the top, and by name rather than by `import`: the tool
# modules call this module's handlers, so a static import would make `main` and the tools
# one import cycle, and a type checker resolves a cycle in whatever order it likes.
MCP: Any = importlib.import_module("motet_api.mcp.server").mount(app.router.routes)
# The lifespan runs the mount that belongs to *its* app, not whatever `MCP` names now: a
# reload of this module re-runs it in the same globals, so `MCP` would name a new mount
# while an app built before the reload kept routing to the old one, which never starts.
app.state.mcp = MCP


def configure_cors(target: FastAPI, config: Settings) -> None:
    """Allow the SPA's origin to call ``/v1`` from a browser, and nothing else.

    The SPA is on ``app.`` and this API is on ``api.`` — two origins, so every call the
    web app makes is cross-origin and a browser blocks it by default. Without this the
    SPA loads, renders, and fails every request with an opaque network error that says
    nothing about the cause.

    A function rather than inline setup so that the tests can apply *this* policy to a
    throwaway app. Retyping the same arguments in a test would mean the test still passed
    after someone changed the real ones, which is the failure mode a CORS test exists to
    prevent.
    """
    origins = config.cors_origins
    if not origins:
        return
    target.add_middleware(
        CORSMiddleware,
        # Exact origins, never `*`. See Settings.cors_origins.
        allow_origins=origins,
        # `Authorization` is a non-simple header, so every request the SPA makes is
        # preflighted and this list is what makes the preflight pass.
        allow_headers=["Authorization", "Content-Type"],
        # Every verb any `/v1` route declares, and `test_deploy_wiring` walks the route
        # table to prove it. A route reachable from the SPA's own generated client but
        # missing from this list fails only in a browser, only cross-origin, and only as
        # a preflight nobody sees — which is the least diagnosable shape a bug has here.
        allow_methods=["GET", "POST", "PUT", "DELETE", "OPTIONS"],
        # Deliberately NOT `allow_credentials=True`. The client never sets
        # `credentials: 'include'` — it carries its token in `Authorization`, which is not
        # a credential in the CORS sense — so allowing them buys nothing, and it would
        # opt this API into honouring cookie-bearing cross-origin requests if anything
        # ever set a cookie.
    )


class UnhandledErrorMiddleware:
    """Turn an exception nobody caught into a 500 the *browser* is allowed to read.

    **This is the middleware that makes a bug diagnosable from a laptop**, and it exists
    because of how the Gmail-connect failure presented. Starlette's own
    ``ServerErrorMiddleware`` sits outside every middleware added here, including
    ``CORSMiddleware`` — so an exception that escapes a route is answered by a 500 that
    never passes through the CORS layer and therefore carries no
    ``Access-Control-Allow-Origin``. A browser refuses to hand that response to the
    caller, and ``fetch`` rejects with ``TypeError: Failed to fetch``: no status, no body,
    no clue. The SPA showed the user that string, and it was the only evidence there was.

    **It must be the innermost middleware, and the two lines it needs are why.** The
    real stack — walked, not assumed, and pinned by ``test_deploy_wiring.py`` — is::

        ServerError → OpenTelemetry → ServerError → OTelExceptionHandler
                    → CORS → UnhandledError → ExceptionMiddleware → routes

    OpenTelemetry is **outermost**, not innermost: ``FastAPIInstrumentor`` patches
    ``build_middleware_stack`` rather than calling ``add_middleware``, so it wraps
    everything an application adds. Catching an exception here therefore stops it reaching
    two things that were quietly relying on seeing it, and each is replaced deliberately:

    * **OTel's own exception handler**, which records the type, message and stacktrace on
      the request span. ``obs.record_exception`` does that here instead. Without it the
      span keeps its ERROR status — derived from the 500 — and loses everything that says
      *what* failed, which under invariant 11 is the only view of production there is.
    * **The Sentry SDK's outermost capture**, which is what puts the error in GlitchTip.
      ``logger.exception`` is what replaces it, through the SDK's logging integration and
      carrying the same exception. Measured both ways, guarded and not, before shipping.

    Neither call is decoration; deleting either one deletes a signal silently, which is
    the failure mode this whole middleware exists to end.

    The body says nothing about the exception on purpose — a stack trace or a vendor
    message can name a KMS key path or a connection string, and this response crosses an
    origin. The detail belongs in the log line, which goes to the obs stack.
    """

    def __init__(self, app: Any) -> None:
        self.app = app

    async def __call__(self, scope: Any, receive: Any, send: Any) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        started = False

        async def watched_send(message: Any) -> None:
            nonlocal started
            if message["type"] == "http.response.start":
                started = True
            await send(message)

        try:
            await self.app(scope, receive, watched_send)
        except ClientDisconnect:
            # Not a fault: somebody closed the tab, or a podcast client stopped pulling an
            # episode. Reported as an error it would be one GlitchTip event per abandoned
            # download, which is how an error channel becomes something nobody reads.
            raise
        except Exception as exc:
            obs.record_exception(exc)
            logger.exception(
                "unhandled error serving %s %s", scope.get("method"), scope.get("path")
            )
            if started:
                # The response is already on the wire and cannot be replaced. Re-raising
                # hands it back to ServerErrorMiddleware, which is what closes the
                # connection — the browser sees a truncated response either way.
                raise
            await JSONResponse(
                {"detail": "Something failed on our side. The error was recorded."},
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            )(scope, receive, send)


# At import, deliberately. Instrumenting adds ASGI middleware, and Starlette refuses that
# once the middleware stack is built — which it is by the time the lifespan runs, so doing
# this next to `obs.configure()` would raise. No provider exists yet and that is fine: the
# middleware holds OpenTelemetry's proxy tracer, which resolves the moment the lifespan
# installs the real one.
obs.instrument(app)

# Added before `configure_cors`, because `add_middleware` prepends: CORS ends up outside
# this, which is what puts `Access-Control-Allow-Origin` on the 500 it returns. Both end
# up *inside* OpenTelemetry regardless of the order here — see the class docstring.
app.add_middleware(UnhandledErrorMiddleware)

# Read once at import rather than per request: an origin policy that could change under a
# running process would be a policy nobody could reason about, and Cloud Run gives a new
# revision for an environment change anyway.
configure_cors(app, Settings.from_env())


#: Where health is served, and it is deliberately **not** ``/healthz``.
#:
#: Google's Cloud Run frontend answers ``/healthz`` itself, with its own HTML 404, before
#: the request reaches the container — on the ``run.app`` URL and on a custom domain, over
#: HTTP/1.1 and HTTP/2 alike. The route existed and was declared in this document the
#: whole time; nothing outside the container could read it, which is the exact failure a
#: health endpoint exists to prevent. See motet#16.
#:
#: The replacement is namespaced under a segment this application owns rather than one the
#: platform might claim. ``/livez`` and ``/readyz`` were rejected because they are the same
#: Kubernetes-style family as the path that was intercepted, and a leading-underscore path
#: was rejected because ``/_ah/`` is reserved territory on Google's own infrastructure.
HEALTH_PATH = "/internal/health"

#: Paths that must never be used for anything this application needs to answer.
#:
#: A prefix match, because the reservation is a namespace rather than one URL. This exists
#: so the collision above cannot come back silently: ``api/tests/test_reserved_paths.py``
#: walks every declared route against it.
#:
#: Copied — not shared — in ``motet_voice.app`` and in ``bin/build-images``, which runs the
#: same claim against a real container. Keep all three in step.
PLATFORM_RESERVED_PATHS = ("/healthz", "/_ah")

#: The shape ``revision`` insists on before this route will repeat it.
#:
#: The value arrives from the deploy, as OTel's ``service.version``, and nothing in this
#: repo can see what the private infrastructure repo puts there. This route is
#: unauthenticated and this repo is public, so relaying an infrastructure-controlled
#: string verbatim would make the field's whole disclosure argument — *a commit SHA
#: discloses nothing ``git log`` does not* — a promise about a variable in the other repo
#: rather than a property of this one. ``vault_ready`` sets the precedent one field along:
#: its ``detail`` is withheld because a KMS refusal quotes the key resource path.
#:
#: A commit SHA is letters, digits, ``_`` and ``-``, and so is the deploy's own
#: ``bootstrap`` sentinel. An image reference, a hostname, a bucket path and a
#: service-account address all carry ``/``, ``:``, ``.`` or ``@`` — so refusing those
#: characters refuses every topology shape AGENTS.md names, structurally rather than by
#: asking the other repo to be careful. Setting ``service.version`` to the full image
#: reference is the realistic accident, and it would publish a project id and a registry
#: host to anyone on the internet.
#:
#: Copied — not shared — in ``motet_voice.app``, whose health reports ``revision`` too;
#: ``voice/tests/test_app.py`` reads this line to keep the two in step.
REVISION_PATTERN = re.compile(r"[A-Za-z0-9_-]{1,64}")


def publishable_revision(service_version: str | None) -> str | None:
    """The build label, when it is one a public route may repeat.

    A refused value reports as ``None``, which is the same answer as "nothing set it" —
    deliberately, because a second public field to distinguish them would be a second
    public field. The lifespan logs the difference instead, at ERROR, which is where an
    operator who knows the deploy sets it will look.
    """
    if service_version is None:
        return None
    return service_version if REVISION_PATTERN.fullmatch(service_version) else None


@app.get(HEALTH_PATH, response_model=HealthResponse, tags=["ops"])
def health(config: Config, trigger: Trigger) -> HealthResponse:
    """Liveness, plus whether telemetry and authentication are actually wired.

    The flags are not decoration. Exporters no-op silently when unconfigured, so without
    this an unmonitored process is indistinguishable from a quiet one — and an
    unauthenticated deployment is indistinguishable from a working one until the bill
    arrives.

    **"internal" in the path names an owned namespace, not a network boundary.** This
    route is unauthenticated and reachable by anyone who can reach the service — which is
    the point, since the whole reason motet#16 mattered is that health has to be askable
    from outside. Nothing secret goes in the response; a new field here is public.
    """
    current = obs.status()
    # `detail` is deliberately not returned: a KMS refusal quotes the key resource path,
    # and this route is public. The backend name and the flag are enough to tell a
    # deployment that cannot seal from one nobody has asked to.
    vault = vault_status()
    return HealthResponse(
        status="ok",
        service=current.service_name,
        # Which build is serving. Public, and deliberately so: a commit SHA discloses
        # nothing `git log` on this public repo does not, and it is not topology — no
        # project id, no bucket, no hostname, no service-account address. It is
        # `vault_backend`'s argument again: "this deployment is on the wrong revision" is
        # exactly the misconfiguration the field exists to surface.
        revision=publishable_revision(current.service_version),
        telemetry_configured=current.otlp_configured,
        telemetry_exporting=current.exporting,
        errors_configured=current.errors_configured,
        authenticated=config.authenticated,
        login_configured=config.login_configured,
        vault_backend=vault.backend,
        vault_ready=vault.ready,
        # Not the job's resource name: that is a project id and a region, which is
        # topology, and this route is public. The boolean is the whole question — "does
        # enqueuing start a worker here" — and it is `vault_ready`'s argument again, since
        # a deployment whose invoker grant never landed looks exactly like one nobody has
        # pasted into.
        drain_trigger=trigger.enabled,
        voice_configured=VoiceConfig.from_env().configured,
        inference_mode=config.inference_mode,
        settings_writable=settings_repo.settings_writable(os.environ),
        # Only where settings are writable — never in production — and cached for 30s.
        llm_overrides_in_force=admin_llm.overrides_in_force(config.database_url, os.environ),
        mcp_tools=len(mcp_registry.ALL_TOOLS),
        mcp_oauth_configured=mcp_oauth_setup(config) is not None,
    )


# --- signing in ----------------------------------------------------------------------
#
# **This is not a user system.** Motet has one account and signup is still Phase 3. What
# these four routes change is how a *browser* proves it may talk to /v1: a Google sign-in
# that mints a session, instead of MOTET_API_TOKEN typed into a text field. The bearer
# token keeps working everywhere it already works — the RSS feed, the iOS app, any script
# — it just stops being something a human types.
#
# **The allowlist is the security control, not Google.** This deployment's consent screen
# is published and unverified, so anyone on the internet with a Google account can reach
# the end of the flow. Completing it therefore proves identity and nothing else;
# MOTET_ALLOWED_EMAILS decides authorization, server-side, after the ID token verifies.
# Unset denies everybody, deliberately.
#
# The first two routes are **unauthenticated**, necessarily: they are how a browser that
# holds nothing gets something. Everything they can do is create a short-lived
# `oauth_states` row and, on a verified and allowlisted identity, a session.


@app.post("/v1/auth/google/start", response_model=StartLoginResponse, tags=["auth"])
def start_login(body: StartLoginRequest, conn: Conn, config: Config) -> StartLoginResponse:
    """Begin a sign-in: record the pending authorization and return a consent URL.

    The state and the PKCE verifier are stored rather than derived, for the same reason
    connecting a mailbox stores them — a callback that validated a state it recomputed
    from its own parameters would defend against nothing. The OIDC ``nonce`` goes in the
    same row: it travels out in the request and comes back inside the signed ID token, so
    checking it means remembering what was sent.

    Refused before anything is written when no allowlist is configured. Sending someone
    through Google's consent screen only to deny them afterwards is a worse answer than
    saying the deployment cannot do this yet.
    """
    if not config.allowed_emails:
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            f"{ALLOWED_EMAILS_ENV} is unset, so no Google account would be accepted and "
            "signing in is switched off. This deployment still takes the API token.",
        )

    redirect_uri = body.redirect_uri.strip()
    if not config.callback_uri_allowed(redirect_uri):
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            f"redirect_uri must be this deployment's app origin plus {CALLBACK_PATH}.",
        )

    # Starting a sign-in is necessarily unauthenticated, so this is the one route on the
    # API that lets anybody who can reach it write a row. Sweeping here bounds the table
    # by its own TTL rather than letting it grow at request rate: the normal outcome of a
    # sign-in is a closed tab, and nothing else was ever going to collect those.
    phase2.purge_expired_oauth_states(conn)
    auth_repo.purge_expired_sessions(conn)

    verifier, challenge = new_pkce_pair()
    state = new_login_state()
    nonce = new_nonce()
    phase2.start_oauth(
        conn,
        state=state,
        # The one account. A sign-in does not create a user and never has: it decides
        # whether this browser may act as the owner, which is the only thing to be.
        user_id=repo.OWNER_USER_ID,
        provider=GOOGLE_PROVIDER,
        source_id_=None,
        code_verifier=verifier,
        redirect_uri=redirect_uri,
        scopes=LOGIN_SCOPES,
        nonce=nonce,
    )

    try:
        url = build_identity_provider().authorization_url(
            redirect_uri=redirect_uri, state=state, nonce=nonce, code_challenge=challenge
        )
    except IdentityError as exc:
        # Real mode with no Google OAuth client provisioned. A 503 rather than a 500:
        # nothing is wrong with the request, the capability is not configured.
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, str(exc)) from exc

    return StartLoginResponse(authorization_url=url, state=state)


#: The URL scheme the iOS app's sign-in sheet waits for. The API builds the whole handoff
#: link from this constant; nothing a caller sends ever becomes part of it.
NATIVE_CALLBACK_SCHEME: Final = "motet"
NATIVE_HANDOFF_URI: Final = f"{NATIVE_CALLBACK_SCHEME}://signed-in"

#: The web app path an https handoff lands on, when MOTET_IOS_APP_LINK says the web app
#: serves an app-site-association file naming it. Apple only lets a sheet wait for such a
#: link if the app carries the associated-domains entitlement for that host, which is what
#: makes it unclaimable by another app — the custom scheme's one weakness.
NATIVE_HANDOFF_PATH: Final = "/app/signed-in"


def _native_handoff_url(config: Settings, code: str) -> str:
    """The link the sign-in sheet is watching for, with this code on it.

    Built only from this deployment's own configured origin and the literals above; nothing
    a caller sent ever reaches it.
    """
    query = urlencode({"code": code})
    origins = config.cors_origins
    if config.ios_app_link and origins:
        return f"{origins[0]}{NATIVE_HANDOFF_PATH}?{query}"
    return f"{NATIVE_HANDOFF_URI}?{query}"


@app.post("/v1/auth/native/start", response_model=StartNativeLoginResponse, tags=["auth"])
def start_native_login(
    body: StartNativeLoginRequest, conn: Conn, config: Config
) -> StartNativeLoginResponse:
    """Begin a sign-in for the iOS app: the web sign-in, returned to the app by a handoff.

    Decided by Tadas, 2026-09-13 (AGENTS.md, "The phone signs in through the web sign-in").
    The app opens the URL this returns in its system sign-in sheet. Google sends that
    sheet back to this deployment's *web app*, whose callback is the one already
    registered on the OAuth client — so the phone needs no Google client of its own — and
    the web app posts the code to ``/v1/auth/google/callback`` exactly as a browser does.
    The pending row carries the app's PKCE challenge, which is what makes that callback
    answer with a handoff link rather than a session.

    The redirect URI is built here from ``MOTET_APP_BASE_URL`` rather than taken from the
    caller: the app knows the API's address, not the web app's, and a caller-supplied
    redirect on an unauthenticated route is the shape ``start_login`` already refuses.
    """
    if not config.allowed_emails:
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            f"{ALLOWED_EMAILS_ENV} is unset, so no Google account would be accepted and "
            "signing in is switched off. This deployment still takes the API token.",
        )
    origins = config.cors_origins
    if not origins:
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            f"{APP_BASE_URL_ENV} is unset, so there is no web app for Google to return the "
            "sign-in to. Paste an API token instead.",
        )
    redirect_uri = f"{origins[0]}{CALLBACK_PATH}"

    phase2.purge_expired_oauth_states(conn)
    auth_repo.purge_expired_sessions(conn)
    auth_repo.purge_expired_handoffs(conn)

    verifier, challenge = new_pkce_pair()
    state = new_login_state()
    nonce = new_nonce()
    phase2.start_oauth(
        conn,
        state=state,
        user_id=repo.OWNER_USER_ID,
        provider=GOOGLE_PROVIDER,
        source_id_=None,
        code_verifier=verifier,
        redirect_uri=redirect_uri,
        scopes=LOGIN_SCOPES,
        nonce=nonce,
        handoff_challenge=body.code_challenge,
    )

    try:
        url = build_identity_provider().authorization_url(
            redirect_uri=redirect_uri, state=state, nonce=nonce, code_challenge=challenge
        )
    except IdentityError as exc:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, str(exc)) from exc

    host = urlsplit(origins[0]).hostname if config.ios_app_link else None
    return StartNativeLoginResponse(
        authorization_url=url,
        callback_scheme=NATIVE_CALLBACK_SCHEME,
        callback_host=host,
        callback_path=NATIVE_HANDOFF_PATH if host else None,
    )


@app.post("/v1/auth/google/callback", response_model=LoginResponse, tags=["auth"])
def complete_login(body: CompleteLoginRequest, conn: Conn, config: Config) -> LoginResponse:
    """Finish a sign-in: verify the ID token, check the allowlist, mint a session.

    The order is the point. The identity is established first and completely — signature
    against Google's JWKS, audience, issuer, expiry, nonce, and ``email_verified`` — and
    only then is the address compared against ``MOTET_ALLOWED_EMAILS``. An email claim out
    of an unverified token is a string somebody typed, and authorizing on one would be the
    whole vulnerability this route exists to avoid.

    The state is consumed exactly once by a ``DELETE ... RETURNING``, so a replayed
    callback finds nothing rather than racing a concurrent one into two sessions.
    """
    state = body.state.strip()
    if not is_login_state(state):
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            "That callback did not come from a sign-in. Connecting a mailbox finishes at "
            "/v1/sources/callback.",
        )

    # Before the consume, like `start_login`: a deployment that lost its allowlist
    # mid-flow must not spend the authorization on its way to a 503, because the answer
    # is "come back when this is configured" and the user would find the code already
    # burned when they did.
    if not config.allowed_emails:
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            f"{ALLOWED_EMAILS_ENV} is unset, so no Google account would be accepted.",
        )

    pending = phase2.consume_oauth_state(conn, state)
    if pending is None or pending["provider"] != GOOGLE_PROVIDER:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            "This sign-in is unknown, already used, or expired. Start again.",
        )

    try:
        identity = build_identity_provider().complete(
            code=body.code,
            redirect_uri=pending["redirect_uri"],
            code_verifier=pending["code_verifier"],
            nonce=pending["nonce"] or "",
        )
    except (IdentityConfigError, IdentityUnavailableError) as exc:
        # Not configured, or Google unreachable. Neither is the caller's fault, and a 400
        # would send someone hunting a problem on their own Google account.
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, str(exc)) from exc
    except IdentityError as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc)) from exc

    if not is_allowed(identity.email, config.allowed_emails):
        # Logged with the address, and that is deliberate: this consent screen is open to
        # the internet, so "somebody who is not you finished a Google sign-in here" is a
        # thing an operator wants to be able to see. It is an identity, not a credential.
        logger.warning("refused a sign-in for %s: not on %s", identity.email, ALLOWED_EMAILS_ENV)
        raise HTTPException(
            status.HTTP_403_FORBIDDEN,
            "That Google account is not allowed to use this Motet.",
        )

    handoff_challenge = pending.get("handoff_challenge")
    if handoff_challenge:
        # Started by the iOS app, finishing in its in-app browser. That browser must not
        # hold the session, so it gets a one-time code to hand back instead; the app
        # redeems it with the verifier only it holds. Same code shape as a session token,
        # and only its hash is stored.
        code = auth_repo.new_session_token()
        auth_repo.create_handoff(
            conn,
            user_id=repo.OWNER_USER_ID,
            email=identity.email,
            code=code,
            code_challenge=handoff_challenge,
        )
        logger.info("handed a sign-in for %s back to the iOS app", identity.email)
        return LoginResponse(email=identity.email, handoff_url=_native_handoff_url(config, code))

    token = auth_repo.new_session_token()
    session = auth_repo.create_session(
        conn, user_id=repo.OWNER_USER_ID, email=identity.email, token=token
    )
    logger.info("signed in %s until %s", session.email, session.expires_at.isoformat())
    # The only time the token is ever readable. Only its hash is stored.
    return LoginResponse(token=token, email=session.email, expires_at=session.expires_at)


@app.post("/v1/auth/mcp/callback", response_model=McpAuthorizationResponse, tags=["auth"])
def complete_mcp_authorization(
    body: CompleteLoginRequest, conn: Conn, config: Config
) -> McpAuthorizationResponse:
    """Finish the Google half of an MCP client's authorization (motet#111).

    Unauthenticated, like the sign-in callback, and for the same reason: the browser that
    arrives holds nothing yet. The identity is verified and the allowlist checked exactly as
    signing in does; what comes back is not a session for this browser but the client's
    authorization code, wrapped in the URLs the SPA offers the person as Allow and Deny.
    """
    return complete_mcp_oauth(conn, config, state=body.state.strip(), code=body.code)


@app.post("/v1/auth/native/redeem", response_model=LoginResponse, tags=["auth"])
def redeem_native_login(
    body: RedeemNativeLoginRequest, conn: Conn, config: Config
) -> LoginResponse:
    """Collect a sign-in the iOS app started: code plus verifier in, session out.

    The code alone is not enough, which is the point of the verifier. The handoff link
    travels through a custom URL scheme, and another app can register the same one; what
    it cannot have is the verifier, which never left the app that made the challenge.

    The allowlist is asked again here, because this is the moment a session is minted and
    ``create_session``'s contract is that every writer asks.

    **The code is consumed by the redeem that succeeds, not by the first one that arrives.**
    A refused redeem raises, the request's transaction rolls back, and the ``DELETE`` in
    ``take_handoff`` goes with it. That is the right direction: a guess without the verifier
    cannot burn the real app's sign-in, and a 256-bit verifier is not something to guess.
    """
    if not config.allowed_emails:
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            f"{ALLOWED_EMAILS_ENV} is unset, so no Google account would be accepted.",
        )

    handoff = auth_repo.take_handoff(conn, body.code)
    if handoff is None or not auth_repo.verifier_matches(
        body.code_verifier, handoff.code_challenge
    ):
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            "This sign-in is unknown, already used, expired, or was started by another app. "
            "Sign in again.",
        )

    if not is_allowed(handoff.email, config.allowed_emails):
        logger.warning(
            "refused to hand a sign-in to the app for %s: no longer on %s",
            handoff.email,
            ALLOWED_EMAILS_ENV,
        )
        raise HTTPException(
            status.HTTP_403_FORBIDDEN,
            "That Google account is not allowed to use this Motet.",
        )

    token = auth_repo.new_session_token()
    session = auth_repo.create_session(
        conn, user_id=handoff.user_id, email=handoff.email, token=token
    )
    logger.info(
        "signed the iOS app in as %s until %s", session.email, session.expires_at.isoformat()
    )
    return LoginResponse(token=token, email=session.email, expires_at=session.expires_at)


@app.get("/v1/auth/session", response_model=SessionResponse, tags=["auth"])
def current_session(caller: Who, config: Config) -> SessionResponse:
    """Who this request is, and how it proved it.

    Answers for the shared API token too, so the SPA can say "signed in as …" or "using an
    API token" from what the *server* believes rather than by inferring it from what it
    happens to have in storage.
    """
    return SessionResponse(
        how=caller.how,
        email=caller.email,
        expires_at=caller.expires_at,
        login_configured=config.login_configured,
        # The guard's own predicate, so the SPA offers the admin screen to exactly the
        # callers `/v1/admin/*` would answer — never a link to a 403.
        admin=is_admin(caller, config),
    )


@app.post("/v1/auth/logout", status_code=status.HTTP_204_NO_CONTENT, tags=["auth"])
def logout(conn: Conn, caller: Who) -> Response:
    """Revoke this browser's session.

    A no-op for the shared API token, which is not a session and cannot be revoked by a
    request — rotating it is a deploy. Answering 204 either way is what lets a client sign
    out without first working out which kind of token it holds.
    """
    if caller.session_id is not None:
        auth_repo.delete_session(conn, caller.session_id)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@app.post("/v1/auth/logout-all", response_model=RevokedResponse, tags=["auth"])
def logout_everywhere(conn: Conn, caller: Who) -> RevokedResponse:
    """Revoke every session, including this one. The answer to a lost phone.

    It exists because the alternative is not an operation. `/v1/auth/logout` needs the
    very token you are trying to revoke, and invariant 10 says nobody has a shell to run
    a `DELETE` from — so without this, "I left my laptop on a train" would mean waiting
    out a thirty-day expiry, or taking your own address off `MOTET_ALLOWED_EMAILS` and
    redeploying twice.

    Reachable with the shared API token as well as with a session, which is what makes it
    usable from a *different* device than the compromised one.
    """
    revoked = auth_repo.delete_sessions_for_user(conn, caller.user_id)
    logger.info("revoked %d session(s) at the owner's request", revoked)
    return RevokedResponse(revoked=revoked)


@app.post(
    "/v1/sources/paste",
    response_model=SourceItemResponse,
    status_code=status.HTTP_201_CREATED,
    tags=["ingestion"],
)
def paste_source(body: PasteRequest, conn: Conn, user_id: User, nudge: Nudge) -> SourceItemResponse:
    """Ingest pasted text as a source item.

    Enqueues rather than processes: ingestion is serialized per user (invariant 6), so the
    work belongs to a worker draining the queue, never to the request thread. The row and
    the job are written in the same transaction — with two systems there would always be a
    window where the source item exists and nothing will ever pick it up.
    """
    stored = enqueue_paste(conn, user_id=user_id, title=body.title.strip(), text=body.text)
    nudge.arm(DrainReason.PASTE)
    return SourceItemResponse(id=stored.id, title=stored.title, state=stored.state.value)


@app.get("/v1/ingestion", response_model=list[IngestionItemResponse], tags=["ingestion"])
def list_ingestion(conn: Conn, user_id: User) -> list[IngestionItemResponse]:
    """What has been ingested but is not in the backlog yet, and why.

    The backlog answers "what do I have to listen to"; it cannot answer "where did the
    thing I just pasted go", because an item that never integrates never becomes a news
    item and so never appears there at all. That gap is the whole reason this route
    exists: content that fails is content that silently disappears.

    Covers both stages a thing can be stuck in on its way to the backlog, which needed
    saying because for a while it covered one. A pasted item has a ``source_items`` row
    from the moment it is accepted; a polled mailbox message does not get one until
    extraction succeeds, so a message the fetch *raised* on — a revoked grant, a mailbox
    that would not answer — was reported nowhere at all while the poll cursor had already
    moved past it (motet#35). A message the extractor deliberately skips, because it is a
    receipt rather than a newsletter, is a different thing and is still not reported. Both
    arms live in ``repo.list_ingestion``.
    """
    return [_ingestion_item(item) for item in repo.list_ingestion(conn, user_id)]


@app.get("/v1/source-items/held", response_model=list[HeldSourceItemResponse], tags=["ingestion"])
def list_held_source_items(conn: Conn, user_id: User) -> list[HeldSourceItemResponse]:
    """Source items extracted from a connected source and waiting to be ingested.

    Connecting a source does the deterministic, free work on its own — poll, fetch,
    extract — and stops before ``integrate``, the first stage that spends inference. What
    it leaves is a ``pending`` source item with no integrate job, and that combination is
    the held state: no column records it. Oldest message first, so the list reads as a
    queue, and bounded at the most one request may act on. A paste is never here for
    longer than its own transaction, because pasting is asking.
    """
    return [
        HeldSourceItemResponse(
            id=item.id,
            title=item.title,
            source_id=item.source_id,
            source_kind=item.source_kind,
            source_name=item.source_name,
            received_at=item.received_at,
            chars=item.chars,
            preview=item.preview,
        )
        for item in repo.list_held_source_items(conn, user_id)
    ]


@app.post("/v1/source-items/integrate", response_model=IntegrateResponse, tags=["ingestion"])
def integrate_source_items(
    body: SourceItemIdsRequest, conn: Conn, user_id: User, nudge: Nudge
) -> IntegrateResponse:
    """Queue held source items for integration — the owner saying "ingest now".

    Each id that is the caller's, ``pending`` and without an integrate job gets one,
    written exactly as a paste's is: same queue, same payload, same per-user serialization
    key (invariant 6). Every other id is skipped rather than refused — an item that was
    queued a moment ago by a second tab is not an error, and a response that 4xx'd on it
    would make the first tab's success look like a failure.
    """
    ids = list(dict.fromkeys(body.ids))
    queued = enqueue_integration(conn, user_id=user_id, source_item_ids=ids)
    if queued:
        nudge.arm(DrainReason.INTEGRATE)
    return IntegrateResponse(queued=len(queued), skipped=len(ids) - len(queued))


@app.post("/v1/source-items/dismiss", response_model=DismissResponse, tags=["ingestion"])
def dismiss_source_items(body: SourceItemIdsRequest, conn: Conn, user_id: User) -> DismissResponse:
    """Discard held source items without spending inference on them.

    The other way out of the held list, so that a newsletter nobody wants briefed does not
    sit there forever. Only a held item can be dismissed; the rest are skipped, exactly as
    ``/integrate`` skips them. The row stays, as ``dismissed``, because it is what stops a
    re-poll from fetching the message and holding it again.
    """
    ids = list(dict.fromkeys(body.ids))
    dismissed = repo.dismiss_held_source_items(conn, user_id, ids)
    return DismissResponse(dismissed=len(dismissed), skipped=len(ids) - len(dismissed))


@app.get(
    "/v1/source-items/{source_item_id}",
    response_model=SourceItemDetailResponse,
    tags=["ingestion"],
)
def get_source_item_detail(
    conn: Conn, user_id: User, source_item_id: Annotated[str, Path()]
) -> SourceItemDetailResponse:
    """One source item across its three stages.

    Pulled in (the deterministic scrape), processed (the steps that spend inference —
    dedup today — with the decision dedup recorded), and the news item it feeds. The
    answer carries the item's full text, so another user's item is a 404, the same as one
    that does not exist.
    """
    life = repo.source_item_lifecycle(conn, user_id, source_item_id)
    if life is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "No such source item.")
    return _source_item_detail(life)


def _source_item_detail(life: repo.SourceItemLifecycle) -> SourceItemDetailResponse:
    job = life.job
    step_status: str | None
    if life.state is SourceItemState.DISMISSED:
        status_, step_status = "dismissed", None
    elif life.state is SourceItemState.INTEGRATED:
        status_ = step_status = "done"
    elif life.state is SourceItemState.FAILED:
        status_ = step_status = "failed"
    elif job is None:
        status_, step_status = "held", None
    elif job.state == "running":
        status_ = step_status = "running"
    elif job.state == "failed":
        status_ = step_status = "failed"
    else:
        # `ready` — first attempt or a retry backing off; `done` with a pending item is
        # a reclaimed lease mid-flight, and "queued" is the honest word for both.
        status_ = step_status = "queued"
    outcome = None
    if life.news_item is not None:
        outcome = "new" if life.news_item.position == 0 else "merged"
    decision = life.decision
    processed = (
        [
            ProcessingStepResponse(
                step="dedup",
                status=step_status,
                job=(
                    SourceItemJobResponse(
                        id=job.id,
                        state=job.state,
                        attempts=job.attempts,
                        max_attempts=DEFAULT_MAX_ATTEMPTS,
                        run_at=job.run_at,
                        locked_at=job.locked_at,
                        created_at=job.created_at,
                        updated_at=job.updated_at,
                        last_error=job.last_error,
                        work_committed=job.work_committed,
                    )
                    if job is not None
                    else None
                ),
                finished_at=life.integrated_at,
                error=life.last_error or (job.last_error if job is not None else None),
                outcome=outcome,
                decision=(
                    DedupDecisionResponse(
                        relation=decision.relation,
                        reason=decision.reason,
                        candidate_id=decision.candidate_id,
                        candidate_title=decision.candidate_title,
                        model=decision.model,
                        basis=decision.basis or "first_pass",
                        title=decision.title,
                        summary=decision.summary,
                        decided_at=decision.decided_at,
                    )
                    if decision is not None
                    else None
                ),
                cost_recorded=False,
            )
        ]
        if step_status is not None
        else []
    )
    return SourceItemDetailResponse(
        id=life.id,
        title=life.title,
        state=life.state.value,
        status=status_,
        pulled=SourceItemPulledStage(
            source_id=life.source_id,
            source_kind=life.source_kind,
            source_name=life.source_name,
            external_id=life.external_id,
            received_at=life.received_at,
            stored_at=life.created_at,
            chars=len(life.text),
            text=life.text,
            raw_stored=False,
        ),
        processed=processed,
        news_items=(
            [
                SourceItemNewsItemResponse(
                    id=life.news_item.id,
                    title=life.news_item.title,
                    summary=life.news_item.summary,
                    read=life.news_item.read,
                    source_count=life.news_item.source_count,
                    position=life.news_item.position,
                )
            ]
            if life.news_item is not None
            else []
        ),
    )


@app.get("/v1/processing", response_model=ProcessingStatusResponse, tags=["ingestion"])
def processing_status(conn: Conn, user_id: User) -> ProcessingStatusResponse:
    """Whether anything is draining the queues — the other half of "where did my paste go".

    ``/v1/ingestion`` says what is waiting. It cannot say whether anything is coming for
    it, and a client that assumes one is is how the SPA came to promise "a few seconds"
    against a queue nothing had touched in hours (motet#38).

    Deployment state rather than user state, so it takes ``user_id`` only to sit behind
    the same lock as everything else under ``/v1``. There is one account in Phase 1 and one
    set of workers behind it; when there are many, the queues are still shared and this
    answer is still the same one.

    ``readiness`` is the other half of that same deployment question, for an operator or a
    scaler rather than for the SPA (motet#78): how much is due per queue, and how many
    workers that work could keep busy. It is here because this is where the heartbeat
    already is — "is anything draining" and "how much is there to drain" are one glance —
    and because it is the only surface that can answer for a queue *no worker is running*,
    which is exactly the queue a scaler has to hear about. The gauges the worker emits
    carry the same numbers and go quiet in that case.
    """
    now, beats = repo.worker_heartbeats(conn)
    return ProcessingStatusResponse(
        now=now,
        worker_last_seen_at=beats[0].last_seen_at if beats else None,
        queues=[
            QueueHeartbeatResponse(queue=beat.queue, last_seen_at=beat.last_seen_at)
            for beat in beats
        ],
        readiness=[
            QueueReadinessResponse(
                queue=entry.queue,
                ready=entry.ready,
                ready_keys=entry.ready_keys,
                blocked_keys=entry.blocked_keys,
            )
            for entry in queue_readiness(conn)
        ],
    )


# --- the operator view ----------------------------------------------------------------
#
# **The first route family that returns data across users** — every user's address, their
# counts, and every job's `last_error`. Every route under `/v1/admin` takes `Admin`, and
# `api/tests/test_admin_overview.py` walks the app's routes to prove none of them escaped
# it. Fails closed: with MOTET_ADMIN_EMAILS unset nobody is an admin, and the shared API
# token never is (`deps.is_admin`).
#
# Deliberately not an `APIRouter` with the check as a router dependency, which would have
# been guarded-by-construction: this FastAPI mounts an included router as one opaque
# entry in `app.routes`, so every route walk in this repo — the reserved-path guard and
# the one above among them — would stop seeing the routes it holds.

#: The jobs list's page size when the caller does not ask, and the most it may ask for. The
#: bound on the response; ``before`` is what makes everything past it reachable.
ADMIN_JOBS_DEFAULT_LIMIT: Final = 200
ADMIN_JOBS_MAX_LIMIT: Final = 500


@app.get("/v1/admin/overview", response_model=AdminOverviewResponse, tags=["admin"])
def admin_overview(
    conn: Conn,
    _admin: Admin,
    user_id: Annotated[
        str | None,
        Query(description="Only list jobs whose subject resolves to this user."),
    ] = None,
    before: Annotated[
        int | None,
        Query(
            ge=1,
            description=(
                "Only list jobs with an id below this one — the previous page's "
                "`jobs_next_before`. Omit for the newest page."
            ),
        ),
    ] = None,
    limit: Annotated[
        int,
        Query(ge=1, le=ADMIN_JOBS_MAX_LIMIT, description="How many jobs to list."),
    ] = ADMIN_JOBS_DEFAULT_LIMIT,
) -> AdminOverviewResponse:
    """The whole deployment at a glance, across every user. Admins only.

    Deployment state rather than user state, like ``/v1/processing``: the caller's own
    account plays no part in the answer. ``user_id``, ``before`` and ``limit`` shape the
    job list only; the per-user and per-queue aggregates are always for everyone.

    **The job list is a page, newest first, keyed on the job id.** A keyset cursor rather
    than an offset because the list is polled while workers insert at its head: an offset
    would shift under a reader every poll, and ``id < before`` does not. Rather than a time
    window because a window does not bound the response — one Gmail backfill puts a
    thousand rows into the last hour.
    """
    users = repo.admin_overview_users(conn)
    queues = repo.admin_overview_queues(conn, [queue.value for queue in PIPELINE])
    # One more than the page, to learn whether there is a next one without a count(*).
    page = repo.admin_overview_jobs(conn, user_id=user_id, before=before, limit=limit + 1)
    jobs_, more = page[:limit], len(page) > limit
    return AdminOverviewResponse(
        generated_at=datetime.now(UTC),
        users=[
            AdminUserResponse(
                user_id=user.user_id,
                email=user.email,
                source_items=AdminSourceItemCounts(**user.source_items),
                news_items=AdminNewsItemCounts(**user.news_items),
                episodes=AdminEpisodeCounts(**user.episodes),
                jobs=AdminJobCounts(**user.jobs),
            )
            for user in users
        ],
        queues=[
            AdminQueueResponse(
                queue=queue.queue,
                ready=queue.ready,
                running=queue.running,
                done=queue.done,
                failed=queue.failed,
                oldest_ready_age_s=queue.oldest_ready_age_s,
                last_heartbeat_at=queue.last_heartbeat_at,
            )
            for queue in queues
        ],
        jobs=[
            AdminJobResponse(
                id=job.id,
                queue=job.queue,
                state=job.state,
                attempts=job.attempts,
                user_id=job.user_id,
                subject=job.subject,
                last_error=job.last_error,
                run_at=job.run_at,
                created_at=job.created_at,
                updated_at=job.updated_at,
                locked_at=job.locked_at,
            )
            for job in jobs_
        ],
        jobs_next_before=jobs_[-1].id if more else None,
    )


# --- the landing page's waitlist --------------------------------------------------------
#
# The public half is the only `/v1` route with no caller at all: the static site on the
# apex domain posts a form to it cross-origin. `motet_api.waitlist` says why it needs no
# CORS configuration, why it answers HTML as well as JSON, and why no address reaches a
# log line. The read half is an admin route like the overview above, and the route walk in
# `test_admin_overview.py` holds it to `Admin` along with every other `/v1/admin` path.

_WAITLIST_FORM_SCHEMA: Final = {
    "type": "object",
    "required": ["email"],
    "properties": {
        "email": {"type": "string", "format": "email", "maxLength": 254},
        "motet_hp": {
            "type": "string",
            "description": "Leave empty. A form that fills it is treated as a bot.",
        },
    },
}


@app.post(
    "/v1/waitlist",
    response_model=WaitlistJoinResponse,
    tags=["waitlist"],
    summary="Join the waitlist",
    openapi_extra={
        "requestBody": {
            "required": True,
            "content": {"application/x-www-form-urlencoded": {"schema": _WAITLIST_FORM_SCHEMA}},
        }
    },
    responses={
        200: {"content": {"text/html": {}}},
        413: {"description": "The body is larger than a waitlist form."},
        415: {"description": "The body is not `application/x-www-form-urlencoded`."},
        422: {"description": "The address is not plausibly an email address."},
        503: {"description": "The address could not be stored; nothing was recorded."},
    },
)
def join_waitlist(
    conn: Conn, submission: Annotated[Submission, Depends(read_submission)]
) -> Response:
    """Put an address on the landing page's waitlist. Public; no credential.

    Sent as a form, so that the landing page's request is a CORS simple request and needs
    no preflight. Answers JSON when the caller's ``Accept`` asks for it and a small HTML
    page otherwise, which is what a form posted without JavaScript lands on. A new
    address, a known one and a submission that filled the honeypot all get the same 200.
    """
    if submission.refused is not None or submission.email is None:
        return waitlist_answer(
            submission.refused or WaitlistOutcome.INVALID, wants_json=submission.wants_json
        )
    try:
        joined = waitlist_repo.join(conn, submission.email)
    except Exception as exc:
        # Caught, and reported by type alone, because this is the one route where letting an
        # exception escape would leak the thing it promises never to log: the error reporter
        # captures frame locals — `email` is one, in `waitlist_repo.join` — and a constraint
        # violation's own message quotes the failing row. The type is enough to find it.
        with suppress(Exception):
            conn.rollback()
        logger.error("waitlist: storing a submission failed (%s)", type(exc).__name__)
        return waitlist_answer(WaitlistOutcome.STORE_FAILED, wants_json=submission.wants_json)
    outcome = WaitlistOutcome.JOINED if joined else WaitlistOutcome.ALREADY_LISTED
    return waitlist_answer(outcome, wants_json=submission.wants_json)


@app.get("/v1/admin/waitlist", response_model=AdminWaitlistResponse, tags=["admin"])
def admin_waitlist(
    conn: Conn,
    _admin: Admin,
    before: Annotated[
        int | None,
        Query(
            ge=1,
            description=(
                "Only list signups with an id below this one — the previous page's "
                "`next_before`. Omit for the newest page."
            ),
        ),
    ] = None,
    limit: Annotated[
        int,
        Query(ge=1, le=ADMIN_JOBS_MAX_LIMIT, description="How many signups to list."),
    ] = ADMIN_JOBS_DEFAULT_LIMIT,
) -> AdminWaitlistResponse:
    """Everyone who asked to join from the landing page, newest first. Admins only."""
    page = waitlist_repo.list_signups(conn, before=before, limit=limit + 1)
    signups, more = page[:limit], len(page) > limit
    return AdminWaitlistResponse(
        total=waitlist_repo.count(conn),
        signups=[
            AdminWaitlistSignupResponse(
                id=signup.id,
                email=signup.email,
                created_at=signup.created_at,
                last_submitted_at=signup.last_submitted_at,
                submissions=signup.submissions,
            )
            for signup in signups
        ],
        next_before=signups[-1].id if more else None,
    )


# The LLM half of the operator view (motet#92): which model each stage is on and why, and
# what the ledger says each stage, user and queue spent. Same guard, same reason — and a
# second one on the write: `PUT` changes what every later job spends, so it is refused
# outright wherever MOTET_SETTINGS_WRITABLE is off, which is production. The logic is in
# `admin_llm`; these are its HTTP surface.


def _llm_stage(value: str) -> LlmStage:
    try:
        return LlmStage(value)
    except ValueError:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"unknown LLM stage {value!r}; one of: {', '.join(s.value for s in LlmStage)}",
        ) from None


@app.get("/v1/admin/llm-config", response_model=LlmConfigResponse, tags=["admin"])
def get_llm_config(conn: Conn, _admin: Admin) -> LlmConfigResponse:
    """Every LLM stage's resolved model and effort, where each came from, and the catalogue.

    Where settings are not writable the rows are neither read nor shown: the answer is the
    environment's, which is what every job on this deployment runs.
    """
    return admin_llm.describe_config(admin_llm.honoured_rows(conn, os.environ), os.environ)


@app.put(
    "/v1/admin/llm-config/{stage}",
    response_model=LlmConfigResponse,
    tags=["admin"],
    responses={
        400: {
            "description": "The change does not resolve: an unknown slug, or an effort "
            "the slug does not take."
        },
        404: {"description": "No such LLM stage."},
        409: {"description": "Settings are read-only on this deployment."},
    },
)
def put_llm_config(
    conn: Conn,
    admin: Admin,
    body: LlmStageConfigUpdate,
    stage: Annotated[str, Path(description="An LLM stage, as `GET` lists them.")],
) -> LlmConfigResponse:
    """Set or clear one stage's model and effort. Applies to the next job a worker claims.

    Validated before anything is written, by the same function the worker applies rows
    through: an unknown slug, an effort the slug does not accept, or a slug outside the
    catalogue is a 400 with the resolver's own message.
    """
    target = _llm_stage(stage)
    try:
        return admin_llm.apply_update(conn, target, body, os.environ, actor=admin.email)
    except admin_llm.SettingsReadOnlyError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from None
    except LlmConfigError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from None


@app.get("/v1/admin/llm-spend", response_model=AdminLlmSpendResponse, tags=["admin"])
def get_llm_spend(conn: Conn, _admin: Admin) -> AdminLlmSpendResponse:
    """What each LLM stage, user and pipeline queue spent, from the `llm_usage` ledger.

    Its own route rather than a field on `/v1/admin/overview`: that one is polled every few
    seconds for queue state, and a sum over the ledger is neither cheap enough nor fresh
    enough to be worth re-asking at that rate.
    """
    return admin_llm.fold_spend(conn)


@app.get("/v1/news-items", response_model=list[NewsItemResponse], tags=["backlog"])
def list_news_items(conn: Conn, user_id: User) -> list[NewsItemResponse]:
    """The backlog: deduped news items with their read state (invariant 5)."""
    items = repo.list_news_items(conn, user_id)
    titles = repo.source_item_titles(conn, [sid for item in items for sid in item.source_item_ids])
    return [_news_item(item, titles) for item in items]


@app.post("/v1/news-items/{news_item_id}/read", response_model=NewsItemResponse, tags=["backlog"])
def set_news_item_read(
    body: ReadStateRequest,
    conn: Conn,
    user_id: User,
    news_item_id: Annotated[str, Path()],
) -> NewsItemResponse:
    """Mark a news item read or unread.

    The same write that "I listened to this episode" performs, which is what invariant 5
    means in practice: one fact, one column, two ways of reaching it.
    """
    updated = repo.set_news_item_read(conn, user_id=user_id, item_id=news_item_id, read=body.read)
    if updated is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "No such news item.")
    return _news_item(updated, repo.source_item_titles(conn, updated.source_item_ids))


@app.post(
    "/v1/episodes",
    response_model=EpisodeResponse,
    status_code=status.HTTP_201_CREATED,
    tags=["episodes"],
)
def create_episode(
    body: CreateEpisodeRequest, conn: Conn, user_id: User, nudge: Nudge
) -> EpisodeResponse:
    """Assemble a manual episode from unread news items, capped by duration.

    Returns immediately, in ``pending``. Assembly, scripting and TTS happen on the queue
    afterwards — so the episode a client polls for moves through states rather than
    appearing finished.
    """
    episode_id = enqueue_episode(
        conn, user_id=user_id, title=body.title.strip(), max_duration_ms=body.max_duration_ms
    )
    nudge.arm(DrainReason.EPISODE)
    episode = repo.get_episode(conn, episode_id, user_id=user_id)
    assert episode is not None
    return _episode(conn, episode)


@app.get("/v1/episodes", response_model=list[EpisodeResponse], tags=["episodes"])
def list_episodes(conn: Conn, user_id: User) -> list[EpisodeResponse]:
    """Every episode, newest first, whatever state it is in."""
    return [_episode(conn, episode) for episode in repo.list_episodes(conn, user_id)]


@app.get("/v1/episodes/{episode_id}", response_model=EpisodeResponse, tags=["episodes"])
def get_episode(conn: Conn, user_id: User, episode_id: Annotated[str, Path()]) -> EpisodeResponse:
    """An episode with its transcript — each claim beside the span it came from."""
    episode = repo.get_episode(conn, episode_id, user_id=user_id)
    if episode is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "No such episode.")
    return _episode(conn, episode)


@app.post(
    "/v1/episodes/{episode_id}/listened",
    response_model=MarkListenedResponse,
    tags=["episodes"],
)
def mark_episode_listened(
    conn: Conn, user_id: User, episode_id: Annotated[str, Path()]
) -> MarkListenedResponse:
    """Mark every news item in this episode read.

    Phase 1's stand-in for playback tracking: RSS gives background audio and CarPlay for
    free, and takes away any way for a client to report where the listener got to. Phase
    2's iOS app reports ``spoken_through_ms`` and this becomes automatic — but the fact it
    writes is the same one, on the same column, which is why swapping the trigger later
    changes nothing about read state.
    """
    episode = repo.get_episode(conn, episode_id, user_id=user_id)
    if episode is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "No such episode.")
    marked = repo.mark_news_items_read(
        conn, user_id=user_id, item_ids=[s.news_item_id for s in episode.segments]
    )
    return MarkListenedResponse(episode_id=episode.id, news_items_marked_read=marked)


@app.get("/v1/feed", response_model=FeedInfoResponse, tags=["feed"])
def get_feed_info(request: Request, conn: Conn, user_id: User, config: Config) -> FeedInfoResponse:
    """The private feed URL, minting a token on first ask."""
    token = repo.ensure_feed_token(conn, user_id)
    base = public_base_url(config, str(request.base_url))
    return FeedInfoResponse(url=feed_url(base, token), token=token)


@app.post("/v1/feed/rotate", response_model=FeedInfoResponse, tags=["feed"])
def rotate_feed(request: Request, conn: Conn, user_id: User, config: Config) -> FeedInfoResponse:
    """Revoke the current feed URL and mint a new one.

    This unsubscribes every client using the old URL, which is the point — it is the
    answer to a leaked feed link, and there is no other way to take one back.
    """
    token = repo.rotate_feed_token(conn, user_id)
    base = public_base_url(config, str(request.base_url))
    return FeedInfoResponse(url=feed_url(base, token), token=token)


@app.get(
    "/feed.xml",
    tags=["feed"],
    response_class=Response,
    responses={200: {"content": {"application/rss+xml": {}}, "description": "The RSS feed"}},
)
def rss_feed(request: Request, conn: Conn, user_id: FeedUser, config: Config) -> Response:
    """The private, authenticated RSS feed Phase 1 ships instead of a player.

    RSS buys background audio, offline, lockscreen, CarPlay, and speed control with zero
    iOS code. Audio is served from object storage behind signed URLs, so this document
    carries links, never bytes.
    """
    token = repo.active_feed_token(conn, user_id)
    assert token is not None  # the dependency resolved this request's token from this row
    base = public_base_url(config, str(request.base_url))
    episodes = repo.list_published_episodes(conn, user_id)
    # Story titles for the show notes and chapters — without them every story is "Story N"
    # — and the source text each story's lead claim cites, for the quote under it, resolved
    # the way the episode screen resolves a span. Two reads for the whole feed rather than
    # two per episode. `load_source_items` carries each lead source's full text, which is
    # fine at one user's volume; if feed polls ever show up in the database's load, cutting
    # the span in SQL is the fix, not dropping the quote.
    segments = [segment for episode in episodes for segment in episode.segments]
    titles = {
        item_id: item.title
        for item_id, item in repo.load_news_items(
            conn, list(dict.fromkeys(segment.news_item_id for segment in segments))
        ).items()
    }
    leads = [segment.claims[0] for segment in segments if segment.claims]
    sources = repo.load_source_items(
        conn, list(dict.fromkeys(claim.source_item_id for claim in leads))
    )
    excerpts = {
        claim.id: SourceExcerpt(
            source_title=source.title, text=source.text[claim.span_start : claim.span_end]
        )
        for claim in leads
        if (source := sources.get(claim.source_item_id)) is not None
    }
    body = render_feed(
        FeedMetadata(
            title=config.feed_title,
            description=config.feed_description,
            author=config.feed_author,
            base_url=base,
            token=token,
        ),
        episodes,
        titles,
        excerpts,
    )
    return Response(content=body, media_type="application/rss+xml")


@app.get(
    ARTWORK_PATH,
    tags=["feed"],
    response_class=Response,
    responses={
        200: {"content": {ARTWORK_MEDIA_TYPE: {}}, "description": "The podcast artwork"},
        304: {"description": "The artwork the client already holds is current"},
    },
)
def feed_artwork(request: Request) -> Response:
    """The podcast artwork the feed's ``<itunes:image>`` points at: the Motet mark.

    **Unauthenticated, deliberately — the one feed URL without the token.** The image is
    not secret: it is the public brand mark, the same bytes for every user, and committed
    to this public repo. And the clients that fetch it are the ones least likely to carry a
    credential faithfully: a podcast app hands artwork to an image cache or a proxy, a
    directory fetches it server-side, and either may drop the query string or keep the URL
    far longer than the feed. A feed token in that URL would copy a bearer secret into more
    caches and logs for nothing — and a token rotation, which is meant to unsubscribe
    players, would also blank the cover in every client that still holds the old image URL.

    No database, no storage backend: it is package data, so it answers wherever the process
    runs, including a container with no ``DATABASE_URL`` at all.
    """
    etag = f'"{artwork_version()}"'
    headers = {
        # A week, and not `immutable`: the feed's URL carries the content hash, so a new
        # mark is a new URL, but a client that dropped the query string still revalidates.
        "Cache-Control": "public, max-age=604800",
        "ETag": etag,
    }
    if request.headers.get("if-none-match") == etag:
        return Response(status_code=status.HTTP_304_NOT_MODIFIED, headers=headers)
    return Response(content=artwork_bytes(), media_type=ARTWORK_MEDIA_TYPE, headers=headers)


@app.get(
    "/v1/episodes/{episode_id}/audio",
    tags=["feed"],
    response_class=Response,
    responses={
        200: {"content": {"audio/mpeg": {}}, "description": "The episode audio"},
        307: {"description": "Redirect to a time-limited signed URL"},
    },
)
def episode_audio(
    conn: Conn,
    user_id: FeedUser,
    blobs: Store,
    episode_id: Annotated[str, Path()],
) -> Response:
    """Serve an episode's audio, or redirect to a signed URL for it.

    Which of the two depends on the storage backend, and the *store* decides rather than
    this route: a backend that can mint a signed URL returns one, and one that cannot
    returns ``None``. A podcast client cannot tell the difference — it follows the
    redirect — so the enclosure URL in the feed is stable across both, and a signed URL's
    expiry never ends up cached inside a feed document.
    """
    episode = repo.get_episode(conn, episode_id, user_id=user_id)
    if episode is None or not episode.has_audio or episode.audio_key is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "This episode has no audio yet.")

    signed = blobs.signed_url(episode.audio_key)
    if signed is not None:
        return RedirectResponse(signed, status_code=status.HTTP_307_TEMPORARY_REDIRECT)
    try:
        data = blobs.get(episode.audio_key)
    except StorageError as exc:
        logger.error("episode %s audio is missing from storage: %s", episode.id, exc)
        raise HTTPException(
            status.HTTP_404_NOT_FOUND, "This episode's audio is no longer available."
        ) from exc
    # Deliberately no `Accept-Ranges: bytes`. Podcast clients do range-request large files,
    # but this branch serves the whole body and ignores `Range` — advertising support we do
    # not have would tell a resuming client it had resumed when it had started over. The
    # deployed backend hands out a signed URL above and gets real range support from object
    # storage; this path is dev and CI only.
    return Response(
        content=data,
        media_type=episode.audio_media_type or "audio/mpeg",
        headers={"Content-Length": str(len(data))},
    )


def _ingestion_item(item: IngestionStatus) -> IngestionItemResponse:
    """The retry ceiling is the worker's constant, reported rather than restated here.

    A second copy of the number would be wrong the moment one of them moved, and "attempt
    3 of 5" is only useful to a reader if the 5 is the 5 the queue is actually counting to.
    """
    return IngestionItemResponse(
        id=item.id,
        title=item.title,
        state=item.state.value,
        attempts=item.attempts,
        max_attempts=DEFAULT_MAX_ATTEMPTS,
        next_attempt_at=item.next_attempt_at,
        last_error=item.last_error,
        created_at=item.created_at,
        source_kind=item.source_kind,
        source_id=item.source_id,
    )


def _news_item(item: StoredNewsItem, titles: Mapping[str, str]) -> NewsItemResponse:
    return NewsItemResponse(
        id=item.id,
        title=item.title,
        summary=item.summary,
        source_item_ids=list(item.source_item_ids),
        sources=[
            NewsItemSourceRef(id=sid, title=titles.get(sid, "")) for sid in item.source_item_ids
        ],
        read=item.read,
        created_at=item.created_at,
    )


# --- Play Live ------------------------------------------------------------------------
#
# The browser asks the API for a voice session; the API assembles the episode's context and
# mints it on the voice service with the start token (invariant 2 — the voice service looks
# nothing up). See `motet_api.voice`. No voice service is deployed yet, so on staging and
# production both routes say "not configured" and the SPA offers no Play Live button.


def voice_config() -> VoiceConfig:
    return VoiceConfig.from_env()


def voice_starter(config: Annotated[VoiceConfig, Depends(voice_config)]) -> VoiceStarter | None:
    """Built per request: it holds nothing but two strings, and the HTTP client inside is
    opened and closed around the one call it makes."""
    return build_starter(config)


@app.get("/v1/voice", response_model=VoiceStatusResponse, tags=["voice"])
def voice_status(
    user_id: User, config: Annotated[VoiceConfig, Depends(voice_config)]
) -> VoiceStatusResponse:
    """Whether Play Live can run in this deployment.

    Asked by the SPA before it offers the button, so that an environment with no voice
    service shows a disabled control with a reason — never a request to a host that does
    not exist, and never a failed call in the console.
    """
    return VoiceStatusResponse(configured=config.configured, reason=config.reason)


@app.post(
    "/v1/episodes/{episode_id}/voice-session",
    response_model=VoiceSessionResponse,
    status_code=status.HTTP_201_CREATED,
    tags=["voice"],
    responses={
        status.HTTP_503_SERVICE_UNAVAILABLE: {
            "description": "No voice service is configured, or it would not mint a session."
        }
    },
)
def start_voice_session(
    conn: Conn,
    user_id: User,
    episode_id: Annotated[str, Path()],
    request: StartVoiceSessionRequest,
    config: Annotated[VoiceConfig, Depends(voice_config)],
    starter: Annotated[VoiceStarter | None, Depends(voice_starter)],
) -> VoiceSessionResponse:
    """Mint a Play Live session for a rendered episode.

    The context — every segment, every claim with the moment it is spoken, where the
    listener's player is — is built here from the database and sent to the voice service
    server-to-server with the start token. The browser gets a token scoped to that one
    config, the socket to open, and the frame to open it with.
    """
    if starter is None:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, config.reason)
    episode = repo.get_episode(conn, episode_id, user_id=user_id)
    if episode is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "No such episode.")
    if episode.state.value != "ready":
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            "Play Live needs a rendered episode — its claims are not timed until then.",
        )
    view = _episode(conn, episode)
    position = (
        request.spoken_through_ms
        if request.spoken_through_ms is not None
        else view.listened_through_ms
    )
    body = session_config(view, spoken_through_ms=position)
    try:
        started = starter.start(body)
    except VoiceUnavailableError as exc:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, str(exc)) from exc
    return VoiceSessionResponse(
        session_id=started.session_id,
        session_token=started.session_token,
        expires_at=started.expires_at,
        websocket_url=started.websocket_url,
        arm=started.arm,
        conversational=started.conversational,
        authenticate_frame={"type": "authenticate", "token": started.session_token, "config": body},
    )


def _episode(conn: psycopg.Connection[Any], episode: StoredEpisode) -> EpisodeResponse:
    """Build the episode view, resolving every claim's span to the text it cites.

    The resolution happens here rather than in the client because it is the whole point of
    the screen: a claim shown next to the sentence it came from is the product's argument
    that it is not making things up. A client that had to fetch sources separately would
    sometimes skip it, and the argument would quietly stop being made.
    """
    source_ids = {claim.source_item_id for segment in episode.segments for claim in segment.claims}
    sources = repo.load_source_items(conn, sorted(source_ids))
    news_titles = {
        item_id: item.title
        for item_id, item in repo.load_news_items(
            conn, [segment.news_item_id for segment in episode.segments]
        ).items()
    }

    segments = []
    for segment in episode.segments:
        claims = []
        for claim in segment.claims:
            source = sources.get(claim.source_item_id)
            excerpt = source.text[claim.span_start : claim.span_end] if source is not None else ""
            claims.append(
                ClaimModel(
                    text=claim.text,
                    span=SourceSpanModel(
                        source_item_id=claim.source_item_id,
                        start=claim.span_start,
                        end=claim.span_end,
                    ),
                    source_excerpt=excerpt,
                    source_title=source.title if source is not None else "(source removed)",
                    start_ms=claim.start_ms,
                    duration_ms=claim.duration_ms,
                )
            )
        segments.append(
            SegmentResponse(
                news_item_id=segment.news_item_id,
                news_item_title=news_titles.get(segment.news_item_id, "(story removed)"),
                text=segment.text,
                start_ms=segment.start_ms,
                duration_ms=segment.duration_ms,
                claims=claims,
            )
        )

    return EpisodeResponse(
        id=episode.id,
        title=episode.title,
        state=episode.state.value,
        duration_ms=episode.duration_ms,
        max_duration_ms=episode.max_duration_ms,
        audio_bytes=episode.audio_bytes,
        audio_media_type=episode.audio_media_type,
        last_error=episode.last_error,
        created_at=episode.created_at,
        published_at=episode.published_at,
        listened_through_ms=episode.listened_through_ms,
        segments=segments,
    )


# --- Phase 2: connected sources ------------------------------------------------------


@app.get("/v1/sources", response_model=list[SourceResponse], tags=["sources"])
def list_sources(conn: Conn, user_id: User) -> list[SourceResponse]:
    """Every source, connected or paused, with whether a credential exists.

    "Connected" is answered *without* decrypting anything: the credential row's existence
    is the answer, and reading it needs no key. Invariant 8 means only workers can open
    one, so a screen that had to decrypt to render would have to break the invariant.
    """
    sources = repo_sources(conn, user_id)
    counts = phase2.source_item_counts(conn, [source.id for source in sources])
    return [_source_response(conn, source, counts.get(source.id)) for source in sources]


def _source_response(
    conn: psycopg.Connection[Any],
    source: StoredSource,
    counts: phase2.SourceItemCounts | None = None,
) -> SourceResponse:
    """One source as every route reports it, so the three that return one cannot disagree.

    ``counts`` is passed by the list route, which asks for every source's in one query; a
    route returning a single source leaves it out and this counts that one.
    """
    credential = phase2.get_source_credential(
        conn, source_id_=source.id, purpose=CredentialPurpose.REFRESH.value
    )
    if counts is None:
        counts = phase2.source_item_counts(conn, [source.id]).get(source.id)
    counts = counts or phase2.SourceItemCounts()
    return SourceResponse(
        id=source.id,
        kind=source.kind,
        name=source.name,
        active=source.active,
        connected=credential is not None,
        scopes=list(credential.scopes) if credential else [],
        last_polled_at=source.last_polled_at,
        last_error=source.last_error,
        created_at=source.created_at,
        disconnected_at=source.disconnected_at,
        items_pulled_in=counts.pulled_in,
        items_integrated=counts.integrated,
        **sync_facts(source),
        label_sync=_label_sync(conn, source, credential.scopes if credential else ()),
    )


@app.post(
    "/v1/sources/connect",
    response_model=ConnectSourceResponse,
    status_code=status.HTTP_201_CREATED,
    tags=["sources"],
)
def connect_source(body: ConnectSourceRequest, conn: Conn, user_id: User) -> ConnectSourceResponse:
    """Start connecting a mailbox: create the source and return a consent URL.

    **The source row is created before consent completes**, and stays inactive until a
    credential lands. That ordering is what lets the callback identify what it is
    connecting *to* without trusting anything in the redirect: the source id is bound to
    the stored `oauth_states` row, not carried in a parameter an attacker could change.

    PKCE and a stored `state` are both required. `state` alone is a CSRF token; the PKCE
    verifier is what makes an intercepted authorization code unusable.

    **`redirect_uri` comes from the client, and that is safe rather than an oversight.**
    The provider validates it against the URIs registered on the OAuth client and rejects
    anything else, so this route cannot be used to redirect a grant somewhere the owner of
    that client did not allow — and reaching this route at all requires the API bearer
    token. It is a parameter because the SPA, a local dev server, and a future iOS app each
    have a different one, and hardcoding one would mean a code change per client.
    """
    if body.provider != PROVIDER:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            f"Only {PROVIDER!r} is supported. X bookmarks are not built — the API tier is "
            "a spend decision that has not been made.",
        )

    source = phase2.create_source(
        conn,
        user_id=user_id,
        kind=SourceKind.GMAIL.value,
        name=body.name.strip(),
        config={"query": body.query.strip()} if body.query and body.query.strip() else {},
    )
    # Inactive until a credential exists: a source with no token would otherwise be
    # picked up by the poll scheduler and fail on every run.
    phase2.set_source_active(conn, source.id, active=False)

    verifier, challenge = new_pkce_pair()
    state = new_oauth_state()
    phase2.start_oauth(
        conn,
        state=state,
        user_id=user_id,
        provider=PROVIDER,
        source_id_=source.id,
        code_verifier=verifier,
        redirect_uri=body.redirect_uri,
        scopes=[GMAIL_READONLY_SCOPE],
    )

    try:
        url = build_oauth_client().authorization_url(
            redirect_uri=body.redirect_uri,
            state=state,
            code_challenge=challenge,
            scopes=[GMAIL_READONLY_SCOPE],
        )
    except SourceError as exc:
        # In real mode with no Google OAuth client provisioned. A 503 rather than a 500:
        # nothing is wrong with the request, the capability is not configured yet.
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, str(exc)) from exc

    return ConnectSourceResponse(source_id=source.id, authorization_url=url, state=state)


@app.post("/v1/sources/callback", response_model=SourceResponse, tags=["sources"])
def oauth_callback(
    body: OAuthCallbackRequest, conn: Conn, user_id: User, wrapper: Wrapper, nudge: Nudge
) -> SourceResponse:
    """Complete consent: exchange the code, seal the tokens, and start polling.

    **This is the one place in the API that touches a third-party credential**, and it can
    only seal — `wrapper` is the encrypt-only half of the vault, and the deployed service
    account has no KMS decrypt permission (invariant 8). The plaintext token exists only
    as a local variable inside this function; it is never logged and never returned.

    The state is consumed exactly once by a `DELETE ... RETURNING`, so a replayed callback
    finds nothing rather than racing a concurrent one into two token exchanges.
    """
    # Refused *before* the consume, mirroring `complete_login`. `oauth_states` now holds
    # two kinds of authorization — connecting a mailbox, and signing in — and they land on
    # the same SPA path; a sign-in consumed here would be spent on a flow that cannot
    # finish it, and the user would start again for no visible reason. Checking the
    # provider on the row afterwards is too late, because consuming is what destroys it.
    if is_login_state(body.state.strip()):
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            "That callback came from a sign-in. It finishes at /v1/auth/google/callback.",
        )
    if is_connector_state(body.state.strip()):
        # The third flow on the same path (motet#102), refused before the consume for the
        # same reason: spending a connector's state here would burn its authorization.
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            "That callback came from a connector authorization. It finishes at "
            "/v1/connectors/oauth/callback.",
        )

    pending = phase2.consume_oauth_state(conn, body.state.strip())
    if pending is None or pending["user_id"] != user_id or pending["provider"] != PROVIDER:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            "This authorization is unknown, already used, or expired. Start again.",
        )

    source_id = pending["source_id"]
    source = phase2.get_source(conn, source_id, user_id=user_id) if source_id else None
    if source is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "The source being connected is gone.")

    try:
        grant = build_oauth_client().exchange_code(
            code=body.code,
            redirect_uri=pending["redirect_uri"],
            code_verifier=pending["code_verifier"],
        )
    except SourceError as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc)) from exc

    if not grant.refresh_token:
        # Without a refresh token the connection dies in an hour and cannot be renewed.
        # Refusing now, with an explanation, beats a mailbox that stops working silently.
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            "The provider returned no refresh token, so this connection could not be "
            "kept alive. Revoke Motet's access in your account settings and try again.",
        )

    # What this authorization asked for, intersected with what came back (motet#96). A
    # provider that folded an earlier, wider grant into this token cannot make a source that
    # asked for read-only access *look* writable: the worker decides whether to write from
    # these recorded scopes, so a scope nobody asked for on this source is never one it acts on.
    asked = set((pending.get("scopes") or "").split())
    scopes = tuple(scope for scope in grant.scopes if not asked or scope in asked) or (
        GMAIL_READONLY_SCOPE,
    )
    try:
        phase2.store_source_credential(
            conn,
            wrapper,
            user_id=user_id,
            source_id_=source.id,
            provider=PROVIDER,
            purpose=CredentialPurpose.REFRESH.value,
            secret=grant.refresh_token,
            scopes=scopes,
        )
    except VaultError as exc:
        # The vault refused — in a deployed environment that means KMS is not reachable or
        # not permitted. Never fall back to storing the token unsealed: invariant 8 has no
        # degraded mode.
        # `exception`, not `error`: the vault translates *everything* Cloud KMS can refuse
        # with into `VaultError`, so this line is the only place a genuine bug in that
        # path and a real KMS refusal can be told apart — and without a traceback they
        # arrive in GlitchTip looking identical.
        logger.exception("could not seal the credential for source %s: %s", source.id, exc)
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            "This credential could not be stored securely, so it was not stored at all.",
        ) from exc

    phase2.set_source_active(conn, source.id, active=True)
    # Storing the grant rewrites its `updated_at`, which is what makes a worker check which
    # account it reaches before reading or writing with it (`ingest.check_mailbox`): a
    # label-sync re-consent replaces an existing source's grant, and the account chooser can
    # return a different account's.
    enqueue_source_poll(conn, source.id)
    nudge.arm(DrainReason.SOURCE_POLL)

    connected = phase2.get_source(conn, source.id, user_id=user_id)
    assert connected is not None, "the row was read above, in this transaction"
    return _source_response(conn, connected)


@app.post("/v1/sources/{source_id}/poll", response_model=SourceResponse, tags=["sources"])
def poll_source(
    conn: Conn, user_id: User, source_id: Annotated[str, Path()], nudge: Nudge
) -> SourceResponse:
    """Queue a poll now, rather than waiting for the scheduler.

    Enqueues; it does not fetch. Polling is serialized per source, so asking twice in a row
    produces one run and one deferral rather than two overlapping fetches.
    """
    source = phase2.get_source(conn, source_id, user_id=user_id)
    if source is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "No such source.")
    if not source.active:
        raise HTTPException(status.HTTP_409_CONFLICT, "This source is paused or not connected yet.")
    enqueue_source_poll(conn, source.id)
    nudge.arm(DrainReason.SOURCE_POLL)
    return _source_response(conn, source)


@app.put("/v1/sources/{source_id}/label-sync", response_model=SourceResponse, tags=["sources"])
def set_label_sync(
    body: LabelSyncRequest, conn: Conn, user_id: User, source_id: Annotated[str, Path()]
) -> SourceResponse:
    """Choose the label a message leaves and the label it joins when its owner ingests it.

    motet#96. Stored on the source's ``config`` — the owner's intent — and read by the
    worker after each *deliberate* ingest; nothing on the poll path reads it. Both empty
    turns label sync off, and off means no mailbox write of any kind.

    **Setting labels widens nothing.** A mailbox connected read-only stays read-only and
    reports ``needs_reauthorization``; the wider grant is a separate, explicit step —
    ``POST /v1/sources/{id}/reauthorize`` — which only a source with labels set may take.
    A system label that could hide mail (TRASH, SPAM, and the rest outside INBOX, UNREAD,
    STARRED and IMPORTANT) is refused here, and refused again by the worker.
    """
    source = phase2.get_source(conn, source_id, user_id=user_id)
    if source is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "No such source.")
    if source.kind != SourceKind.GMAIL.value:
        raise HTTPException(status.HTTP_409_CONFLICT, "Only a mailbox has labels to sync.")
    try:
        chosen = LabelSettings.parse(remove=body.remove_label, add=body.add_label)
    except LabelSettingsError as exc:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, str(exc)) from exc

    phase2.set_source_config_key(
        conn, source.id, LABEL_SYNC_CONFIG_KEY, chosen.to_config() if chosen else None
    )
    updated = phase2.get_source(conn, source.id, user_id=user_id)
    assert updated is not None
    return _source_response(conn, updated)


@app.post(
    "/v1/sources/{source_id}/reauthorize",
    response_model=ConnectSourceResponse,
    status_code=status.HTTP_201_CREATED,
    tags=["sources"],
)
def reauthorize_source(
    body: ReauthorizeSourceRequest, conn: Conn, user_id: User, source_id: Annotated[str, Path()]
) -> ConnectSourceResponse:
    """Ask the owner to let this mailbox's labels be changed — the label-sync re-consent.

    **The only route that ever asks Google for ``gmail.modify``, and only for a source that
    has label sync set.** Connecting asks for ``gmail.readonly`` alone, so a mailbox whose
    owner never turns label sync on is never shown a consent screen that mentions changing
    mail, and never holds a grant that could. Refused with 409 until labels are set, so the
    wider scope is always asked for *because of* a setting the owner chose.

    The consent itself is the owner's click (invariant 9) and finishes on the same
    ``/v1/sources/callback`` a first connect does, against the same source: the new grant
    replaces the stored one, and the worker refreshes any access token minted under the old
    one before it writes. The URL carries a ``login_hint`` for the address the source was
    first seen to reach, and the worker refuses — and disconnects — a grant that reaches a
    different one, because Google's account chooser does not stop the owner picking another.
    """
    source = phase2.get_source(conn, source_id, user_id=user_id)
    if source is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "No such source.")
    if source.kind != SourceKind.GMAIL.value:
        raise HTTPException(status.HTTP_409_CONFLICT, "Only a mailbox has labels to sync.")
    if LabelSettings.from_config(source.config) is None:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            "Choose a label to add or remove first. Motet asks a mailbox for permission to "
            "change labels only when label sync is set up for it.",
        )

    verifier, challenge = new_pkce_pair()
    state = new_oauth_state()
    phase2.start_oauth(
        conn,
        state=state,
        user_id=user_id,
        provider=PROVIDER,
        source_id_=source.id,
        code_verifier=verifier,
        redirect_uri=body.redirect_uri,
        scopes=LABEL_SYNC_SCOPES,
    )
    try:
        address = source.sync_state.get(MAILBOX_ADDRESS_KEY)
        url = build_oauth_client().authorization_url(
            redirect_uri=body.redirect_uri,
            state=state,
            code_challenge=challenge,
            scopes=LABEL_SYNC_SCOPES,
            # Preselects the account this source already reads; the worker still checks.
            login_hint=address if isinstance(address, str) else None,
        )
    except SourceError as exc:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, str(exc)) from exc
    return ConnectSourceResponse(source_id=source.id, authorization_url=url, state=state)


def _label_sync(
    conn: psycopg.Connection[Any], source: StoredSource, scopes: Sequence[str]
) -> LabelSyncResponse | None:
    """What a Sources screen shows about label sync — read without decrypting anything.

    Whether the grant can write is the credential row's recorded scopes, which is metadata
    (invariant 8); the label names come from the catalog the last poll cached.
    """
    if source.kind != SourceKind.GMAIL.value:
        return None
    chosen = LabelSettings.from_config(source.config)
    granted = GMAIL_MODIFY_SCOPE in scopes
    summary = phase2.label_writeback_summary(conn, source.id)
    return LabelSyncResponse(
        status="off" if chosen is None else ("on" if granted else "needs_reauthorization"),
        remove_label=chosen.remove if chosen else None,
        add_label=chosen.add if chosen else None,
        modify_granted=granted,
        available_labels=pickable(catalog_from_sync_state(source.sync_state)),
        labels_read_at=catalog_fetched_at(source.sync_state),
        last_synced_at=summary.last_synced_at,
        failed_items=summary.failed,
        last_error=summary.last_error,
    )


@app.delete(
    "/v1/sources/{source_id}/credentials",
    status_code=status.HTTP_204_NO_CONTENT,
    tags=["sources"],
)
def disconnect_source(conn: Conn, user_id: User, source_id: Annotated[str, Path()]) -> Response:
    """Forget a mailbox's credentials and stop polling it.

    The source row and everything it ingested survive: deleting the source would cascade
    to its source items and take the claims that cite them with it, which would silently
    break the transcript of an episode the user has already heard.
    """
    source = phase2.get_source(conn, source_id, user_id=user_id)
    if source is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "No such source.")
    # `disconnected_at` only when a credential was actually forgotten. Disconnecting a
    # row that never held one — an abandoned consent — must not turn it into a
    # "disconnected" mailbox, which the dismiss route below would then refuse to remove.
    if phase2.delete_source_credentials(conn, source.id):
        phase2.mark_source_disconnected(conn, source.id)
    else:
        phase2.set_source_active(conn, source.id, active=False)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


#: Why a dismiss was refused, in words that say what to do instead.
_REMOVAL_REFUSED: dict[phase2.SourceRemoval, str] = {
    phase2.SourceRemoval.BUILT_IN: "Pasted text is built in and cannot be removed.",
    phase2.SourceRemoval.HELD_A_CREDENTIAL: (
        "This source was connected, so it is kept: what it pulled in may be cited by "
        "episodes. Disconnect it instead. Only a consent attempt that never finished can "
        "be removed."
    ),
    phase2.SourceRemoval.HAS_ITEMS: (
        "This source has pulled items in, and removing it would delete them. Only a "
        "consent attempt that never finished can be removed."
    ),
    phase2.SourceRemoval.CONSENT_IN_PROGRESS: (
        "Consent for this source is being completed right now. Refresh in a moment: it "
        "will either be connected or removable."
    ),
}


@app.delete(
    "/v1/sources/{source_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    tags=["sources"],
)
def remove_source(conn: Conn, user_id: User, source_id: Annotated[str, Path()]) -> Response:
    """Dismiss a consent attempt that never finished.

    `POST /v1/sources/connect` creates the source row before the user leaves for the
    provider, so every cancelled consent leaves one behind, forever. This removes such a
    row and **refuses everything else with a 409**: the built-in paste source, any source
    that holds or ever held a credential, and any source that has pulled an item in. Those
    guards are the route, not a detail of it — deleting a source cascades to its source
    items and to the claims and highlights that cite them, which is why disconnecting keeps
    the row. `motet_db.phase2.remove_unused_source` holds the checks, under a row lock.

    Another user's source is a 404, exactly like one that does not exist.
    """
    outcome = phase2.remove_unused_source(conn, user_id=user_id, source_id_=source_id)
    if outcome is phase2.SourceRemoval.NOT_FOUND:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "No such source.")
    if outcome is not phase2.SourceRemoval.REMOVED:
        raise HTTPException(status.HTTP_409_CONFLICT, _REMOVAL_REFUSED[outcome])
    return Response(status_code=status.HTTP_204_NO_CONTENT)


# --- Credentials: connectors for agentic enrichment (motet#102) ---------------------------
#
# The validation and the response shape are `connectors.py`'s; the OAuth client is
# `motet_sources.mcp_oauth`, which a worker also reaches to refresh. What lives here is the
# HTTP surface, and one rule every route keeps: no answer ever carries a secret.


@app.get("/v1/connectors", response_model=list[ConnectorResponse], tags=["connectors"])
def list_connectors(conn: Conn, user_id: User) -> list[ConnectorResponse]:
    """Every site and MCP server this user has added, with whether a secret is stored.

    Answered without decrypting anything, for ``/v1/sources``' reason: the API cannot open
    a credential (invariant 8), so a screen that needed one opened would need the invariant
    broken.
    """
    return [connector_response(c) for c in connector_repo.list_connectors(conn, user_id)]


@app.post(
    "/v1/connectors",
    response_model=ConnectorResponse,
    status_code=status.HTTP_201_CREATED,
    tags=["connectors"],
)
def create_connector(
    body: CreateConnectorRequest, conn: Conn, user_id: User, wrapper: Wrapper
) -> ConnectorResponse:
    """Add a site — the opt-in to fetching its articles — or an MCP server.

    A site needs only its domain; its username and password are for a site that needs a
    login, and the password is sealed here and never readable again from this process. An
    MCP server is refused without ``acknowledge_risk``, and starts ``needs_auth``: nothing
    about it works until ``/authorize`` and the callback have produced a token set.
    """
    try:
        spec = connector_spec(body)
    except ConnectorInputError as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc)) from exc
    try:
        created = connector_repo.create_connector(
            conn,
            wrapper,
            user_id=user_id,
            kind=spec.kind,
            label=spec.label,
            domain=spec.domain,
            domains=spec.domains,
            url=spec.url,
            username=spec.username,
            secret=spec.secret,
            risk_acknowledged=spec.risk_acknowledged,
        )
    except psycopg.errors.UniqueViolation as exc:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            f"{spec.domain} is already on your list. Remove it to replace its login.",
        ) from exc
    except VaultError as exc:
        # Never fall back to storing the password unsealed: invariant 8 has no degraded mode.
        logger.exception("could not seal a site password: %s", exc)
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            "This credential could not be stored securely, so it was not stored at all.",
        ) from exc
    return connector_response(created)


@app.delete(
    "/v1/connectors/{connector_id}", status_code=status.HTTP_204_NO_CONTENT, tags=["connectors"]
)
def delete_connector(conn: Conn, user_id: User, connector_id: Annotated[str, Path()]) -> Response:
    """Forget a connector and its sealed secret. Another user's is a 404, like a missing one."""
    if not connector_repo.delete_connector(conn, user_id=user_id, connector_id=connector_id):
        raise HTTPException(status.HTTP_404_NOT_FOUND, "No such connector.")
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@app.post(
    "/v1/connectors/{connector_id}/authorize",
    response_model=AuthorizeConnectorResponse,
    tags=["connectors"],
)
def authorize_connector(
    body: AuthorizeConnectorRequest,
    conn: Conn,
    user_id: User,
    config: Config,
    connector_id: Annotated[str, Path()],
) -> AuthorizeConnectorResponse:
    """Discover the server's authorization server, register a client, mint a consent URL.

    **Nothing about the connector changes until consent completes.** What discovery and
    registration produced is bound to the state row and written onto the connector only by
    the callback, beside the token set it issued — so a re-authorize the owner abandons
    leaves a working server's client, endpoint and grant exactly as they were.

    Discovery runs on every authorize, because a person re-authorizing is the moment to
    notice a server that moved. The recorded client is reused only while both the issuer
    and the redirect URI it was registered with are unchanged: a dynamically registered
    client is bound to its redirect URI, so a new app origin needs a new client.

    ``redirect_uri`` must be this deployment's own callback (``Settings.callback_uri_allowed``,
    as sign-in checks it). Unlike Google's, a dynamically registered client accepts whatever
    URI it was registered with, so the server's own check proves nothing here.
    """
    redirect_uri = body.redirect_uri.strip()
    if not config.callback_uri_allowed(redirect_uri):
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            f"redirect_uri must be this deployment's app origin plus {CALLBACK_PATH}.",
        )
    connector = connector_repo.get_connector(conn, connector_id, user_id=user_id)
    if connector is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "No such connector.")
    if connector.kind != connector_repo.MCP or connector.url is None:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Only an MCP server is authorized.")

    client = build_mcp_oauth_client()
    try:
        server = client.discover(connector.url)
    except UnsafeUrlError as exc:
        raise _connector_refused(
            conn, connector, "error", status.HTTP_400_BAD_REQUEST, exc
        ) from exc
    except McpOAuthError as exc:
        raise _connector_refused(
            conn, connector, "error", status.HTTP_502_BAD_GATEWAY, exc
        ) from exc

    reusable = (
        connector.oauth_issuer == server.issuer and connector.oauth_redirect_uri == redirect_uri
    )
    client_id = connector.oauth_client_id if reusable else None
    if client_id is None:
        try:
            client_id = client.register(server, redirect_uri=redirect_uri)
        except RegistrationUnsupportedError as exc:
            raise _connector_refused(
                conn, connector, "needs_auth", status.HTTP_409_CONFLICT, exc
            ) from exc
        except McpOAuthError as exc:
            raise _connector_refused(
                conn, connector, "error", status.HTTP_502_BAD_GATEWAY, exc
            ) from exc

    phase2.purge_expired_oauth_states(conn)
    verifier, challenge = new_pkce_pair()
    state = new_connector_state()
    phase2.start_oauth(
        conn,
        state=state,
        user_id=user_id,
        provider=MCP_PROVIDER,
        source_id_=None,
        connector_id_=connector.id,
        code_verifier=verifier,
        redirect_uri=redirect_uri,
        scopes=server.scopes,
        oauth_client={
            "issuer": server.issuer,
            "client_id": client_id,
            "token_endpoint": server.token_endpoint,
            "resource": server.resource,
            "iss_parameter_supported": server.iss_parameter_supported,
        },
    )
    url = authorization_url(
        server,
        client_id=client_id,
        redirect_uri=redirect_uri,
        state=state,
        code_challenge=challenge,
    )
    return AuthorizeConnectorResponse(authorization_url=url, state=state)


@app.post("/v1/connectors/oauth/callback", response_model=ConnectorResponse, tags=["connectors"])
def connector_oauth_callback(
    body: ConnectorOAuthCallbackRequest, conn: Conn, user_id: User, wrapper: Wrapper
) -> ConnectorResponse:
    """Exchange the code, seal the token set onto the connector, and mark it ready.

    The token set exists as a local variable and nowhere else: it is sealed under
    ``user_id:connector_id:mcp`` and this process cannot read it back. A state from either
    other flow is refused *before* the consume, as ``/v1/sources/callback`` refuses one, so
    a misrouted callback does not burn the authorization it belongs to.
    """
    state = body.state.strip()
    if not is_connector_state(state):
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST, "That callback does not belong to a connector."
        )
    pending = phase2.consume_oauth_state(conn, state)
    if pending is None or pending["user_id"] != user_id or pending["provider"] != MCP_PROVIDER:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            "This authorization is unknown, already used, or expired. Start again.",
        )
    connector = connector_repo.get_connector(conn, pending["connector_id"] or "", user_id=user_id)
    if connector is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "The connector being authorized is gone.")
    bound = pending.get("oauth_client")
    if not isinstance(bound, dict) or not all(
        bound.get(key) for key in ("issuer", "client_id", "token_endpoint", "resource")
    ):
        raise HTTPException(status.HTTP_409_CONFLICT, "This connector was never sent to consent.")
    issuer = str(bound["issuer"])
    # RFC 9207. A code delivered under another issuer is a mix-up, not a grant — and a
    # server that promised to name itself on every response and did not name itself on this
    # one is exactly the response a mix-up attacker would strip the name from.
    if body.iss:
        if body.iss.rstrip("/") != issuer:
            raise HTTPException(
                status.HTTP_400_BAD_REQUEST,
                "The authorization response came from a different issuer.",
            )
    elif bound.get("iss_parameter_supported"):
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            "The server says it names itself on every authorization response, and this one "
            "did not. Start again.",
        )

    server = AuthorizationServer(
        issuer=issuer,
        authorization_endpoint="",
        token_endpoint=str(bound["token_endpoint"]),
        registration_endpoint=None,
        resource=str(bound["resource"]),
        scopes=(),
        iss_parameter_supported=bool(bound.get("iss_parameter_supported")),
    )
    try:
        tokens = build_mcp_oauth_client().exchange_code(
            server,
            client_id=str(bound["client_id"]),
            code=body.code,
            redirect_uri=pending["redirect_uri"],
            code_verifier=pending["code_verifier"],
        )
    except McpOAuthError as exc:
        raise _connector_refused(
            conn, connector, "needs_auth", status.HTTP_400_BAD_REQUEST, exc
        ) from exc

    # The client that issued this grant, recorded beside it, so a refresh asks the right one.
    connector_repo.set_connector_oauth_client(
        conn,
        connector.id,
        issuer=issuer,
        client_id=str(bound["client_id"]),
        token_endpoint=server.token_endpoint,
        resource=server.resource,
        redirect_uri=pending["redirect_uri"],
    )
    try:
        stored = connector_repo.store_connector_secret(
            conn,
            wrapper,
            connector_id=connector.id,
            secret=tokens.to_json(),
            expires_at=tokens.expires_at,
        )
    except VaultError as exc:
        logger.exception("could not seal the token set for connector %s: %s", connector.id, exc)
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            "This credential could not be stored securely, so it was not stored at all.",
        ) from exc
    return connector_response(stored)


def _connector_refused(
    conn: psycopg.Connection[Any],
    connector: connector_repo.StoredConnector,
    outcome: connector_repo.ConnectorStatus,
    http_status: int,
    exc: McpOAuthError,
) -> HTTPException:
    """Write the reason onto the row and *commit it* before the request fails.

    ``deps.connection`` rolls back on the exception about to be raised, which would undo the
    very ``last_error`` the screen needs to explain the pill — the same reason
    ``require_caller`` commits its revoke before raising.

    **A server that already works stays `ready`.** A failed *re*-authorize — a network blip
    during discovery, a Cancel, a refused code — leaves its sealed grant in force, so only
    the reason is recorded; demoting the row would switch off a working server over a click.
    """
    keep = connector.has_secret and connector.status == "ready"
    connector_repo.set_connector_status(
        conn, connector.id, status="ready" if keep else outcome, last_error=str(exc)
    )
    conn.commit()
    return HTTPException(http_status, str(exc))


# --- Phase 2: smart episodes ---------------------------------------------------------


@app.post(
    "/v1/episodes/smart",
    response_model=EpisodeResponse,
    status_code=status.HTTP_201_CREATED,
    tags=["episodes"],
)
def create_smart_episode(
    body: CreateSmartEpisodeRequest, conn: Conn, user_id: User, nudge: Nudge
) -> EpisodeResponse:
    """Assemble an episode by rule rather than by "everything unread".

    The rule is validated **here**, at creation, and stored as a snapshot on the episode.
    Validating at assembly time instead would surface a typo as a failed episode minutes
    later on a queue, with the mistake and the error in different places.
    """
    try:
        rule = SmartRule.from_json(body.rule.model_dump())
    except RuleError as exc:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, str(exc)) from exc

    episode_id = enqueue_smart_episode(
        conn,
        user_id=user_id,
        title=body.title.strip(),
        max_duration_ms=body.max_duration_ms,
        rule=rule,
    )
    nudge.arm(DrainReason.SMART_EPISODE)
    episode = repo.get_episode(conn, episode_id, user_id=user_id)
    assert episode is not None
    return _episode(conn, episode)


# --- Phase 2: read state from the audio side -----------------------------------------


# The explicit `summary` is load-bearing rather than cosmetic: FastAPI derives one from
# the handler's name, so two routes on one handler would carry the same summary — and the
# Swift generator names its endpoint function from the summary, so both would emit
# `MotetEndpoints.reportListenProgress` and the enum would not compile. The generator
# refuses on a collision now, so a future stack fails at `bin/ci` rather than at the one
# job in this project that has a Swift compiler.
@app.put(
    "/v1/episodes/{episode_id}/position",
    response_model=ListenProgressResponse,
    summary="Set Playback Position",
    tags=["episodes"],
)
@app.post(
    "/v1/episodes/{episode_id}/progress",
    response_model=ListenProgressResponse,
    tags=["episodes"],
)
def report_listen_progress(
    body: ListenProgressRequest,
    conn: Conn,
    user_id: User,
    episode_id: Annotated[str, Path()],
) -> ListenProgressResponse:
    """Record how far the listener has got, and mark what they have passed as read.

    **This is how the audio surface participates in invariant 5.** Phase 1 only had the
    visual side plus an all-or-nothing "mark listened"; this makes partial listening count.
    A story is read once its segment has been *passed* — the comparison is against the end
    of the segment, because marking at the start would tick a story off on its first word.

    Position is monotonic on the server (invariant 4: we own it). A client that seeks
    backwards is reviewing, not un-listening, so a lower report never lowers the recorded
    position and never un-marks a story.

    **Two paths, one handler, and the stacked decorators are the point** (motet#11).
    ``PUT /v1/episodes/{id}/position`` is the position resource a syncing player wants: an
    idempotent write of one integer, whose current value comes back on every
    ``EpisodeResponse`` so a device that has never played the episode can still resume.
    ``POST /v1/episodes/{id}/progress`` is the same write under the name the shipped
    clients already generate against. A *second* write path would be a second definition
    of one fact, which is the failure invariant 5 exists to prevent — so there is one
    function, one column, and one monotonicity rule, reachable by two spellings.
    """
    try:
        position, marked = phase2.record_listen_progress(
            conn,
            user_id=user_id,
            episode_id_=episode_id,
            listened_through_ms=body.listened_through_ms,
        )
    except LookupError as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "No such episode.") from exc
    return ListenProgressResponse(
        episode_id=episode_id, listened_through_ms=position, news_items_marked_read=marked
    )


# --- Phase 2: highlights -------------------------------------------------------------


@app.get("/v1/highlights", response_model=list[HighlightResponse], tags=["highlights"])
def list_highlights(conn: Conn, user_id: User) -> list[HighlightResponse]:
    """Every saved passage, newest first."""
    return [_highlight(item) for item in phase2.list_highlights(conn, user_id)]


@app.post(
    "/v1/highlights",
    response_model=HighlightResponse,
    status_code=status.HTTP_201_CREATED,
    tags=["highlights"],
)
def save_highlight(body: SaveHighlightRequest, conn: Conn, user_id: User) -> HighlightResponse:
    """Save a passage — what the `save_highlight` platform tool calls.

    **The quote is read out of the source item, not taken from the caller.** That is the
    whole trust property: in the voice case the caller is a model, and a model that quoted
    loosely would otherwise write its own paraphrase into the user's highlights where it
    would look verbatim.

    Anchored to the source span and nothing else. Claims are rewritten on every script
    retry and audio offsets move on every re-render; `source_items.text` never changes.
    `episode_id` and `anchor_ms` record where the listener was — provenance, not anchor.

    `news_item_id` is checked against the source item's actual story rather than trusted,
    for the same reason the quote is — a source item belongs to exactly one news item, so
    the caller's copy of that pairing can only ever be redundant or wrong.
    """
    if body.span_end <= body.span_start:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY,
            "span_end must be greater than span_start: an empty span anchors nothing.",
        )
    saved = phase2.save_highlight(
        conn,
        user_id=user_id,
        news_item_id=body.news_item_id,
        source_item_id_=body.source_item_id,
        span_start=body.span_start,
        span_end=body.span_end,
        note=body.note,
        episode_id_=body.episode_id,
        anchor_ms=body.anchor_ms,
    )
    if saved is None:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY,
            "That span does not resolve inside that source item, or that source item is "
            "not part of that news item. Either way it is not an anchor.",
        )
    return _highlight(saved)


@app.delete(
    "/v1/highlights/{highlight_id}", status_code=status.HTTP_204_NO_CONTENT, tags=["highlights"]
)
def delete_highlight(conn: Conn, user_id: User, highlight_id: Annotated[str, Path()]) -> Response:
    if not phase2.delete_highlight(conn, user_id=user_id, highlight_id_=highlight_id):
        raise HTTPException(status.HTTP_404_NOT_FOUND, "No such highlight.")
    return Response(status_code=status.HTTP_204_NO_CONTENT)


# --- Phase 2: subtitles and chapters -------------------------------------------------


@app.get(
    "/v1/episodes/{episode_id}/transcript.vtt",
    tags=["feed"],
    response_class=Response,
    responses={200: {"content": {"text/vtt": {}}, "description": "WebVTT captions"}},
)
def episode_transcript(
    conn: Conn, user_id: FeedUser, episode_id: Annotated[str, Path()]
) -> Response:
    """WebVTT captions, one cue per spoken claim.

    Authenticated by the **feed** token rather than the API token, because the client that
    fetches this is the podcast app — it found the URL in a `<podcast:transcript>` tag and
    will send exactly the credential that was in it.
    """
    episode, titles = _episode_with_titles(conn, user_id, episode_id)
    return Response(
        content=transcript_vtt(episode, titles),
        media_type="text/vtt",
        headers={"Content-Disposition": f'inline; filename="{episode.id}.vtt"'},
    )


@app.get(
    "/v1/episodes/{episode_id}/chapters.json",
    tags=["feed"],
    response_class=Response,
    responses={
        200: {
            "content": {"application/json+chapters": {}},
            "description": "Podcasting 2.0 chapters",
        }
    },
)
def episode_chapters(conn: Conn, user_id: FeedUser, episode_id: Annotated[str, Path()]) -> Response:
    """The Podcasting 2.0 chapters document, one chapter per story.

    Served with `application/json+chapters`, the media type the namespace specifies and the
    one the `<podcast:chapters>` tag declares. A client that fetched `application/json`
    here and got a mismatch would be within its rights to ignore the document.
    """
    episode, titles = _episode_with_titles(conn, user_id, episode_id)
    return Response(content=chapters_json(episode, titles), media_type="application/json+chapters")


def _episode_with_titles(
    conn: psycopg.Connection[Any], user_id: str, episode_id: str
) -> tuple[StoredEpisode, dict[str, str]]:
    """An episode plus its story titles, or a 404.

    Requires the episode to have audio: before TTS runs, every claim's timing is zero, so
    a transcript would be a stack of cues at 00:00 and chapters would all point at the
    start. An absent document reads as "not available yet"; a wrong one reads as broken.
    """
    episode = repo.get_episode(conn, episode_id, user_id=user_id)
    if episode is None or not episode.has_audio:
        raise HTTPException(
            status.HTTP_404_NOT_FOUND, "This episode has no rendered audio to caption yet."
        )
    titles = {
        item_id: item.title
        for item_id, item in repo.load_news_items(
            conn, [segment.news_item_id for segment in episode.segments]
        ).items()
    }
    return episode, titles


def _highlight(item: Highlight) -> HighlightResponse:
    return HighlightResponse(
        id=item.id,
        news_item_id=item.news_item_id,
        source_item_id=item.source_item_id,
        span=SourceSpanModel(
            source_item_id=item.source_item_id, start=item.span_start, end=item.span_end
        ),
        quote=item.quote,
        note=item.note,
        episode_id=item.episode_id,
        anchor_ms=item.anchor_ms,
        created_at=item.created_at,
    )


def sync_facts(source: StoredSource) -> dict[str, Any]:
    """The three facts a poll records on a source, as ``SourceResponse`` fields.

    Read out of ``sync_state``, which the worker writes and this route only reports. The
    cursor is deliberately not among them: it is the adapter's own, and opaque above it.
    """
    if source.kind != SourceKind.GMAIL.value:
        return {"query": None, "first_sync_days": None, "last_sync": None}
    days = source.sync_state.get("first_sync_days")
    raw = source.sync_state.get("last_sync")
    last: SourceSyncResult | None = None
    if isinstance(raw, dict):
        try:
            last = SourceSyncResult.model_validate(raw)
        except ValidationError:
            # A shape this build does not know is reported as absent rather than as a
            # 500 on the Sources screen; the worker is what writes it.
            logger.warning("source %s has an unreadable last_sync; not reporting it", source.id)
    return {
        "query": source_query(source.config),
        "first_sync_days": days if isinstance(days, int) and not isinstance(days, bool) else None,
        "last_sync": last,
    }


def repo_sources(conn: psycopg.Connection[Any], user_id: str) -> list[StoredSource]:
    """Named separately so the route's own name can be `list_sources`."""
    return phase2.list_sources(conn, user_id)
