# The Python half of Motet: one build, four runtime targets.
#
# `motet-api` and `motet-worker` are separate images in Artifact Registry because the
# infrastructure pins them separately, but they are the same tree — the API writes rows
# and enqueues jobs, the worker drains the queues, and both import the same
# workspace packages. Two Dockerfiles would be two copies of one dependency graph that
# drift the first time somebody edits only one of them, so this is one file with a
# shared `runtime` stage and a thin target on top of it:
#
#     docker build --target api    -t motet-api    .
#     docker build --target worker -t motet-worker .
#     docker build --target voice  -t motet-voice  .
#     docker build --target enrich -t motet-enrich .
#
# `motet-voice` and `motet-enrich` are the two targets that are NOT the shared tree. Each
# resolves from the same lockfile in the same build stage and then installs only its own
# package and what that depends on — see the `voice-build` and `enrich-build` stages for
# why, which is the same reason in both cases: a service that holds no database credential
# should not have a driver in its image either.
#
# Build context is the REPO ROOT, not a subdirectory. `uv.lock` describes the whole
# workspace, so a context rooted at `api/` could not resolve it.
#
# See bin/build-images, which is the supported way to build these and is what CI runs.

# ---------------------------------------------------------------------------
# Build — resolve and install into a self-contained virtualenv at /app/.venv.
# ---------------------------------------------------------------------------
FROM python:3.13-slim-bookworm AS build

# uv as a binary copied out of its own published image, rather than curl-piped into a
# shell. The tag is pinned to the same version bin/ci installs, so a container build and
# a CI run resolve the lockfile with identical machinery.
COPY --from=ghcr.io/astral-sh/uv:0.12.3 /uv /usr/local/bin/uv

ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    # The base image already has the interpreter this workspace asks for. Downloading a
    # second one would make the image bigger and the two Pythons a thing to reason about.
    UV_PYTHON_DOWNLOADS=never

WORKDIR /app

# Dependency metadata first, workspace source second. The dependency graph changes far
# less often than the code does, so this ordering keeps the expensive resolve-and-install
# layer cached across ordinary source edits.
#
# EVERY WORKSPACE MEMBER IS COPIED, INCLUDING ONES THESE IMAGES DO NOT IMPORT. `uv sync`
# resolves the whole workspace: uv.lock records each member as a path source, so a member
# missing from the build context fails the sync outright —
# `Distribution not found at: file:///app/voice` — rather than being quietly skipped.
# `voice` is the case in point. Neither motet-api nor motet-worker imports motet_voice,
# and the voice service is a separate deployable by design, but it is a member of this
# workspace and so it has to be here for the sync to plan at all.
#
# Keep this list in step with `[tool.uv.workspace] members` in the root pyproject.toml.
# It went out of step once already, and in a way no CI run could see: this Dockerfile was
# written on a branch whose workspace had five members while `voice` was being added on
# main in parallel. Both branches were green; the merge of the two was not.
COPY pyproject.toml uv.lock ./
COPY api/pyproject.toml api/pyproject.toml
COPY db/pyproject.toml db/pyproject.toml
COPY enrich/pyproject.toml enrich/pyproject.toml
COPY inference/pyproject.toml inference/pyproject.toml
COPY obs/pyproject.toml obs/pyproject.toml
COPY sources/pyproject.toml sources/pyproject.toml
COPY storage/pyproject.toml storage/pyproject.toml
COPY vault/pyproject.toml vault/pyproject.toml
COPY voice/pyproject.toml voice/pyproject.toml
COPY workers/pyproject.toml workers/pyproject.toml

# `--frozen` is the point of this line: it fails rather than silently re-resolving when
# uv.lock does not match the pyproject files. An image built from a quietly different
# dependency set than CI tested is the failure this flag exists to prevent.
RUN uv sync --frozen --no-dev --no-install-workspace

COPY api api
COPY db db
COPY enrich enrich
COPY inference inference
COPY obs obs
COPY sources sources
COPY storage storage
COPY vault vault
COPY voice voice
COPY workers workers

