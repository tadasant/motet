#!/usr/bin/env python3
"""Print a compact, redacted view of a Pi --mode json transcript (stdin or file)."""

import json
import os
import re
import sys

REDACT = [
    (re.compile(r"\b\d{6}\b"), "<6-digit>"),  # login codes
    (re.compile(r"strad_[A-Za-z0-9_-]+"), "strad_<…>"),
    # The email MCP server's origin, from the same variable run.sh reads; never spelled here.
    (
        re.compile(re.escape(os.environ["PROTO_EMAIL_MCP_URL"].split("/mcp")[0]) + r'[^\s"\')]*'),
        "<email-mcp-url>",
    )
    if os.environ.get("PROTO_EMAIL_MCP_URL")
    else (re.compile(r"(?!x)x"), ""),
    (re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[a-z]{2,}"), "<email>"),
    (re.compile(r"(eu|token|code)=([A-Za-z0-9_-]{8,})"), r"\1=<redacted>"),
]


def red(s):
    for rx, rep in REDACT:
        s = rx.sub(rep, s)
    return s


src = open(sys.argv[1]) if len(sys.argv) > 1 and sys.argv[1] != "-" else sys.stdin
limit = int(sys.argv[2]) if len(sys.argv) > 2 else 700
calls = 0
cost = 0.0
tokens = {}
for line in src:
    try:
        e = json.loads(line)
    except Exception:
        continue
    t = e.get("type")
    if t == "tool_execution_start":
        calls += 1
        print(f"CALL #{calls} {e['toolName']} {red(json.dumps(e['args']))[:limit]}")
    elif t == "tool_execution_end":
        r = e.get("result")
        s = json.dumps(r) if not isinstance(r, str) else r
        print(f"  -> {'ERROR' if e.get('isError') else 'ok'} {red(s)[:limit]}")
    elif t == "message_end" and e["message"].get("role") == "assistant":
        m = e["message"]
        for c in m.get("content", []):
            if c.get("type") == "text" and c["text"].strip():
                print("ASSISTANT:", red(c["text"])[:limit])
        u = m.get("usage") or {}
        cost += (u.get("cost") or {}).get("total", 0) or 0
        for k in ("input", "output", "cacheRead", "cacheWrite"):
            tokens[k] = tokens.get(k, 0) + (u.get(k) or 0)
print(f"\nTOOL CALLS: {calls}  COST: ${cost:.4f}  TOKENS: {tokens}")
