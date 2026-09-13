import { Client } from "@modelcontextprotocol/sdk/client/index.js";
import { StdioClientTransport } from "@modelcontextprotocol/sdk/client/stdio.js";
const url = process.argv[2];
const t = new StdioClientTransport({ command: "npx", args: ["-y", "playwright-stealth-mcp-server"], env: { ...process.env, STEALTH_MODE: "true", HEADLESS: "true" } });
const c = new Client({ name: "probe", version: "0" }); await c.connect(t);
const r = await c.callTool({ name: "browser_execute", arguments: { timeout: 60000, code: `
  await page.goto(${JSON.stringify(url)}, { waitUntil: 'domcontentloaded', timeout: 45000 });
  await page.waitForTimeout(4000);
  const paras = await page.evaluate(() => Array.from(document.querySelectorAll('article p, main p')).map(p => p.innerText.trim()).filter(t => t.length > 60));
  return { n: paras.length, paras: paras.map(p => p.slice(0, 160)) };` } });
console.log(r.content[0].text.slice(0, 6000));
await c.callTool({ name: "browser_close", arguments: {} }); await c.close(); process.exit(0);