RUN uv sync --frozen --no-dev

# ---------------------------------------------------------------------------
# voice-build — the voice service's venv, and nothing the voice service does not import.
# ---------------------------------------------------------------------------
FROM build AS voice-build

# `--package motet-voice` makes the venv exactly `motet-voice`'s dependency closure, and
# because `uv sync` is exact it UNINSTALLS the rest: `motet-db`, `psycopg`, the API, the
# worker. That is invariant 2 made a property of the artifact — the voice service holds no
# database credential, and its image does not even carry a driver it could use one with.
# `voice/tests/test_no_database_access.py` makes the claim against the source tree and
# `bin/build-images` makes it against this image.
#
# `--no-editable` installs the workspace members as real wheels inside the venv, so the
# `voice` target below copies the venv and nothing else. An editable install would point
# back at /app/<member>/src, and copying that tree would put `db/` into the image as source.
#
# From the `build` stage rather than a fresh resolve, so the lockfile, the uv version and
# the downloaded wheels are the ones the other two targets were built from. The cost is
# caching: that stage copied every member's source, so an edit under `api/` or `db/` also
# re-runs this step.
RUN uv sync --frozen --no-dev --package motet-voice --no-editable

# ---------------------------------------------------------------------------
# enrich-build — the enrichment service's venv, and nothing it does not import.
# ---------------------------------------------------------------------------
FROM build AS enrich-build

# `voice-build`'s argument, one service along, and with more riding on it. Design option D2
# (motet#102) puts the agentic run in a container whose service account holds nothing,
# because the code it shells out to is third-party npm driving a browser over untrusted
# pages — and any process in a Cloud Run container can mint that container's
# service-account token. `--package motet-enrich` makes the venv exactly that package's
# closure, so `motet_db`, `psycopg` and `motet_vault` are not in the image at all: there is
# no database credential to hand it and no code that could use one.
# `enrich/tests/test_no_database_reach.py` makes the claim against the source tree and
# `bin/build-images` makes it against this image.
RUN uv sync --frozen --no-dev --package motet-enrich --no-editable

# ---------------------------------------------------------------------------
# base — what every Python image shares: the user, the interpreter settings.
# ---------------------------------------------------------------------------
FROM python:3.13-slim-bookworm AS base

# Not root. Cloud Run does not require it, but nothing in any of these processes needs to
# write outside its own temp dir, and a container that cannot modify its own code is one
# less thing to think about if a dependency is ever compromised.
RUN useradd --create-home --uid 10001 motet

WORKDIR /app

ENV PATH="/app/.venv/bin:$PATH" \
    # Logs must reach the log collector as they are written. Without this, Python buffers
    # stdout when it is a pipe — which it always is here — and a container that dies
    # takes its last and most interesting lines with it.
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

USER motet

# ---------------------------------------------------------------------------
# Runtime — the venv and the source, and nothing that built them.
# ---------------------------------------------------------------------------
FROM base AS runtime

COPY --from=build --chown=motet:motet /app /app

# ---------------------------------------------------------------------------
# api — the HTTP service.
# ---------------------------------------------------------------------------
FROM runtime AS api

# Cloud Run sets $PORT and may not use 8080; the default is for `docker run` by hand.
ENV PORT=8080
EXPOSE 8080

# Shell form, so $PORT is expanded at start rather than baked in as a literal.
#
# One worker per container on purpose. Cloud Run scales by adding instances, and each
# instance holds its own Postgres connections — a second in-container worker would
# double the connection count against a db-f1-micro for no extra concurrency that
# `max_instance_request_concurrency` does not already provide.
#
# `--forwarded-allow-ips='*'` is load-bearing rather than lax. uvicorn honours
# `X-Forwarded-Proto` only from `forwarded_allow_ips`, which defaults to `127.0.0.1`; on
# Cloud Run the peer is the front end, never loopback, so the header is discarded and
# `request.base_url` comes back `http://`. The feed builds enclosure URLs from that
# whenever `MOTET_PUBLIC_BASE_URL` is unset, which would put `http://` links inside an RSS
# document a podcast client caches for hours. Trusting the header is safe here precisely
# because Cloud Run is the only route to the container — nothing else can reach it to
# forge one.
CMD exec uvicorn motet_api.main:app --host 0.0.0.0 --port "$PORT" --forwarded-allow-ips='*'

