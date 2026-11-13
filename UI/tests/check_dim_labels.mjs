/**
 * Label-resolution checks for the dimension registry (DIM_AXIS / dimName / dimDrill).
 *
 * The accessors fail soft by design: an unknown key renders as its raw string.
 * That is the right behaviour -- the row still exists -- but it means the failure
 * mode for a mis-wired axis is a dashboard showing a bare "05.2" where a label
 * belongs: no exception, no console error, nothing a smoke test catches. Before
 * the registry existed, three separate ternary chains resolved these names and
 * every one of them was binary, so a CAMEO row fell through to themeName() (the
 * WRONG axis, silently) or to the raw key.
 *
 * Boots the real dashboard against the real metrics.json, same as check_taxonomy.mjs.
 *
 * Run:  node UI/tests/check_dim_labels.mjs
 */
import { JSDOM } from 'jsdom';
import { readFileSync } from 'fs';

const ROOT = new URL('..', import.meta.url).pathname.replace(/\/$/, '');   // UI/ folder, wherever the repo lives
const html = readFileSync(`${ROOT}/index.html`, 'utf8');
const metrics = JSON.parse(readFileSync(`${ROOT}/data/metrics.json`, 'utf8'));

const warnings = [];
const dom = new JSDOM(html, {
  runScripts: 'dangerously',
  url: 'http://localhost/',
  beforeParse(win) {
    win.fetch = (url) => {
      if (String(url).includes('metrics.json')) {
        return Promise.resolve({ ok: true, json: () => Promise.resolve(metrics) });
      }
      return Promise.resolve({ ok: true, json: () => Promise.resolve({}) });
    };
    win.console.warn = (...a) => warnings.push(a.join(' '));
    win.requestAnimationFrame = (cb) => setTimeout(() => cb(0), 0);
    Object.defineProperty(win.HTMLElement.prototype, 'clientWidth', { get: () => 900 });
    Object.defineProperty(win.HTMLElement.prototype, 'clientHeight', { get: () => 400 });
  },
});

// index.html is a shell that loads assets/ over HTTP; jsdom fetches nothing, so
// inject dash.js explicitly (same as check_taxonomy.mjs).
const bootErrors = [];
const script = dom.window.document.createElement('script');
script.textContent = readFileSync(`${ROOT}/assets/dash.js`, 'utf8');
try { dom.window.document.body.appendChild(script); } catch (e) { bootErrors.push(e.stack); }

await new Promise((r) => setTimeout(r, 3000));

const pass = [], fail = [];
const check = (name, cond, detail = '') => {
  (cond ? pass : fail).push(name);
  console.log(`  ${cond ? 'PASS' : 'FAIL'}  ${name}${detail ? '  ' + detail : ''}`);
};

const W = dom.window;
check('dash.js booted and exposed the registry',
  !!(W.__TAX__ && W.__DIM__ && W.__DIM__.axis),
  bootErrors.length ? bootErrors[0].split('\n')[0] : '');
if (!W.__DIM__) {
  console.log('\n  0/1 checks passed');
  process.exit(1);
}

const TAX = W.__TAX__;
const { axis: DIM_AXIS, name: dimName, drill: dimDrill } = W.__DIM__;
const axes = (metrics.taxonomy && metrics.taxonomy.axes) || {};

// Snapshot BEFORE any probe below. TAX.unknown() is cumulative, and the checks
// in this file deliberately ask for keys on the wrong axis and for undeclared
// keys -- so reading it at the end would measure this harness, not the render.
const renderUnknown = Object.entries(TAX.unknown ? TAX.unknown() : {})
  .flatMap(([a, ks]) => Object.keys(ks).map((k) => `${a}/${k}`));

check('the app loaded the taxonomy from metrics.json', Object.keys(axes).length > 0,
  `axes=${Object.keys(axes).join(', ')}`);

