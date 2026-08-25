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

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Annotated, Any

import psycopg
from fastapi import Depends, FastAPI, HTTPException, Path, Request, Response, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, RedirectResponse
from motet_db import (
    CredentialPurpose,
    Highlight,
    IngestionStatus,
    RuleError,
    SmartRule,
    SourceKind,
    StoredEpisode,
    StoredNewsItem,
    StoredSource,
    phase2,
    repo,
)
from motet_db import auth as auth_repo
from motet_inference.llm import load_config as load_llm_config
from motet_sources import (
    GMAIL_READONLY_SCOPE,
    PROVIDER,
    SourceError,
    build_oauth_client,
    new_oauth_state,
    new_pkce_pair,
)
from motet_storage import ObjectStore, StorageError
from motet_vault import DekWrapper, VaultError, vault_status
from motet_workers import (
    DEFAULT_MAX_ATTEMPTS,
    enqueue_episode,
    enqueue_paste,
    enqueue_smart_episode,
    enqueue_source_poll,
)
from starlette.requests import ClientDisconnect

from . import obs
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
from .deps import (
    Caller,
    connection,
    dek_wrapper,
    public_base_url,
    require_api_token,
    require_caller,
    require_feed_token,
    settings,
    store,
)
from .feed import FeedMetadata, feed_url, render_feed
from .schemas import (
    ClaimModel,
    CompleteLoginRequest,
    ConnectSourceRequest,
    ConnectSourceResponse,
    CreateEpisodeRequest,
    CreateSmartEpisodeRequest,
    EpisodeResponse,
    FeedInfoResponse,
    HealthResponse,
    HighlightResponse,
    IngestionItemResponse,
    ListenProgressRequest,
    ListenProgressResponse,
    LoginResponse,
    MarkListenedResponse,
    NewsItemResponse,
    OAuthCallbackRequest,
    PasteRequest,
    ReadStateRequest,
    RevokedResponse,
    SaveHighlightRequest,
    SegmentResponse,
    SessionResponse,
    SourceItemResponse,
    SourceResponse,
    SourceSpanModel,
    StartLoginRequest,
    StartLoginResponse,
)
from .shownotes import chapters_json, transcript_vtt

logger = logging.getLogger("motet.api")

Conn = Annotated[psycopg.Connection[Any], Depends(connection)]
User = Annotated[str, Depends(require_api_token)]
#: The caller *and how they proved it* — a signed-in browser, the shared API token, or an
#: unlocked deployment. Only the sign-in routes need the distinction; every other route
#: takes ``User``, because there is one account and the answer is always the same row.
Who = Annotated[Caller, Depends(require_caller)]
FeedUser = Annotated[str, Depends(require_feed_token)]
Config = Annotated[Settings, Depends(settings)]
Store = Annotated[ObjectStore, Depends(store)]
#: The **encrypt-only** half of the credential vault. The API seals third-party tokens
#: because the OAuth callback lands on an HTTP route; it must never hold anything that can
#: unseal one (invariant 8). `DekWrapper` has no `unwrap`, and the deployed service
#: account has no `useToDecrypt` — the type is the reminder, IAM is the control.
Wrapper = Annotated[DekWrapper, Depends(dek_wrapper)]


@asynccontextmanager
async def lifespan(_: FastAPI) -> AsyncIterator[None]:
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
    obs.configure()
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
    try:
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
        allow_methods=["GET", "POST", "OPTIONS"],
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


@app.get(HEALTH_PATH, response_model=HealthResponse, tags=["ops"])
def health(config: Config) -> HealthResponse:
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
        telemetry_configured=current.otlp_configured,
        telemetry_exporting=current.exporting,
        errors_configured=current.errors_configured,
        authenticated=config.authenticated,
        login_configured=config.login_configured,
        vault_backend=vault.backend,
        vault_ready=vault.ready,
        inference_mode=config.inference_mode,
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

    token = auth_repo.new_session_token()
    session = auth_repo.create_session(
        conn, user_id=repo.OWNER_USER_ID, email=identity.email, token=token
    )
    logger.info("signed in %s until %s", session.email, session.expires_at.isoformat())
    # The only time the token is ever readable. Only its hash is stored.
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
def paste_source(body: PasteRequest, conn: Conn, user_id: User) -> SourceItemResponse:
    """Ingest pasted text as a source item.

    Enqueues rather than processes: ingestion is serialized per user (invariant 6), so the
    work belongs to a worker draining the queue, never to the request thread. The row and
    the job are written in the same transaction — with two systems there would always be a
    window where the source item exists and nothing will ever pick it up.
    """
    stored = enqueue_paste(conn, user_id=user_id, title=body.title.strip(), text=body.text)
    return SourceItemResponse(id=stored.id, title=stored.title, state=stored.state.value)


