#!/usr/bin/env bash
# Called by pi-mcp-adapter's `!command` header hook at connect time. Prints `Bearer <token>`.
set -euo pipefail
cd "$(dirname "$0")/../.."
export GOOGLE_APPLICATION_CREDENTIALS="${GOOGLE_APPLICATION_CREDENTIALS:-$HOME/.config/motet/local-dev.json}"
printf 'Bearer %s' "$(UV_ENV_FILE=.env uv run --quiet python proto/enrich-spike/mcp_token.py "${1:-cn_e1bd093da927}" 2>>proto/enrich-spike/runs/mcp_token.stderr)"
