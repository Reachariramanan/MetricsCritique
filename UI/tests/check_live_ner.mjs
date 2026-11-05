/**
 * Live NER section render checks (jsdom), from canned /api/gemma/status payloads.
 * Needs a built page + metrics (never the live UI/index.html):
 *   LIVE_NER_DIR=<dir with index.html + metrics.json>  (default: the scratch ui_live dir)
 * Build: python UI/build_dashboard.py --out <dir>/index.html --json-out <dir>/metrics.json
 * Run:   node UI/tests/check_live_ner.mjs
 */
import { JSDOM } from 'jsdom';
import { readFileSync } from 'fs';

const ASSETS = '/home/hariramanan/NTRO/EDA/UI/assets';
const DIR = process.env.LIVE_NER_DIR ||
  '/tmp/claude-1004/-home-hariramanan-NTRO-EDA/d6f393bd-035c-481c-a0cc-e18adc04b014/scratchpad/ui_live';
const html = readFileSync(`${DIR}/index.html`, 'utf8');
const metrics = JSON.parse(readFileSync(`${DIR}/metrics.json`, 'utf8'));

const OK = {
  available: true, updated_at: 'x', routed: 1234, extracted_posts: 900, entities: 4321, ok_rate: 0.987,
  rate_per_min: 6.5, incoming_pending: 3, by_domain: { D01: 10, D02: 5, none: 1 },
  domain_names: { D01: 'Conflict Fixture', D02: 'Economy Fixture' }, by_level: { L1: 1, L2: 2 },
  top_entities: [{ name: 'Acme', entity_type: 'ORG', level: 'L2', n: 7 }],
  recent: [
    { post_uid: 'n1', platform: 'x', snippet: 'brand new <b>post</b>', domain_1: 'D01', domain_2: null, event_mode: 'none',
      confidence_1: 0.9, routed_at: 't', source: 'new', entities: [{ type: 'ORG', name: 'Acme', level: 'L2' }] },
    { post_uid: 'b1', platform: 'tg', snippet: 'old one', domain_1: 'D02', domain_2: 'D01', event_mode: 'none',
      confidence_1: 0.8, routed_at: 't', source: 'backlog', entities: [] },
  ],
  watcher: { last_poll: 't', new_seen_total: 5 },
};

async function boot(handler) {
  const dom = new JSDOM(html, {
    runScripts: 'dangerously', url: 'http://localhost/', pretendToBeVisual: true,
    beforeParse(win) {
      win.fetch = (url) => {
        if (String(url).includes('metrics.json')) return Promise.resolve({ ok: true, json: () => Promise.resolve(metrics) });
        if (String(url).includes('api/gemma/status')) return handler();
        return Promise.resolve({ ok: true, json: () => Promise.resolve({}) });
      };
      win.requestAnimationFrame = (cb) => setTimeout(() => cb(0), 0);
      Object.defineProperty(win.HTMLElement.prototype, 'clientWidth', { get: () => 900 });
      Object.defineProperty(win.HTMLElement.prototype, 'clientHeight', { get: () => 400 });
    },
  });
  const s = dom.window.document.createElement('script');
  s.textContent = readFileSync(`${ASSETS}/dash.js`, 'utf8');
  dom.window.document.body.appendChild(s);
  await new Promise((r) => setTimeout(r, 2500));
  return dom.window;
}

const fail = [];
const check = (n, c) => { console.log((c ? 'PASS ' : 'FAIL ') + n); if (!c) fail.push(n); };

let w = await boot(() => Promise.resolve({ ok: true, status: 200, json: () => Promise.resolve(OK) }));
let d = w.document;
check('section + TOC entry exist', !!d.getElementById('livener') && !!d.querySelector('nav.toc a[href="#livener"]'));
check('provenance label', d.getElementById('live-ner').textContent.includes('provisional: routing/entity accuracy not yet judged'));
const strip = d.getElementById('ln-strip').textContent;
check('status strip values', strip.includes('1,234') && strip.includes('98.7%') && strip.includes('6.5') && strip.includes('900') && strip.includes('4,321'));
check('priority badge when pending>0', /priority/.test(d.getElementById('ln-strip').innerHTML));
const dom = d.getElementById('ln-domains').textContent;
check('domain bars use API names', dom.includes('Conflict Fixture') && dom.includes('Economy Fixture') && dom.includes('No domain'));
check('feed has 2 items, 1 highlighted new', d.querySelectorAll('.ln-item').length === 2 && d.querySelectorAll('.ln-item.is-new').length === 1);
check('snippet rendered as text (no HTML injection)', !d.querySelector('.ln-item b') && d.getElementById('ln-feed').textContent.includes('<b>post</b>'));
check('top entities', d.getElementById('ln-top').textContent.includes('Acme'));
check('last updated ticker', /last updated \d+s ago/.test(d.getElementById('ln-updated').textContent));
check('note hidden when available', d.getElementById('ln-note').hidden === true);
w.close();

w = await boot(() => Promise.resolve({ ok: true, status: 200, json: () => Promise.resolve({ available: false, error: 'gemma_ner.duckdb not found' }) }));
d = w.document;
check('available=false: calm not-running note', !d.getElementById('ln-note').hidden && /not running/.test(d.getElementById('ln-note').textContent) && d.getElementById('ln-content').hidden);
check('available=false: rest of dashboard intact', d.querySelectorAll('section.sec').length >= 15);
w.close();

w = await boot(() => Promise.resolve({ ok: false, status: 404, json: () => Promise.reject(new Error('x')) }));
d = w.document;
check('404: static-snapshot note, content hidden', /static snapshot/.test(d.getElementById('ln-note').textContent) && d.getElementById('ln-content').hidden);
check('404: dashboard still rendered', d.querySelectorAll('section.sec').length >= 15);
w.close();

console.log(fail.length ? `\nFAILED ${fail.length}` : '\nOK');
process.exit(fail.length ? 1 : 0);
