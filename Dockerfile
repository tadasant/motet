# The Python half of Motet: one build, two runtime targets.
#
# `motet-api` and `motet-worker` are separate images in Artifact Registry because the
# infrastructure pins them separately, but they are the same tree — the API writes rows
# and enqueues jobs, the worker drains the queues, and both import the same five
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
COPY pyproject.toml uv.lock ./
COPY api/pyproject.toml api/pyproject.toml
COPY db/pyproject.toml db/pyproject.toml
COPY inference/pyproject.toml inference/pyproject.toml
COPY storage/pyproject.toml storage/pyproject.toml
COPY workers/pyproject.toml workers/pyproject.toml

# `--frozen` is the point of this line: it fails rather than silently re-resolving when
# uv.lock does not match the pyproject files. An image built from a quietly different
# dependency set than CI tested is the failure this flag exists to prevent.
RUN uv sync --frozen --no-dev --no-install-workspace

COPY api api
COPY db db
COPY inference inference
COPY storage storage
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
CMD exec uvicorn motet_api.main:app --host 0.0.0.0 --port "$PORT"

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
ENTRYPOINT ["python", "-m", "motet_workers.runner"]
