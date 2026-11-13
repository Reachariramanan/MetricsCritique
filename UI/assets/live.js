/* Live window + Military mode (reads GET api/live/overview; no-op on a static snapshot).
   Loaded after dash.js. Self-contained: it adds the third mode button and view itself, and
   draws a "Live topics" card in the Overview. All numbers are routed by Gemma and are
   provisional until routing precision has been judged. */
(function () {
  'use strict';

  var URL = 'api/live/overview';
  var KEY_DOMAINS = 'z-mil-domains';
  var L = { data: null, timer: null, dead: false, domains: null, days: 7 };

  function h(tag, attrs, kids) {
    var e = document.createElement(tag);
    if (attrs) {
      for (var k in attrs) {
        if (k === 'class') { e.className = attrs[k]; }
        else if (k === 'text') { e.textContent = attrs[k]; }
        else if (attrs[k] != null) { e.setAttribute(k, attrs[k]); }
      }
    }
    (kids || []).forEach(function (c) { if (c) { e.appendChild(c); } });
    return e;
  }
  function fmt(n) { return n == null ? '–' : Number(n).toLocaleString('en-US'); }
  function pct(x) { return x == null ? '–' : (x * 100).toFixed(x < 0.1 ? 1 : 0) + '%'; }
  function dayLabel(s) {
    var d = new Date(s + 'T00:00:00Z');
    return isNaN(d) ? s : d.toLocaleDateString('en-GB', { day: 'numeric', month: 'short', timeZone: 'UTC' });
  }
  function delta(n, prev) {
    if (!prev) { return n ? 'new vs prior week' : 'no change'; }
    var c = (n - prev) / prev * 100;
    return (c >= 0 ? '+' : '') + c.toFixed(0) + '% vs prior week';
  }
  function store(k, v) { try { localStorage.setItem(k, v); } catch (e) { /* noop */ } }
  function load(k) { try { return localStorage.getItem(k); } catch (e) { return null; } }

  /* ------------------------------------------------------------ pieces */

  function kpi(label, val, sub) {
    return h('div', { class: 'kpi' }, [h('div', { class: 'k-lbl', text: label }),
      h('div', { class: 'k-val', text: val }), h('div', { class: 'k-sub', text: sub || '' })]);
  }

  function windowBanner(d) {
    var b = h('div', { class: 'lv-window' });
    b.appendChild(h('span', { class: 'lv-range', text: 'Past ' + d.days.length + ' days · ' +
      dayLabel(d.from) + ' – ' + dayLabel(d.to) }));
    if (d.stale_days > 0) {
      b.appendChild(h('span', { class: 'lv-stale', text: '⚠ corpus ends ' + dayLabel(d.data_end) +
        ' — ' + d.stale_days + ' day' + (d.stale_days === 1 ? '' : 's') + ' behind today; showing the latest ' +
        d.days.length + ' days of data' }));
    } else {
      b.appendChild(h('span', { class: 'lv-fresh', text: '● up to date through today' }));
    }
    return b;
  }

  /* daily bars; days with no routed posts are drawn hatched, not as zero */
  function dailyBars(days, vals, routed, color) {
    var max = Math.max.apply(null, vals.concat([1]));
    var wrap = h('div', { class: 'lv-bars' });
    days.forEach(function (day, i) {
      var unrouted = routed && !routed[i];
      var bar = h('div', { class: 'lv-bar' + (unrouted ? ' off' : '') });
      var fill = h('div', { class: 'f' });
      fill.style.height = (unrouted ? 100 : Math.max(2, vals[i] / max * 100)) + '%';
      if (color && !unrouted) { fill.style.background = color; }
      bar.appendChild(fill);
      bar.appendChild(h('div', { class: 'v', text: unrouted ? 'n/a' : fmt(vals[i]) }));
      bar.appendChild(h('div', { class: 'd', text: dayLabel(day) }));
      bar.title = dayLabel(day) + ': ' + (unrouted ? 'not routed yet (backlog still processing)' : fmt(vals[i]) + ' posts');
      wrap.appendChild(bar);
    });
    return wrap;
  }

  function domainBars(d, military) {
    var host = h('div', { class: 'lv-doms' });
    var rows = d.domains || [];
    var max = Math.max.apply(null, rows.map(function (r) { return r.n; }).concat([1]));
    var tot = rows.reduce(function (a, r) { return a + r.n; }, 0) || 1;
    rows.forEach(function (r) {
      var isMil = military && military.indexOf(r.domain) >= 0;
      var row = h('div', { class: 'lv-dom' + (isMil ? ' mil' : '') }, [
        h('span', { class: 'n', text: (r.domain === 'none' ? '' : r.domain + ' · ') + r.name }),
        h('span', { class: 't' }, [h('i')]),
        h('span', { class: 'c', text: fmt(r.n) + ' · ' + pct(r.n / tot) })]);
      row.querySelector('i').style.width = Math.max(2, r.n / max * 100) + '%';
      row.title = r.name + ' — ' + delta(r.n, r.prev_n);
      host.appendChild(row);
    });
    return host;
  }

  /* ------------------------------------------------------- military view */

  function selDomains(d) {
    var avail = Object.keys(d.domain_names || {});
    var sel = (L.domains || d.military.domains.map(function (x) { return x.domain; }))
      .filter(function (x) { return avail.indexOf(x) >= 0; });
    return sel.length ? sel : d.military.domains.map(function (x) { return x.domain; });
  }

  function renderMilitary(root) {
    root.textContent = '';
    var d = L.data;
    if (!d || !d.available) {
      root.appendChild(h('div', { class: 'empty', text: 'Live data is not available' +
        (d && d.error ? ' (' + d.error + ')' : ' (this page is a static snapshot — serve it with api_server.py)') + '.' }));
      return;
    }
    var m = d.military;
    if (!m) {
      root.appendChild(h('div', { class: 'empty', text: 'Domain routing has not produced data yet' +
        (d.routing_error ? ' (' + d.routing_error + ')' : '') + '.' }));
      return;
    }
    var sel = selDomains(d);

    root.appendChild(h('div', { class: 'lv-head' }, [
      h('h2', { text: 'Military mode' }),
      h('span', { class: 'badge gray', text: 'provisional: Gemma routing precision not yet judged' })]));
    root.appendChild(windowBanner(d));

    var chips = h('div', { class: 'lv-chips' }, [h('span', { class: 'lbl', text: 'Domains counted as military-related' })]);
    Object.keys(d.domain_names).forEach(function (k) {
      var b = h('button', { class: 'chip', type: 'button', text: k + ' ' + d.domain_names[k] });
      b.setAttribute('aria-pressed', sel.indexOf(k) >= 0 ? 'true' : 'false');
      b.addEventListener('click', function () {
        var cur = selDomains(d);
        var i = cur.indexOf(k);
        if (i >= 0) { if (cur.length > 1) { cur.splice(i, 1); } } else { cur.push(k); }
        L.domains = cur; store(KEY_DOMAINS, cur.join(','));
        poll();
      });
      chips.appendChild(b);
    });
    root.appendChild(chips);

    var cov = d.coverage || {};
    root.appendChild(h('div', { class: 'kpi-row' }, [
      kpi('Military-related posts', fmt(m.n), delta(m.n, m.prev_n)),
      kpi('Share of routed', pct(m.share_of_routed), 'of ' + fmt(cov.routed_in_window) + ' routed posts'),
      kpi('Also touch it', fmt(m.also_touches), 'runner-up domain only, not counted'),
      kpi('Routing coverage', pct(cov.share), fmt(cov.routed_in_window) + ' of ' + fmt(cov.posts_in_window) + ' posts')]));

    var c1 = h('div', { class: 'card' }, [h('div', { class: 'ct', text: 'Military-related posts per day' }),
      dailyBars(d.days, m.daily, cov.routed_daily, 'var(--gold)')]);
    if ((cov.routed_daily || []).some(function (x) { return !x; })) {
      c1.appendChild(h('div', { class: 'fnote', text: 'Hatched days have no routed posts yet — the Gemma backlog is being ' +
        'worked newest-first, so older days fill in over time. They are not zero.' }));
    }
    root.appendChild(c1);

    var cols = h('div', { class: 'lv-cols' });
    var ent = h('ol', { class: 'lv-list' });
    (m.entities || []).slice(0, 14).forEach(function (e) {
      ent.appendChild(h('li', {}, [h('span', { class: 'n', text: e.name }),
        h('span', { class: 'm', text: e.entity_type + ' · ' + fmt(e.posts) + ' posts' })]));
    });
    if (!m.entities.length) { ent.appendChild(h('li', { class: 'fnote', text: 'No entities extracted for these posts yet.' })); }
    cols.appendChild(h('div', { class: 'card' }, [h('div', { class: 'ct', text: 'Entities in military posts' }), ent]));
    var pl = h('div', { class: 'lv-doms' });
    var pmax = Math.max.apply(null, (m.platforms || []).map(function (p) { return p.n; }).concat([1]));
    (m.platforms || []).forEach(function (p) {
      var r = h('div', { class: 'lv-dom mil' }, [h('span', { class: 'n', text: p.platform }),
        h('span', { class: 't' }, [h('i')]), h('span', { class: 'c', text: fmt(p.n) })]);
      r.querySelector('i').style.width = Math.max(2, p.n / pmax * 100) + '%';
      pl.appendChild(r);
    });
    cols.appendChild(h('div', { class: 'card' }, [h('div', { class: 'ct', text: 'By platform' }), pl]));
    root.appendChild(cols);

    root.appendChild(h('div', { class: 'card' }, [h('div', { class: 'ct', text: 'Topics now — all routed domains (military-related highlighted)' }),
      domainBars(d, sel)]));

    var feed = h('ul', { class: 'lv-feed' });
    (m.recent || []).forEach(function (r) {
      feed.appendChild(h('li', {}, [
        h('div', { class: 'meta', text: dayLabel(r.day) + ' · ' + r.platform + ' · ' + r.domain +
          (r.event_mode && r.event_mode !== 'none' ? ' · ' + r.event_mode : '') }),
        h('div', { class: 'tx', text: r.text })]));
    });
    root.appendChild(h('div', { class: 'card' }, [h('div', { class: 'ct', text: 'Latest military-related posts' }), feed]));
  }

  /* ------------------------------------------------- overview live topics */

  function renderOverviewCard() {
    var host = document.getElementById('lv-overview');
    var wrap = document.querySelector('#basic-view .basic-wrap');
    if (!host && wrap) {
      host = h('div', { class: 'basic-panel', id: 'lv-overview' });
      wrap.insertBefore(host, wrap.children[2] || null);
    }
    if (!host) { return; }
    host.textContent = '';
    var d = L.data;
    host.appendChild(h('p', { class: 'bp-t', text: 'Topics now — past ' + L.days + ' days' }));
    if (!d || !d.available || !d.domains) {
      host.appendChild(h('div', { class: 'fnote', text: 'Live topics appear when the server is running with routing data.' }));
      return;
    }
    host.appendChild(windowBanner(d));
    host.appendChild(domainBars(d, null));
    host.appendChild(h('div', { class: 'fnote', text: 'Gemma domain routing, ' + pct((d.coverage || {}).share) +
      ' of posts in the window routed so far · provisional' }));
  }

  /* ------------------------------------------------------------- plumbing */

  function paint() {
    var mv = document.getElementById('military-view');
    if (mv && mv.style.display !== 'none') { renderMilitary(mv); }
    renderOverviewCard();
  }

  function poll() {
    if (L.dead) { return Promise.resolve(); }
    var q = URL + '?days=' + L.days + (L.domains ? '&domains=' + encodeURIComponent(L.domains.join(',')) : '');
    return fetch(q, { cache: 'no-store' }).then(function (r) {
      if (!r.ok) { throw new Error('http ' + r.status); }
      return r.json();
    }).then(function (d) { L.data = d; paint(); })
      .catch(function () {
        if (!L.data) { L.dead = true; }   // static snapshot (no API): stop polling
        paint();
      });
  }

  function setMode(mode) {
    var basic = document.getElementById('basic-view'), adv = document.getElementById('advanced-view');
    var mv = document.getElementById('military-view');
    var bb = document.getElementById('mode-basic-btn'), ab = document.getElementById('mode-advanced-btn');
    var mb = document.getElementById('mode-military-btn');
    if (!mv || !mb) { return; }
    var mil = mode === 'military';
    mv.style.display = mil ? '' : 'none';
    if (mil) {
      document.body.setAttribute('data-mode', 'military');   // hides the shared date-range bar (CSS)
      basic.style.display = 'none'; adv.style.display = 'none';
      bb.setAttribute('aria-pressed', 'false'); ab.setAttribute('aria-pressed', 'false');
      store('z-mode', 'military');
      renderMilitary(mv);
    }
    mb.setAttribute('aria-pressed', mil ? 'true' : 'false');
  }

  function init() {
    var ab = document.getElementById('mode-advanced-btn');
    if (!ab || document.getElementById('military-view')) { return; }
    var mb = document.getElementById('mode-military-btn');   // normally in the page shell
    if (!mb) {
      mb = h('button', { type: 'button', id: 'mode-military-btn', text: 'Military' });
      mb.setAttribute('aria-pressed', 'false');
      ab.parentNode.appendChild(mb);
    }
    var adv = document.getElementById('advanced-view');
    var mv = h('div', { class: 'wrap', id: 'military-view' });
    mv.style.display = 'none';
    adv.parentNode.insertBefore(mv, adv.nextSibling);

    mb.addEventListener('click', function () { setMode('military'); });
    ['mode-basic-btn', 'mode-advanced-btn', 'basic-to-advanced-link'].forEach(function (id) {
      var b = document.getElementById(id);
      if (b) { b.addEventListener('click', function () { setMode('other'); }); }
    });

    var saved = load(KEY_DOMAINS);
    if (saved) { L.domains = saved.split(',').filter(Boolean); }
    poll().then(function () {
      // dash.js builds its chrome after the data loads and resets the mode; restore ours afterwards
      var tries = 0, t = setInterval(function () {
        tries++;
        if (document.querySelector('#filters-in .chip') || tries > 60) {
          clearInterval(t);
          if (load('z-mode') === 'military') { setMode('military'); }
        }
      }, 150);
    });
    L.timer = setInterval(function () {
      if (document.visibilityState === 'visible') { poll(); }
    }, 60000);
    window.__liveViewPoll = poll;
  }

  if (document.readyState === 'loading') { document.addEventListener('DOMContentLoaded', init); }
  else { init(); }
})();
