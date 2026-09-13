// Does the per-recipient `eu=` parameter on TheInformation's newsletter links log the reader in?
import { Client } from "@modelcontextprotocol/sdk/client/index.js";
import { StdioClientTransport } from "@modelcontextprotocol/sdk/client/stdio.js";
const url = process.argv[2];
const t = new StdioClientTransport({ command: "npx", args: ["-y", "playwright-stealth-mcp-server"], env: { ...process.env, STEALTH_MODE: "true", HEADLESS: "true" } });
const c = new Client({ name: "probe", version: "0" }); await c.connect(t);
const r = await c.callTool({ name: "browser_execute", arguments: { timeout: 60000, code: `
  await page.goto(${JSON.stringify(url)}, { waitUntil: 'domcontentloaded', timeout: 45000 });
  await page.waitForTimeout(4000);
  const text = await page.evaluate(() => document.body.innerText);
  const paras = await page.evaluate(() => Array.from(document.querySelectorAll('article p, main p')).map(p => p.innerText.trim()).filter(t => t.length > 60));
  return { finalUrl: page.url(), title: await page.title(), bodyChars: text.length, paragraphs: paras.length, wall: /Subscribe to read|Subscribe to unlock|Sign in/.test(text), signedIn: /Sign out|My account|Account/.test(text), cookies: (await page.context().cookies()).map(c => c.name) };` } });
console.log(r.content[0].text.slice(0, 1500));
await c.callTool({ name: "browser_close", arguments: {} }); await c.close(); process.exit(0);
