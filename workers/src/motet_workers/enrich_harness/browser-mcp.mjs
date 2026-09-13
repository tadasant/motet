#!/usr/bin/env node
// PROTOTYPE — the browser MCP server the enrichment agent drives, with the storage state
// handled by the harness rather than the model.
//
// This is `playwright-stealth-mcp-server` (the same server as `.mcp.json`, stealth mode,
// headless) with one subclass of its PlaywrightClient: the browser context is seeded from
// `MOTET_STORAGE_STATE_IN` (a Playwright `storageState` JSON file) when it is first created,
// and `page.context().storageState()` is written to `MOTET_STORAGE_STATE_OUT` after every
// `browser_execute` call. The spike showed the model echoing 10 KB of cookies through its
// output when it was asked to import and export them itself — most of a run's cost and most
// of its wall time — so neither is the model's job any more, and the cookies never appear in
// the transcript.
//
// The packages are resolved from `MOTET_ENRICH_TOOLCHAIN_DIR` (the directory holding the
// `node_modules` the spike installed), so this file can live in the worker package.

import { existsSync, readFileSync, writeFileSync } from 'node:fs';
import { createRequire } from 'node:module';
import { join } from 'node:path';
import { pathToFileURL } from 'node:url';

const toolchain = process.env.MOTET_ENRICH_TOOLCHAIN_DIR;
if (!toolchain) {
  console.error('browser-mcp: MOTET_ENRICH_TOOLCHAIN_DIR is not set');
  process.exit(2);
}
const require = createRequire(join(toolchain, 'package.json'));
const resolveUrl = (id) => pathToFileURL(require.resolve(id)).href;

const serverPackage = 'playwright-stealth-mcp-server';
const packageRoot = join(require.resolve(`${serverPackage}/package.json`), '..');
// `shared/index.js` re-exports createMCPServer but not the client class; both come from
// `shared/server.js` directly.
const { createMCPServer, PlaywrightClient } = await import(
  pathToFileURL(join(packageRoot, 'shared', 'server.js')).href
);
const { logInfo, logError } = await import(
  pathToFileURL(join(packageRoot, 'shared', 'logging.js')).href
);
// The server's own copy of the SDK if it has one, else the toolchain's — the transport has
// to come from the same module instance as the Server class or the connect() types disagree.
const sdkStdio = (() => {
  const nested = join(packageRoot, 'node_modules', '@modelcontextprotocol', 'sdk', 'dist', 'esm', 'server', 'stdio.js');
  return existsSync(nested) ? pathToFileURL(nested).href : resolveUrl('@modelcontextprotocol/sdk/server/stdio.js');
})();
const { StdioServerTransport } = await import(sdkStdio);

const stateIn = process.env.MOTET_STORAGE_STATE_IN;
const stateOut = process.env.MOTET_STORAGE_STATE_OUT;

class StatefulPlaywrightClient extends PlaywrightClient {
  async ensureBrowser(options) {
    if (!this.page && stateIn && existsSync(stateIn)) {
      try {
        const storageState = JSON.parse(readFileSync(stateIn, 'utf-8'));
        options = { ...(options ?? {}), storageState };
        logInfo('browser-mcp', `seeded ${storageState.cookies?.length ?? 0} cookies`);
      } catch (error) {
        logError('browser-mcp', `could not read the saved storage state: ${error}`);
      }
    }
    return super.ensureBrowser(options);
  }

  async execute(code, options) {
    const result = await super.execute(code, options);
    await this.persist();
    return result;
  }

  async persist() {
    if (!stateOut || !this.context) return;
    try {
      const state = await this.context.storageState();
      writeFileSync(stateOut, JSON.stringify(state), { mode: 0o600 });
    } catch (error) {
      logError('browser-mcp', `could not persist the storage state: ${error}`);
    }
  }
}

const version = JSON.parse(readFileSync(join(packageRoot, 'package.json'), 'utf-8')).version;
const { server, registerHandlers, cleanup } = createMCPServer({
  version: `${version}+motet`,
  ignoreHttpsErrors: process.env.IGNORE_HTTPS_ERRORS !== 'false',
});

let activeClient = null;
await registerHandlers(server, () => {
  const headless = process.env.HEADLESS !== 'false';
  activeClient = new StatefulPlaywrightClient({
    stealthMode: process.env.STEALTH_MODE === 'true',
    headless,
    timeout: parseInt(process.env.TIMEOUT || '30000', 10),
    navigationTimeout: parseInt(process.env.NAVIGATION_TIMEOUT || '60000', 10),
    stealthUserAgent: process.env.STEALTH_USER_AGENT,
    stealthMaskLinux: process.env.STEALTH_MASK_LINUX === undefined ? undefined : process.env.STEALTH_MASK_LINUX !== 'false',
    stealthLocale: process.env.STEALTH_LOCALE,
    ignoreHttpsErrors: process.env.IGNORE_HTTPS_ERRORS !== 'false',
  });
  return activeClient;
});

const shutdown = async () => {
  try {
    if (activeClient) await activeClient.persist();
    await cleanup();
    if (activeClient) await activeClient.close();
  } catch {
    // exiting anyway
  }
  process.exit(0);
};
process.on('SIGINT', shutdown);
process.on('SIGTERM', shutdown);
process.stdin.on('close', shutdown);

const transport = new StdioServerTransport();
await server.connect(transport);
