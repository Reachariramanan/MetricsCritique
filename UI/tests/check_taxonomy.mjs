/* Headless render of the real dashboard against the real metrics.json.
   Asserts the taxonomy rewrite did not change what the user sees:
     - no panel throws
     - no rendered label is a raw snake_case key
     - TAX.unknown() is empty (nothing in the data is undeclared)
     - the same categories are rendered as before the rewrite
*/
import { JSDOM } from 'jsdom';
import { readFileSync } from 'fs';

const ROOT = new URL('..', import.meta.url).pathname.replace(/\/$/, '');   // UI/ folder, wherever the repo lives
const html = readFileSync(`${ROOT}/index.html`, 'utf8');
const metrics = JSON.parse(readFileSync(`${ROOT}/data/metrics.json`, 'utf8'));

const errors = [];
const warnings = [];

const dom = new JSDOM(html, {
  runScripts: 'dangerously',
  resources: undefined,
  url: 'http://localhost/',
  beforeParse(win) {
    win.fetch = (url) => {
      if (String(url).includes('metrics.json')) {
        return Promise.resolve({ ok: true, json: () => Promise.resolve(metrics) });
      }
      if (String(url).includes('geo_alerts.json')) {
        return Promise.resolve({ ok: true, json: () => Promise.resolve({}) });
      }
      return Promise.resolve({ ok: false, status: 404, json: () => Promise.resolve({}) });
    };
    win.console.error = (...a) => errors.push(a.join(' '));
    win.console.warn = (...a) => warnings.push(a.join(' '));
    win.requestAnimationFrame = (cb) => setTimeout(() => cb(0), 0);
    Object.defineProperty(win.HTMLElement.prototype, 'clientWidth', { get: () => 900 });
    Object.defineProperty(win.HTMLElement.prototype, 'clientHeight', { get: () => 400 });
  }
});
dom.window.addEventListener('error', (e) => errors.push('uncaught: ' + (e.error?.stack || e.message)));

// inject dash.js explicitly (index.html is a shell that loads assets/)
const css = readFileSync(`${ROOT}/assets/dash.css`, 'utf8');
const js = readFileSync(`${ROOT}/assets/dash.js`, 'utf8');
const style = dom.window.document.createElement('style');
style.textContent = css;
dom.window.document.head.appendChild(style);
const script = dom.window.document.createElement('script');
script.textContent = js;
try { dom.window.document.body.appendChild(script); } catch (e) { errors.push('dash.js threw: ' + e.stack); }

await new Promise((r) => setTimeout(r, 3000));

const doc = dom.window.document;
const TAX = dom.window.__TAX__;

function report(name, ok, detail) {
  console.log(`  ${ok ? 'PASS' : 'FAIL'}  ${name}${detail ? '  -- ' + detail : ''}`);
  return ok;
}

const results = [];

// 1. taxonomy actually shipped in the payload
const axes = metrics.taxonomy?.axes || {};
results.push(report('taxonomy present in metrics.json',
  Object.keys(axes).length === 6,
  Object.keys(axes).map(a => `${a}=${axes[a].order.length}`).join(' ')));

// 2. nothing in the rendered data is undeclared
const unknown = TAX ? TAX.unknown() : null;
const unknownFlat = unknown
  ? Object.entries(unknown).flatMap(([a, m]) => Object.keys(m).map(k => `${a}.${k}`))
  : ['__TAX__ not exposed'];
results.push(report('no undeclared category reached the UI',
  unknownFlat.length === 0, unknownFlat.slice(0, 8).join(', ')));

// 3. no raw snake_case key rendered as a label anywhere
const declaredKeys = new Set(Object.values(axes).flatMap(a => a.order));
const rawLeaks = [];
doc.querySelectorAll('.tc-name, .sr-name, .lane-label, text, .dr-title').forEach((el) => {
  const t = (el.textContent || '').trim();
  if (declaredKeys.has(t)) rawLeaks.push(t);
});
results.push(report('no raw taxonomy key rendered as a label',
  rawLeaks.length === 0, [...new Set(rawLeaks)].slice(0, 6).join(', ')));

// 4. the risk strip still shows all nine, empty ones included
const strip = doc.querySelectorAll('.status-strip .status-row .sr-name');
const stripNames = [...strip].map(e => e.textContent.trim());
results.push(report('risk strip renders all 9 categories (empty slots kept)',
  stripNames.length === 9, `${stripNames.length}: ${stripNames.slice(0, 3).join(' | ')}...`));

// 5. display names resolve to the taxonomy, not to keys
const expectRisk = axes.risk.order.map(k => axes.risk.entries[k].display);
results.push(report('risk strip labels match the taxonomy exactly',
  JSON.stringify(stripNames) === JSON.stringify(expectRisk),
  stripNames.filter((n, i) => n !== expectRisk[i]).join(', ')));

// 6. taxonomy cards render the full declared taxonomy (incl. zero-instance slots)
const cards = doc.querySelectorAll('.taxcard .tc-name');
const cardNames = new Set([...cards].map(e => e.textContent.trim()));
const missingTheme = axes.theme.order
  .map(k => axes.theme.entries[k].display).filter(d => !cardNames.has(d));
const missingSubtype = axes.entity_subtype.order
  .map(k => axes.entity_subtype.entries[k].display).filter(d => !cardNames.has(d));
results.push(report('every theme has a taxonomy card',
  missingTheme.length === 0, missingTheme.join(', ')));
results.push(report('every entity subtype has a taxonomy card (incl. zero-instance)',
  missingSubtype.length === 0, missingSubtype.join(', ')));

// 7. no runtime errors
const realErrors = errors.filter(e => !/Could not parse CSS|Error: Not implemented/.test(e));
results.push(report('no runtime errors', realErrors.length === 0,
  realErrors.slice(0, 3).join(' | ').slice(0, 300)));

// 8. the CAMEO axes shipped but are correctly marked not-yet-populated
results.push(report('CAMEO axes declared and honestly marked unpopulated',
  axes.theme_code.status === 'declared_not_yet_populated'
  && axes.action.entries.b20_mass_violence.alertable === false
  && !!axes.action.entries.b20_mass_violence.alertable_reason,
  axes.action.entries.b20_mass_violence.alertable_reason?.slice(0, 60)));

const pass = results.filter(Boolean).length;
console.log(`\n  ${pass}/${results.length} checks passed`);
if (warnings.length) console.log(`  warnings: ${warnings.length}\n   ` + warnings.slice(0, 5).join('\n   '));
process.exit(pass === results.length ? 0 : 1);