# ---------------------------------------------------------------------------
# voice — the voice service: StartSession over HTTP, the session over a WebSocket.
# ---------------------------------------------------------------------------
FROM base AS voice

COPY --from=voice-build --chown=motet:motet /app/.venv /app/.venv

ENV PORT=8080
EXPOSE 8080

# `create_app --factory` because the module deliberately has no app instance: building one
# reads the environment and builds the arm, which must not happen at import.
#
# WebSockets need nothing extra. uvicorn picks its WebSocket implementation from what is
# importable, and `websockets` is a direct dependency of `motet-voice` (the realtime arm is
# a WebSocket client too). `bin/build-images` opens a real socket against this image,
# because "the handshake answers 404 because no implementation was found" is a warning
# uvicorn prints once at startup and nothing else.
#
# `--forwarded-allow-ips='*'` for the API's reason: Cloud Run's front end is the peer, so
# without it `X-Forwarded-Proto` and the client address are discarded. Nothing in this
# service builds a URL from the request today — the socket URL a browser is handed is
# built by the API from `MOTET_VOICE_BASE_URL` — so this keeps logs and spans honest and
# keeps a future `request.url` from quietly coming back `http://`.
#
# `--timeout-graceful-shutdown 8`, because a socket is a request that does not end on its
# own. Cloud Run sends SIGTERM and kills the instance ten seconds later. uvicorn closes open
# sockets on shutdown but then waits, by default without limit, for their handlers to
# return — and a session's close can be waiting on a vendor socket. A handler that outlived
# the ten seconds would get the instance SIGKILLed before the lifespan's `finally` ran, and
# that `finally` is the telemetry flush. Eight bounds the wait and leaves two for it.
CMD exec uvicorn motet_voice.app:create_app --factory --host 0.0.0.0 --port "$PORT" --forwarded-allow-ips='*' --timeout-graceful-shutdown 8

# ---------------------------------------------------------------------------
# worker — one Cloud Run job invocation drains one queue and exits.
# ---------------------------------------------------------------------------
FROM runtime AS worker

# ENTRYPOINT rather than CMD, so the queue name is the container's argument:
#
#     docker run motet-worker integrate
#
# which is exactly the shape a Cloud Run job's `args` takes. The runner validates the
# name against the Queue enum and refuses anything else, so a typo is a failed job
# rather than a silently idle one.
#
# `motet_workers.runner` holds the CLI and NOTHING the package imports — the drain loop
# lives in `motet_workers.loop`. `python -m` executes this module, so a module the
# package has already imported would be executed a second time under a second name, with
# a second copy of its module-level state; runpy warns about exactly that, and it shipped
# here (motet#21). `workers/tests/test_entrypoint.py` reads this line and runs it, so
# changing the module below without moving the loop out of it fails CI.
ENTRYPOINT ["python", "-m", "motet_workers.runner"]

# ---------------------------------------------------------------------------
# enrich-toolchain — Node, the agent, and a Chromium. The one heavy stage here.
# ---------------------------------------------------------------------------
#
# From Playwright's own image rather than apt-get'ing a browser: Chromium's shared-library
# list on Debian is long, changes between Playwright releases, and a missing one shows up
# as a browser that will not launch at run time rather than as a build failure. The tag is
# pinned to the Playwright version `enrich/harness/package-lock.json` resolves — 1.63.0 —
# because the browser build and the driver have to match: a mismatch is `Executable doesn't
# exist at /ms-playwright/...` on the first fetch and nowhere earlier.
# `enrich/tests/test_toolchain_pin.py` reads both files and fails when they drift.
FROM mcr.microsoft.com/playwright:v1.63.0-noble AS enrich-toolchain

