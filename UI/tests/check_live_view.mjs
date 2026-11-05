/**
 * Live window + Military mode checks (jsdom): the real UI/index.html + UI/data/metrics.json,
 * canned /api/live/overview payloads. Run: node UI/tests/check_live_view.mjs
 */
import { JSDOM, VirtualConsole } from 'jsdom';
const vc = new VirtualConsole(); vc.on('jsdomError', (e) => console.log('JSERR', e.message, ((e.detail && e.detail.stack) || '').split('\n').slice(0, 3).join(' | ')));
import { readFileSync } from 'fs';

const UI = new URL('..', import.meta.url).pathname;
const html = readFileSync(`${UI}index.html`, 'utf8');
const metrics = JSON.parse(readFileSync(`${UI}data/metrics.json`, 'utf8'));
const days = ['2026-10-17', '2026-10-18', '2026-10-19', '2026-10-20', '2026-10-21', '2026-10-22', '2026-10-23'];
const OK = {
  available: true, today: '2026-11-02', data_end: '2026-10-23', anchor: '2026-10-23', from: days[0], to: days[6],
  stale_days: 10, days, volume: { daily: [1, 2, 3, 4, 5, 6, 7], total: 28, prev_total: 20 },
  coverage: { routed_daily: [0, 0, 0, 0, 10, 20, 30], routed_in_window: 60, posts_in_window: 28 * 100, share: 0.43 },
  domain_names: { D01: 'Conflict Fixture', D05: 'Military Fixture', D02: 'Politics Fixture' },
  domains: [{ domain: 'D02', name: 'Politics Fixture', n: 30, prev_n: 0, daily: [] },
            { domain: 'D05', name: 'Military Fixture', n: 20, prev_n: 10, daily: [] },
            { domain: 'none', name: 'Not security-related', n: 10, prev_n: 0, daily: [] }],
  military: { domains: [{ domain: 'D05', name: 'Military Fixture' }, { domain: 'D01', name: 'Conflict Fixture' }],
    n: 20, also_touches: 7, prev_n: 10, daily: [0, 0, 0, 0, 5, 7, 8], share_of_routed: 0.33,
    platforms: [{ platform: 'telegram', n: 12 }, { platform: 'reddit', n: 8 }],
    entities: [{ name: 'Ukraine', entity_type: 'LOC_COUNTRY', level: 'L1', n: 9, posts: 8 }],
    recent: [{ post_uid: 'p1', day: '2026-10-23', platform: 'reddit', domain: 'D05', domain_2: null, confidence: 0.9,
               event_mode: 'factual', text: 'tank <b>column</b> moves', at: 't' }] },
};

async function boot(handler, urls) {
  const dom = new JSDOM(html, {
    virtualConsole: vc, runScripts: 'dangerously', url: 'http://localhost/', pretendToBeVisual: true,
    beforeParse(win) {
      win.fetch = (url) => {
        const u = String(url);
        if (u.includes('metrics.json')) return Promise.resolve({ ok: true, json: () => Promise.resolve(metrics) });
        if (u.includes('api/live/overview')) { urls.push(u); return handler(); }
        return Promise.resolve({ ok: false, status: 404, json: () => Promise.resolve({}) });
      };
      win.requestAnimationFrame = (cb) => setTimeout(() => cb(0), 0);
      Object.defineProperty(win.HTMLElement.prototype, 'clientWidth', { get: () => 900 });
      Object.defineProperty(win.HTMLElement.prototype, 'clientHeight', { get: () => 400 });
    },
  });
  for (const f of ['dash.js', 'live.js']) {
    const s = dom.window.document.createElement('script');
    s.textContent = readFileSync(`${UI}assets/${f}`, 'utf8');
    dom.window.document.body.appendChild(s);
  }
  await new Promise((r) => setTimeout(r, 2500));
  return dom.window;
}
const fail = [];
const check = (n, c) => { console.log((c ? 'PASS ' : 'FAIL ') + n); if (!c) fail.push(n); };
const wait = (ms) => new Promise((r) => setTimeout(r, ms));

const urls = [];
let w = await boot(() => Promise.resolve({ ok: true, json: () => Promise.resolve(OK) }), urls);
let d = w.document;
const dates = d.querySelectorAll('#filters-in input[type=date]');
const span = (new Date(dates[1].value) - new Date(dates[0].value)) / 86400000;
check('default window is 7 days (6-day span), not the full window', span === 6);
check('military button added', !!d.getElementById('mode-military-btn'));
check('overview "Topics now" card present', /Topics now/.test(d.getElementById('lv-overview')?.textContent || ''));
check('overview card shows stale banner', /10 days behind today/.test(d.getElementById('lv-overview').textContent));
d.getElementById('mode-military-btn').click();
const mv = d.getElementById('military-view');
check('military view visible, others hidden', mv.style.display === '' &&
  d.getElementById('basic-view').style.display === 'none' && d.getElementById('advanced-view').style.display === 'none');
check('military button pressed', d.getElementById('mode-military-btn').getAttribute('aria-pressed') === 'true');
check('stale-corpus banner', /corpus ends .*10 days behind today/.test(mv.textContent));
check('KPIs', mv.textContent.includes('20') && mv.textContent.includes('+100% vs prior week') && mv.textContent.includes('43%'));
check('unrouted days hatched, not zero', mv.querySelectorAll('.lv-bar.off').length === 4 && mv.textContent.includes('n/a'));
check('entities + platforms', mv.textContent.includes('Ukraine') && mv.textContent.includes('telegram'));
check('post text rendered as text (no HTML injection)', !mv.querySelector('.lv-feed b') && mv.textContent.includes('<b>column</b>'));
check('military domains highlighted', mv.querySelectorAll('.lv-dom.mil').length >= 1);
const chip = [...mv.querySelectorAll('.lv-chips .chip')].find((b) => b.textContent.startsWith('D02'));
chip.click(); await wait(100);
check('toggling a domain re-queries with domains=', urls.some((u) => /domains=.*D02/.test(decodeURIComponent(u))));
d.getElementById('mode-basic-btn').click();
check('Overview button leaves military mode', mv.style.display === 'none' && d.getElementById('basic-view').style.display === '');
w.close();

const urls2 = [];
w = await boot(() => Promise.resolve({ ok: false, status: 404, json: () => Promise.resolve({}) }), urls2);
d = w.document;
d.getElementById('mode-military-btn').click();
check('static snapshot (404): calm message, no crash', /not available|static snapshot/.test(d.getElementById('military-view').textContent));
check('static snapshot: dashboard still rendered', d.querySelectorAll('[data-panel]').length > 5);
w.close();

console.log(fail.length ? `\n${fail.length} FAILED` : '\nall passed');
process.exit(fail.length ? 1 : 0);
