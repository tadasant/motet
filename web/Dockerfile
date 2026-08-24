# The SPA: a Vite build, served as static files by nginx.
#
# Build context is the REPO ROOT, not `web/` — `npm run generate:client` reads
# `../openapi.yaml`, and the contract check in bin/ci means the committed client is
# already in sync with it, so the build itself only needs `web/`. The root context is
# what keeps bin/build-images able to build all three images with one context.
#
#     docker build -f web/Dockerfile -t motet-web .
#
# NO SECRETS EVER REACH THIS IMAGE, at build time or at run time. Everything it holds it
# hands to a browser, so anything secret in here is public by construction. The service
# definition (private infra repo) gives it no secret env and a service account holding
# nothing; this file must not become the exception.

# ---------------------------------------------------------------------------
# Build the bundle.
# ---------------------------------------------------------------------------
FROM node:22-bookworm-slim AS build

WORKDIR /app/web

# Manifest first, so `npm ci` is cached across ordinary source edits.
COPY web/package.json web/package-lock.json ./

# `npm ci` rather than `npm install`: it installs exactly the lockfile and fails if the
# manifest disagrees, which is the same guarantee `uv sync --frozen` gives the Python
# image. An image built from a different dependency set than CI tested is worth failing
# the build over.
RUN npm ci

COPY web/ ./

# NOT `VITE_API_BASE_URL`. The API origin is deliberately absent from the bundle — it is
# written at container start-up into /config.js instead, so one image serves staging and
# production. See web/src/api/client.ts for why.
RUN npm run build

# ---------------------------------------------------------------------------
# Serve it.
# ---------------------------------------------------------------------------
FROM nginx:1.27-alpine AS runtime

# The image's own default server block listens on 80 and would shadow ours in
# /etc/nginx/conf.d. Removing it leaves exactly one server block to reason about.
RUN rm -f /etc/nginx/conf.d/default.conf

# Rendered through envsubst by the base image's 20-envsubst-on-templates.sh, which is
# how ${PORT} becomes the port Cloud Run asked for. It substitutes only names that are
# actually set in the environment, so nginx's own `$uri` and `$host` survive untouched.
COPY web/nginx.conf /etc/nginx/templates/motet.conf.template

# Runs before nginx starts and writes /config.js from MOTET_API_BASE_URL.
COPY web/docker-entrypoint.d/10-motet-config.sh /docker-entrypoint.d/10-motet-config.sh
RUN chmod +x /docker-entrypoint.d/10-motet-config.sh

COPY --from=build /app/web/dist /usr/share/nginx/html

# The default for `docker run` by hand; Cloud Run overrides it with its own $PORT.
ENV PORT=8080
EXPOSE 8080
