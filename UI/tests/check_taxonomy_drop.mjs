/* Regression test for the defect this rewrite existed to kill: a category
   present in the data but absent from the UI's order array used to vanish
   silently. Inject one and assert it now renders AND warns. */
import { JSDOM } from 'jsdom';
import { readFileSync } from 'fs';
const ROOT = new URL('..', import.meta.url).pathname.replace(/\/$/, '');   // UI/ folder, wherever the repo lives
const html = readFileSync(`${ROOT}/index.html`, 'utf8');
const metrics = JSON.parse(readFileSync(`${ROOT}/data/metrics.json`, 'utf8'));

// clone one real risk row into an undeclared category, on every day it had
const rmd = metrics.tables.risk_metrics_daily;
const idx = rmd.columns.indexOf('risk_category');
const n = rmd.rows.length;
for (let i = 0; i < n; i++) {
  if (rmd.rows[i][idx] === 'sanctions') {
    const clone = rmd.rows[i].slice();
    clone[idx] = 'space_domain_contestation';   // undeclared on purpose
    rmd.rows.push(clone);
  }
}
console.log(`  injected ${rmd.rows.length - n} rows for undeclared category "space_domain_contestation"`);

const warnings = [];
const dom = new JSDOM(html, { runScripts: 'dangerously', url: 'http://localhost/',
  beforeParse(win) {
    win.fetch = (u) => Promise.resolve(String(u).includes('metrics.json')
      ? { ok: true, json: () => Promise.resolve(metrics) }
      : { ok: true, json: () => Promise.resolve({}) });
    win.console.warn = (...a) => warnings.push(a.join(' '));
    win.requestAnimationFrame = (cb) => setTimeout(() => cb(0), 0);
    Object.defineProperty(win.HTMLElement.prototype, 'clientWidth', { get: () => 900 });
  }});
const s = dom.window.document.createElement('script');
s.textContent = readFileSync(`${ROOT}/assets/dash.js`, 'utf8');
dom.window.document.body.appendChild(s);
await new Promise(r => setTimeout(r, 3000));

const doc = dom.window.document;
const TAX = dom.window.__TAX__;
const txt = doc.body.textContent;
const inStrip = [...doc.querySelectorAll('.status-strip .sr-name')].map(e => e.textContent.trim());
const flagged = Object.keys(TAX.unknown().risk || {});
const warned = warnings.filter(w => w.includes('space_domain_contestation'));

const checks = [
  ['undeclared category is rendered, not dropped', txt.includes('space_domain_contestation')],
  ['it appears in the risk strip',                 inStrip.includes('space_domain_contestation')],
  ['it is recorded in TAX.unknown()',              flagged.includes('space_domain_contestation')],
  ['a console warning names it and the fix',       warned.length === 1 && warned[0].includes('taxonomy.py')],
  ['declared categories keep their order first',   inStrip.indexOf('space_domain_contestation') === inStrip.length - 1],
  ['it warns exactly once, not per render',        warned.length === 1],
];
checks.forEach(([n, ok]) => console.log(`  ${ok ? 'PASS' : 'FAIL'}  ${n}`));
console.log(`\n  ${checks.filter(c => c[1]).length}/${checks.length} checks passed`);
process.exit(checks.every(c => c[1]) ? 0 : 1);