// --- the registry must cover every dim_type the API can emit ----------------
// Kept in sync with ALERT_ROUTES / CAMEO_RELATED_AXES in UI/api_server.py.
for (const d of ['risk', 'theme', 'theme_code', 'theme_subcode', 'action']) {
  check(`registry covers dim_type "${d}"`, !!DIM_AXIS[d],
    DIM_AXIS[d] ? `axis=${DIM_AXIS[d].axis} param=${DIM_AXIS[d].param}` : 'MISSING');
}
const orphan = Object.values(DIM_AXIS).map((d) => d.axis).filter((a) => !axes[a]);
check('every registry axis exists in the published taxonomy', orphan.length === 0,
  orphan.join(',') || 'all present');

// --- no declared key may render as its own raw key --------------------------
const raw = [];
for (const [dim, spec] of Object.entries(DIM_AXIS)) {
  for (const k of (axes[spec.axis] && axes[spec.axis].order) || []) {
    const n = dimName(dim, k);
    if (!n || n === k) { raw.push(`${dim}/${k}`); }
  }
}
check('no declared key renders as a bare code', raw.length === 0,
  raw.length ? `raw: ${raw.slice(0, 6).join(', ')}` : 'every declared key resolved to a label');

// The case that used to fall through to themeName().
const first = (axes.theme_code && axes.theme_code.order || [])[0];
check('a theme_code resolves through its OWN axis, not theme',
  !!first && dimName('theme_code', first) === TAX.name('theme_code', first)
    && dimName('theme_code', first) !== TAX.name('theme', first),
  first ? `${first} -> "${dimName('theme_code', first)}"` : 'axis empty');

// --- drill params must match what the API actually reads --------------------
check('CAMEO drills send code=, legacy send category=, entity sends entity_uid=',
  dimDrill('theme_code', '05').params.code === '05'
  && dimDrill('action', 'b18_assault').params.code === 'b18_assault'
  && dimDrill('risk', 'sanctions').params.category === 'sanctions'
  && dimDrill('entity', 'e1').params.entity_uid === 'e1',
  'param names match ALERT_ROUTES');

// --- render level: the chips renderRelated actually emits -------------------
// The plan lists check_related.mjs as a gate for this function, but that harness
// only ever existed in a scratchpad and is not in the repo -- so this is it.
// A CAMEO row in the related payload used to render its label through
// themeName() and its drill through params.category, both silently wrong.
{
  const host = W.document.createElement('div');
  W.__DIM__.renderRelated(host, {
    entities: [], topics: [], similar_posts: [],
    categories: [
      { dim_type: 'risk', category: 'sanctions', co_posts: 12, global_posts: 578, score: 3.1 },
      { dim_type: 'theme_code', category: first, co_posts: 9, global_posts: 400, score: 2.4 },
      { dim_type: 'action', category: 'b18_assault', co_posts: 7, global_posts: 300, score: 2.0 },
    ],
  });
  const text = host.textContent || '';
  const chips = host.querySelectorAll('.related-chips > *');
  check('renderRelated emits a chip per category row', chips.length === 3,
    `${chips.length} chips`);
  check('a CAMEO chip shows its label, not its raw code',
    text.includes(TAX.name('theme_code', first))
    && text.includes(TAX.name('action', 'b18_assault'))
    && !text.includes('b18_assault'),
    `rendered: ${text.slice(0, 90).replace(/\s+/g, ' ')}`);
  check('mixed axes render side by side without one shadowing the other',
    text.includes(TAX.name('risk', 'sanctions')), 'legacy axis still labelled');
}

// --- the soft-fail path must still fire -------------------------------------
const before = warnings.length;
const bogus = dimName('theme_code', 'zz.9_not_a_code');
check('an undeclared key still renders AND is reported',
  bogus === 'zz.9_not_a_code' && warnings.length > before,
  `warned=${warnings.length - before}`);

// --- and the real render itself must not have flagged anything --------------
check('rendering the real dashboard flagged no undeclared key',
  renderUnknown.length === 0, renderUnknown.slice(0, 8).join(', ') || 'clean');

console.log(`\n  ${pass.length}/${pass.length + fail.length} checks passed`);
process.exit(fail.length ? 1 : 0);
