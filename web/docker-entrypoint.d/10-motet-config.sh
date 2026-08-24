#!/bin/sh
#
# Write the SPA's runtime configuration from the environment, before nginx starts.
#
# The official nginx image runs every executable `*.sh` under /docker-entrypoint.d/ on
# start-up, so this needs no custom ENTRYPOINT and keeps nginx's own signal handling.
#
# WHY THIS EXISTS. `app.` and `api.` are two different hostnames in every deployed
# environment, so the SPA cannot use same-origin paths. Vite inlines `import.meta.env`
# at build time, so baking the API origin into the bundle would mean one image per
# environment — and would ignore the MOTET_API_BASE_URL the service definition already
# sets. Writing it here means one image, configured where it runs.
#
# An unset MOTET_API_BASE_URL leaves the empty default in place, which means same-origin.
# That is right for `docker run` by hand and wrong in a deployment, so it is logged
# rather than left silent: a SPA that quietly asks its own static-file server for /v1
# gets index.html back and reports a JSON parse error, which points nowhere near here.
set -eu

CONFIG_FILE=/usr/share/nginx/html/config.js
BASE_URL="${MOTET_API_BASE_URL:-}"

if [ -z "$BASE_URL" ]; then
  echo "motet-web: MOTET_API_BASE_URL is unset; the SPA will use same-origin paths." >&2
  echo "motet-web: that is correct only if something else proxies /v1 to the API." >&2
fi

# Strip trailing slashes: the client joins this with paths that already begin with one,
# and `https://host//v1/...` is a different path to some routers.
BASE_URL="$(printf '%s' "$BASE_URL" | sed 's:/*$::')"

# The value goes into a single-quoted JS string, so refuse anything that could leave that
# string — or the file — malformed. A hostname has no business containing any of this, so
# a value that does is a misconfiguration rather than input to escape cleverly.
#
# The printable-ASCII check is the one that matters most, and it is not about injection: a
# value containing a newline would sail past a quote-and-backslash check, `sed` is
# line-oriented so only its last line would be stripped, and the result is an unterminated
# string literal. `config.js` then fails to parse, `__MOTET_CONFIG__` is never defined, and
# the SPA silently falls back to same-origin — asking nginx for /v1, getting index.html,
# and reporting a JSON parse error. Exactly the failure this script exists to prevent.
case "$BASE_URL" in
  *[!\ -~]*)
    echo "motet-web: refusing MOTET_API_BASE_URL with a newline or non-printable character." >&2
    exit 1
    ;;
  *\'*|*\\*|*'<'*)
    echo "motet-web: refusing MOTET_API_BASE_URL containing a quote, backslash or '<'." >&2
    exit 1
    ;;
esac

# Shape check. Anything that is not an absolute http(s) URL would be joined onto paths
# that already begin with a slash and produce nonsense requests.
case "$BASE_URL" in
  ''|http://*|https://*) ;;
  *)
    echo "motet-web: MOTET_API_BASE_URL must be an absolute http:// or https:// URL." >&2
    exit 1
    ;;
esac

printf "window.__MOTET_CONFIG__ = { apiBaseUrl: '%s' };\n" "$BASE_URL" > "$CONFIG_FILE"
echo "motet-web: serving with apiBaseUrl='${BASE_URL}'"
