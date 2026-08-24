# The Python half of Motet: one build, two runtime targets.
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
COPY inference inference
COPY obs obs
COPY sources sources
COPY storage storage
COPY vault vault
COPY voice voice
COPY workers workers

RUN uv sync --frozen --no-dev

# ---------------------------------------------------------------------------
# Runtime — the venv and the source, and nothing that built them.
# ---------------------------------------------------------------------------
FROM python:3.13-slim-bookworm AS runtime

# Not root. Cloud Run does not require it, but nothing in either process needs to write
# outside its own temp dir, and a container that cannot modify its own code is one less
# thing to think about if a dependency is ever compromised.
RUN useradd --create-home --uid 10001 motet

WORKDIR /app

COPY --from=build --chown=motet:motet /app /app

ENV PATH="/app/.venv/bin:$PATH" \
    # Logs must reach the log collector as they are written. Without this, Python buffers
    # stdout when it is a pipe — which it always is here — and a container that dies
    # takes its last and most interesting lines with it.
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

USER motet

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
# lives in `motet_workers.drain`. `python -m` executes this module, so a module the
# package has already imported would be executed a second time under a second name, with
# a second copy of its module-level state; runpy warns about exactly that, and it shipped
# here (motet#21). `workers/tests/test_entrypoint.py` reads this line and runs it, so
# changing the module below without moving the loop out of it fails CI.
ENTRYPOINT ["python", "-m", "motet_workers.runner"]