WORKDIR /opt/motet-enrich

# `npm ci` against a committed lockfile, never `npm install`. This is third-party code that
# drives a browser over untrusted pages inside a container that holds the owner's session
# cookies; the whole tree is pinned, and a bump is a PR with a diff somebody reads.
COPY enrich/harness/package.json enrich/harness/package-lock.json ./
RUN npm ci --omit=dev --no-audit --no-fund

COPY enrich/harness/browser-mcp.mjs harness/browser-mcp.mjs

# ---------------------------------------------------------------------------
# enrich — the agentic enrichment service (motet#102, design option D2).
# ---------------------------------------------------------------------------
#
# NOT built on `base`: it needs a Chromium and the hundred-odd shared libraries one needs,
# and Playwright's image is where those are known to be right. So the venv and the Python
# interpreter are copied *in* rather than the browser being installed into a slim image.
#
# **This image deliberately carries no database driver** — see `enrich-build` — so there is
# nothing in it that could be handed a credential even if one were mounted.
FROM mcr.microsoft.com/playwright:v1.63.0-noble AS enrich

# The interpreter the venv was built against, byte-for-byte. Copying the venv without it
# leaves every shebang pointing at a Python that is not there.
COPY --from=enrich-build /usr/local/lib/ /usr/local/lib/
COPY --from=enrich-build /usr/local/bin/python3.13 /usr/local/bin/python3.13
RUN ln -sf /usr/local/bin/python3.13 /usr/local/bin/python3 \
 && ln -sf /usr/local/bin/python3.13 /usr/local/bin/python \
 && ldconfig

# Not root, for the reason `base` gives — and here it matters more than anywhere else in
# this repo, because this container runs a browser over pages nobody at Motet wrote. The
# browsers under /ms-playwright are world-readable in the base image, so a user of our own
# rather than its `pwuser` costs nothing.
#
# Strict, with no `|| true`: the Playwright image ships `pwuser` at uid 1001 and leaves
# 10001 free, and the day a base image bump takes that uid this must be a failed build
# rather than a `USER motet` that cannot be resolved at run time — which Docker reports as
# "unable to find user" on every start, long after the change that caused it.
RUN useradd --create-home --uid 10001 motet

WORKDIR /app

COPY --from=enrich-build --chown=motet:motet /app/.venv /app/.venv
COPY --from=enrich-toolchain --chown=motet:motet /opt/motet-enrich /opt/motet-enrich

ENV PATH="/app/.venv/bin:/opt/motet-enrich/node_modules/.bin:$PATH" \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    # Where `motet_enrich.config.resolve_toolchain` looks, and where Playwright put the
    # browsers in its own image. Both are reported by /internal/health as `toolchain_ready`,
    # which is the field that makes "the npm half is missing" visible from outside.
    MOTET_ENRICH_TOOLCHAIN_DIR=/opt/motet-enrich \
    PLAYWRIGHT_BROWSERS_PATH=/ms-playwright \
    PORT=8080

USER motet
EXPOSE 8080

# `create_app --factory` for `motet-voice`'s reason: building the app reads the environment
# and resolves the toolchain, which must not happen at import.
#
# `--forwarded-allow-ips='*'` because Cloud Run's front end is the peer, never loopback.
#
# One worker, and the service's own concurrency is 1: a run holds a Chromium for up to ten
# minutes, and two of them in one container is two browsers in 2 GiB.
#
# `--timeout-graceful-shutdown 8`: Cloud Run sends SIGTERM and kills ten seconds later, and
# the lifespan's `finally` is the telemetry flush. Eight bounds the wait and leaves two for
# it — the same figure `motet-voice` uses, for the same reason.
CMD exec uvicorn motet_enrich.app:create_app --factory --host 0.0.0.0 --port "$PORT" --forwarded-allow-ips='*' --timeout-graceful-shutdown 8