@app.get("/v1/ingestion", response_model=list[IngestionItemResponse], tags=["ingestion"])
def list_ingestion(conn: Conn, user_id: User) -> list[IngestionItemResponse]:
    """What has been ingested but is not in the backlog yet, and why.

    The backlog answers "what do I have to listen to"; it cannot answer "where did the
    thing I just pasted go", because an item that never integrates never becomes a news
    item and so never appears there at all. That gap is the whole reason this route
    exists: content that fails is content that silently disappears.

    Scoped to the ``integrate`` stage, because that is the stage a ``source_items`` row
    exists for. A Gmail ``extract`` that fails has no row to report — the message was
    never turned into one — so it is invisible here and is tracked separately as motet#35.
    """
    return [_ingestion_item(item) for item in repo.list_ingestion(conn, user_id)]


@app.get("/v1/news-items", response_model=list[NewsItemResponse], tags=["backlog"])
def list_news_items(conn: Conn, user_id: User) -> list[NewsItemResponse]:
    """The backlog: deduped news items with their read state (invariant 5)."""
    return [_news_item(item) for item in repo.list_news_items(conn, user_id)]


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
    return _news_item(updated)


@app.post(
    "/v1/episodes",
    response_model=EpisodeResponse,
    status_code=status.HTTP_201_CREATED,
    tags=["episodes"],
)
def create_episode(body: CreateEpisodeRequest, conn: Conn, user_id: User) -> EpisodeResponse:
    """Assemble a manual episode from unread news items, capped by duration.

    Returns immediately, in ``pending``. Assembly, scripting, grounding validation, and TTS
    happen on the queue afterwards, and nothing is synthesized until grounding passes
    (invariant 3) — so the episode a client polls for moves through states rather than
    appearing finished.
    """
    episode_id = enqueue_episode(
        conn, user_id=user_id, title=body.title.strip(), max_duration_ms=body.max_duration_ms
    )
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
    body = render_feed(
        FeedMetadata(
            title=config.feed_title,
            description=config.feed_description,
            author=config.feed_author,
            base_url=base,
            token=token,
        ),
        repo.list_published_episodes(conn, user_id),
    )
    return Response(content=body, media_type="application/rss+xml")


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
    )


def _news_item(item: StoredNewsItem) -> NewsItemResponse:
    return NewsItemResponse(
        id=item.id,
        title=item.title,
        summary=item.summary,
        source_item_ids=list(item.source_item_ids),
        read=item.read,
        created_at=item.created_at,
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
    out = []
    for source in repo_sources(conn, user_id):
        credential = phase2.get_source_credential(
            conn, source_id_=source.id, purpose=CredentialPurpose.REFRESH.value
        )
        out.append(
            SourceResponse(
                id=source.id,
                kind=source.kind,
                name=source.name,
                active=source.active,
                connected=credential is not None,
                scopes=list(credential.scopes) if credential else [],
                last_polled_at=source.last_polled_at,
                last_error=source.last_error,
                created_at=source.created_at,
            )
        )
    return out


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
    body: OAuthCallbackRequest, conn: Conn, user_id: User, wrapper: Wrapper
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

    scopes = grant.scopes or (GMAIL_READONLY_SCOPE,)
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
    enqueue_source_poll(conn, source.id)

    return SourceResponse(
        id=source.id,
        kind=source.kind,
        name=source.name,
        active=True,
        connected=True,
        scopes=list(scopes),
        last_polled_at=source.last_polled_at,
        last_error=None,
        created_at=source.created_at,
    )


@app.post("/v1/sources/{source_id}/poll", response_model=SourceResponse, tags=["sources"])
def poll_source(conn: Conn, user_id: User, source_id: Annotated[str, Path()]) -> SourceResponse:
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
    credential = phase2.get_source_credential(
        conn, source_id_=source.id, purpose=CredentialPurpose.REFRESH.value
    )
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
    phase2.delete_source_credentials(conn, source.id)
    phase2.set_source_active(conn, source.id, active=False)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


# --- Phase 2: smart episodes ---------------------------------------------------------


@app.post(
    "/v1/episodes/smart",
    response_model=EpisodeResponse,
    status_code=status.HTTP_201_CREATED,
    tags=["episodes"],
)
def create_smart_episode(
    body: CreateSmartEpisodeRequest, conn: Conn, user_id: User
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
    episode = repo.get_episode(conn, episode_id, user_id=user_id)
    assert episode is not None
    return _episode(conn, episode)


# --- Phase 2: read state from the audio side -----------------------------------------


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


def repo_sources(conn: psycopg.Connection[Any], user_id: str) -> list[StoredSource]:
    """Named separately so the route's own name can be `list_sources`."""
    return phase2.list_sources(conn, user_id)
