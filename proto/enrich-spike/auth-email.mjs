// One-time, human-owned OAuth bootstrap for the `email` MCP server — no model in the loop.
// Uses pi-mcp-adapter's own OAuth flow (DCR + PKCE + loopback callback), so the token lands
// exactly where Pi will look for it: macOS Keychain, service `pi-mcp-adapter.oauth`, account `email`.
// Usage: node auth-email.mjs [timeout-seconds]   (needs PROTO_EMAIL_MCP_URL in env)
import { authenticate, getAuthStatus } from "./node_modules/pi-mcp-adapter/mcp-auth-flow.ts";
import { writeFileSync } from "node:fs";
const url = process.env.PROTO_EMAIL_MCP_URL;
if (!url) { console.error("PROTO_EMAIL_MCP_URL unset"); process.exit(2); }
const timeout = Number(process.argv[2] ?? 600) * 1000;
const definition = { url, auth: "oauth", oauth: { clientName: "motet-enrich-spike (pi)" } };
console.log("status before:", await getAuthStatus("email"));
const ac = new AbortController();
const t = setTimeout(() => ac.abort(new Error("timed out waiting for consent")), timeout);
try {
  const status = await authenticate("email", url, definition, {
    signal: ac.signal,
    onAuthorizationUrl: (u) => {
      writeFileSync("runs/auth-url.txt", u + "\n");
      console.log("authorization URL written to runs/auth-url.txt; the system browser is being opened on it.");
      console.log("→ HUMAN: sign in with the owner's Google account and approve. Waiting up to", timeout / 1000, "s …");
    },
  });
  console.log("status after:", status);
} catch (e) {
  console.error("auth failed:", e?.message ?? e);
  process.exitCode = 1;
} finally { clearTimeout(t); }
process.exit();
