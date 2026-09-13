#!/usr/bin/env bash
# Run one agentic enrichment: Pi + pi-mcp-adapter driving playwright-stealth-mcp-server and the
# owner's email MCP server. Usage: ./run.sh <article-url> [extra pi args...]
# Writes runs/<timestamp>/{transcript.jsonl,stderr.txt,article.md,storage-state.json,summary.txt}
set -euo pipefail
cd "$(dirname "$0")"
ARTICLE_URL="${1:?article url}"; shift || true
STATE_FILE=""
if [ "${1:-}" = "--state" ]; then STATE_FILE="$2"; shift 2; fi
set -a; . ../../.env; set +a
: "${OPENROUTER_API_KEY:?}" "${PROTO_THEINFORMATION_USERNAME:?}" "${PROTO_EMAIL_MCP_URL:?}"
export PI_CODING_AGENT_DIR="$PWD/.pi-home"
RUN="runs/$(date +%Y%m%d-%H%M%S)"; mkdir -p "$RUN"
MODEL="${PI_MODEL:-anthropic/claude-sonnet-5}"
PROMPT="ARTICLE_URL: ${ARTICLE_URL}
LOGIN_EMAIL: ${PROTO_THEINFORMATION_USERNAME}"
if [ -n "$STATE_FILE" ]; then PROMPT="$PROMPT
STORAGE_STATE_JSON: $(cat "$STATE_FILE")"; fi
PROMPT="$PROMPT
Go."
START=$(date +%s)
set +e
npx pi -p --mode json -e ./node_modules/pi-mcp-adapter --no-skills --no-context-files \
  --session "$PWD/$RUN/pi-session.jsonl" --no-builtin-tools \
  --provider openrouter --model "$MODEL" --thinking "${PI_THINKING:-low}" \
  --system-prompt "$(cat system-prompt.md)" "$@" "$PROMPT" \
  </dev/null >"$RUN/transcript.jsonl" 2>"$RUN/stderr.txt"
RC=$?
set -e
END=$(date +%s)
python3 - "$RUN" "$((END-START))" "$RC" <<'PY'
import json, re, sys, pathlib
run, secs, rc = pathlib.Path(sys.argv[1]), int(sys.argv[2]), sys.argv[3]
final = ""
for line in open(run / "transcript.jsonl"):
    try: e = json.loads(line)
    except Exception: continue
    if e.get("type") == "message_end" and e["message"].get("role") == "assistant":
        for c in e["message"].get("content", []):
            if c.get("type") == "text" and c["text"].strip(): final = c["text"]
def fence(tag):
    m = re.search(r"```" + tag + r"\n(.*?)```", final, re.S)
    return m.group(1).strip() if m else ""
art, state = fence("ARTICLE_MARKDOWN"), fence("STORAGE_STATE_JSON")
if art: (run / "article.md").write_text(art + "\n")
if state:
    try: json.loads(state); (run / "storage-state.json").write_text(state + "\n")
    except Exception: (run / "storage-state.invalid.txt").write_text(state)
head = "\n".join(l for l in final.splitlines() if l.startswith(("STATUS:", "LOGGED_IN:", "NOTES:")))
(run / "summary.txt").write_text(f"exit={rc} wall_seconds={secs}\n{head}\narticle_chars={len(art)} storage_state_chars={len(state)}\n")
print((run / "summary.txt").read_text())
PY
python3 summarize.py "$RUN/transcript.jsonl" 400 | tail -3
echo "run dir: $RUN"
