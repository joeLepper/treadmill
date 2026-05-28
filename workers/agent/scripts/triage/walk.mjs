// Walk the page: viewport-sized screenshots at each scroll position,
// plus the full body text. Surfaces both layout density AND the items
// below the fold.
import { chromium } from 'playwright';
import { mkdir, writeFile } from 'node:fs/promises';

const url = process.argv[2] || 'http://localhost:5174/';
const out = process.argv[3] || '/tmp/walk-out';
const vw = Number(process.argv[4] ?? 1440);
const vh = Number(process.argv[5] ?? 900);

await mkdir(out, { recursive: true });

const browser = await chromium.launch();
const ctx = await browser.newContext({ viewport: { width: vw, height: vh } });
const page = await ctx.newPage();
const log = [];
page.on('console', (m) => log.push(`[${m.type()}] ${m.text()}`));
page.on('pageerror', (e) => log.push(`[pageerror] ${e}`));
page.on('requestfailed', (r) => log.push(`[reqfail] ${r.method()} ${r.url()} ${r.failure()?.errorText}`));

await page.goto(url, { waitUntil: 'networkidle', timeout: 15000 });
await page.waitForTimeout(2500);

const scrollHeight = await page.evaluate(() => document.documentElement.scrollHeight);
const text = await page.evaluate(() => document.body.innerText);
await writeFile(`${out}/body.txt`, text);
await writeFile(`${out}/log.txt`, log.join('\n') + '\n');
await writeFile(`${out}/meta.txt`, `url=${url}\nviewport=${vw}x${vh}\nscrollHeight=${scrollHeight}\n`);

let frame = 0;
for (let y = 0; y < scrollHeight; y += vh) {
  await page.evaluate((y) => window.scrollTo(0, y), y);
  await page.waitForTimeout(250);
  await page.screenshot({ path: `${out}/scroll-${String(frame).padStart(2, '0')}-y${y}.png`, fullPage: false });
  frame++;
  if (frame > 8) break; // cap
}

console.log(`walk done: ${frame} frames, scrollHeight=${scrollHeight}px, ${log.length} console events`);
await browser.close();
