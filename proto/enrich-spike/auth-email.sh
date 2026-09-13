#!/usr/bin/env bash
# One-time, human-owned step (invariant 9): obtain an OAuth token for the email MCP server.
# Pi runs the adapter's auth-start (which opens the system browser on the authorization URL and
# arms a loopback callback), then polls `connect` until the human has consented or the wait runs out.
# The token lands in the macOS login Keychain: service `pi-mcp-adapter.oauth`, account `email`.
set -euo pipefail
cd "$(dirname "$0")"
set -a; . ../../.env; set +a
export PI_CODING_AGENT_DIR="$PWD/.pi-home"
mkdir -p runs
OUT="runs/auth-$(date +%Y%m%d-%H%M%S).jsonl"
WAIT_SECONDS="${1:-240}"
PROMPT="You are performing a one-time OAuth setup. Steps, exactly:
1. Call mcp({action:\"auth-start\", server:\"email\"}). Copy the authorization URL from the result into a line of the form AUTH_URL: <url> in your reply text (this is the only place it should appear).
2. Then loop for at most ${WAIT_SECONDS} seconds: run bash 'sleep 15', then call mcp({connect:\"email\"}). Stop looping as soon as the connect result reports the server connected (it will list tools).
3. Finish with one line: AUTH_RESULT: connected  or  AUTH_RESULT: not-connected, followed by a one-sentence reason. Do not call any other tool."
npx pi -p --mode json -e ./node_modules/pi-mcp-adapter --no-skills --no-context-files --no-session \
  --tools bash,mcp "$PROMPT" </dev/null 2>"${OUT%.jsonl}.stderr" | tee "$OUT" | python3 summarize.py - 600
echo "transcript: $OUT"
