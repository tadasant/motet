import { chromium } from "playwright";
const b = await chromium.launch({ headless: true });
const p = await b.newPage();
const r = await p.goto(process.argv[2], { waitUntil: "domcontentloaded", timeout: 30000 }).catch(e => null);
console.log(p.url());
console.log(await p.title());
await b.close();
