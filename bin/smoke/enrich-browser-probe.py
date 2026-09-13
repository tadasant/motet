"""Drive the enrichment image's browser harness over stdio, inside the container.

`bin/build-images enrich` copies this in and runs it with the image's own interpreter. It
is a real MCP client — initialize, `tools/list`, two `browser_execute` calls — because the
three claims it makes are claims about a *running* Chromium and nothing short of one can
make them:

1. **The stealth browser launches at all.** `toolchain_ready` on /internal/health says the
   files are present; only this says the hundred-odd shared libraries a Chromium needs are
   there too, which is the whole reason the image is built on Playwright's own base.
2. **The navigation lock works** (design option G3, motet#102). A top-level navigation to a
   host the run was not given comes back `net::ERR_BLOCKED_BY_CLIENT`. That is the control
   standing between a page that says "continue reading at elsewhere.example" and an agent
   that goes there — and it is unreachable from a unit test, which can only assert the
   predicate. `enrich/harness/browser-mcp.test.mjs` asserts the predicate; this asserts the
   browser honours it.
3. **The storage state is written by the harness after every call**, so a run that logs in
   and then runs out of clock still leaves the cookies that login bought.

No network is touched: the page is `setContent`, and the blocked navigation is refused
before a connection is made.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile

ROOT = os.environ.get("MOTET_ENRICH_TOOLCHAIN_DIR", "/opt/motet-enrich")
ALLOWED = "example.com"
FORBIDDEN = "https://elsewhere.example/steal"


def main() -> int:
    workdir = tempfile.mkdtemp()
    # Not `mktemp`: the harness creates this file itself, and a name handed out by a
    # deprecated function that also reserves nothing is a race for no gain.
    state_out = os.path.join(workdir, "state.json")
    env = {
        "PATH": os.environ["PATH"],
        "HOME": workdir,
        "NODE_PATH": f"{ROOT}/node_modules",
        "STEALTH_MODE": "true",
        "HEADLESS": "true",
        "IGNORE_HTTPS_ERRORS": "false",
        "TIMEOUT": "30000",
        "MOTET_ALLOWED_HOSTS": ALLOWED,
        "MOTET_STORAGE_STATE_OUT": state_out,
        "PLAYWRIGHT_BROWSERS_PATH": os.environ.get("PLAYWRIGHT_BROWSERS_PATH", "/ms-playwright"),
    }
    proc = subprocess.Popen(  # noqa: S603 — argv is a literal
        ["node", f"{ROOT}/harness/browser-mcp.mjs"],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        bufsize=1,
        env=env,
    )
    assert proc.stdin is not None and proc.stdout is not None

    def send(message: dict[str, object]) -> None:
        proc.stdin.write(json.dumps(message) + "\n")  # type: ignore[union-attr]
        proc.stdin.flush()  # type: ignore[union-attr]

    def read() -> dict[str, object]:
        line = proc.stdout.readline()  # type: ignore[union-attr]
        if not line.strip():
            raise SystemExit("the browser server closed its stdout without answering")
        answer: dict[str, object] = json.loads(line)
        return answer

    def call(code: str, request_id: int) -> str:
        send(
            {
                "jsonrpc": "2.0",
                "id": request_id,
                "method": "tools/call",
                "params": {"name": "browser_execute", "arguments": {"code": code}},
            }
        )
        return json.dumps(read())

    try:
        send(
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "initialize",
                "params": {
                    "protocolVersion": "2024-11-05",
                    "capabilities": {},
                    "clientInfo": {"name": "motet-smoke", "version": "1"},
                },
            }
        )
        read()
        send({"jsonrpc": "2.0", "method": "notifications/initialized"})
        send({"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}})
        listed = read()
        names = [tool["name"] for tool in listed["result"]["tools"]]  # type: ignore[index]
        if "browser_execute" not in names:
            print(f"smoke: the browser server registers no browser_execute: {names}")
            return 1
        print(f"    tools -> {', '.join(names)}")

        ran = call(
            "await page.setContent('<h1>Motet smoke</h1>'); return await page.textContent('h1');",
            3,
        )
        if "Motet smoke" not in ran:
            print(f"smoke: the stealth browser did not run a page: {ran[:400]}")
            return 1
        print("    browser_execute -> a real Chromium ran the page")

        blocked = call(
            "try { await page.goto('" + FORBIDDEN + "', {timeout: 8000}); return 'NAVIGATED'; }"
            " catch (error) { return 'REFUSED: ' + error.message.split('\\n')[0]; }",
            4,
        )
        if "ERR_BLOCKED_BY_CLIENT" not in blocked:
            print(f"smoke: the navigation lock did not refuse {FORBIDDEN}: {blocked[:400]}")
            return 1
        print(f"    navigation to {FORBIDDEN} -> refused by the harness (option G3)")

        if not os.path.exists(state_out) or os.path.getsize(state_out) == 0:
            print("smoke: the harness wrote no storage state after a browser call")
            return 1
        print(f"    storage state -> written after every call ({os.path.getsize(state_out)} bytes)")
        return 0
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()


if __name__ == "__main__":
    sys.exit(main())
