// Proves, against the real playwright-stealth-mcp-server over stdio, that storage state can be
// exported from `browser_execute` and re-imported into a fresh browser without any server change.
import { Client } from "@modelcontextprotocol/sdk/client/index.js";
import { StdioClientTransport } from "@modelcontextprotocol/sdk/client/stdio.js";
const site = process.argv[2] ?? "https://www.theinformation.com/";
async function connect() {
  const t = new StdioClientTransport({ command: "npx", args: ["-y", "playwright-stealth-mcp-server"],
    env: { ...process.env, STEALTH_MODE: "true", HEADLESS: "true" } });
  const c = new Client({ name: "probe", version: "0" }); await c.connect(t); return c;
}
const run = async (c, code) => JSON.parse((await c.callTool({ name: "browser_execute", arguments: { code } })).content[0].text.replace(/^Result:\n/, ""));
const c1 = await connect();
const tools = (await c1.listTools()).tools.map(t => t.name);
console.log("tools:", tools.length, tools.slice(0, 8).join(","), "…");
const exported = await run(c1, `await page.goto(${JSON.stringify(site)}, {waitUntil:'domcontentloaded'}); await page.waitForTimeout(2000);
  await page.evaluate(() => localStorage.setItem('probe-marker', 'set-in-run-1'));
  const s = await page.context().storageState(); return { cookies: s.cookies.length, origins: s.origins.length, title: await page.title(), state: s };`);
console.log("run 1:", { cookies: exported.cookies, origins: exported.origins, title: exported.title });
await c1.callTool({ name: "browser_close", arguments: {} }); await c1.close();
const c2 = await connect();
const back = await run(c2, `const s = ${JSON.stringify(exported.state)};
  await page.context().addCookies(s.cookies);
  await page.goto(${JSON.stringify(site)}, {waitUntil:'domcontentloaded'});
  for (const o of s.origins) if (o.origin === new URL(page.url()).origin) await page.evaluate(items => items.forEach(i => localStorage.setItem(i.name, i.value)), o.localStorage);
  return { cookiesNow: (await page.context().cookies()).length, marker: await page.evaluate(() => localStorage.getItem('probe-marker')) };`);
console.log("run 2 (fresh browser, state re-imported):", back);
await c2.callTool({ name: "browser_close", arguments: {} }); await c2.close();
process.exit(0);
