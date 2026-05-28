// Probe one URL and write artifacts the role-ui-triage prompt expects
// at the paths the schema documents.
//
// Per ADR-0061's prompt at docs/triage/role-ui-triage.v1.md § "Tooling",
// every per-finding directory must carry:
//   - screen.png             full-page PNG at 1440×900
//   - console.log            console events, one per line
//   - network.log            failed requests + 4xx/5xx responses
//   - dom.html               rendered HTML snapshot
//   - evidence_summary.json  { console_errors, http_4xx, http_5xx, requestfailed }
//
// Counter shape MUST match the `evidence_summary` JSONB in
// services/api/treadmill_api/models/triage_finding.py (ADR-0061
// Step 1) so the schema's typed Pydantic round-trip succeeds when
// the role POSTs findings.
//
// Usage:
//   node probe.mjs <url> <out-dir> [waitMs]

import { chromium } from 'playwright';
import { mkdir, writeFile } from 'node:fs/promises';

const url = process.argv[2] || 'http://localhost:5174/';
const out = process.argv[3] || '/tmp/probe-out';
const waitMs = Number(process.argv[4] ?? 3000);

await mkdir(out, { recursive: true });

const browser = await chromium.launch();
const ctx = await browser.newContext({ viewport: { width: 1440, height: 900 } });
const page = await ctx.newPage();

const consoleLines = [];
const networkLines = [];
const counters = {
  console_errors: 0,
  http_4xx: 0,
  http_5xx: 0,
  requestfailed: 0,
};

page.on('console', (m) => {
  const type = m.type();
  consoleLines.push(`[${type}] ${m.text()}`);
  if (type === 'error') counters.console_errors += 1;
});
page.on('pageerror', (e) => {
  consoleLines.push(`[pageerror] ${String(e?.stack || e)}`);
  counters.console_errors += 1;
});
page.on('requestfailed', (r) => {
  networkLines.push(`[requestfailed] ${r.method()} ${r.url()} :: ${r.failure()?.errorText}`);
  counters.requestfailed += 1;
});
page.on('response', (r) => {
  const s = r.status();
  if (s >= 400 && s < 500) {
    networkLines.push(`[http.${s}] ${r.request().method()} ${r.url()}`);
    counters.http_4xx += 1;
  } else if (s >= 500) {
    networkLines.push(`[http.${s}] ${r.request().method()} ${r.url()}`);
    counters.http_5xx += 1;
  }
});

const t0 = Date.now();
try {
  await page.goto(url, { waitUntil: 'networkidle', timeout: 15000 });
} catch (e) {
  consoleLines.push(`[navigation] ${String(e?.message || e)}`);
}
await page.waitForTimeout(waitMs);

const title = await page.title().catch(() => '<error>');
const dom = await page.content();
await page.screenshot({ path: `${out}/screen.png`, fullPage: true });

await writeFile(`${out}/console.log`, consoleLines.join('\n') + '\n');
await writeFile(`${out}/network.log`, networkLines.join('\n') + '\n');
await writeFile(`${out}/dom.html`, dom);
await writeFile(`${out}/evidence_summary.json`, JSON.stringify(counters, null, 2));
await writeFile(
  `${out}/meta.txt`,
  `url=${url}\ntitle=${title}\nelapsed_ms=${Date.now() - t0}\nviewport=1440x900\n`,
);

console.log(
  `probe done: ${consoleLines.length} console + ${networkLines.length} network events; counters=${JSON.stringify(counters)}`,
);
await browser.close();
