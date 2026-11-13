/* ==========================================================================
   OSINT metrics dashboard -- rendering engine
   --------------------------------------------------------------------------
   No libraries. Charts are inline SVG built by the small helper set below.

   Two rules shape every chart here, and they are why this file has no
   categorical palette at all:

   1. A black/white/gray/gold palette cannot separate 9 topics or 6 platforms
      by hue. So identity is NEVER carried by hue. It is carried by
      *position*: one small panel per platform or per topic. Inside a panel
      there are at most two mark colours -- gold (the focal measure) and gray
      (context/reference) -- a pair validated at OKLab dE 19.2 (dark) and
      25.8 (light) including under simulated protanopia/deuteranopia.
   2. Because hue never encodes identity, a filter can never repaint a series.
      Gold always means "the measure this panel is about"; gray always means
      "context". Changing the series count changes nothing about colour.

   Magnitude uses a single gold ramp (7 steps, light->dark, one hue).
   Polarity uses gold vs clay with a neutral GRAY midpoint -- never a hue at
   the midpoint. Status (critical/high/medium) is a reserved scale, always
   rendered with an icon AND a text label, never colour alone.
   ========================================================================== */

(function () {
  'use strict';

  /* Data is a snapshot (metrics.json), not a live DB. The UI is dynamic:
     filters re-scope every chart client-side. Reload metrics.json only after
     rebuild (python UI/build_dashboard.py). */
  var DATA = null;
  var SVGNS = 'http://www.w3.org/2000/svg';
  var patSeq = 0;

  /* ------------------------------------------------------------ tables ---- */

  var _cache = {};
  function T(name) {
    if (!DATA || !DATA.tables) { return []; }
    if (_cache[name]) { return _cache[name]; }
    var t = DATA.tables[name];
    if (!t) { _cache[name] = []; return _cache[name]; }
    var cols = t.columns, out = new Array(t.rows.length);
    for (var i = 0; i < t.rows.length; i++) {
      var o = {}, r = t.rows[i];
      for (var j = 0; j < cols.length; j++) { o[cols[j]] = r[j]; }
      out[i] = o;
    }
    _cache[name] = out;
    return out;
  }
  function hasTable(name) {
    return !!(DATA && DATA.tables && DATA.tables[name] && DATA.tables[name].rows.length);
  }

  var META = {};
  var TOPIC = {};
  var TAG = {};
  var PLATFORMS = [];
  var PLATMETA = {};
  var TOPIC_IDS = [];

  function metaName(k) { return (META[k] && META[k].display_name) || k; }
  function metaUnits(k) { return (META[k] && META[k].units) || ''; }
  function metaFormula(k) { return (META[k] && META[k].formula) || ''; }
  function metaCaveat(k) { return (META[k] && META[k].caveats) || ''; }
  function metaGate(k) { return (META[k] && META[k].support_gate) || ''; }

  function topicLabel(id) {
    var t = TOPIC[id];
    var raw = t ? (t.label || t.topic || id) : (id == null ? 'All topics' : id);
    return /^unassigned(\s*\/\s*unassigned)?$/i.test(String(raw)) ? 'Unclassified' : raw;
  }
  function tagLabel(id) { return TAG[id] ? '#' + TAG[id].tag : (id || ''); }

  /* ------------------------------------------------------------ format ---- */

  function isNum(v) { return typeof v === 'number' && isFinite(v); }

  function fmt(v, dp) {
    if (!isNum(v)) { return '—'; }
    var d = dp == null ? 2 : dp;
    return v.toLocaleString(undefined, { minimumFractionDigits: d, maximumFractionDigits: d });
  }
  function fmtInt(v) {
    return isNum(v) ? Math.round(v).toLocaleString() : '—';
  }
  function fmtCompact(v) {
    if (!isNum(v)) { return '—'; }
    var a = Math.abs(v);
    if (a >= 1e9) { return (v / 1e9).toFixed(1) + 'B'; }
    if (a >= 1e6) { return (v / 1e6).toFixed(1) + 'M'; }
    if (a >= 1e4) { return (v / 1e3).toFixed(1) + 'K'; }
    return Math.round(v).toLocaleString();
  }
  function fmtQ(v) {
    if (!isNum(v)) { return '—'; }
    if (v === 0) { return '<1e-300'; }
    if (v < 0.001) { return v.toExponential(1); }
    return v.toFixed(4);
  }
  function fmtSigned(v, dp) {
    if (!isNum(v)) { return '—'; }
    return (v > 0 ? '+' : '') + fmt(v, dp == null ? 3 : dp);
  }
  function fmtPct(v, dp) {
    return isNum(v) ? (v * 100).toFixed(dp == null ? 0 : dp) + '%' : '—';
  }
  var MON = ['Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun', 'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec'];
  function fmtDay(s) {
    if (!s) { return '—'; }
    var p = String(s).slice(0, 10).split('-');
    return MON[(+p[1]) - 1] + ' ' + (+p[2]);
  }
  function fmtDayFull(s) {
    if (!s) { return '—'; }
    var p = String(s).slice(0, 10).split('-');
    return p[0] + '-' + p[1] + '-' + p[2];
  }
  function day(s) { return String(s).slice(0, 10); }

  /* ------------------------------------------------------------- theme ---- */

  var K = {};
  function readTokens() {
    var cs = getComputedStyle(document.documentElement);
    function g(n) { return cs.getPropertyValue(n).trim(); }
    K = {
      surface: g('--surface'), sunken: g('--sunken'), elev: g('--elev'),
      ink: g('--ink'), ink2: g('--ink-2'), ink3: g('--ink-3'),
      gold: g('--gold'), goldBorder: g('--gold-border'), goldHair: g('--gold-hair'),
      focal: g('--mark-focal'), focalSoft: g('--mark-focal-soft'),
      context: g('--mark-context'), contextSoft: g('--mark-context-soft'),
      neg: g('--mark-neg'), grid: g('--grid'), axis: g('--axis'),
      crit: g('--status-crit'),
      seq: [g('--seq-0'), g('--seq-1'), g('--seq-2'), g('--seq-3'), g('--seq-4'), g('--seq-5'), g('--seq-6')],
      div: [g('--div-n3'), g('--div-n2'), g('--div-n1'), g('--div-0'), g('--div-p1'), g('--div-p2'), g('--div-p3')]
    };
  }

  function seqColor(t) {
    if (!isNum(t)) { return null; }
    var i = Math.round(Math.max(0, Math.min(1, t)) * (K.seq.length - 1));
    return K.seq[i];
  }
  function divColor(t) {  /* t in [-1, 1] */
    if (!isNum(t)) { return null; }
    var i = Math.round((Math.max(-1, Math.min(1, t)) + 1) / 2 * (K.div.length - 1));
    return K.div[i];
  }

  /* --------------------------------------------------------------- dom ---- */

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
  function el(tag, attrs) {
    var e = document.createElementNS(SVGNS, tag);
    for (var k in attrs) { if (attrs[k] != null) { e.setAttribute(k, attrs[k]); } }
    return e;
  }
  function txt(s) { return document.createTextNode(s); }
  function svgText(x, y, s, o) {
    var a = { x: x, y: y, fill: (o && o.fill) || K.ink3, 'font-size': (o && o.size) || 10,
      'text-anchor': (o && o.anchor) || 'start', 'font-family': 'inherit' };
    if (o && o.weight) { a['font-weight'] = o.weight; }
    if (o && o.tabular) { a['font-variant-numeric'] = 'tabular-nums'; }
    if (o && o.baseline) { a['dominant-baseline'] = o.baseline; }
    var t = el('text', a);
    t.textContent = s;
    return t;
  }

  /* ----------------------------------------------------------- tooltip ---- */

  var tipEl = null;
  var TIP = {
    show: function (ev, node) {
      if (!tipEl) { tipEl = document.getElementById('tip'); }
      if (!tipEl) { return; }
      tipEl.textContent = '';
      tipEl.appendChild(node);
      tipEl.classList.add('on');
      var r = tipEl.getBoundingClientRect();
      var x = ev.clientX + 14, y = ev.clientY + 14;
      if (x + r.width > window.innerWidth - 8) { x = ev.clientX - r.width - 14; }
      if (y + r.height > window.innerHeight - 8) { y = ev.clientY - r.height - 14; }
      tipEl.style.left = Math.max(6, x) + 'px';
      tipEl.style.top = Math.max(6, y) + 'px';
    },
    hide: function () { if (tipEl) { tipEl.classList.remove('on'); } }
  };

  function tipNode(head, rows, note) {
    var box = h('div', {}, [h('div', { class: 't-hd', text: head })]);
    (rows || []).forEach(function (r) {
      var key = h('span', { class: 'k' });
      if (r.color) {
        var sw = h('span', { class: 'sw' });
        sw.style.background = r.color;
        key.appendChild(sw);
      }
      key.appendChild(txt(r.k));
      box.appendChild(h('div', { class: 't-row' }, [key, h('span', { class: 'v', text: r.v })]));
    });
    if (note) { box.appendChild(h('div', { class: 't-note', text: note })); }
    return box;
  }

  function hoverable(node, headFn, rowsFn, noteFn) {
    function on(ev) { TIP.show(ev, tipNode(headFn(), rowsFn(), noteFn ? noteFn() : null)); }
    node.addEventListener('pointerenter', on);
    node.addEventListener('pointermove', on);
    node.addEventListener('pointerleave', TIP.hide);
    node.setAttribute('tabindex', '0');
    node.addEventListener('focus', function () {
      var r = node.getBoundingClientRect();
      TIP.show({ clientX: r.left + r.width / 2, clientY: r.top }, tipNode(headFn(), rowsFn(), noteFn ? noteFn() : null));
    });
    node.addEventListener('blur', TIP.hide);
  }

  /* ------------------------------------------------------------- drill ---- */
  /* Click-to-inspect: every clickable mark in this file carries a small
     `drill` descriptor ({view, params, title}). Clicking it fires exactly one
     GET /api/drill?view=...&<params> against api_server.py -- a bounded,
     column-selected, read-only DuckDB query, nothing preloaded. The panel is
     closable (Escape, backdrop click, × button); closing clears its DOM and
     drops the fetched rows, so nothing accumulates across clicks. Opening a
     new drill while one is loading invalidates the older fetch via `seq` so
     a slow response can never repaint a panel the user already moved past. */

  var DRILL = { seq: 0 };

  DRILL.open = function (cfg) {
    var mySeq = ++DRILL.seq;
    var panel = document.getElementById('drill-panel');
    var backdrop = document.getElementById('drill-backdrop');
    var titleEl = document.getElementById('drill-title');
    var subEl = document.getElementById('drill-sub');
    var bodyEl = document.getElementById('drill-body');
    if (!panel || !backdrop || !titleEl || !subEl || !bodyEl) { return; }

    titleEl.textContent = cfg.title || 'Underlying posts';
    subEl.textContent = cfg.subtitle || 'Loading…';
    bodyEl.textContent = '';
    backdrop.classList.add('on');
    panel.classList.add('on');
    panel.setAttribute('aria-hidden', 'false');

    var params = cfg.params || {};
    var qs = Object.keys(params)
      .filter(function (k) { return params[k] != null && params[k] !== ''; })
      .map(function (k) { return encodeURIComponent(k) + '=' + encodeURIComponent(params[k]); })
      .join('&');
    var url = '/api/drill?view=' + encodeURIComponent(cfg.view) + (qs ? '&' + qs : '') + '&limit=60';

    fetch(url).then(function (r) { return r.json(); }).then(function (data) {
      if (mySeq !== DRILL.seq) { return; }
      bodyEl.textContent = '';
      if (data.error) {
        subEl.textContent = '';
        bodyEl.appendChild(h('div', { class: 'empty', text: 'Could not load rows: ' + data.error }));
        return;
      }
      /* risk/theme drills are ordered by classifier score, everything else by
         recency -- say which, rather than always claiming "most recent". */
      var byScore = (cfg.view === 'risk' || cfg.view === 'theme'
        || (cfg.view === 'alert' && (cfg.params || {}).dimension_type !== 'entity'));
      subEl.textContent = 'Showing ' + data.shown + ' of ' + fmtInt(data.total) +
        ' underlying posts, ' + (byScore ? 'strongest match first.' : 'most recent first.');
      if (!data.rows.length) {
        bodyEl.appendChild(h('div', { class: 'empty', text: 'No underlying posts matched this selection.' }));
        return;
      }
      var list = h('div', { class: 'drill-list' });
      data.rows.forEach(function (row) { list.appendChild(drillRowNode(row)); });
      bodyEl.appendChild(list);
      if (data.related) { renderRelated(bodyEl, data.related); }
    }).catch(function (e) {
      if (mySeq !== DRILL.seq) { return; }
      subEl.textContent = '';
      bodyEl.textContent = '';
      bodyEl.appendChild(h('div', { class: 'empty',
        text: 'Could not reach the live data API. Serve this dashboard with '
          + '"python3 api_server.py" (not a plain static file server) to enable click-to-inspect. (' + e + ')' }));
    });
  };

  DRILL.close = function () {
    DRILL.seq++; // invalidate any in-flight fetch so it can't repaint after close
    var panel = document.getElementById('drill-panel');
    var backdrop = document.getElementById('drill-backdrop');
    var bodyEl = document.getElementById('drill-body');
    if (panel) { panel.classList.remove('on'); panel.setAttribute('aria-hidden', 'true'); }
    if (backdrop) { backdrop.classList.remove('on'); }
    if (bodyEl) { bodyEl.textContent = ''; } // release rows, nothing kept in memory once closed
  };

  function drillRowNode(row) {
    var head = h('div', { class: 'dr-head' }, [
      h('span', { class: 'dr-plat', text: row.platform || '—' }),
      h('span', { class: 'dr-day', text: row.posted_at ? String(row.posted_at).slice(0, 16).replace('T', ' ') : '' })
    ]);
    var metaBits = [];
    if (row.author_name) { metaBits.push(row.author_name); }
    if (row.surface_form) { metaBits.push('mentions "' + row.surface_form + '"'); }
    if (row.entity_subtype) { metaBits.push(subtypeName(row.entity_subtype)); }
    if (isNum(row.confidence)) { metaBits.push('confidence ' + fmt(row.confidence, 2)); }
    if (isNum(row.score)) { metaBits.push('score ' + fmt(row.score, 2)); }
    if (isNum(row.similarity)) { metaBits.push('similarity ' + fmt(row.similarity, 2)); }
    if (row.gray_zone) { metaBits.push('gray-zone'); }
    var wrap = h('div', { class: 'dr-row' }, [head]);
    if (metaBits.length) { wrap.appendChild(h('div', { class: 'dr-meta', text: metaBits.join(' · ') })); }
    wrap.appendChild(h('div', { class: 'dr-text', text: row.text || '(no text captured)' }));
    if (row.url) {
      var a = h('a', { class: 'dr-link', text: 'Open source ↗' });
      a.setAttribute('href', row.url); a.setAttribute('target', '_blank'); a.setAttribute('rel', 'noopener noreferrer');
      wrap.appendChild(a);
    }
    return wrap;
  }

  /* "Related" section appended under the posts list in the drill panel --
     co-occurring entities/categories/topics (chips, click drills into that
     item) and a few similar posts (content-based, single-seed cosine
     similarity -- see api_server.py::compute_related). Renders nothing if
     `related` came back empty; a failed related-items lookup never blocks
     the posts list above it from showing (see DRILL.open). */
  function relatedChip(label, drill, tipRows) {
    var chip = h('button', { class: 'chip', type: 'button', text: label });
    attachDrill(chip, drill);
    if (tipRows) {
      chip.addEventListener('mousemove', function (ev) {
        TIP.show(ev, tipNode(tipRows.head, tipRows.rows, tipRows.note));
      });
      chip.addEventListener('mouseleave', TIP.hide);
    }
    return chip;
  }

  /* The chip shows co-occurring post count; the RANKING is by a shrunk lift.
     Surface both on hover so the ordering is never a mystery number. */
  function relatedTip(name, d) {
    return {
      head: name,
      rows: [
        { k: 'co-occurring posts', color: K.focal, v: fmtInt(d.co_posts) },
        { k: 'posts corpus-wide', v: fmtInt(d.global_posts) },
        { k: 'association score', v: isNum(d.score) ? fmt(d.score, 2) + '×' : '—' }
      ],
      note: 'Score is how much more often this appears here than in the corpus overall '
        + '(shrunk toward 1× when the overlap is small). Ranked by score, not raw count.'
    };
  }

  function renderRelated(host, related) {
    var entities = related.entities || [], categories = related.categories || [],
      topics = related.topics || [], similar = related.similar_posts || [];
    if (!entities.length && !categories.length && !topics.length && !similar.length) { return; }

    var section = h('div', { class: 'related-section' });
    section.appendChild(h('div', { class: 'related-hd', text: 'Related' }));

    if (entities.length) {
      section.appendChild(h('div', { class: 'related-lbl', text: 'Entities that also appear here' }));
      var eRow = h('div', { class: 'related-chips' });
      entities.forEach(function (e) {
        eRow.appendChild(relatedChip(e.name + ' (' + fmtInt(e.co_posts) + ' posts)',
          { view: 'entity', params: { entity_uid: e.entity_uid }, title: e.name },
          relatedTip(e.name, e)));
      });
      section.appendChild(eRow);
    }

    if (categories.length) {
      section.appendChild(h('div', { class: 'related-lbl', text: 'Related categories' }));
      var cRow = h('div', { class: 'related-chips' });
      categories.forEach(function (c) {
        var name = dimName(c.dim_type, c.category);
        cRow.appendChild(relatedChip(name + ' (' + fmtInt(c.co_posts) + ' posts)',
          dimDrill(c.dim_type, c.category, name),
          relatedTip(name, c)));
      });
      section.appendChild(cRow);
    }

    if (topics.length) {
      section.appendChild(h('div', { class: 'related-lbl', text: 'Related topics' }));
      var tRow = h('div', { class: 'related-chips' });
      topics.forEach(function (t) {
        tRow.appendChild(relatedChip(t.name + ' (' + fmtInt(t.co_posts) + ' posts)',
          { view: 'topic_v3', params: { cluster_id: t.cluster_id }, title: t.name },
          relatedTip(t.name, t)));
      });
      section.appendChild(tRow);
    }

    if (similar.length) {
      section.appendChild(h('div', { class: 'related-lbl', text: 'Similar posts' }));
      var sList = h('div', { class: 'drill-list' });
      similar.forEach(function (row) { sList.appendChild(drillRowNode(row)); });
      section.appendChild(sList);
    }

    host.appendChild(section);
  }

  function initDrillChrome() {
    var closeBtn = document.getElementById('drill-close');
    var backdrop = document.getElementById('drill-backdrop');
    if (closeBtn) { closeBtn.addEventListener('click', DRILL.close); }
    if (backdrop) { backdrop.addEventListener('click', DRILL.close); }
    document.addEventListener('keydown', function (ev) {
      if (ev.key === 'Escape') { DRILL.close(); }
    });
  }

  /* Attach a click-to-inspect handler to an already-built SVG/DOM node.
     `drill` is {view, params, title, subtitle} or falsy (no-op, so callers
     can pass a datum's optional drill field unconditionally). */
  function attachDrill(node, drill) {
    if (!drill || !node) { return; }
    node.classList.add('clk');
    if (!node.hasAttribute('tabindex')) { node.setAttribute('tabindex', '0'); }
    node.setAttribute('role', 'button');
    node.addEventListener('click', function (ev) {
      ev.stopPropagation();
      DRILL.open(drill);
    });
    node.addEventListener('keydown', function (ev) {
      if (ev.key === 'Enter' || ev.key === ' ') { ev.preventDefault(); DRILL.open(drill); }
    });
  }

  /* ------------------------------------------------------------ scales ---- */

  function linScale(d0, d1, r0, r1) {
    var span = (d1 - d0) || 1;
    function f(v) { return r0 + (v - d0) / span * (r1 - r0); }
    f.d0 = d0; f.d1 = d1; f.r0 = r0; f.r1 = r1;
    f.inv = function (p) { return d0 + (p - r0) / ((r1 - r0) || 1) * span; };
    return f;
  }

  function niceTicks(d0, d1, n) {
    if (!isFinite(d0) || !isFinite(d1)) { return [0]; }
    if (d0 === d1) { return [d0]; }
    n = n || 5;
    var raw = (d1 - d0) / n;
    var mag = Math.pow(10, Math.floor(Math.log(raw) / Math.LN10));
    var norm = raw / mag, step;
    if (norm < 1.5) { step = 1; } else if (norm < 3) { step = 2; }
    else if (norm < 7) { step = 5; } else { step = 10; }
    step *= mag;
    var out = [], s = Math.ceil(d0 / step) * step;
    for (var v = s; v <= d1 + step * 1e-6; v += step) { out.push(Math.abs(v) < step * 1e-9 ? 0 : v); }
    return out.length ? out : [d0, d1];
  }

  function extent(arr) {
    var lo = Infinity, hi = -Infinity;
    for (var i = 0; i < arr.length; i++) {
      var v = arr[i];
      if (isNum(v)) { if (v < lo) { lo = v; } if (v > hi) { hi = v; } }
    }
    if (lo === Infinity) { return null; }
    return [lo, hi];
  }

  function padDomain(ex, opts) {
    if (!ex) { return [0, 1]; }
    var lo = ex[0], hi = ex[1];
    if (opts && opts.zeroBase && lo > 0) { lo = 0; }
    if (opts && opts.symmetric) { var m = Math.max(Math.abs(lo), Math.abs(hi)) || 1; lo = -m; hi = m; }
    if (lo === hi) { lo -= Math.abs(lo) * 0.1 + 1; hi += Math.abs(hi) * 0.1 + 1; }
    else { var p = (hi - lo) * 0.08; lo -= p; hi += p; }
    if (opts && opts.zeroBase && ex[0] >= 0) { lo = 0; }
    return [lo, hi];
  }

  /* ------------------------------------------------------------- frame ---- */

  function frame(host, cfg) {
    var m = cfg.margin || { t: 10, r: 14, b: 22, l: 40 };
    var w = cfg.width, ht = cfg.height;
    var s = el('svg', { width: w, height: ht, viewBox: '0 0 ' + w + ' ' + ht });
    if (cfg.title) {
      var ti = el('title'); ti.textContent = cfg.title; s.appendChild(ti);
    }
    host.appendChild(s);
    var g = el('g', { transform: 'translate(' + m.l + ',' + m.t + ')' });
    s.appendChild(g);
    return {
      svg: s, g: g, m: m, w: w, h: ht,
      iw: Math.max(10, w - m.l - m.r),
      ih: Math.max(10, ht - m.t - m.b)
    };
  }

  function yAxis(F, y, o) {
    o = o || {};
    var tks = niceTicks(y.d0, y.d1, o.count || 4);
    tks.forEach(function (v) {
      var yy = y(v);
      if (yy < -1 || yy > F.ih + 1) { return; }
      F.g.appendChild(el('line', { x1: 0, x2: F.iw, y1: yy, y2: yy,
        stroke: (v === 0 && o.zeroEmph) ? K.axis : K.grid, 'stroke-width': 1 }));
      F.g.appendChild(svgText(-6, yy, o.fmt ? o.fmt(v) : fmtCompact(v),
        { anchor: 'end', baseline: 'middle', size: o.size || 9.5, tabular: true, fill: K.ink3 }));
    });
    if (o.label) {
      var t = svgText(0, 0, o.label, { size: 9, fill: K.ink3 });
      t.setAttribute('transform', 'translate(' + (-F.m.l + 9) + ',' + (F.ih / 2) + ') rotate(-90)');
      t.setAttribute('text-anchor', 'middle');
      F.g.appendChild(t);
    }
  }

  function xDayAxis(F, x, days, o) {
    o = o || {};
    F.g.appendChild(el('line', { x1: 0, x2: F.iw, y1: F.ih, y2: F.ih, stroke: K.axis, 'stroke-width': 1 }));
    var want = Math.max(2, Math.min(o.count || 5, days.length));
    var step = Math.max(1, Math.floor((days.length - 1) / (want - 1)));
    for (var i = 0; i < days.length; i += step) {
      var xx = x(i);
      F.g.appendChild(el('line', { x1: xx, x2: xx, y1: F.ih, y2: F.ih + 3, stroke: K.axis, 'stroke-width': 1 }));
      F.g.appendChild(svgText(xx, F.ih + 13, fmtDay(days[i]),
        { anchor: i === 0 ? 'start' : 'middle', size: o.size || 9, tabular: true, fill: K.ink3 }));
    }
  }

  function hatchPattern(svg) {
    var id = 'hx' + (patSeq++);
    var defs = el('defs');
    var p = el('pattern', { id: id, width: 5, height: 5, patternUnits: 'userSpaceOnUse',
      patternTransform: 'rotate(45)' });
    p.appendChild(el('rect', { width: 5, height: 5, fill: K.sunken }));
    p.appendChild(el('line', { x1: 0, y1: 0, x2: 0, y2: 5, stroke: K.axis, 'stroke-width': 1 }));
    defs.appendChild(p);
    svg.insertBefore(defs, svg.firstChild);
    return 'url(#' + id + ')';
  }

  /* ------------------------------------------------------------ legend ---- */

  function legend(host, items) {
    var l = h('div', { class: 'legend' });
    items.forEach(function (it) {
      var sw = h('span', { class: 'swatch' + (it.box ? ' box' : '') + (it.hatch ? ' hatch' : '') });
      if (!it.hatch) { sw.style.background = it.color; }
      if (it.hatch) {
        sw.style.background = 'repeating-linear-gradient(45deg,' + K.axis + ' 0 1px,transparent 1px 4px)';
      }
      l.appendChild(h('span', { class: 'item' }, [sw, txt(it.label)]));
    });
    host.appendChild(l);
    return l;
  }

  function rampLegend(host, lo, hi, ramp, label, fmtFn) {
    var box = h('div', { class: 'ramp-legend' });
    if (label) { box.appendChild(h('span', { text: label })); }
    box.appendChild(h('span', { text: fmtFn ? fmtFn(lo) : fmt(lo, 1), class: 'lo' }));
    var steps = h('div', { class: 'steps' });
    ramp.forEach(function (c) { var i = h('i'); i.style.background = c; steps.appendChild(i); });
    box.appendChild(steps);
    box.appendChild(h('span', { text: fmtFn ? fmtFn(hi) : fmt(hi, 1) }));
    host.appendChild(box);
  }

  /* -------------------------------------------------------- table view ---- */

  function tableView(host, cols, rows, opts) {
    opts = opts || {};
    var d = h('details', { class: 'tv' });
    if (opts.open) { d.open = true; }
    d.appendChild(h('summary', { text: opts.label || ('Table view — ' + rows.length + ' rows') }));
    if (!rows.length) {
      d.appendChild(h('div', { class: 'empty', text: 'No rows in the current selection.' }));
      host.appendChild(d);
      return d;
    }
    var wrap = h('div', { class: 'tbl-h' });
    var t = h('table');
    var thead = h('thead'), tr = h('tr');
    cols.forEach(function (c) { tr.appendChild(h('th', { class: c.num ? 'num' : '', text: c.label })); });
    thead.appendChild(tr); t.appendChild(thead);
    var tb = h('tbody');
    rows.slice(0, opts.max || 400).forEach(function (r) {
      var row = h('tr');
      cols.forEach(function (c) {
        var raw = c.get ? c.get(r) : r[c.k];
        var s = c.fmt ? c.fmt(raw, r) : (raw == null ? '—' : String(raw));
        var cls = [];
        if (c.num) { cls.push('num'); }
        if (c.wrap) { cls.push('wrap'); }
        if (c.strong) { cls.push('strong'); }
        if (s === '—') { cls.push('nil'); }
        var td = h('td', { class: cls.join(' ') });
        if (c.node) { td.appendChild(c.node(raw, r)); } else { td.textContent = s; }
        row.appendChild(td);
      });
      if (opts.rowDrill) { attachDrill(row, opts.rowDrill(r)); }
      tb.appendChild(row);
    });
    t.appendChild(tb);
    wrap.appendChild(t);
    d.appendChild(wrap);
    if (rows.length > (opts.max || 400)) {
      d.appendChild(h('div', { class: 'fnote',
        text: 'Showing first ' + (opts.max || 400) + ' of ' + rows.length + ' rows. The full set is in UI/data/metrics.json.' }));
    }
    host.appendChild(d);
    return d;
  }

  function empty(host, msg) {
    host.appendChild(h('div', { class: 'empty', text: msg }));
  }

  /* --------------------------------------------------------- linechart ---- */
  /* series: [{label, pts:[{i, v}], role:'focal'|'context', kind:'line'|'area'|'step'}] */

  function lineChart(host, cfg) {
    var days = cfg.days;
    if (!days.length) { empty(host, cfg.emptyMsg || 'No days in range.'); return; }
    var all = [];
    cfg.series.forEach(function (s) { s.pts.forEach(function (p) { all.push(p.v); }); });
    if (!extent(all)) { empty(host, cfg.emptyMsg || 'No supported values — every cell failed its support gate.'); return; }

    var w = cfg.width, ht = cfg.height || 150;
    var m = cfg.margin || { t: 8, r: cfg.labelEnds ? 58 : 10, b: 22, l: 40 };
    var F = frame(host, { width: w, height: ht, margin: m, title: cfg.title });
    var dom = cfg.domain || padDomain(extent(all), { zeroBase: cfg.zeroBase, symmetric: cfg.symmetric });
    var y = linScale(dom[0], dom[1], F.ih, 0);
    var x = linScale(0, Math.max(days.length - 1, 1), 0, F.iw);

    yAxis(F, y, { count: cfg.yTicks || 3, fmt: cfg.yFmt, zeroEmph: true, label: cfg.yLabel });
    xDayAxis(F, x, days, { count: cfg.xTicks || 4 });

    if (cfg.zeroLine && dom[0] < 0 && dom[1] > 0) {
      F.g.appendChild(el('line', { x1: 0, x2: F.iw, y1: y(0), y2: y(0),
        stroke: K.axis, 'stroke-width': 1 }));
      F.g.appendChild(svgText(F.iw, y(0) - 3, '0', { anchor: 'end', size: 8.5, fill: K.ink3 }));
    }

    /* burst ribbon: a separate ordered channel under the plot, not a series */
    if (cfg.ribbon && cfg.ribbon.length) {
      var rh = 4;
      cfg.ribbon.forEach(function (b) {
        if (!b.on) { return; }
        var x0 = x(b.i) - (F.iw / Math.max(days.length, 1)) / 2;
        F.g.appendChild(el('rect', { x: Math.max(0, x0), y: F.ih + 4,
          width: Math.max(1.5, F.iw / Math.max(days.length, 1)), height: rh,
          fill: K.focal, opacity: 0.85 }));
      });
    }

    cfg.series.forEach(function (s) {
      var focal = s.role !== 'context';
      var col = focal ? K.focal : K.context;
      var segs = [], cur = [];
      s.pts.forEach(function (p) {
        if (isNum(p.v)) { cur.push(p); } else if (cur.length) { segs.push(cur); cur = []; }
      });
      if (cur.length) { segs.push(cur); }

      if (s.kind === 'area') {
        segs.forEach(function (sg) {
          if (sg.length < 2) { return; }
          var d = 'M' + x(sg[0].i) + ',' + y(Math.max(dom[0], 0));
          sg.forEach(function (p) { d += 'L' + x(p.i) + ',' + y(p.v); });
          d += 'L' + x(sg[sg.length - 1].i) + ',' + y(Math.max(dom[0], 0)) + 'Z';
          F.g.appendChild(el('path', { d: d, fill: focal ? K.focalSoft : K.contextSoft, stroke: 'none' }));
        });
      }
      if (s.kind === 'bar') {
        var bw = Math.max(1.2, Math.min(18, F.iw / Math.max(days.length, 1) - 2));
        s.pts.forEach(function (p) {
          if (!isNum(p.v)) { return; }
          var y0 = y(0), y1 = y(p.v);
          F.g.appendChild(el('rect', { x: x(p.i) - bw / 2, y: Math.min(y0, y1),
            width: bw, height: Math.max(1, Math.abs(y1 - y0)), rx: 1,
            fill: p.v < 0 ? K.neg : col }));
        });
      } else {
        segs.forEach(function (sg) {
          if (sg.length === 1) {
            F.g.appendChild(el('circle', { cx: x(sg[0].i), cy: y(sg[0].v), r: 2.6, fill: col }));
            return;
          }
          var d = 'M' + sg.map(function (p) { return x(p.i) + ',' + y(p.v); }).join('L');
          F.g.appendChild(el('path', { d: d, fill: 'none', stroke: col, 'stroke-width': 2,
            'stroke-linejoin': 'round', 'stroke-linecap': 'round' }));
        });
      }

      var last = null;
      for (var i = s.pts.length - 1; i >= 0; i--) { if (isNum(s.pts[i].v)) { last = s.pts[i]; break; } }
      if (last && s.kind !== 'bar') {
        F.g.appendChild(el('circle', { cx: x(last.i), cy: y(last.v), r: 4,
          fill: col, stroke: K.sunken, 'stroke-width': 2 }));
        if (cfg.labelEnds) {
          F.g.appendChild(svgText(x(last.i) + 8, y(last.v), s.label,
            { size: 9, baseline: 'middle', fill: K.ink2 }));
        }
      }
    });

    /* crosshair */
    var xh = el('line', { x1: 0, x2: 0, y1: 0, y2: F.ih, stroke: K.goldHair,
      'stroke-width': 1, opacity: 0 });
    F.g.appendChild(xh);
    var hit = el('rect', { x: 0, y: 0, width: F.iw, height: F.ih, fill: 'transparent' });
    F.g.appendChild(hit);
    function move(ev) {
      var r = F.svg.getBoundingClientRect();
      var px = ev.clientX - r.left - F.m.l;
      var i = Math.round(x.inv(Math.max(0, Math.min(F.iw, px))));
      i = Math.max(0, Math.min(days.length - 1, i));
      xh.setAttribute('x1', x(i)); xh.setAttribute('x2', x(i));
      xh.setAttribute('opacity', 1);
      var rows = cfg.series.map(function (s) {
        var p = s.pts[i];
        return { k: s.label, color: s.role === 'context' ? K.context : K.focal,
          v: (p && isNum(p.v)) ? (cfg.tipFmt ? cfg.tipFmt(p.v) : fmt(p.v, 3)) : 'no value (gated)' };
      });
      if (cfg.extraRows) { rows = rows.concat(cfg.extraRows(i)); }
      TIP.show(ev, tipNode((cfg.tipHead ? cfg.tipHead + ' · ' : '') + fmtDayFull(days[i]),
        rows, cfg.tipNote));
    }
    hit.addEventListener('pointermove', move);
    hit.addEventListener('pointerleave', function () { xh.setAttribute('opacity', 0); TIP.hide(); });
    return F;
  }

  /* ------------------------------------------------------ small multiple -- */

  function smallMultiples(host, cfg) {
    if (!cfg.facets.length) { empty(host, cfg.emptyMsg || 'Nothing to show for the current filters.'); return; }
    var grid = h('div', { class: 'grid ' + (cfg.cols === 2 ? 'g2' : 'g3') });
    host.appendChild(grid);
    var shared = null;
    if (cfg.shareY) {
      var all = [];
      cfg.facets.forEach(function (f) {
        f.series.forEach(function (s) { s.pts.forEach(function (p) { all.push(p.v); }); });
      });
      var ex = extent(all);
      if (ex) { shared = padDomain(ex, { zeroBase: cfg.zeroBase, symmetric: cfg.symmetric }); }
    }
    cfg.facets.forEach(function (f) {
      var cell = h('div', { class: 'facet', id: f.id || null });
      var head = h('div', { class: 'facet-t' }, [h('span', { text: f.title })]);
      if (f.note) { head.appendChild(h('span', { class: 'n', text: f.note })); }
      attachDrill(head, f.drill);
      cell.appendChild(head);
      var plot = h('div', { class: 'plot' });
      cell.appendChild(plot);
      grid.appendChild(cell);
      var w = Math.max(180, plot.clientWidth || cell.clientWidth - 20);
      lineChart(plot, {
        days: cfg.days, series: f.series, width: w, height: cfg.height || 118,
        domain: shared, zeroBase: cfg.zeroBase, symmetric: cfg.symmetric,
        zeroLine: cfg.zeroLine, yFmt: cfg.yFmt, tipFmt: cfg.tipFmt,
        tipHead: f.title, tipNote: cfg.tipNote, yTicks: 3, xTicks: 3,
        ribbon: f.ribbon, emptyMsg: cfg.facetEmptyMsg,
        margin: { t: 8, r: 10, b: f.ribbon ? 30 : 22, l: 36 },
        extraRows: f.extraRows
      });
      if (f.sub) { cell.appendChild(h('div', { class: 'fnote', text: f.sub })); }
    });
  }

  /* --------------------------------------------------------- bar charts --- */

  function hBars(host, cfg) {
    var items = cfg.items.filter(function (d) { return isNum(d.value); });
    if (!items.length) { empty(host, cfg.emptyMsg || 'No supported values in the current selection.'); return; }
    var rowH = cfg.rowH || 20;
    var labelW = cfg.labelW || 150;
    var m = { t: 6, r: 52, b: 20, l: labelW };
    var ht = items.length * rowH + m.t + m.b;
    var F = frame(host, { width: cfg.width, height: ht, margin: m, title: cfg.title });
    var ex = extent(items.map(function (d) { return d.value; }));
    var dom = cfg.diverging ? padDomain(ex, { symmetric: true }) : padDomain(ex, { zeroBase: true });
    if (!cfg.diverging && dom[0] < 0) { dom = padDomain(ex, { symmetric: true }); }
    var x = linScale(dom[0], dom[1], 0, F.iw);
    var zero = x(0);

    niceTicks(dom[0], dom[1], 4).forEach(function (v) {
      F.g.appendChild(el('line', { x1: x(v), x2: x(v), y1: 0, y2: items.length * rowH,
        stroke: v === 0 ? K.axis : K.grid, 'stroke-width': 1 }));
      F.g.appendChild(svgText(x(v), items.length * rowH + 12, cfg.xFmt ? cfg.xFmt(v) : fmt(v, 2),
        { anchor: 'middle', size: 9, tabular: true }));
    });

    items.forEach(function (d, i) {
      var yTop = i * rowH + 3, bh = Math.min(14, rowH - 6);
      var x0 = Math.min(zero, x(d.value)), x1 = Math.max(zero, x(d.value));
      var col = cfg.diverging ? (d.value < 0 ? K.neg : K.focal)
        : (d.dim ? K.context : K.focal);
      var g = el('g');
      g.appendChild(el('rect', { x: x0, y: yTop, width: Math.max(1.5, x1 - x0), height: bh,
        rx: 2, fill: col }));
      g.appendChild(svgText(-8, yTop + bh / 2, d.label,
        { anchor: 'end', baseline: 'middle', size: 9.5, fill: K.ink2 }));
      var vx = d.value < 0 ? x0 - 6 : x1 + 6;
      g.appendChild(svgText(vx, yTop + bh / 2, cfg.vFmt ? cfg.vFmt(d.value) : fmt(d.value, 2),
        { anchor: d.value < 0 ? 'end' : 'start', baseline: 'middle', size: 9, tabular: true, fill: K.ink2 }));
      hoverable(g, function () { return d.label; },
        function () {
          var r = [{ k: cfg.measure || 'value', color: col, v: cfg.vFmt ? cfg.vFmt(d.value) : fmt(d.value, 3) }];
          return r.concat(d.rows || []);
        }, function () { return d.note || cfg.tipNote; });
      attachDrill(g, d.drill);
      F.g.appendChild(g);
    });
    return F;
  }

  function vBars(host, cfg) {
    var items = cfg.items;
    if (!items.length) { empty(host, cfg.emptyMsg || 'Nothing to plot.'); return; }
    var m = { t: 10, r: 8, b: 30, l: 38 };
    var F = frame(host, { width: cfg.width, height: cfg.height || 130, margin: m, title: cfg.title });
    var ex = extent(items.map(function (d) { return d.value; })) || [0, 1];
    var dom = padDomain(ex, { zeroBase: true });
    var y = linScale(dom[0], dom[1], F.ih, 0);
    yAxis(F, y, { count: 3, fmt: cfg.yFmt });
    var step = F.iw / items.length;
    var bw = Math.max(1.5, Math.min(cfg.maxBar || 24, step - 2));
    items.forEach(function (d, i) {
      var cx = i * step + step / 2;
      if (isNum(d.value)) {
        var y1 = y(d.value), y0 = y(0);
        var g = el('g');
        g.appendChild(el('rect', { x: cx - bw / 2, y: Math.min(y0, y1), width: bw,
          height: Math.max(1, Math.abs(y1 - y0)), rx: 2, fill: d.color || K.focal }));
        hoverable(g, function () { return d.label; },
          function () { return [{ k: cfg.measure || 'value', color: d.color || K.focal,
            v: cfg.vFmt ? cfg.vFmt(d.value) : fmtInt(d.value) }].concat(d.rows || []); },
          function () { return cfg.tipNote; });
        attachDrill(g, d.drill);
        F.g.appendChild(g);
      }
      if (cfg.everyLabel || i % Math.ceil(items.length / (cfg.labelCount || 7)) === 0) {
        F.g.appendChild(svgText(cx, F.ih + 13, d.short || d.label,
          { anchor: 'middle', size: 8.5, tabular: true }));
      }
    });
    F.g.appendChild(el('line', { x1: 0, x2: F.iw, y1: y(0), y2: y(0), stroke: K.axis, 'stroke-width': 1 }));
    return F;
  }

  /* ----------------------------------------------------------- heatmap ---- */

  function heatmap(host, cfg) {
    var rowsL = cfg.rows, colsL = cfg.cols;
    if (!rowsL.length || !colsL.length) { empty(host, cfg.emptyMsg || 'No cells in the current selection.'); return; }
    var cw = cfg.cellW || 13, ch = cfg.cellH || 20, gap = 2;
    var labelW = cfg.labelW || 168;
    var m = { t: 6, r: 10, b: 24, l: labelW };
    var w = labelW + colsL.length * cw + m.r;
    var ht = m.t + rowsL.length * ch + m.b;
    var scroll = h('div', { class: 'scrollx' });
    host.appendChild(scroll);
    var F = frame(scroll, { width: w, height: ht, margin: m, title: cfg.title });
    var hatch = hatchPattern(F.svg);

    rowsL.forEach(function (r, ri) {
      F.g.appendChild(svgText(-8, ri * ch + ch / 2, r.label,
        { anchor: 'end', baseline: 'middle', size: 9.5, fill: K.ink2 }));
      colsL.forEach(function (c, ci) {
        var d = cfg.get(r, c);
        var x = ci * cw, y = ri * ch;
        var col = d && isNum(d.v) ? cfg.color(d.v) : null;
        var rect = el('rect', { x: x, y: y, width: Math.max(1, cw - gap), height: Math.max(1, ch - gap),
          fill: col || hatch, stroke: 'none' });
        F.g.appendChild(rect);
        if (d && d.ring) {
          F.g.appendChild(el('rect', { x: x - 0.5, y: y - 0.5, width: cw - gap + 1, height: ch - gap + 1,
            fill: 'none', stroke: K.ink, 'stroke-width': 1.2 }));
        }
        var hitR = el('rect', { x: x - 1, y: y - 1, width: cw + 1, height: ch + 1, fill: 'transparent' });
        F.g.appendChild(hitR);
        hoverable(hitR, function () { return r.label + ' · ' + c.label; },
          function () { return (d && d.rows) || [{ k: cfg.measure || 'value', color: col || K.context,
            v: d && isNum(d.v) ? fmt(d.v, 3) : 'no value (support gate not met)' }]; },
          function () { return (d && d.note) || cfg.tipNote; });
        attachDrill(hitR, d && d.drill);
      });
    });

    var want = Math.max(2, Math.min(cfg.xLabelCount || 7, colsL.length));
    var step = Math.max(1, Math.floor((colsL.length - 1) / (want - 1)));
    for (var ci = 0; ci < colsL.length; ci += step) {
      F.g.appendChild(svgText(ci * cw + (cw - gap) / 2, rowsL.length * ch + 12, colsL[ci].short || colsL[ci].label,
        { anchor: 'middle', size: 8.5, tabular: true }));
    }
    return F;
  }

  /* ------------------------------------------------------------- gantt ---- */

  function gantt(host, cfg) {
    if (!cfg.rows.length) { empty(host, cfg.emptyMsg || 'No episodes detected in the current selection.'); return; }
    var rowH = 22, labelW = cfg.labelW || 200;
    var m = { t: 6, r: 16, b: 24, l: labelW };
    var inner = Math.max(280, (cfg.width || 700) - labelW - m.r);
    var w = labelW + inner + m.r;
    var ht = m.t + cfg.rows.length * rowH + m.b;
    var scroll = h('div', { class: 'scrollx' });
    host.appendChild(scroll);
    var F = frame(scroll, { width: w, height: ht, margin: m, title: cfg.title });
    var days = cfg.days;
    var x = linScale(0, Math.max(days.length - 1, 1), 0, F.iw);
    var idx = {}; days.forEach(function (d, i) { idx[d] = i; });

    var want = Math.max(2, Math.min(7, days.length));
    var st = Math.max(1, Math.floor((days.length - 1) / (want - 1)));
    for (var i = 0; i < days.length; i += st) {
      F.g.appendChild(el('line', { x1: x(i), x2: x(i), y1: 0, y2: cfg.rows.length * rowH,
        stroke: K.grid, 'stroke-width': 1 }));
      F.g.appendChild(svgText(x(i), cfg.rows.length * rowH + 13, fmtDay(days[i]),
        { anchor: 'middle', size: 8.5, tabular: true }));
    }

    cfg.rows.forEach(function (r, ri) {
      var y = ri * rowH + rowH / 2;
      F.g.appendChild(svgText(-8, y, r.label, { anchor: 'end', baseline: 'middle', size: 9.5, fill: K.ink2 }));
      var i0 = idx[r.start], i1 = idx[r.end];
      if (i0 == null) { i0 = 0; }
      if (i1 == null) { i1 = days.length - 1; }
      var x0 = x(i0), x1 = x(i1);
      var g = el('g');
      g.appendChild(el('rect', { x: x0, y: y - 5, width: Math.max(3, x1 - x0), height: 10, rx: 2,
        fill: r.color || K.focal, opacity: 0.55 }));
      if (idx[r.peak] != null) {
        g.appendChild(el('circle', { cx: x(idx[r.peak]), cy: y, r: 4.5, fill: r.color || K.focal,
          stroke: K.sunken, 'stroke-width': 2 }));
      }
      var hit = el('rect', { x: Math.max(0, x0 - 6), y: y - 10, width: Math.max(16, x1 - x0 + 12),
        height: 20, fill: 'transparent' });
      g.appendChild(hit);
      hoverable(hit, function () { return r.label; }, function () { return r.rows; },
        function () { return r.note; });
      attachDrill(hit, r.drill);
      F.g.appendChild(g);
    });
    return F;
  }

  /* ---------------------------------------------------------- swimlane ---- */
  /* One row per category/entity, one dot per day it fired -- a gantt without
     the interval (point events, not episodes), shared with any day axis. */

  function swimlane(host, cfg) {
    if (!cfg.rows.length) { empty(host, cfg.emptyMsg || 'No rows to show.'); return; }
    var rowH = cfg.rowH || 20, labelW = cfg.labelW || 190;
    var m = { t: 6, r: 10, b: 24, l: labelW };
    var inner = Math.max(240, (cfg.width || 700) - labelW - m.r);
    var w = labelW + inner + m.r;
    var ht = m.t + cfg.rows.length * rowH + m.b;
    var scroll = h('div', { class: 'scrollx' });
    host.appendChild(scroll);
    var F = frame(scroll, { width: w, height: ht, margin: m, title: cfg.title });
    var days = cfg.days;
    var x = linScale(0, Math.max(days.length - 1, 1), 0, F.iw);
    var idx = {}; days.forEach(function (d, i) { idx[d] = i; });

    var want = Math.max(2, Math.min(7, days.length));
    var st = Math.max(1, Math.floor((days.length - 1) / (want - 1)));
    for (var i = 0; i < days.length; i += st) {
      F.g.appendChild(el('line', { x1: x(i), x2: x(i), y1: 0, y2: cfg.rows.length * rowH,
        stroke: K.grid, 'stroke-width': 1 }));
      F.g.appendChild(svgText(x(i), cfg.rows.length * rowH + 13, fmtDay(days[i]),
        { anchor: 'middle', size: 8.5, tabular: true }));
    }
    var byRow = {};
    cfg.rows.forEach(function (r, ri) {
      byRow[r.id] = ri;
      var y0 = ri * rowH + rowH / 2;
      F.g.appendChild(svgText(-8, y0, r.label, { anchor: 'end', baseline: 'middle', size: 9.5, fill: K.ink2 }));
      F.g.appendChild(el('line', { x1: 0, x2: F.iw, y1: y0, y2: y0, stroke: K.grid, 'stroke-width': 1 }));
    });
    (cfg.points || []).forEach(function (p) {
      var ri = byRow[p.rowId], di = idx[p.day];
      if (ri == null || di == null) { return; }
      var cx = x(di), cy = ri * rowH + rowH / 2;
      var g = el('g');
      g.appendChild(el('circle', { cx: cx, cy: cy, r: p.r || 5, fill: p.color || K.focal,
        stroke: K.surface, 'stroke-width': 1.5 }));
      var hit = el('circle', { cx: cx, cy: cy, r: 9, fill: 'transparent' });
      g.appendChild(hit);
      hoverable(hit, function () { return p.label; }, function () { return p.rows; },
        function () { return p.note; });
      attachDrill(hit, p.drill);
      F.g.appendChild(g);
    });
    return F;
  }

  /* ----------------------------------------------------------- dotplot ---- */

  function dotPlot(host, cfg) {
    var items = cfg.items.filter(function (d) { return isNum(d.value); });
    if (!items.length) { empty(host, cfg.emptyMsg || 'No fitted cells in the current selection.'); return; }
    var rowH = cfg.rowH || 19, labelW = cfg.labelW || 190;
    var m = { t: 8, r: 46, b: 22, l: labelW };
    var ht = m.t + items.length * rowH + m.b;
    var F = frame(host, { width: cfg.width, height: ht, margin: m, title: cfg.title });
    var vals = [];
    items.forEach(function (d) {
      vals.push(d.value);
      if (isNum(d.lo)) { vals.push(d.lo); }
      if (isNum(d.hi)) { vals.push(d.hi); }
    });
    var dom = cfg.domain || padDomain(extent(vals), {});
    var x = linScale(dom[0], dom[1], 0, F.iw);
    niceTicks(dom[0], dom[1], 4).forEach(function (v) {
      F.g.appendChild(el('line', { x1: x(v), x2: x(v), y1: 0, y2: items.length * rowH,
        stroke: Math.abs(v) < 1e-12 ? K.axis : K.grid, 'stroke-width': 1 }));
      F.g.appendChild(svgText(x(v), items.length * rowH + 12, fmt(v, 2),
        { anchor: 'middle', size: 9, tabular: true }));
    });
    if (isNum(cfg.refValue)) {
      F.g.appendChild(el('line', { x1: x(cfg.refValue), x2: x(cfg.refValue), y1: 0,
        y2: items.length * rowH, stroke: K.context, 'stroke-width': 1, 'stroke-dasharray': 'none' }));
    }
    items.forEach(function (d, i) {
      var y = i * rowH + rowH / 2;
      var g = el('g');
      F.g.appendChild(svgText(-8, y, d.label, { anchor: 'end', baseline: 'middle', size: 9.5, fill: K.ink2 }));
      if (isNum(d.lo) && isNum(d.hi)) {
        g.appendChild(el('line', { x1: x(d.lo), x2: x(d.hi), y1: y, y2: y,
          stroke: K.context, 'stroke-width': 2, 'stroke-linecap': 'round' }));
      }
      g.appendChild(el('circle', { cx: x(d.value), cy: y, r: 4.5,
        fill: d.dim ? K.context : K.focal, stroke: K.surface, 'stroke-width': 2 }));
      g.appendChild(svgText(F.iw + 6, y, cfg.vFmt ? cfg.vFmt(d.value) : fmt(d.value, 2),
        { anchor: 'start', baseline: 'middle', size: 9, tabular: true, fill: K.ink2 }));
      var hit = el('rect', { x: 0, y: y - rowH / 2, width: F.iw, height: rowH, fill: 'transparent' });
      g.appendChild(hit);
      hoverable(hit, function () { return d.label; }, function () { return d.rows; },
        function () { return d.note; });
      attachDrill(hit, d.drill);
      F.g.appendChild(g);
    });
    return F;
  }

  /* ---------------------------------------------------------- dumbbell ---- */

  function dumbbell(host, cfg) {
    var items = cfg.items;
    if (!items.length) { empty(host, cfg.emptyMsg || 'No values.'); return; }
    var rowH = 19, labelW = cfg.labelW || 190;
    var m = { t: 8, r: 52, b: 22, l: labelW };
    var ht = m.t + items.length * rowH + m.b;
    var F = frame(host, { width: cfg.width, height: ht, margin: m, title: cfg.title });
    var vals = [];
    items.forEach(function (d) { vals.push(d.a); vals.push(d.b); });
    var dom = cfg.domain || padDomain(extent(vals), { symmetric: cfg.symmetric });
    var x = linScale(dom[0], dom[1], 0, F.iw);
    niceTicks(dom[0], dom[1], 4).forEach(function (v) {
      F.g.appendChild(el('line', { x1: x(v), x2: x(v), y1: 0, y2: items.length * rowH,
        stroke: Math.abs(v) < 1e-12 ? K.axis : K.grid, 'stroke-width': 1 }));
      F.g.appendChild(svgText(x(v), items.length * rowH + 12, fmt(v, 2),
        { anchor: 'middle', size: 9, tabular: true }));
    });
    items.forEach(function (d, i) {
      var y = i * rowH + rowH / 2;
      F.g.appendChild(svgText(-8, y, d.label, { anchor: 'end', baseline: 'middle', size: 9.5, fill: K.ink2 }));
      var g = el('g');
      if (isNum(d.a) && isNum(d.b)) {
        g.appendChild(el('line', { x1: x(d.a), x2: x(d.b), y1: y, y2: y, stroke: K.axis, 'stroke-width': 1.5 }));
      }
      if (isNum(d.a)) {
        g.appendChild(el('circle', { cx: x(d.a), cy: y, r: 4, fill: K.context,
          stroke: K.surface, 'stroke-width': 2 }));
      }
      if (isNum(d.b)) {
        g.appendChild(el('circle', { cx: x(d.b), cy: y, r: 4.5, fill: K.focal,
          stroke: K.surface, 'stroke-width': 2 }));
      }
      g.appendChild(svgText(F.iw + 6, y, d.right || '', { anchor: 'start', baseline: 'middle',
        size: 9, tabular: true, fill: K.ink3 }));
      var hit = el('rect', { x: 0, y: y - rowH / 2, width: F.iw, height: rowH, fill: 'transparent' });
      g.appendChild(hit);
      hoverable(hit, function () { return d.label; }, function () { return d.rows; },
        function () { return d.note; });
      attachDrill(hit, d.drill);
      F.g.appendChild(g);
    });
    return F;
  }

  /* --------------------------------------------------------- histogram ---- */

  function histogram(host, cfg) {
    var vals = cfg.values.filter(isNum);
    if (vals.length < 2) { empty(host, cfg.emptyMsg || 'Fewer than 2 scored cells.'); return; }
    var n = cfg.bins || 10, lo = cfg.lo, hi = cfg.hi;
    if (lo == null) { var ex = extent(vals); lo = ex[0]; hi = ex[1]; }
    var counts = new Array(n).fill(0);
    vals.forEach(function (v) {
      var i = Math.min(n - 1, Math.max(0, Math.floor((v - lo) / ((hi - lo) || 1) * n)));
      counts[i]++;
    });
    var items = counts.map(function (c, i) {
      var b0 = lo + (hi - lo) * i / n, b1 = lo + (hi - lo) * (i + 1) / n;
      return { label: fmt(b0, 1) + '–' + fmt(b1, 1), short: (i % 2 === 0 ? fmt(b0, 1) : ''),
        value: c, rows: [{ k: 'cells', color: K.focal, v: fmtInt(c) },
          { k: 'range', v: fmt(b0, 2) + ' – ' + fmt(b1, 2) }] };
    });
    vBars(host, { items: items, width: cfg.width, height: cfg.height || 110,
      measure: 'cells', vFmt: fmtInt, yFmt: fmtInt, everyLabel: true, maxBar: 30,
      tipNote: cfg.tipNote });
  }

  /* ----------------------------------------------------------- scatter ---- */

  function scatter(host, cfg) {
    var pts = cfg.points.filter(function (p) { return isNum(p.x) && isNum(p.y); });
    if (!pts.length) { empty(host, cfg.emptyMsg || 'No cells with both measures present.'); return; }
    var m = { t: 10, r: 14, b: 34, l: 46 };
    var F = frame(host, { width: cfg.width, height: cfg.height || 260, margin: m, title: cfg.title });
    var dx = padDomain(extent(pts.map(function (p) { return p.x; })), { zeroBase: cfg.xZero });
    var dy = padDomain(extent(pts.map(function (p) { return p.y; })), { zeroBase: cfg.yZero });
    var x = linScale(dx[0], dx[1], 0, F.iw), y = linScale(dy[0], dy[1], F.ih, 0);
    yAxis(F, y, { count: 4, fmt: cfg.yFmt, label: cfg.yLabel });
    niceTicks(dx[0], dx[1], 5).forEach(function (v) {
      F.g.appendChild(el('line', { x1: x(v), x2: x(v), y1: 0, y2: F.ih, stroke: K.grid, 'stroke-width': 1 }));
      F.g.appendChild(svgText(x(v), F.ih + 13, cfg.xFmt ? cfg.xFmt(v) : fmt(v, 1),
        { anchor: 'middle', size: 9, tabular: true }));
    });
    if (cfg.xLabel) {
      F.g.appendChild(svgText(F.iw / 2, F.ih + 27, cfg.xLabel, { anchor: 'middle', size: 9, fill: K.ink3 }));
    }
    pts.forEach(function (p) {
      var g = el('g');
      if (p.flag) {
        g.appendChild(el('circle', { cx: x(p.x), cy: y(p.y), r: 7, fill: 'none',
          stroke: K.crit, 'stroke-width': 1.6 }));
        g.appendChild(el('path', {
          d: 'M' + (x(p.x) - 3) + ',' + (y(p.y) - 3) + 'L' + (x(p.x) + 3) + ',' + (y(p.y) + 3) +
             'M' + (x(p.x) + 3) + ',' + (y(p.y) - 3) + 'L' + (x(p.x) - 3) + ',' + (y(p.y) + 3),
          stroke: K.crit, 'stroke-width': 1.6 }));
      } else {
        g.appendChild(el('circle', { cx: x(p.x), cy: y(p.y), r: 4, fill: K.focal,
          opacity: 0.75, stroke: K.surface, 'stroke-width': 1.5 }));
      }
      var hit = el('circle', { cx: x(p.x), cy: y(p.y), r: 12, fill: 'transparent' });
      g.appendChild(hit);
      hoverable(hit, function () { return p.label; }, function () { return p.rows; },
        function () { return p.note; });
      attachDrill(hit, p.drill);
      F.g.appendChild(g);
    });
    return F;
  }

  /* ------------------------------------------------------- meter / bullet - */

  function meterRow(label, value, note, ok) {
    var row = h('div', { class: 'facet' });
    row.appendChild(h('div', { class: 'facet-t' }, [h('span', { text: label }),
      h('span', { class: 'n', text: fmtPct(value, 1) })]));
    var track = h('div');
    track.style.height = '8px';
    track.style.background = K.grid;
    track.style.borderRadius = '2px';
    track.style.overflow = 'hidden';
    var fill = h('div');
    fill.style.height = '100%';
    fill.style.width = Math.max(0, Math.min(100, (value || 0) * 100)) + '%';
    fill.style.background = ok ? K.focal : K.context;
    track.appendChild(fill);
    row.appendChild(track);
    if (note) { row.appendChild(h('div', { class: 'fnote', text: note })); }
    return row;
  }

  /* ================================================================= state */

  var ALLDAYS = [];
  var WIN = {};
  var WINDAYS = [];
  var S = { from: '', to: '', platforms: {}, topics: {}, windowOnly: true };

  /* Geo-ontology layer (entity/risk/theme/alerts): its own day axis, no
     platform/topic dimension, so it never joins the S/F filter machinery
     above -- same pattern topic_cards already uses for topic_dim. */
  var GEODAYS = [];

  function hydrate(data) {
    DATA = data;
    window.__METRICS__ = data;
    _cache = {};
    META = {};
    TOPIC = {};
    TAG = {};
    PLATMETA = {};
    T('metric_meta').forEach(function (m) { META[m.metric_key] = m; });
    T('topic_dim').forEach(function (t) { TOPIC[t.topic_id] = t; });
    T('tag_dim').forEach(function (t) { TAG[t.tag_id] = t; });
    PLATFORMS = T('platform_dim').map(function (p) { return p.platform; });
    PLATMETA = {};
    T('platform_dim').forEach(function (p) { PLATMETA[p.platform] = p; });
    TOPIC_IDS = T('topic_dim').map(function (t) { return t.topic_id; });

    var s = {};
    T('metrics_daily').forEach(function (r) { s[day(r.day)] = 1; });
    T('metrics_topic_day').forEach(function (r) { s[day(r.day)] = 1; });
    ALLDAYS = Object.keys(s).sort();
    WIN = (DATA && DATA.window) || {};
    WINDAYS = ALLDAYS.filter(function (d) {
      return (!WIN.start || d >= day(WIN.start)) && (!WIN.end || d <= day(WIN.end));
    });
    // Default view is a rolling week ending at the newest day with data (never the whole
    // multi-month window); the "Full window" / 14d / 30d presets widen it on demand.
    var lastDay = WINDAYS[WINDAYS.length - 1] || ALLDAYS[ALLDAYS.length - 1] || '';
    var week = (WINDAYS.length ? WINDAYS : ALLDAYS);
    S = {
      from: (week[Math.max(0, week.length - 7)] || ''),
      to: lastDay,
      platforms: {},
      topics: {},
      windowOnly: true
    };
    PLATFORMS.forEach(function (p) { S.platforms[p] = true; });
    TOPIC_IDS.forEach(function (t) { S.topics[t] = true; });

    var gd = {};
    T('risk_metrics_daily').forEach(function (r) { gd[day(r.day)] = 1; });
    T('theme_metrics_daily').forEach(function (r) { gd[day(r.day)] = 1; });
    GEODAYS = Object.keys(gd).sort();
  }

  function selPlatforms() { return PLATFORMS.filter(function (p) { return S.platforms[p]; }); }
  function selTopics() { return TOPIC_IDS.filter(function (t) { return S.topics[t]; }); }

  var F = {};
  function applyFilters() {
    function dayOk(d) { var x = day(d); return x >= S.from && x <= S.to; }
    F.days = ALLDAYS.filter(dayOk);
    F.md = T('metrics_daily').filter(function (r) {
      return dayOk(r.day) && S.platforms[r.platform] && S.topics[r.topic_id] &&
        (!S.windowOnly || r.in_window);
    });
    F.mdAll = T('metrics_daily').filter(function (r) {
      return dayOk(r.day) && S.platforms[r.platform] && S.topics[r.topic_id];
    });
    F.mtd = T('metrics_topic_day').filter(function (r) {
      return dayOk(r.day) && S.topics[r.topic_id] && (!S.windowOnly || r.in_window);
    });
    var mode = (document.body && document.body.getAttribute('data-mode')) || 'basic';
    if (mode === 'advanced') {
      F.tags = T('tag_metrics_daily').filter(function (r) {
        return dayOk(r.day) && S.platforms[r.platform];
      });
    } else {
      F.tags = [];
    }
    F.alerts = T('alert_candidates').filter(function (r) {
      return dayOk(r.day) && (r.platform == null || S.platforms[r.platform]) &&
        (r.topic_id == null || S.topics[r.topic_id]);
    });
    F.bursts = T('burst_episodes').filter(function (r) {
      return S.platforms[r.platform] && S.topics[r.topic_id] &&
        day(r.end_day) >= S.from && day(r.start_day) <= S.to;
    });
    F.pers = T('persistence').filter(function (r) {
      return S.platforms[r.platform] && S.topics[r.topic_id];
    });
  }

  function series(rows, key, days, opts) {
    var by = {};
    rows.forEach(function (r) {
      var d = day(r.day), v = r[key];
      if (!isNum(v)) { return; }
      if (!by[d]) { by[d] = { s: 0, n: 0 }; }
      by[d].s += v; by[d].n++;
    });
    var agg = (opts && opts.agg) || 'sum';
    return days.map(function (d, i) {
      var b = by[d];
      if (!b) { return { i: i, v: null }; }
      return { i: i, v: agg === 'mean' ? b.s / b.n : b.s };
    });
  }

  /* ================================================================ panels */

  var P = {};

  /* ---- 1. KPI row ---- */
  P.kpis = function (host) {
    var md = F.md, mdAll = F.mdAll;
    var corpus = 0; mdAll.forEach(function (r) { corpus += (r.V || 0); });
    var cells = md.length;
    var ok = 0; md.forEach(function (r) { if (r.support_ok) { ok++; } });
    var alerts = F.alerts.length;
    var crit = F.alerts.filter(function (a) { return a.severity === 'critical'; }).length;
    var top = null;
    md.forEach(function (r) {
      if (r.topic_id !== 'unassigned' && isNum(r.emergence_score) && (!top || r.emergence_score > top.emergence_score)) { top = r; }
    });
    var dr = F.days.length;

    function kpi(cls, lbl, val, sub) {
      return h('div', { class: 'kpi ' + (cls || '') }, [
        h('div', { class: 'k-lbl', text: lbl }),
        h('div', { class: 'k-val', text: val }),
        h('div', { class: 'k-sub', text: sub })
      ]);
    }
    var box = h('div', { class: 'kpis' }, [
      kpi('hero', 'Active alerts (FDR q < 0.05)', String(alerts),
        crit + ' critical · ' + (alerts - crit) + ' high/medium'),
      kpi('', 'Corpus size in view', fmtCompact(corpus),
        fmtInt(corpus) + ' posts across ' + selPlatforms().length + ' platforms'),
      kpi('', 'Analysis window', fmtDay(S.from) + ' – ' + fmtDay(S.to),
        dr + ' days' + (S.windowOnly ? ' · in-window only' : ' · incl. backfill')),
      kpi('', 'Cells passing support gate', fmtInt(ok) + ' / ' + fmtInt(cells),
        cells ? fmtPct(ok / cells, 1) + ' of cells carry publishable gated metrics' : 'no cells'),
      kpi('', 'Top emerging cell', top ? topicLabel(top.topic_id) : '—',
        top ? (top.platform + ' · ' + fmtDay(top.day) + ' · emergence ' + fmt(top.emergence_score, 2))
          : 'no cell cleared the gate')
    ]);
    host.appendChild(box);
  };

  /* ---- 2. Alerts ---- */
  function sevChip(sev) {
    var ic = sev === 'critical' ? '◆' : (sev === 'high' ? '▲' : '●');
    var s = h('span', { class: 'sev ' + sev });
    s.appendChild(h('span', { class: 'ic', text: ic }));
    s.appendChild(txt(sev));
    return s;
  }

  P.alerts_day = function (host) {
    if (!F.alerts.length) { empty(host, 'No FDR-gated alert candidates in this slice. That is a result, not a gap: at q < 0.05 nothing exceeded its baseline with enough evidence.'); return; }
    var by = {};
    F.alerts.forEach(function (a) {
      var d = day(a.day);
      if (!by[d]) { by[d] = { critical: 0, high: 0, medium: 0, t: 0 }; }
      by[d][a.severity] = (by[d][a.severity] || 0) + 1;
      by[d].t++;
    });
    var items = F.days.map(function (d) {
      var b = by[d] || { critical: 0, high: 0, medium: 0, t: 0 };
      return { label: fmtDayFull(d), short: fmtDay(d), value: b.t,
        color: b.critical ? K.crit : K.focal,
        rows: [{ k: 'alerts', color: K.focal, v: fmtInt(b.t) },
          { k: 'critical', color: K.crit, v: fmtInt(b.critical) },
          { k: 'high', v: fmtInt(b.high) }, { k: 'medium', v: fmtInt(b.medium) }] };
    });
    legend(host, [{ color: K.focal, label: 'alert candidates', box: true },
      { color: K.crit, label: 'day contains a critical alert (◆)', box: true }]);
    vBars(host, { items: items, width: host.clientWidth || 700, height: 130,
      measure: 'alerts', vFmt: fmtInt, yFmt: fmtInt, labelCount: 8,
      tipNote: 'Counts are FDR-gated candidates, one row per (day, cell, metric).' });
    tableView(host, [
      { k: 'label', label: 'Day' },
      { k: 'value', label: 'Alerts', num: true, fmt: fmtInt }
    ], items, { label: 'Table view — alerts per day' });
  };

  P.alerts_table = function (host) {
    var rows = F.alerts.slice().sort(function (a, b) {
      var o = { critical: 0, high: 1, medium: 2 };
      if (o[a.severity] !== o[b.severity]) { return o[a.severity] - o[b.severity]; }
      return (a.surprise_q || 1) - (b.surprise_q || 1);
    });
    if (!rows.length) { empty(host, 'No alert candidates in the current selection.'); return; }
    var cols = [
      { k: 'severity', label: 'Severity', node: function (v) { return sevChip(v); } },
      { k: 'day', label: 'Day', fmt: fmtDayFull },
      { k: 'entity', label: 'Entity', strong: true,
        get: function (r) { return r.tag_id ? tagLabel(r.tag_id) : topicLabel(r.topic_id); } },
      { k: 'platform', label: 'Platform', fmt: function (v) { return v || 'all'; } },
      { k: 'metric_key', label: 'Metric', get: function (r) { return metaName(r.metric_key); } },
      { k: 'value', label: 'Value', num: true, fmt: function (v) { return fmt(v, 3); } },
      { k: 'surprise_q', label: 'q-value', num: true, fmt: fmtQ },
      { k: 'rationale', label: 'Rationale', wrap: true }
    ];
    var wrap = h('div', { class: 'tbl-h' });
    var t = h('table');
    var tr = h('tr');
    cols.forEach(function (c) { tr.appendChild(h('th', { class: c.num ? 'num' : '', text: c.label })); });
    t.appendChild(h('thead', {}, [tr]));
    var tb = h('tbody');
    rows.forEach(function (r) {
      var row = h('tr');
      cols.forEach(function (c) {
        var raw = c.get ? c.get(r) : r[c.k];
        var cls = [];
        if (c.num) { cls.push('num'); }
        if (c.wrap) { cls.push('wrap'); }
        if (c.strong) { cls.push('strong'); }
        var td = h('td', { class: cls.join(' ') });
        if (c.node) { td.appendChild(c.node(raw, r)); }
        else { td.textContent = c.fmt ? c.fmt(raw, r) : (raw == null ? '—' : String(raw)); }
        row.appendChild(td);
      });
      attachDrill(row, r.tag_id
        ? { view: 'tag', params: { tag_id: r.tag_id, platform: r.platform, day: day(r.day) },
            title: 'Alert: ' + tagLabel(r.tag_id), subtitle: fmtDayFull(r.day) + ' · ' + (r.platform || 'all platforms') }
        : { view: 'topic', params: { topic_id: r.topic_id, platform: r.platform, day: day(r.day) },
            title: 'Alert: ' + topicLabel(r.topic_id), subtitle: fmtDayFull(r.day) + ' · ' + (r.platform || 'all platforms') });
      tb.appendChild(row);
    });
    t.appendChild(tb);
    wrap.appendChild(t);
    host.appendChild(wrap);
  };

  /* ---- 3. Acceleration & emergence ---- */
  P.emg_components = function (host) {
    var cand = F.md.filter(function (r) { return r.topic_id !== 'unassigned' && isNum(r.emergence_score); });
    if (!cand.length) { empty(host, 'No cell has an emergence score in this slice — every candidate failed the support gate or had fewer than 2 of the 4 components.'); return; }
    cand.sort(function (a, b) { return b.emergence_score - a.emergence_score; });
    var top = cand.slice(0, 8);

    var comps = [
      { k: 'surprise', label: 'surprise', norm: function (v) { return Math.min(1, v / 6); } },
      { k: 'accel_smooth', label: 'accel_smooth', norm: function (v) { return Math.min(1, Math.max(0, (v + 1) / 2)); } },
      { k: 'novelty', label: 'novelty', norm: function (v) { return Math.min(1, v / 0.6); } },
      { k: 'days_to_threshold', label: 'speed to 5/day', norm: function (v) { return Math.min(1, Math.max(0, 1 - v / 30)); } }
    ];

    legend(host, [
      { color: K.focal, label: 'component present (bar length = normalised strength)', box: true },
      { color: K.context, label: 'component NULL — not counted in the composite', hatch: true }
    ]);

    var grid = h('div', { class: 'grid g2' });
    host.appendChild(grid);
    top.forEach(function (r) {
      var card = h('div', { class: 'facet' });
      card.appendChild(h('div', { class: 'facet-t' }, [
        h('span', { text: topicLabel(r.topic_id) + ' · ' + r.platform }),
        h('span', { class: 'n', text: fmtDay(r.day) + ' · ' + fmt(r.emergence_score, 2) })
      ]));
      var plot = h('div', { class: 'plot' });
      card.appendChild(plot);
      grid.appendChild(card);
      var w = Math.max(200, plot.clientWidth || 260);
      var Fr = frame(plot, { width: w, height: comps.length * 17 + 8,
        margin: { t: 4, r: 44, b: 4, l: 92 } });
      var hatch = hatchPattern(Fr.svg);
      comps.forEach(function (c, i) {
        var raw = r[c.k];
        var yv = i * 17 + 2, bh = 9;
        Fr.g.appendChild(svgText(-6, yv + bh / 2, c.label,
          { anchor: 'end', baseline: 'middle', size: 9, fill: K.ink3 }));
        Fr.g.appendChild(el('rect', { x: 0, y: yv, width: Fr.iw, height: bh, fill: K.grid, rx: 1 }));
        var g = el('g');
        if (isNum(raw)) {
          var t = c.norm(raw);
          g.appendChild(el('rect', { x: 0, y: yv, width: Math.max(2, Fr.iw * t), height: bh,
            fill: K.focal, rx: 1 }));
          g.appendChild(svgText(Fr.iw + 5, yv + bh / 2, fmt(raw, c.k === 'days_to_threshold' ? 0 : 2),
            { anchor: 'start', baseline: 'middle', size: 9, tabular: true, fill: K.ink2 }));
        } else {
          g.appendChild(el('rect', { x: 0, y: yv, width: Fr.iw, height: bh, fill: hatch, rx: 1 }));
          g.appendChild(svgText(Fr.iw + 5, yv + bh / 2, 'null',
            { anchor: 'start', baseline: 'middle', size: 9, fill: K.ink3 }));
        }
        var hit = el('rect', { x: -92, y: yv - 3, width: Fr.iw + 130, height: bh + 6, fill: 'transparent' });
        g.appendChild(hit);
        hoverable(hit, function () { return metaName(c.k); },
          function () {
            return [{ k: 'value', color: K.focal, v: isNum(raw) ? fmt(raw, 3) : 'NULL (gated)' },
              { k: 'units', v: metaUnits(c.k) }];
          },
          function () { return metaFormula(c.k); });
        Fr.g.appendChild(g);
      });
      var present = comps.filter(function (c) { return isNum(r[c.k]); }).length;
      card.appendChild(h('div', { class: 'fnote',
        text: present + ' of 4 components present · q = ' + fmtQ(r.surprise_q) +
          ' · V = ' + fmtInt(r.V) + ' · support n = ' + fmtInt(r.support_n) }));
    });

    tableView(host, [
      { k: 'day', label: 'Day', fmt: fmtDayFull },
      { k: 'topic_id', label: 'Topic', strong: true, fmt: topicLabel },
      { k: 'platform', label: 'Platform' },
      { k: 'emergence_score', label: 'Emergence', num: true, fmt: function (v) { return fmt(v, 3); } },
      { k: 'surprise', label: 'Surprise', num: true, fmt: function (v) { return fmt(v, 2); } },
      { k: 'accel_smooth', label: 'Accel (smooth)', num: true, fmt: function (v) { return fmtSigned(v, 3); } },
      { k: 'novelty', label: 'Novelty', num: true, fmt: function (v) { return fmt(v, 3); } },
      { k: 'days_to_threshold', label: 'Days to 5/day', num: true, fmt: fmtInt },
      { k: 'burst_state', label: 'Burst', fmt: function (v) { return v === 1 ? 'bursting' : (v === 0 ? 'baseline' : '—'); } },
      { k: 'surprise_q', label: 'q', num: true, fmt: fmtQ },
      { k: 'support_n', label: 'n', num: true, fmt: fmtInt }
    ], cand, { label: 'Table view — all scored cells, ranked', max: 300,
      rowDrill: function (r) {
        return { view: 'topic', params: { topic_id: r.topic_id, platform: r.platform, day: day(r.day) },
          title: topicLabel(r.topic_id) + ' · ' + r.platform, subtitle: fmtDayFull(r.day) };
      } });
  };

  P.accel_facets = function (host) {
    var topics = selTopics().filter(function (t) {
      return F.md.some(function (r) { return r.topic_id === t && isNum(r.accel_smooth); });
    });
    if (!topics.length) { empty(host, 'No topic has a smoothed-acceleration value in this slice. Savitzky-Golay needs 5 consecutive supported days.'); return; }
    legend(host, [
      { color: K.focal, label: metaName('accel_smooth') + ' (gold line)' },
      { color: K.focal, label: 'burst_state = 1 (gold ribbon under the axis)', box: true }
    ]);
    smallMultiples(host, {
      days: F.days, shareY: true, symmetric: true, zeroLine: true, cols: 3, height: 118,
      tipFmt: function (v) { return fmtSigned(v, 3); },
      tipNote: metaFormula('accel_smooth'),
      facets: topics.map(function (t) {
        var rows = F.md.filter(function (r) { return r.topic_id === t; });
        var pts = series(rows, 'accel_smooth', F.days, { agg: 'mean' });
        var burstBy = {};
        rows.forEach(function (r) { if (r.burst_state === 1) { burstBy[day(r.day)] = 1; } });
        var n = pts.filter(function (p) { return isNum(p.v); }).length;
        return { title: topicLabel(t), note: n + ' days',
          series: [{ label: 'accel_smooth', pts: pts, role: 'focal', kind: 'line' }],
          ribbon: F.days.map(function (d, i) { return { i: i, on: !!burstBy[d] }; }),
          drill: { view: 'topic', params: { topic_id: t }, title: topicLabel(t) } };
      })
    });
  };

  P.novelty_facets = function (host) {
    var topics = selTopics().filter(function (t) {
      return F.md.some(function (r) { return r.topic_id === t && isNum(r.novelty); });
    });
    if (!topics.length) { empty(host, 'No cell reached the 5-posts/day floor novelty requires.'); return; }
    legend(host, [{ color: K.focal, label: metaName('novelty') }]);
    smallMultiples(host, {
      days: F.days, shareY: true, zeroBase: true, cols: 3, height: 106,
      tipFmt: function (v) { return fmt(v, 3); }, tipNote: metaFormula('novelty'),
      facets: topics.map(function (t) {
        var rows = F.md.filter(function (r) { return r.topic_id === t; });
        return { title: topicLabel(t),
          series: [{ label: 'novelty', pts: series(rows, 'novelty', F.days, { agg: 'mean' }),
            role: 'focal', kind: 'area' }],
          drill: { view: 'topic', params: { topic_id: t }, title: topicLabel(t) } };
      })
    });
  };

  P.dtt_bars = function (host) {
    var seen = {};
    F.md.forEach(function (r) {
      if (!isNum(r.days_to_threshold)) { return; }
      var k = r.topic_id + '|' + r.platform;
      if (!seen[k] || r.days_to_threshold < seen[k].days_to_threshold) { seen[k] = r; }
    });
    var items = Object.keys(seen).map(function (k) {
      var r = seen[k];
      return { label: topicLabel(r.topic_id) + ' · ' + r.platform, value: r.days_to_threshold,
        rows: [{ k: 'days to 5/day', color: K.focal, v: fmtInt(r.days_to_threshold) },
          { k: 'first crossing', v: fmtDayFull(r.day) }],
        drill: { view: 'topic', params: { topic_id: r.topic_id, platform: r.platform },
          title: topicLabel(r.topic_id) + ' · ' + r.platform } };
    }).sort(function (a, b) { return a.value - b.value; });
    if (!items.length) { empty(host, 'No cell crossed 5 posts/day in this slice, so time-to-threshold is undefined everywhere. Absence here is not slowness.'); return; }
    hBars(host, { items: items, width: host.clientWidth || 700, measure: metaName('days_to_threshold'),
      vFmt: fmtInt, xFmt: fmtInt, labelW: 210,
      tipNote: 'Lower is faster. ' + metaCaveat('days_to_threshold') });
  };

  /* ---- 4. Burst episodes ---- */
  P.bursts = function (host) {
    var rows = F.bursts.slice().sort(function (a, b) { return day(a.start_day) < day(b.start_day) ? -1 : 1; });
    if (!rows.length) { empty(host, 'No burst episodes overlap this window/filter selection.'); return; }
    legend(host, [
      { color: K.focal, label: 'episode extent (start → end)', box: true },
      { color: K.focal, label: 'peak day (dot)' }
    ]);
    gantt(host, {
      days: F.days, width: host.clientWidth || 760, labelW: 230,
      rows: rows.map(function (e) {
        return {
          label: topicLabel(e.topic_id) + ' · ' + e.platform,
          start: day(e.start_day), end: day(e.end_day), peak: day(e.peak_day),
          rows: [
            { k: 'peak V', color: K.focal, v: fmtInt(e.peak_V) },
            { k: 'total V', v: fmtInt(e.total_V) },
            { k: metaName('onset_slope'), v: isNum(e.onset_slope) ? fmtSigned(e.onset_slope, 3) : '—' },
            { k: 'inflection', v: fmtDayFull(e.inflection_day) },
            { k: 'decay lambda', v: isNum(e.decay_lambda) ? fmt(e.decay_lambda, 3) : '—' },
            { k: 'half-life', v: isNum(e.half_life_days) ? fmt(e.half_life_days, 1) + ' d' : '—' },
            { k: 'fit r²', v: isNum(e.r2) ? fmt(e.r2, 2) : '—' },
            { k: 'level', v: String(e.level) }
          ],
          note: fmtDayFull(e.start_day) + ' → ' + fmtDayFull(e.end_day) +
            (isNum(e.r2) && e.r2 < 0.3 ? ' · decay fit r² < 0.3: half-life not quotable' : ''),
          drill: { view: 'topic', params: { topic_id: e.topic_id, platform: e.platform },
            title: topicLabel(e.topic_id) + ' · ' + e.platform,
            subtitle: 'Burst episode ' + fmtDayFull(e.start_day) + ' → ' + fmtDayFull(e.end_day) }
        };
      })
    });
    tableView(host, [
      { k: 'episode_id', label: 'Episode' },
      { k: 'topic_id', label: 'Topic', strong: true, fmt: topicLabel },
      { k: 'platform', label: 'Platform' },
      { k: 'start_day', label: 'Start', fmt: fmtDayFull },
      { k: 'peak_day', label: 'Peak', fmt: fmtDayFull },
      { k: 'end_day', label: 'End', fmt: fmtDayFull },
      { k: 'peak_V', label: 'Peak V', num: true, fmt: fmtInt },
      { k: 'total_V', label: 'Total V', num: true, fmt: fmtInt },
      { k: 'onset_slope', label: 'Onset slope', num: true, fmt: function (v) { return fmtSigned(v, 3); } },
      { k: 'decay_lambda', label: 'Decay λ', num: true, fmt: function (v) { return fmt(v, 3); } },
      { k: 'half_life_days', label: 'Half-life (d)', num: true, fmt: function (v) { return fmt(v, 1); } },
      { k: 'r2', label: 'r²', num: true, fmt: function (v) { return fmt(v, 2); } },
      { k: 'level', label: 'Level', num: true, fmt: fmtInt }
    ], rows, { rowDrill: function (e) {
      return { view: 'topic', params: { topic_id: e.topic_id, platform: e.platform },
        title: topicLabel(e.topic_id) + ' · ' + e.platform };
    } });
  };

  /* ---- 5. Volume & growth ---- */
  P.vol_facets = function (host) {
    var plats = selPlatforms().filter(function (p) {
      return F.md.some(function (r) { return r.platform === p; });
    });
    if (!plats.length) { empty(host, 'No platform selected has rows in this window.'); return; }
    legend(host, [
      { color: K.focal, label: metaName('V_rate') + ' — the publishable series' },
      { color: K.context, label: metaName('effort') + ' — plotted in its own strip below, never on a second y-axis' }
    ]);
    var grid = h('div', { class: 'grid g3' });
    host.appendChild(grid);
    var allRate = [];
    plats.forEach(function (p) {
      series(F.md.filter(function (r) { return r.platform === p; }), 'V_rate', F.days)
        .forEach(function (pt) { allRate.push(pt.v); });
    });
    var domRate = padDomain(extent(allRate), { zeroBase: true });

    plats.forEach(function (p) {
      var rows = F.md.filter(function (r) { return r.platform === p; });
      var cell = h('div', { class: 'facet' });
      var totV = 0; rows.forEach(function (r) { totV += (r.V || 0); });
      var facetHead = h('div', { class: 'facet-t' }, [h('span', { text: p }),
        h('span', { class: 'n', text: fmtCompact(totV) + ' posts' })]);
      attachDrill(facetHead, { view: 'platform', params: { platform: p }, title: p + ' — recent posts' });
      cell.appendChild(facetHead);
      var plot = h('div', { class: 'plot' });
      cell.appendChild(plot);
      grid.appendChild(cell);
      var w = Math.max(190, plot.clientWidth || 240);
      var ratePts = series(rows, 'V_rate', F.days);
      var vPts = series(rows, 'V', F.days);
      lineChart(plot, {
        days: F.days, width: w, height: 112, domain: domRate, zeroBase: true,
        series: [{ label: 'V_rate', pts: ratePts, role: 'focal', kind: 'area' }],
        yFmt: function (v) { return fmt(v, 1); }, tipFmt: function (v) { return fmt(v, 3); },
        tipHead: p, yTicks: 3, xTicks: 3, margin: { t: 8, r: 10, b: 22, l: 38 },
        extraRows: function (i) {
          var eff = {}; rows.forEach(function (r) { eff[day(r.day)] = r.effort; });
          return [{ k: 'V (raw count)', v: isNum(vPts[i].v) ? fmtInt(vPts[i].v) : '—' },
            { k: 'effort (channels)', color: K.context, v: fmtInt(eff[F.days[i]]) }];
        },
        tipNote: metaCaveat('V_rate')
      });
      var effBy = {};
      rows.forEach(function (r) { if (isNum(r.effort)) { effBy[day(r.day)] = r.effort; } });
      var effPts = F.days.map(function (d, i) { return { i: i, v: isNum(effBy[d]) ? effBy[d] : null }; });
      var strip = h('div', { class: 'plot' });
      cell.appendChild(h('div', { class: 'fnote', text: 'collection effort (own baseline)' }));
      cell.appendChild(strip);
      lineChart(strip, {
        days: F.days, width: w, height: 46,
        series: [{ label: 'effort', pts: effPts, role: 'context', kind: 'line' }],
        zeroBase: true, yTicks: 2, xTicks: 2, yFmt: fmtInt, tipFmt: fmtInt, tipHead: p + ' effort',
        margin: { t: 6, r: 10, b: 14, l: 38 },
        tipNote: metaCaveat('effort')
      });
    });

    var byP = {};
    F.md.forEach(function (r) {
      if (!byP[r.platform]) { byP[r.platform] = { V: 0, n: 0, ok: 0 }; }
      byP[r.platform].V += (r.V || 0); byP[r.platform].n++;
      if (r.support_ok) { byP[r.platform].ok++; }
    });
    tableView(host, [
      { k: 'p', label: 'Platform', strong: true },
      { k: 'V', label: 'Posts', num: true, fmt: fmtInt },
      { k: 'n', label: 'Cells', num: true, fmt: fmtInt },
      { k: 'ok', label: 'Supported cells', num: true, fmt: fmtInt },
      { k: 'share', label: 'Supported %', num: true, fmt: function (v) { return fmtPct(v, 1); } }
    ], Object.keys(byP).map(function (p) {
      return { p: p, V: byP[p].V, n: byP[p].n, ok: byP[p].ok, share: byP[p].ok / byP[p].n };
    }).sort(function (a, b) { return b.V - a.V; }), { label: 'Table view — volume by platform',
      rowDrill: function (r) { return { view: 'platform', params: { platform: r.p }, title: r.p + ' — recent posts' }; } });
  };

  P.growth_div = function (host) {
    var lastDay = null;
    F.md.forEach(function (r) {
      if (isNum(r.growth)) { var d = day(r.day); if (!lastDay || d > lastDay) { lastDay = d; } }
    });
    if (!lastDay) { empty(host, 'No supported growth value in this slice.'); return; }
    var byT = {};
    F.md.forEach(function (r) {
      if (day(r.day) !== lastDay || !isNum(r.growth)) { return; }
      if (!byT[r.topic_id]) { byT[r.topic_id] = { s: 0, n: 0, sh: 0 }; }
      byT[r.topic_id].s += r.growth; byT[r.topic_id].n++;
      if (isNum(r.growth_share)) { byT[r.topic_id].sh += r.growth_share; }
    });
    var items = Object.keys(byT).map(function (t) {
      var b = byT[t];
      return { label: topicLabel(t), value: b.s / b.n,
        rows: [{ k: metaName('growth'), color: b.s >= 0 ? K.focal : K.neg, v: fmtSigned(b.s / b.n, 3) },
          { k: metaName('growth_share'), v: fmtPct(b.sh, 1) },
          { k: 'cells', v: fmtInt(b.n) }],
        drill: { view: 'topic', params: { topic_id: t, day: lastDay }, title: topicLabel(t) } };
    }).sort(function (a, b) { return b.value - a.value; });
    legend(host, [{ color: K.focal, label: 'accelerating (positive)', box: true },
      { color: K.neg, label: 'decelerating (negative)', box: true }]);
    host.appendChild(h('div', { class: 'fnote',
      text: 'Most recent day with supported growth: ' + fmtDayFull(lastDay) +
        '. Diverging scale: gold positive, clay negative, neutral gray at zero.' }));
    hBars(host, { items: items, width: host.clientWidth || 700, diverging: true,
      measure: metaName('growth'), vFmt: function (v) { return fmtSigned(v, 3); },
      xFmt: function (v) { return fmtSigned(v, 2); }, labelW: 200,
      tipNote: metaCaveat('growth') });
  };

  P.momentum_facets = function (host) {
    var topics = selTopics().filter(function (t) {
      return F.mtd.some(function (r) { return r.topic_id === t && isNum(r.momentum); });
    });
    if (!topics.length) { empty(host, 'No topic has a momentum value — EMA(7)−EMA(21) needs 21 trailing days.'); return; }
    legend(host, [{ color: K.focal, label: metaName('momentum') },
      { color: K.context, label: metaName('growth_7d') }]);
    smallMultiples(host, {
      days: F.days, shareY: true, symmetric: true, zeroLine: true, cols: 3, height: 112,
      tipFmt: function (v) { return fmtSigned(v, 3); }, tipNote: metaCaveat('momentum'),
      facets: topics.map(function (t) {
        var rows = F.mtd.filter(function (r) { return r.topic_id === t; });
        return { title: topicLabel(t),
          series: [
            { label: 'momentum', pts: series(rows, 'momentum', F.days, { agg: 'mean' }), role: 'focal', kind: 'line' },
            { label: 'growth_7d', pts: series(rows, 'growth_7d', F.days, { agg: 'mean' }), role: 'context', kind: 'line' }
          ],
          drill: { view: 'topic', params: { topic_id: t }, title: topicLabel(t) } };
      })
    });
  };

  /* ---- 6. Intensity heatmap ---- */
  P.intensity_heat = function (host) {
    var topics = selTopics();
    var src = F.mtd.length ? F.mtd : F.md;
    var byKey = {};
    src.forEach(function (r) {
      var k = r.topic_id + '|' + day(r.day);
      if (!byKey[k]) { byKey[k] = { s: 0, n: 0, q: 1, flag: false, V: 0, sup: 0, ok: 0 }; }
      var b = byKey[k];
      if (isNum(r.surprise)) { b.s += r.surprise; b.n++; }
      if (isNum(r.surprise_q)) { b.q = Math.min(b.q, r.surprise_q); }
      if (r.alert_flag) { b.flag = true; }
      b.V += (r.V || 0);
      b.sup += (r.support_n || 0);
      if (r.support_ok) { b.ok++; }
    });
    var vals = [];
    Object.keys(byKey).forEach(function (k) { if (byKey[k].n) { vals.push(byKey[k].s / byKey[k].n); } });
    if (!vals.length) { empty(host, 'No supported surprise value in this slice — the whole panel failed the support gate.'); return; }
    var ex = extent(vals), lo = 0, hi = ex[1];
    rampLegend(host, lo, hi, K.seq, metaName('surprise') + ' (−log10 p)', function (v) { return fmt(v, 1); });
    legend(host, [
      { color: K.ink, label: 'white outline = alert_flag (BH q < 0.05)', box: true },
      { hatch: true, label: 'hatched = no value: support gate not met' }
    ]);
    heatmap(host, {
      rows: topics.map(function (t) { return { id: t, label: topicLabel(t) }; }),
      cols: F.days.map(function (d) { return { id: d, label: fmtDayFull(d), short: fmtDay(d) }; }),
      cellW: 14, cellH: 21, labelW: 176,
      color: function (v) { return seqColor((v - lo) / ((hi - lo) || 1)); },
      get: function (r, c) {
        var b = byKey[r.id + '|' + c.id];
        var drill = { view: 'topic', params: { topic_id: r.id, day: c.id }, title: r.label, subtitle: c.label };
        if (!b || !b.n) {
          return b ? { v: null, rows: [{ k: 'V', v: fmtInt(b.V) },
            { k: metaName('surprise'), v: 'NULL — support gate not met' },
            { k: 'supported sub-cells', v: fmtInt(b.ok) }],
            note: metaGate('surprise'), drill: drill } : null;
        }
        var v = b.s / b.n;
        return { v: v, ring: b.flag,
          rows: [{ k: metaName('surprise'), color: seqColor((v - lo) / ((hi - lo) || 1)), v: fmt(v, 2) },
            { k: 'BH q', v: fmtQ(b.q) },
            { k: 'V', v: fmtInt(b.V) },
            { k: 'support n', v: fmtInt(b.sup) },
            { k: 'alert', v: b.flag ? 'FLAGGED (q < 0.05)' : 'not flagged' }],
          note: metaCaveat('surprise'), drill: drill };
      },
      measure: metaName('surprise'),
      xLabelCount: 8
    });
    var flat = [];
    Object.keys(byKey).forEach(function (k) {
      var p = k.split('|'), b = byKey[k];
      flat.push({ topic_id: p[0], topic: topicLabel(p[0]), day: p[1], surprise: b.n ? b.s / b.n : null,
        q: b.n ? b.q : null, V: b.V, flag: b.flag });
    });
    flat.sort(function (a, b) { return (b.surprise || -1) - (a.surprise || -1); });
    tableView(host, [
      { k: 'day', label: 'Day', fmt: fmtDayFull },
      { k: 'topic', label: 'Topic', strong: true },
      { k: 'surprise', label: 'Surprise', num: true, fmt: function (v) { return fmt(v, 2); } },
      { k: 'q', label: 'BH q', num: true, fmt: fmtQ },
      { k: 'V', label: 'V', num: true, fmt: fmtInt },
      { k: 'flag', label: 'Alert', fmt: function (v) { return v ? 'yes' : 'no'; } }
    ], flat, { label: 'Table view — topic × day surprise', max: 500,
      rowDrill: function (r) {
        return { view: 'topic', params: { topic_id: r.topic_id, day: r.day }, title: r.topic, subtitle: fmtDayFull(r.day) };
      } });
  };

  /* ---- 7. Persistence ---- */
  P.persistence = function (host) {
    var rows = F.pers.slice();
    if (!rows.length) { empty(host, 'No (topic × platform) cell has ≥ 20 observations in this slice.'); return; }
    rows.sort(function (a, b) { return (b.ar1_phi || -9) - (a.ar1_phi || -9); });
    legend(host, [
      { color: K.focal, label: 'AR(1) φ (point estimate)' },
      { color: K.context, label: '± 1 standard error' }
    ]);
    dotPlot(host, {
      width: host.clientWidth || 700, labelW: 210, refValue: 0,
      items: rows.map(function (r) {
        var sig = isNum(r.ljung_box_p) && r.ljung_box_p < 0.05;
        return {
          label: topicLabel(r.topic_id) + ' · ' + r.platform,
          value: r.ar1_phi, lo: isNum(r.phi_se) ? r.ar1_phi - r.phi_se : null,
          hi: isNum(r.phi_se) ? r.ar1_phi + r.phi_se : null,
          dim: !sig,
          rows: [
            { k: metaName('ar1_phi'), color: K.focal, v: fmt(r.ar1_phi, 3) },
            { k: metaName('phi_se'), color: K.context, v: fmt(r.phi_se, 3) },
            { k: metaName('half_life_days'), v: isNum(r.half_life_days) ? fmt(r.half_life_days, 2) + ' d' : 'undefined (φ outside 0–1)' },
            { k: metaName('volatility'), v: fmt(r.volatility, 3) },
            { k: 'Ljung–Box Q', v: fmt(r.ljung_box_q, 2) },
            { k: 'Ljung–Box p', v: fmtQ(r.ljung_box_p) },
            { k: 'n obs', v: fmtInt(r.n_obs) }
          ],
          note: sig ? 'Ljung–Box p < 0.05: the series has autocorrelation worth modelling.'
            : 'Ljung–Box p ≥ 0.05: φ and its half-life describe noise. Shown in gray.',
          drill: { view: 'topic', params: { topic_id: r.topic_id, platform: r.platform },
            title: topicLabel(r.topic_id) + ' · ' + r.platform }
        };
      }),
      vFmt: function (v) { return fmt(v, 2); }
    });
    tableView(host, [
      { k: 'topic_id', label: 'Topic', strong: true, fmt: topicLabel },
      { k: 'platform', label: 'Platform' },
      { k: 'ar1_phi', label: 'AR(1) φ', num: true, fmt: function (v) { return fmt(v, 3); } },
      { k: 'phi_se', label: 'SE', num: true, fmt: function (v) { return fmt(v, 3); } },
      { k: 'half_life_days', label: 'Half-life (d)', num: true, fmt: function (v) { return fmt(v, 2); } },
      { k: 'volatility', label: 'Volatility', num: true, fmt: function (v) { return fmt(v, 3); } },
      { k: 'ljung_box_q', label: 'LB Q', num: true, fmt: function (v) { return fmt(v, 2); } },
      { k: 'ljung_box_p', label: 'LB p', num: true, fmt: fmtQ },
      { k: 'n_obs', label: 'n', num: true, fmt: fmtInt }
    ], rows, { rowDrill: function (r) {
      return { view: 'topic', params: { topic_id: r.topic_id, platform: r.platform },
        title: topicLabel(r.topic_id) + ' · ' + r.platform };
    } });
  };

  /* ---- 8. Tone ---- */
  P.tone_heat = function (host) {
    var topics = selTopics(), plats = selPlatforms();
    var by = {};
    F.md.forEach(function (r) {
      var k = r.topic_id + '|' + r.platform;
      if (!by[k]) { by[k] = { eb: 0, ebn: 0, raw: 0, rawn: 0, n: 0 }; }
      var b = by[k];
      if (isNum(r.tone_eb)) { b.eb += r.tone_eb; b.ebn++; }
      if (isNum(r.tone_mean)) { b.raw += r.tone_mean; b.rawn++; }
      b.n += (r.tone_n || 0);
    });
    var any = Object.keys(by).some(function (k) { return by[k].ebn; });
    if (!any) { empty(host, 'No tone values in this slice.'); return; }
    rampLegend(host, -1, 1, K.div, metaName('tone_eb') + ' (negative → positive)',
      function (v) { return fmtSigned(v, 1); });
    legend(host, [{ color: K.div[3], label: 'neutral midpoint is GRAY, not a hue', box: true },
      { hatch: true, label: 'hatched = no tone-scored post in the cell' }]);
    heatmap(host, {
      rows: topics.map(function (t) { return { id: t, label: topicLabel(t) }; }),
      cols: plats.map(function (p) { return { id: p, label: p }; }),
      cellW: 74, cellH: 24, labelW: 176, xLabelCount: plats.length,
      color: function (v) { return divColor(v); },
      get: function (r, c) {
        var b = by[r.id + '|' + c.id];
        var drill = { view: 'topic', params: { topic_id: r.id, platform: c.id }, title: r.label + ' · ' + c.label };
        if (!b || !b.ebn) { return { v: null, rows: [{ k: metaName('tone_eb'), v: 'no scored post' }], drill: drill }; }
        var v = b.eb / b.ebn;
        return { v: v, rows: [
          { k: metaName('tone_eb'), color: divColor(v), v: fmtSigned(v, 3) },
          { k: metaName('tone_mean'), color: K.context, v: b.rawn ? fmtSigned(b.raw / b.rawn, 3) : '—' },
          { k: metaName('tone_n'), v: fmtInt(b.n) }
        ], note: metaCaveat('tone_eb'), drill: drill };
      },
      measure: metaName('tone_eb')
    });
  };

  P.tone_shrink = function (host) {
    var by = {};
    F.md.forEach(function (r) {
      if (!isNum(r.tone_eb) && !isNum(r.tone_mean)) { return; }
      var k = r.topic_id;
      if (!by[k]) { by[k] = { eb: 0, ebn: 0, raw: 0, rawn: 0, n: 0 }; }
      var b = by[k];
      if (isNum(r.tone_eb)) { b.eb += r.tone_eb; b.ebn++; }
      if (isNum(r.tone_mean)) { b.raw += r.tone_mean; b.rawn++; }
      b.n += (r.tone_n || 0);
    });
    var items = Object.keys(by).map(function (t) {
      var b = by[t];
      return { label: topicLabel(t), a: b.rawn ? b.raw / b.rawn : null,
        b: b.ebn ? b.eb / b.ebn : null, right: 'n=' + fmtInt(b.n),
        rows: [{ k: metaName('tone_mean'), color: K.context, v: b.rawn ? fmtSigned(b.raw / b.rawn, 3) : '—' },
          { k: metaName('tone_eb'), color: K.focal, v: b.ebn ? fmtSigned(b.eb / b.ebn, 3) : '—' },
          { k: metaName('tone_n'), v: fmtInt(b.n) }],
        note: 'Shrinkage pulls low-n cells toward the parent mean by construction. Read n first.',
        drill: { view: 'topic', params: { topic_id: t }, title: topicLabel(t) } };
    }).sort(function (x, y) { return (x.b == null ? 9 : x.b) - (y.b == null ? 9 : y.b); });
    if (!items.length) { empty(host, 'No tone values in this slice.'); return; }
    legend(host, [{ color: K.context, label: metaName('tone_mean') + ' (raw)' },
      { color: K.focal, label: metaName('tone_eb') + ' (published, EB-shrunk)' }]);
    dumbbell(host, { items: items, width: host.clientWidth || 700, labelW: 200, symmetric: true });
    tableView(host, [
      { k: 'label', label: 'Topic', strong: true },
      { k: 'a', label: 'tone_mean', num: true, fmt: function (v) { return fmtSigned(v, 3); } },
      { k: 'b', label: 'tone_eb', num: true, fmt: function (v) { return fmtSigned(v, 3); } },
      { k: 'right', label: 'Support' }
    ], items, { label: 'Table view — raw vs shrunk tone', rowDrill: function (r) { return r.drill; } });
  };

  /* ---- 9. Hate (gated) ---- */
  P.hate_gated = function (host) {
    var by = {};
    F.md.forEach(function (r) {
      if (!isNum(r.hate_eb)) { return; }
      if (!by[r.topic_id]) { by[r.topic_id] = { s: 0, n: 0, p: 0, pn: 0, sup: 0 }; }
      var b = by[r.topic_id];
      b.s += r.hate_eb; b.n++;
      if (isNum(r.hate_prevalence)) { b.p += r.hate_prevalence; b.pn++; }
      b.sup += (r.tone_n || 0);
    });
    var items = Object.keys(by).map(function (t) {
      var b = by[t];
      return { label: topicLabel(t), value: b.s / b.n, dim: true,
        rows: [{ k: metaName('hate_eb'), color: K.context, v: fmt(b.s / b.n, 3) },
          { k: metaName('hate_prevalence'), v: b.pn ? fmtPct(b.p / b.pn, 1) : '—' },
          { k: 'support n', v: fmtInt(b.sup) },
          { k: 'calibrated', v: 'NO' }],
        note: 'UNCALIBRATED. ' + metaCaveat('hate_eb'),
        drill: { view: 'topic', params: { topic_id: t }, title: topicLabel(t) } };
    }).sort(function (a, b) { return b.value - a.value; });
    var d = h('details', { class: 'tv' });
    d.appendChild(h('summary', { text: 'Show uncalibrated values anyway (rendered in gray, never gold)' }));
    var inner = h('div', { class: 'hgated' });
    d.appendChild(inner);
    host.appendChild(d);
    if (!items.length) { empty(inner, 'No hate scores in this slice.'); return; }
    var plot = h('div', { class: 'plot' });
    inner.appendChild(plot);
    hBars(plot, { items: items, width: host.clientWidth || 700, measure: metaName('hate_eb'),
      vFmt: function (v) { return fmt(v, 3); }, xFmt: function (v) { return fmt(v, 2); }, labelW: 200,
      tipNote: 'Not publishable. ' + metaGate('hate_eb') });
    tableView(inner, [
      { k: 'label', label: 'Topic', strong: true },
      { k: 'value', label: 'hate_eb (UNCALIBRATED)', num: true, fmt: function (v) { return fmt(v, 3); } }
    ], items, { label: 'Table view — uncalibrated hate scores', rowDrill: function (r) { return r.drill; } });
  };

  /* ---- 10. Virality & reach ---- */
  P.virality_facets = function (host) {
    var plats = selPlatforms().filter(function (p) {
      return F.md.some(function (r) { return r.platform === p && isNum(r.virality_pct); });
    });
    var noEng = selPlatforms().filter(function (p) {
      return !F.md.some(function (r) { return r.platform === p && isNum(r.virality_pct); });
    });
    if (!plats.length) {
      empty(host, 'No platform in this selection has virality percentiles. ' +
        (noEng.length ? 'Platforms without engagement data: ' + noEng.join(', ') + '.' : ''));
      return;
    }
    legend(host, [{ color: K.focal, label: 'cells per percentile bin', box: true }]);
    var grid = h('div', { class: 'grid g3' });
    host.appendChild(grid);
    plats.forEach(function (p) {
      var vals = F.md.filter(function (r) { return r.platform === p; })
        .map(function (r) { return r.virality_pct; }).filter(isNum);
      var cell = h('div', { class: 'facet' });
      var vHead = h('div', { class: 'facet-t' }, [h('span', { text: p }),
        h('span', { class: 'n', text: vals.length + ' cells' })]);
      attachDrill(vHead, { view: 'platform', params: { platform: p }, title: p + ' — recent posts' });
      cell.appendChild(vHead);
      var plot = h('div', { class: 'plot' });
      cell.appendChild(plot);
      grid.appendChild(cell);
      histogram(plot, { values: vals, lo: 0, hi: 1, bins: 10,
        width: Math.max(190, plot.clientWidth || 240), height: 104,
        tipNote: metaCaveat('virality_pct') });
    });
    if (noEng.length) {
      host.appendChild(h('div', { class: 'caveat' }, [
        h('b', { text: 'No engagement data: ' }),
        txt(noEng.join(', ') + '. These are absent, not zero — the metric is NULL rather than 0 so the distinction survives.')
      ]));
    }
  };

  P.reach_bars = function (host) {
    var by = {};
    F.md.forEach(function (r) {
      if (!isNum(r.reach_proxy)) { return; }
      if (!by[r.platform]) { by[r.platform] = { s: 0, n: 0, an: 0, ann: 0 }; }
      by[r.platform].s += r.reach_proxy; by[r.platform].n++;
      if (isNum(r.reach_audience_norm)) { by[r.platform].an += r.reach_audience_norm; by[r.platform].ann++; }
    });
    var items = Object.keys(by).map(function (p) {
      var b = by[p];
      return { label: p, value: b.s,
        rows: [{ k: metaName('reach_proxy'), color: K.focal, v: fmtCompact(b.s) },
          { k: 'cells', v: fmtInt(b.n) },
          { k: metaName('reach_audience_norm'), v: b.ann ? fmt(b.an / b.ann, 2) + '×' : 'n/a (Telegram only)' }],
        note: metaCaveat('reach_proxy'),
        drill: { view: 'platform', params: { platform: p }, title: p + ' — recent posts' } };
    }).sort(function (a, b) { return b.value - a.value; });
    if (!items.length) { empty(host, 'No reach proxy values in this slice.'); return; }
    hBars(host, { items: items, width: host.clientWidth || 700, measure: metaName('reach_proxy'),
      vFmt: fmtCompact, xFmt: fmtCompact, labelW: 110,
      tipNote: 'PROXY. No follower or subscriber counts exist anywhere in the corpus.' });
  };

  /* ---- 10c. Cross-Platform Narrative Diffusion ---- */
  P.cross_platform = function (host) {
    var stories = T('cross_platform_stories');
    if (!stories || !stories.length) {
      empty(host, 'No cross-platform narrative clusters detected in the current dataset.');
      return;
    }

    var filtered = stories.filter(function (s) {
      if (!s.platforms) { return false; }
      var plist = Array.isArray(s.platforms) ? s.platforms : [s.platforms];
      return plist.some(function (p) { return S.platforms[p]; });
    });

    if (!filtered.length) {
      empty(host, 'No cross-platform narratives in this platform selection.');
      return;
    }

    var tableRows = filtered.slice(0, 35).map(function (s) {
      var plist = Array.isArray(s.platforms) ? s.platforms : [s.platforms];
      var dests = plist.filter(function (p) { return p !== s.origin_platform; });
      var bMult = s.breadth_multiplier || (1.0 + 0.5 * (s.n_platforms - 1));
      var uReach = s.unified_reach != null ? s.unified_reach : (s.total_views || 0);
      var effReach = s.effective_reach != null ? s.effective_reach : (uReach * bMult);
      return {
        story_id: s.story_id,
        origin: s.origin_platform,
        destinations: dests.length ? dests.join(', ') : plist.join(', '),
        breadth: s.n_platforms,
        breadth_multiplier: bMult,
        posts: s.n_posts,
        unified_reach: uReach,
        effective_reach: effReach,
        lag: (s.spread_duration_hours != null ? s.spread_duration_hours + 'h' : '—'),
        snippet: s.snippet,
        first_seen: s.origin_time ? s.origin_time.split(' ')[0] : '—'
      };
    });

    tableView(host, [
      { k: 'origin', label: 'Origin Platform', strong: true },
      { k: 'destinations', label: 'Diffusion Network' },
      { k: 'breadth', label: 'Breadth', num: true, fmt: function (v) { return v + ' platforms'; } },
      { k: 'posts', label: 'Total Posts', num: true, fmt: fmtInt },
      { k: 'unified_reach', label: 'Unified Story Reach', num: true, fmt: fmtCompact },
      { k: 'effective_reach', label: 'Effective Reach', num: true, fmt: function (v, r) { return fmtCompact(v) + ' (' + (r.breadth_multiplier ? r.breadth_multiplier + 'x' : '1.0x') + ')'; } },
      { k: 'lag', label: 'Spread Lag' },
      { k: 'first_seen', label: 'First Seen' },
      { k: 'snippet', label: 'Narrative Message Sample', wrap: true }
    ], tableRows, {
      open: true,
      label: 'Top Multi-Platform Narratives & Improvised Reach (' + tableRows.length + ' stories) — click any story to inspect all posts across platforms',
      rowDrill: function (r) {
        return {
          view: 'cross_platform',
          params: { story_id: r.story_id },
          title: 'Narrative Spread: ' + r.origin + ' → ' + r.destinations + ' (' + r.posts + ' posts · ' + fmtCompact(r.unified_reach) + ' reach)'
        };
      }
    });
  };

  /* ---- 11. Authors & coordination ---- */
  P.authors_scatter = function (host) {
    var pts = F.md.filter(function (r) { return isNum(r.author_1_hhi) && isNum(r.n_authors); })
      .map(function (r) {
        return {
          x: r.n_authors, y: r.author_1_hhi, flag: !!r.coordination_flag,
          label: topicLabel(r.topic_id) + ' · ' + r.platform + ' · ' + fmtDay(r.day),
          rows: [{ k: metaName('author_1_hhi'), color: r.coordination_flag ? K.crit : K.focal, v: fmt(r.author_1_hhi, 3) },
            { k: metaName('n_authors'), v: fmtInt(r.n_authors) },
            { k: metaName('author_entropy'), v: isNum(r.author_entropy) ? fmt(r.author_entropy, 3) : '—' },
            { k: 'V', v: fmtInt(r.V) },
            { k: metaName('coordination_flag'), v: r.coordination_flag ? 'FLAGGED' : 'no' }],
          note: r.coordination_flag ? metaCaveat('coordination_flag') : null,
          drill: { view: 'topic', params: { topic_id: r.topic_id, platform: r.platform, day: day(r.day) },
            title: topicLabel(r.topic_id) + ' · ' + r.platform, subtitle: fmtDayFull(r.day) }
        };
      });
    if (!pts.length) { empty(host, 'No cell has ≥ 2 distinct authors in this slice.'); return; }
    legend(host, [
      { color: K.focal, label: 'cell (dot)' },
      { color: K.crit, label: '✕ coordination_flag — heuristic screen, needs a human look' }
    ]);
    scatter(host, { points: pts, width: host.clientWidth || 700, height: 260,
      xLabel: metaName('n_authors'), yLabel: '1 − HHI', xZero: true, yZero: true,
      xFmt: fmtInt, yFmt: function (v) { return fmt(v, 1); } });
    var flagged = F.md.filter(function (r) { return r.coordination_flag; });
    tableView(host, [
      { k: 'day', label: 'Day', fmt: fmtDayFull },
      { k: 'topic_id', label: 'Topic', strong: true, fmt: topicLabel },
      { k: 'platform', label: 'Platform' },
      { k: 'V', label: 'V', num: true, fmt: fmtInt },
      { k: 'n_authors', label: 'Authors', num: true, fmt: fmtInt },
      { k: 'author_1_hhi', label: '1 − HHI', num: true, fmt: function (v) { return fmt(v, 3); } },
      { k: 'author_entropy', label: 'Entropy', num: true, fmt: function (v) { return fmt(v, 3); } }
    ], flagged, { label: 'Table view — ' + flagged.length + ' coordination-flagged cells',
      rowDrill: function (r) {
        return { view: 'topic', params: { topic_id: r.topic_id, platform: r.platform, day: day(r.day) },
          title: topicLabel(r.topic_id) + ' · ' + r.platform, subtitle: fmtDayFull(r.day) };
      } });
  };

  P.entropy_facets = function (host) {
    var plats = selPlatforms().filter(function (p) {
      return F.md.some(function (r) { return r.platform === p && isNum(r.author_entropy); });
    });
    if (!plats.length) { empty(host, 'No author-entropy values in this slice.'); return; }
    legend(host, [{ color: K.focal, label: metaName('author_entropy') + ' (nats)' }]);
    smallMultiples(host, {
      days: F.days, shareY: true, zeroBase: true, cols: 3, height: 100,
      tipFmt: function (v) { return fmt(v, 3); }, tipNote: metaCaveat('author_entropy'),
      facets: plats.map(function (p) {
        var rows = F.md.filter(function (r) { return r.platform === p; });
        return { title: p, series: [{ label: 'entropy',
          pts: series(rows, 'author_entropy', F.days, { agg: 'mean' }), role: 'focal', kind: 'line' }],
          drill: { view: 'platform', params: { platform: p }, title: p + ' — recent posts' } };
      })
    });
  };

  /* ---- 12. Readability ---- */
  P.readability = function (host) {
    var by = {};
    F.md.forEach(function (r) {
      if (!by[r.platform]) { by[r.platform] = { v: 0, vn: 0, c: 0, cn: 0 }; }
      var b = by[r.platform];
      if (isNum(r.readability_mean)) { b.v += r.readability_mean; b.vn++; }
      if (isNum(r.readability_coverage)) { b.c += r.readability_coverage; b.cn++; }
    });
    var keys = Object.keys(by).filter(function (p) { return by[p].vn || by[p].cn; });
    if (!keys.length) { empty(host, 'No readability values in this slice.'); return; }
    legend(host, [{ color: K.focal, label: metaName('readability_mean') + ' (Flesch)' },
      { color: K.focal, label: metaName('readability_coverage') + ' (meter, share of scorable posts)', box: true }]);
    var items = keys.map(function (p) {
      var b = by[p];
      return { label: p, value: b.vn ? b.v / b.vn : null, cov: b.cn ? b.c / b.cn : null,
        rows: [{ k: metaName('readability_mean'), color: K.focal, v: b.vn ? fmt(b.v / b.vn, 1) : '—' },
          { k: metaName('readability_coverage'), v: b.cn ? fmtPct(b.c / b.cn, 1) : '—' }],
        note: metaCaveat('readability_mean'),
        drill: { view: 'platform', params: { platform: p }, title: p + ' — recent posts' } };
    }).sort(function (a, b) { return (b.value || -1) - (a.value || -1); });
    var plot = h('div', { class: 'plot' });
    host.appendChild(plot);
    hBars(plot, { items: items, width: host.clientWidth || 700, measure: metaName('readability_mean'),
      vFmt: function (v) { return fmt(v, 1); }, xFmt: function (v) { return fmt(v, 0); }, labelW: 110,
      tipNote: 'The Flesch mean describes only the scorable minority — read the coverage meter below.' });
    var grid = h('div', { class: 'grid g3' });
    host.appendChild(h('div', { class: 'fnote', text: 'Coverage — the share of posts long enough to score at all. This is what decides whether the Flesch mean means anything.' }));
    host.appendChild(grid);
    items.forEach(function (d) {
      grid.appendChild(meterRow(d.label, d.cov,
        isNum(d.cov) ? (d.cov < 0.2 ? 'below 20%: the mean describes a small minority' : '') : 'no coverage figure',
        isNum(d.cov) && d.cov >= 0.2));
    });
    tableView(host, [
      { k: 'label', label: 'Platform', strong: true },
      { k: 'value', label: 'Flesch mean', num: true, fmt: function (v) { return fmt(v, 1); } },
      { k: 'cov', label: 'Coverage', num: true, fmt: function (v) { return fmtPct(v, 1); } }
    ], items, { label: 'Table view — readability and coverage', rowDrill: function (r) { return r.drill; } });
  };

  /* ---- 13. Ontology ---- */
  P.topic_cards = function (host) {
    var vol = {}, alerts = {};
    F.md.forEach(function (r) { vol[r.topic_id] = (vol[r.topic_id] || 0) + (r.V || 0); });
    F.alerts.forEach(function (a) { if (a.topic_id) { alerts[a.topic_id] = (alerts[a.topic_id] || 0) + 1; } });
    var grid = h('div', { class: 'grid g3' });
    host.appendChild(grid);
    T('topic_dim').forEach(function (t) {
      var on = !!S.topics[t.topic_id];
      var card = h('div', { class: 'tcard' });
      if (!on) { card.style.opacity = '0.42'; }
      card.appendChild(h('div', { class: 'id', text: t.topic_id + (on ? '' : ' · filtered out') }));
      card.appendChild(h('h3', { text: t.label || t.topic || t.topic_id }));
      card.appendChild(h('div', { class: 'dom', text: (t.domain || '—') + ' → ' + (t.topic || '—') }));
      if (t.description) { card.appendChild(h('p', { text: t.description })); }
      var mt = h('div', { class: 'mt' });
      mt.appendChild(h('span', {}, [txt('corpus '), h('b', { text: fmtCompact(t.n_posts) })]));
      mt.appendChild(h('span', {}, [txt('in view '), h('b', { text: fmtCompact(vol[t.topic_id] || 0) })]));
      mt.appendChild(h('span', {}, [txt('alerts '), h('b', { text: String(alerts[t.topic_id] || 0) })]));
      card.appendChild(mt);
      attachDrill(card, { view: 'topic', params: { topic_id: t.topic_id },
        title: t.label || t.topic || t.topic_id });
      grid.appendChild(card);
    });
  };

  /* ---- 14. Hashtags ---- */
  P.tags = function (host) {
    if (!F.tags.length) { empty(host, 'No hashtag rows in this slice.'); return; }
    var by = {};
    F.tags.forEach(function (r) {
      if (!by[r.tag_id]) { by[r.tag_id] = { V: 0, e: 0, en: 0, q: 1, flag: 0, s: 0, sn: 0 }; }
      var b = by[r.tag_id];
      b.V += (r.V || 0);
      if (isNum(r.emergence_score)) { b.e += r.emergence_score; b.en++; }
      if (isNum(r.surprise)) { b.s += r.surprise; b.sn++; }
      if (isNum(r.surprise_q)) { b.q = Math.min(b.q, r.surprise_q); }
      if (r.alert_flag) { b.flag++; }
    });
    var items = Object.keys(by).map(function (id) {
      var b = by[id];
      return { label: tagLabel(id), value: b.V, id: id,
        rows: [{ k: 'V', color: K.focal, v: fmtInt(b.V) },
          { k: 'mean ' + metaName('emergence_score'), v: b.en ? fmt(b.e / b.en, 3) : '—' },
          { k: 'mean surprise', v: b.sn ? fmt(b.s / b.sn, 2) : '—' },
          { k: 'best BH q', v: b.sn ? fmtQ(b.q) : '—' },
          { k: 'flagged days', v: fmtInt(b.flag) }],
        note: 'Hashtag grain covers ~15% of the corpus. Alert on topic_id, not on this.',
        drill: { view: 'tag', params: { tag_id: id }, title: tagLabel(id) } };
    }).sort(function (a, b) { return b.value - a.value; }).slice(0, 20);
    legend(host, [{ color: K.focal, label: 'posts carrying the tag in view', box: true }]);
    hBars(host, { items: items, width: host.clientWidth || 700, measure: 'V',
      vFmt: fmtInt, xFmt: fmtInt, labelW: 130,
      tipNote: 'Top 20 tags by volume in the current slice.' });
    var flat = Object.keys(by).map(function (id) {
      var b = by[id];
      return { tag_id: id, tag: tagLabel(id), V: b.V, e: b.en ? b.e / b.en : null,
        s: b.sn ? b.s / b.sn : null, q: b.sn ? b.q : null, flag: b.flag };
    }).sort(function (a, b) { return b.V - a.V; });
    tableView(host, [
      { k: 'tag', label: 'Tag', strong: true },
      { k: 'V', label: 'Posts', num: true, fmt: fmtInt },
      { k: 'e', label: 'Emergence (mean)', num: true, fmt: function (v) { return fmt(v, 3); } },
      { k: 's', label: 'Surprise (mean)', num: true, fmt: function (v) { return fmt(v, 2); } },
      { k: 'q', label: 'Best BH q', num: true, fmt: fmtQ },
      { k: 'flag', label: 'Flagged days', num: true, fmt: fmtInt }
    ], flat, { label: 'Table view — all tags in slice',
      rowDrill: function (r) { return { view: 'tag', params: { tag_id: r.tag_id }, title: r.tag }; } });
  };

  /* ---- 15. Methodology ---- */
  P.meta_table = function (host) {
    var rows = T('metric_meta');
    if (!rows.length) { empty(host, 'metric_meta is empty — the dashboard cannot describe metrics it has no semantics for.'); return; }
    var fam = {};
    rows.forEach(function (r) { (fam[r.family] = fam[r.family] || []).push(r); });
    Object.keys(fam).sort().forEach(function (f) {
      var d = h('details', { class: 'tv' });
      d.appendChild(h('summary', { text: f + ' — ' + fam[f].length + ' metrics' }));
      var dl = h('dl', { class: 'kv' });
      fam[f].forEach(function (m) {
        var dt = h('dt');
        dt.appendChild(h('b', { text: m.display_name }));
        dt.appendChild(h('div', { class: 'fnote', text: m.metric_key + ' · ' + (m.units || '') }));
        var dd = h('dd');
        dd.appendChild(h('div', {}, [h('span', { class: 'gate', text: m.formula || '—' })]));
        dd.appendChild(h('div', { class: 'fnote',
          text: 'grain: ' + (m.grain || '—') + ' · direction: ' + (m.direction || '—') +
            (m.higher_is_worse ? ' · higher is worse' : '') }));
        dd.appendChild(h('div', {}, [txt('gate: '), h('span', { class: 'gate', text: m.support_gate || 'none' })]));
        if (m.caveats) { dd.appendChild(h('div', { class: 'caveat', text: m.caveats })); }
        dl.appendChild(h('div', {}, [dt, dd]));
      });
      d.appendChild(dl);
      host.appendChild(d);
    });
  };

  P.quality = function (host) {
    var md = F.mdAll;
    var tot = md.length, inw = 0, ok = 0, v2 = 0, vSum = 0;
    md.forEach(function (r) {
      if (r.in_window) { inw++; }
      if (r.support_ok) { ok++; }
      if ((r.V || 0) <= 2) { v2++; }
      vSum += (r.V || 0);
    });
    var rows = [
      { k: 'Cells in selection', v: fmtInt(tot), n: 'topic × platform × day' },
      { k: 'Cells inside collection window', v: fmtInt(inw) + ' (' + fmtPct(inw / (tot || 1), 1) + ')',
        n: 'Window: ' + fmtDayFull(WIN.start) + ' → ' + fmtDayFull(WIN.end) +
          '. Time-series metrics are computed only here; the pre-window backfill is retained for cross-sectional metrics and never used for trends.' },
      { k: 'Cells passing support gate', v: fmtInt(ok) + ' (' + fmtPct(ok / (tot || 1), 1) + ')',
        n: metaGate('surprise') },
      { k: 'Cells with V ≤ 2', v: fmtInt(v2) + ' (' + fmtPct(v2 / (tot || 1), 1) + ')',
        n: 'This is the class of cell that produced |z| = 143 in v1. Here they are gated to NULL instead.' },
      { k: 'Posts represented', v: fmtInt(vSum), n: 'Sum of V across the selection.' },
      { k: 'Multiplicity control', v: 'Benjamini–Hochberg, α = 0.05, per day',
        n: 'Alert thresholds are FDR q-values, never raw z cutoffs.' },
      { k: 'Hate/toxicity family', v: 'BLOCKED — uncalibrated',
        n: 'Gated until reports/calibration_sample.csv is hand-labelled.' },
      { k: 'Reach', v: 'PROXY only', n: 'No follower or subscriber counts exist anywhere in the corpus.' },
      { k: 'Hashtag coverage', v: '~15% of corpus, ~13% of Telegram',
        n: 'Alerting must key on topic_id; hashtag and keyword are secondary facets.' }
    ];
    var dl = h('dl', { class: 'kv' });
    rows.forEach(function (r) {
      dl.appendChild(h('div', {}, [h('dt', { text: r.k }),
        h('dd', {}, [h('div', { class: 'strong', text: r.v }), h('div', { class: 'fnote', text: r.n })])]));
    });
    host.appendChild(dl);
  };

  P.run_meta = function (host) {
    var rm = T('run_meta')[0];
    if (!rm) { empty(host, 'run_meta is empty.'); return; }
    var dl = h('dl', { class: 'kv' });
    function add(k, v) {
      dl.appendChild(h('div', {}, [h('dt', { text: k }), h('dd', { text: v == null ? '—' : String(v) })]));
    }
    add('run_id', rm.run_id);
    add('created_at', rm.created_at);
    add('source_extract_id', rm.source_extract_id);
    add('window_start', fmtDayFull(rm.window_start));
    add('window_end', fmtDayFull(rm.window_end));
    host.appendChild(dl);
    var pj = rm.params_json;
    if (pj) {
      var d = h('details', { class: 'tv' });
      d.appendChild(h('summary', { text: 'Run parameters (params_json)' }));
      var pre = h('pre');
      pre.style.whiteSpace = 'pre-wrap';
      pre.style.fontSize = '0.74rem';
      pre.style.color = 'var(--ink-2)';
      pre.style.maxWidth = '100%';
      pre.style.overflowWrap = 'anywhere';
      try { pre.textContent = JSON.stringify(JSON.parse(pj), null, 2); }
      catch (e) { pre.textContent = String(pj); }
      d.appendChild(pre);
      host.appendChild(d);
    }
  };

  /* ======================================================= geo-ontology === */
  /* Entity directory, risk/theme signal, timeline & alerts. Own DB, own day
     axis (GEODAYS), no platform/topic filter -- reads T('entity_dim') etc.
     straight from DATA.tables, same as topic_cards reads T('topic_dim'). */

  /* ---- taxonomy ------------------------------------------------------
     Display names, ordering, descriptions and alertability all come from
     DATA.taxonomy, which scripts/lib/taxonomy.py emits into metrics.json.

     They used to be four object literals right here, which quietly made this
     file the *effective* schema, and both of its failure modes were silent:

       * `ORDER.filter(function (c) { return present[c]; })` DROPPED any
         category that existed in the data but not in the array -- the panel
         just rendered fewer rows, with no error anywhere.
       * `META[k][0] || k` rendered an undeclared key as a raw snake_case
         string that still looked like a label.

     TAX inverts the direction of failure. `TAX.order()` applies the declared
     order first and then APPENDS anything present in the data but undeclared,
     so a category can never vanish; every such key is recorded in
     `TAX.unknown()` and warned once, so it surfaces as a thing to fix rather
     than as a missing row nobody notices.

     Lookups are resolved per call, not captured at load: DATA is null until
     the metrics fetch resolves (see the assignment in boot), so caching the
     axes here would permanently freeze an empty taxonomy.

     Zero-instance slots (business_executive, railway, pipeline as an entity
     subtype -- distinct from pipeline_vulnerabilities the risk category) are
     still declared, so the full taxonomy stays visible and is honestly marked
     empty rather than omitted. */
  var TAX = (function () {
    var EMPTY = { label: '', order: [], entries: {}, groups: {} };
    var unknown = {};   // axis -> { key: true }
    function ax(a) {
      return (DATA && DATA.taxonomy && DATA.taxonomy.axes && DATA.taxonomy.axes[a]) || EMPTY;
    }
    function ent(a, k) { return ax(a).entries[k] || null; }
    function flag(a, k) {
      var seen = unknown[a] = unknown[a] || {};
      if (!seen[k]) {
        seen[k] = true;
        if (window.console && console.warn) {
          console.warn('[taxonomy] "' + k + '" appears in the data but is not declared in axis "'
            + a + '" -- rendering the raw key. Add it to scripts/lib/taxonomy.py.');
        }
      }
    }
    return {
      axis: ax,
      entry: ent,
      label: function (a) { return ax(a).label || a; },
      status: function (a) { return ax(a).status || 'unknown'; },
      groups: function (a) { return ax(a).groups || {}; },
      /* Undeclared key -> still render the raw key, so the row exists, but
         record it. Never silently substitute something prettier. */
      name: function (a, k) {
        var e = ent(a, k);
        if (e) { return e.display; }
        if (k === null || k === undefined || k === '') { return 'Unresolved'; }
        flag(a, k);
        return String(k);
      },
      desc: function (a, k) { var e = ent(a, k); return (e && e.description) || ''; },
      /* The exact prompt the bi-encoder was given, and the exact NLI
         hypothesis -- so a label is auditable in the UI, not just asserted. */
      prompt: function (a, k) { var e = ent(a, k); return (e && e.model_description) || ''; },
      hypothesis: function (a, k) { var e = ent(a, k); return (e && e.hypothesis) || ''; },
      code: function (a, k) { var e = ent(a, k); return (e && e.code) || ''; },
      /* Classes the taxonomy declares un-alertable (measured too sparse to
         clear the support gate) carry the reason as data, so the UI can say
         why a panel is empty instead of implying nothing happened. */
      alertable: function (a, k) { var e = ent(a, k); return !e || e.alertable !== false; },
      alertableReason: function (a, k) { var e = ent(a, k); return (e && e.alertable_reason) || ''; },
      /* present: a truthy map { key: 1 } or an array of keys. Omit to get the
         full declared order (used for taxonomy cards, which show empty slots). */
      order: function (a, present) {
        var declared = ax(a).order || [];
        if (present === null || present === undefined) { return declared.slice(); }
        var set = present;
        if (Object.prototype.toString.call(present) === '[object Array]') {
          set = {};
          present.forEach(function (k) { set[k] = 1; });
        }
        var out = [], seen = {};
        declared.forEach(function (k) { if (set[k]) { out.push(k); seen[k] = 1; } });
        Object.keys(set).forEach(function (k) {
          if (set[k] && !seen[k]) { flag(a, k); out.push(k); }
        });
        return out;
      },
      /* Union mode: every declared key PLUS anything present in the data.
         For panels that deliberately show empty slots (the risk strip renders
         all nine with an "Insufficient data" state rather than hiding the ones
         with no rows -- absence is itself information). `order(a, present)`
         would hide those, so the two modes are kept distinct instead of one
         call quietly doing both. */
      orderAll: function (a, present) {
        var set = {};
        (ax(a).order || []).forEach(function (k) { set[k] = 1; });
        if (present) {
          Object.keys(present).forEach(function (k) { if (present[k]) { set[k] = 1; } });
        }
        return this.order(a, set);
      },
      unknown: function () { return unknown; }
    };
  })();
  window.__TAX__ = TAX;   // jsdom harnesses assert TAX.unknown() stays empty

  function riskName(k) { return TAX.name('risk', k); }
  function riskDesc(k) { return TAX.desc('risk', k); }
  function themeName(k) { return TAX.name('theme', k); }
  function themeDesc(k) { return TAX.desc('theme', k); }
  function subtypeName(k) { return TAX.name('entity_subtype', k); }

  /* dim_type -> (taxonomy axis, drill query param). This registry is the
     extension point: a new dimension needs one line here, not a new branch in
     each of the three places that used to resolve names with a ternary chain.
     Those chains were binary and fell through to themeName / the raw key, so a
     CAMEO row would have rendered its label through the *wrong* axis or shown a
     bare "05.2" -- both silent. Dimensions absent from this map are not
     taxonomy-backed: entity uids and topic cluster ids are data, not declared
     classes, so they name themselves. */
  var DIM_AXIS = {
    risk: { axis: 'risk', param: 'category' },
    theme: { axis: 'theme', param: 'category' },
    theme_code: { axis: 'theme_code', param: 'code' },
    theme_subcode: { axis: 'theme_subcode', param: 'code' },
    action: { axis: 'action', param: 'code' }
  };

  /* jsdom harness hook -- see UI/tests/check_dim_labels.mjs. These resolvers
     fail soft, so the only way to catch a mis-wired axis is to call them. */
  window.__DIM__ = { axis: DIM_AXIS, name: dimName, drill: dimDrill,
                     renderRelated: renderRelated };

  function dimName(dimType, key) {
    var d = DIM_AXIS[dimType];
    if (d) { return TAX.name(d.axis, key); }
    if (dimType === 'topic') { return topicLabel(key); }
    return key;
  }

  /* The drill param differs by axis (category vs code); sending the wrong one
     silently returns the unfiltered view rather than erroring. */
  function dimDrill(dimType, key, title) {
    var d = DIM_AXIS[dimType];
    var params = {};
    params[(d && d.param) || (dimType === 'entity' ? 'entity_uid' : 'category')] = key;
    return { view: dimType, params: params, title: title || dimName(dimType, key) };
  }

  /* Generic clickable taxonomy card -- used for entity subtypes, risk
     categories and thematic dimensions so the full taxonomy is always laid
     out, even where a slot currently has zero instances (shown dimmed, not
     omitted). Clicking scrolls + flashes the matching row/facet elsewhere
     on the page (data-jump="<anchor id>"). */
  function taxCard(cfg) {
    var isEmpty = !cfg.value;
    var card = h('button', {
      class: 'taxcard' + (isEmpty ? ' tax-empty' : ''), type: 'button',
      'aria-pressed': 'false', title: cfg.desc || ''
    });
    card.appendChild(h('div', { class: 'tc-name', text: cfg.name }));
    card.appendChild(h('div', { class: 'tc-row' }, [
      h('span', { text: cfg.metaLabel || '' }),
      h('span', { class: 'tc-val', text: isEmpty ? 'no instances yet' : cfg.valueFmt(cfg.value) })
    ]));
    if (cfg.hoverRows) {
      card.addEventListener('mousemove', function (ev) { TIP.show(ev, tipNode(cfg.name, cfg.hoverRows, cfg.desc)); });
      card.addEventListener('mouseleave', TIP.hide);
    }
    if (!isEmpty && cfg.drill) {
      card.addEventListener('click', function () { DRILL.open(cfg.drill); });
    } else if (!isEmpty && cfg.jumpTo) {
      card.addEventListener('click', function () { jumpFlash(cfg.jumpTo); });
    }
    return card;
  }
  function jumpFlash(id) {
    var el2 = document.getElementById(id);
    if (!el2) { return; }
    if (typeof el2.scrollIntoView === 'function') { el2.scrollIntoView({ behavior: 'smooth', block: 'center' }); }
    el2.classList.remove('tax-flash');
    void el2.offsetWidth; /* restart animation on repeat clicks */
    el2.classList.add('tax-flash');
    setTimeout(function () { el2.classList.remove('tax-flash'); }, 1300);
  }

  /* ---- 16. Entity directory ---- */
  P.entity_top_bars = function (host) {
    var rows = T('entity_dim');
    if (!rows.length) { empty(host, 'entity_dim is empty (or every entity was seen in only 1 post).'); return; }

    /* Full taxonomy map: every subtype slot from the original spec, dimmed
       where the count is zero, so the shape of what was asked for is always
       visible -- not just whatever happens to have data today. */
    var subCounts = {}; var subDistinctPosts = {};
    rows.forEach(function (r) {
      var k = r.entity_subtype;
      if (!k) { return; }
      subCounts[k] = (subCounts[k] || 0) + (r.mention_count || 0);
    });
    host.appendChild(h('div', { class: 'lede', style: 'margin:0 0 0.5rem' }, [
      txt('Taxonomy coverage -- every subtype the schema defines, click a populated one to jump to its row below.')
    ]));
    ['PERSON', 'ORG', 'LOCATION'].forEach(function (baseType) {
      host.appendChild(h('div', { class: 'taxgroup-lbl', text: baseType + ' subtypes' }));
      var grid = h('div', { class: 'taxgrid' });
      host.appendChild(grid);
      (TAX.groups('entity_subtype')[baseType] || []).forEach(function (sk) {
        grid.appendChild(taxCard({
          name: subtypeName(sk), metaLabel: 'mentions', value: subCounts[sk] || 0,
          valueFmt: fmtInt, desc: baseType + ' subtype', jumpTo: 'entity-type-' + baseType,
          drill: { view: 'entity_subtype', params: { entity_subtype: sk }, title: subtypeName(sk) }
        }));
      });
    });

    var types = ['LOCATION', 'ORG', 'PERSON'];
    var grid = h('div', { class: 'grid g3' });
    host.appendChild(grid);
    types.forEach(function (ty) {
      var cell = h('div', { class: 'facet', id: 'entity-type-' + ty });
      cell.appendChild(h('div', { class: 'facet-t' }, [h('span', { text: ty })]));
      var plot = h('div', { class: 'plot' });
      cell.appendChild(plot);
      grid.appendChild(cell);
      var top = rows.filter(function (r) { return r.entity_type === ty; })
        .sort(function (a, b) { return (b.mention_count || 0) - (a.mention_count || 0); })
        .slice(0, 10);
      if (!top.length) { empty(plot, 'No ' + ty + ' entities.'); return; }
      var items = top.map(function (r) {
        return {
          label: r.canonical_name, value: r.mention_count,
          rows: [
            { k: 'subtype', v: subtypeName(r.entity_subtype) },
            { k: 'subtype source', v: r.subtype_source || '—' },
            { k: 'distinct posts', v: fmtInt(r.distinct_posts) },
            { k: 'avg NER score', v: fmt(r.avg_ner_score, 3) }
          ],
          note: 'first seen ' + fmtDayFull(r.first_seen) + ' · last seen ' + fmtDayFull(r.last_seen),
          drill: { view: 'entity', params: { entity_uid: r.entity_uid }, title: r.canonical_name }
        };
      });
      hBars(plot, { items: items, width: plot.clientWidth || 240, measure: 'mentions',
        vFmt: fmtInt, xFmt: fmtInt, labelW: 104, rowH: 18 });
    });
  };

  /* ---- 16b. Entity directory search ---- */
  P.entity_search = function (host) {
    var rows = T('entity_dim');
    var box = h('div', { class: 'searchbox' });
    var input = h('input', { type: 'text', placeholder: 'Filter entity directory by name or subtype…' });
    var count = h('span', { class: 'n', text: rows.length.toLocaleString() + ' entities' });
    box.appendChild(input); box.appendChild(count);
    host.appendChild(box);
    var resultsHost = h('div');
    host.appendChild(resultsHost);
    function run() {
      var q = input.value.trim().toLowerCase();
      resultsHost.textContent = '';
      if (!q) { count.textContent = rows.length.toLocaleString() + ' entities'; return; }
      var hits = rows.filter(function (r) {
        return (r.canonical_name || '').toLowerCase().indexOf(q) >= 0 ||
          subtypeName(r.entity_subtype).toLowerCase().indexOf(q) >= 0 ||
          (r.entity_type || '').toLowerCase().indexOf(q) >= 0;
      }).sort(function (a, b) { return (b.mention_count || 0) - (a.mention_count || 0); }).slice(0, 40);
      count.textContent = hits.length.toLocaleString() + ' of ' + rows.length.toLocaleString() + ' match';
      if (!hits.length) { empty(resultsHost, 'No entity matches "' + q + '".'); return; }
      tableView(resultsHost, [
        { k: 'canonical_name', label: 'Entity', strong: true },
        { k: 'entity_type', label: 'Type' },
        { k: 'entity_subtype', label: 'Subtype', fmt: subtypeName },
        { k: 'mention_count', label: 'Mentions', num: true, fmt: fmtInt },
        { k: 'distinct_posts', label: 'Posts', num: true, fmt: fmtInt }
      ], hits, { label: 'Search results (top 40 by mentions)', max: 40,
        rowDrill: function (r) { return { view: 'entity', params: { entity_uid: r.entity_uid }, title: r.canonical_name }; } });
    }
    input.addEventListener('input', run);
  };

  P.entity_directory_table = function (host) {
    var rows = T('entity_dim');
    if (!rows.length) { empty(host, 'entity_dim is empty.'); return; }
    tableView(host, [
      { k: 'canonical_name', label: 'Entity', strong: true },
      { k: 'entity_type', label: 'Type' },
      { k: 'entity_subtype', label: 'Subtype', fmt: subtypeName },
      { k: 'subtype_source', label: 'Subtype source', fmt: function (v) { return v || '—'; } },
      { k: 'mention_count', label: 'Mentions', num: true, fmt: fmtInt },
      { k: 'distinct_posts', label: 'Posts', num: true, fmt: fmtInt },
      { k: 'avg_ner_score', label: 'Avg NER score', num: true, fmt: function (v) { return fmt(v, 3); } },
      { k: 'first_seen', label: 'First seen', fmt: fmtDayFull },
      { k: 'last_seen', label: 'Last seen', fmt: fmtDayFull }
    ], rows, { label: 'Table view — ' + rows.length.toLocaleString() + ' entities (≥2 distinct posts), sorted by mentions', max: 500,
      rowDrill: function (r) { return { view: 'entity', params: { entity_uid: r.entity_uid }, title: r.canonical_name }; } });
  };

  /* ---- 17. Risk categories & thematic dimensions ---- */

  /* Shared card grid for the 9 risk categories / 12 themes -- called by both
     the standalone taxonomy overview and reused wherever a compact category
     picker is useful. Value = latest-day volume; hover shows the fuller
     picture (latest surprise/q, active alert count) without leaving the card. */
  function categoryOverview(host, opts) {
    var rows = T(opts.table);
    var catKey = opts.catKey; /* 'risk_category' | 'theme_category' */
    var byCat = {};
    rows.forEach(function (r) {
      var c = r[catKey];
      (byCat[c] = byCat[c] || []).push(r);
    });
    var alerts = T('geo_alert_candidates').filter(function (a) { return a.dimension_type === opts.dimType; });
    var alertCount = {};
    alerts.forEach(function (a) { alertCount[a.dimension_key] = (alertCount[a.dimension_key] || 0) + 1; });
    host.appendChild(h('div', { class: 'taxgroup-lbl', text: opts.groupLabel }));
    var grid = h('div', { class: 'taxgrid' });
    host.appendChild(grid);
    opts.order.forEach(function (k) {
      var crows = (byCat[k] || []).slice().sort(function (a, b) { return day(b.day) < day(a.day) ? -1 : 1; });
      var totalV = crows.reduce(function (s, r) { return s + (r.V || 0); }, 0);
      var latest = crows[0];
      var hoverRows = latest ? [
        { k: 'latest day V', v: fmtInt(latest.V) },
        { k: 'latest surprise', v: isNum(latest.surprise) ? fmt(latest.surprise, 2) : 'gated (no support)' },
        { k: 'alert-days (FDR)', v: fmtInt(alertCount[k] || 0) },
        { k: 'total posts in window', v: fmtInt(totalV) }
      ] : [{ k: 'status', v: 'no in-window data yet' }];
      grid.appendChild(taxCard({
        name: opts.nameFn(k), metaLabel: 'window volume', value: totalV,
        valueFmt: fmtInt, desc: opts.descFn(k), hoverRows: hoverRows,
        jumpTo: opts.jumpTo,
        drill: { view: opts.dimType, params: { category: k }, title: opts.nameFn(k) }
      }));
    });
  }

  P.risk_overview = function (host) {
    categoryOverview(host, {
      table: 'risk_metrics_daily', catKey: 'risk_category', dimType: 'risk',
      order: TAX.order('risk'), nameFn: riskName, descFn: riskDesc,
      groupLabel: 'Risk assessment — 9 categories', jumpTo: 'risk-heatmap-wrap'
    });
  };
  P.theme_overview = function (host) {
    categoryOverview(host, {
      table: 'theme_metrics_daily', catKey: 'theme_category', dimType: 'theme',
      order: TAX.order('theme'), nameFn: themeName, descFn: themeDesc,
      groupLabel: 'Thematic & relational dimensions — 12 categories', jumpTo: 'theme-facets-wrap'
    });
  };

  P.risk_heat = function (host) {
    var rows = T('risk_metrics_daily');
    if (!rows.length) { empty(host, 'risk_metrics_daily is empty.'); return; }
    var wrap = h('div', { id: 'risk-heatmap-wrap' });
    host.appendChild(wrap);
    host = wrap;
    var catSet = {}; rows.forEach(function (r) { catSet[r.risk_category] = 1; });
    var cats = TAX.order('risk', catSet);
    var byKey = {};
    rows.forEach(function (r) { byKey[r.risk_category + '|' + day(r.day)] = r; });
    var vals = rows.filter(function (r) { return isNum(r.surprise); }).map(function (r) { return r.surprise; });
    if (!vals.length) { empty(host, 'No supported surprise value — every cell failed its support gate.'); return; }
    var ex = extent(vals), lo = 0, hi = ex[1];
    rampLegend(host, lo, hi, K.seq, 'surprise (NB exceedance, higher = more anomalous)', function (v) { return fmt(v, 1); });
    legend(host, [
      { color: K.ink, label: 'white outline = alert_flag (BH q < 0.05)', box: true },
      { hatch: true, label: 'hatched = no value: support gate not met' }
    ]);
    heatmap(host, {
      rows: cats.map(function (c) { return { id: c, label: riskName(c) }; }),
      cols: GEODAYS.map(function (d) { return { id: d, label: fmtDayFull(d), short: fmtDay(d) }; }),
      cellW: 13, cellH: 21, labelW: 192,
      color: function (v) { return seqColor((v - lo) / ((hi - lo) || 1)); },
      get: function (r, c) {
        var b = byKey[r.id + '|' + c.id];
        var drill = { view: 'risk', params: { category: r.id, day: c.id }, title: r.label, subtitle: c.label };
        if (!b || !isNum(b.surprise)) {
          return { v: null,
            rows: [{ k: 'V', v: fmtInt(b && b.V) }, { k: 'surprise', v: 'NULL — support gate not met' }],
            note: 'gate: baseline_days >= 14 & baseline_mean >= 0.5 & V >= 3', drill: drill };
        }
        return { v: b.surprise, ring: !!b.alert_flag,
          rows: [
            { k: 'surprise', color: seqColor((b.surprise - lo) / ((hi - lo) || 1)), v: fmt(b.surprise, 2) },
            { k: 'BH q', v: fmtQ(b.surprise_q) },
            { k: 'V', v: fmtInt(b.V) },
            { k: 'gray_zone share', v: fmtPct(b.gray_zone_share, 0) },
            { k: 'alert', v: b.alert_flag ? 'FLAGGED (q < 0.05)' : 'not flagged' }
          ], drill: drill };
      },
      measure: 'surprise', xLabelCount: 8
    });
    var flat = rows.map(function (r) {
      return { risk_category: r.risk_category, category: riskName(r.risk_category), day: day(r.day),
        V: r.V, surprise: r.surprise, q: r.surprise_q, flag: r.alert_flag };
    }).sort(function (a, b) { return (isNum(b.surprise) ? b.surprise : -1) - (isNum(a.surprise) ? a.surprise : -1); });
    tableView(host, [
      { k: 'day', label: 'Day', fmt: fmtDayFull },
      { k: 'category', label: 'Risk category', strong: true },
      { k: 'V', label: 'V', num: true, fmt: fmtInt },
      { k: 'surprise', label: 'Surprise', num: true, fmt: function (v) { return fmt(v, 2); } },
      { k: 'q', label: 'BH q', num: true, fmt: fmtQ },
      { k: 'flag', label: 'Alert', fmt: function (v) { return v ? 'yes' : 'no'; } }
    ], flat, { label: 'Table view — risk category × day', max: 500,
      rowDrill: function (r) {
        return { view: 'risk', params: { category: r.risk_category, day: r.day }, title: r.category, subtitle: fmtDayFull(r.day) };
      } });
  };

  P.theme_facets = function (host) {
    var rows = T('theme_metrics_daily');
    if (!rows.length) { empty(host, 'theme_metrics_daily is empty.'); return; }
    var wrap = h('div', { id: 'theme-facets-wrap' });
    host.appendChild(wrap);
    host = wrap;
    var catSet = {}; rows.forEach(function (r) { catSet[r.theme_category] = 1; });
    var cats = TAX.order('theme', catSet);
    var byCat = {};
    cats.forEach(function (c) { byCat[c] = {}; });
    rows.forEach(function (r) { byCat[r.theme_category][day(r.day)] = r; });
    var facets = cats.map(function (c) {
      var pts = GEODAYS.map(function (d, i) {
        var r = byCat[c][d];
        return { i: i, v: (r && isNum(r.share_V)) ? r.share_V : null };
      });
      var ribbon = GEODAYS.map(function (d, i) {
        var r = byCat[c][d];
        return { i: i, on: !!(r && r.burst_state === 1) };
      });
      var latestV = null, latestQ = null;
      for (var k = GEODAYS.length - 1; k >= 0; k--) {
        var r = byCat[c][GEODAYS[k]];
        if (r) { latestV = r.V; latestQ = r.surprise_q; break; }
      }
      return {
        id: 'theme-facet-' + c, title: themeName(c), note: 'V=' + fmtInt(latestV),
        series: [{ label: 'share of labelled posts', role: 'focal', kind: 'area', pts: pts }],
        ribbon: ribbon,
        sub: 'best-day q ' + fmtQ(latestQ),
        drill: { view: 'theme', params: { category: c }, title: themeName(c) }
      };
    });
    legend(host, [
      { color: K.focal, label: 'share of that day’s labelled posts carrying this theme', box: true },
      { color: K.focal, label: 'ribbon under axis = burst-flagged day', box: true }
    ]);
    smallMultiples(host, {
      facets: facets, days: GEODAYS, cols: 3, height: 112, zeroBase: true,
      yFmt: function (v) { return fmtPct(v, 0); }, tipFmt: function (v) { return fmtPct(v, 1); },
      tipNote: 'share_V: share of that day’s risk/theme-labelled posts carrying this theme. Labels are UNSOUND per verification (§17) — read the shape, not the boundary.'
    });
  };

  function dimKeyName(dimType, key) { return dimName(dimType, key); }

  /* ---- 18. Timeline Analysis — entity / risk / theme alerts over time ---- */
  P.geo_timeline = function (host) {
    var alerts = T('geo_alert_candidates');
    if (!alerts.length) { empty(host, 'No FDR-gated geo-ontology alerts.'); return; }

    function laneChart(dimType, catRows, title) {
      var pts = [];
      alerts.forEach(function (a) {
        if (a.dimension_type !== dimType) { return; }
        pts.push({
          rowId: a.dimension_key, day: day(a.day),
          color: a.severity === 'high' ? K.focal : (a.severity === 'critical' ? K.neg : K.context),
          r: a.severity === 'high' ? 5.5 : 4,
          label: dimKeyName(dimType, a.dimension_key) + ' · ' + fmtDayFull(a.day),
          rows: [
            { k: 'severity', color: a.severity === 'high' ? K.focal : K.context, v: a.severity },
            { k: 'metric', v: a.metric }, { k: 'value', v: fmt(a.value, 2) },
            { k: 'BH q', v: fmtQ(a.q_value) }, { k: 'support n', v: fmtInt(a.support_n) }
          ],
          note: a.rationale,
          drill: { view: 'alert', params: { dimension_type: a.dimension_type, dimension_key: a.dimension_key, day: day(a.day) },
            title: dimKeyName(dimType, a.dimension_key), subtitle: fmtDayFull(a.day) }
        });
      });
      var wrap = h('div', { style: 'margin-top:0.7rem', id: dimType === 'risk' ? 'timeline-lane-risk'
        : dimType === 'theme' ? 'timeline-lane-theme' : null });
      wrap.appendChild(h('div', { class: 'facet-t' }, [h('span', { text: title }),
        h('span', { class: 'n', text: catRows.length + ' rows · ' + pts.length + ' alert-days' })]));
      host.appendChild(wrap);
      if (!catRows.length) { empty(wrap, 'No ' + dimType + ' rows.'); return; }
      swimlane(wrap, { rows: catRows, days: GEODAYS, points: pts,
        width: host.clientWidth || 760, labelW: 172, rowH: 19 });
    }

    legend(host, [
      { color: K.focal, label: 'high severity (q < 0.01)', box: true },
      { color: K.context, label: 'medium severity (q < 0.05)', box: true }
    ]);

    var riskSrc = T('risk_metrics_daily');
    var riskSet = {}; riskSrc.forEach(function (r) { riskSet[r.risk_category] = 1; });
    if (!riskSrc.length) { alerts.forEach(function (a) { if (a.dimension_type === 'risk') { riskSet[a.dimension_key] = 1; } }); }
    laneChart('risk',
      TAX.order('risk', riskSet).map(function (id) { return { id: id, label: riskName(id) }; }),
      'Risk categories (9)');

    var themeSrc = T('theme_metrics_daily');
    var themeSet = {}; themeSrc.forEach(function (r) { themeSet[r.theme_category] = 1; });
    if (!themeSrc.length) { alerts.forEach(function (a) { if (a.dimension_type === 'theme') { themeSet[a.dimension_key] = 1; } }); }
    laneChart('theme',
      TAX.order('theme', themeSet).map(function (id) { return { id: id, label: themeName(id) }; }),
      'Thematic & relational dimensions (12)');

    var entCounts = {};
    alerts.forEach(function (a) {
      if (a.dimension_type === 'entity') { entCounts[a.dimension_key] = (entCounts[a.dimension_key] || 0) + 1; }
    });
    var topEnt = Object.keys(entCounts).sort(function (a, b) { return entCounts[b] - entCounts[a]; }).slice(0, 20);
    laneChart('entity', topEnt.map(function (id) { return { id: id, label: id }; }), 'Top 20 entities by alert-day count');
  };

  P.geo_alerts_table = function (host) {
    var rows = T('geo_alert_candidates').slice().sort(function (a, b) {
      var oa = a.severity === 'high' ? 0 : (a.severity === 'critical' ? -1 : 1);
      var ob = b.severity === 'high' ? 0 : (b.severity === 'critical' ? -1 : 1);
      if (oa !== ob) { return oa - ob; }
      return (isNum(a.q_value) ? a.q_value : 1) - (isNum(b.q_value) ? b.q_value : 1);
    });
    if (!rows.length) { empty(host, 'No geo-ontology alert candidates.'); return; }
    var box = h('div', { class: 'searchbox' });
    var input = h('input', { type: 'text', placeholder: 'Filter alerts by category or entity…' });
    var count = h('span', { class: 'n', text: rows.length.toLocaleString() + ' alerts' });
    box.appendChild(input); box.appendChild(count);
    host.appendChild(box);
    var resultsHost = h('div');
    host.appendChild(resultsHost);
    var cols = [
      { k: 'severity', label: 'Severity', node: function (v) { return sevChip(v); } },
      { k: 'day', label: 'Day', fmt: fmtDayFull },
      { k: 'dimension_type', label: 'Grain' },
      { k: 'dimension_key', label: 'Entity / category', strong: true,
        fmt: function (v, r) { return dimKeyName(r.dimension_type, v); } },
      { k: 'metric', label: 'Metric' },
      { k: 'value', label: 'Value', num: true, fmt: function (v) { return fmt(v, 3); } },
      { k: 'q_value', label: 'q-value', num: true, fmt: fmtQ },
      { k: 'support_n', label: 'Support n', num: true, fmt: fmtInt },
      { k: 'rationale', label: 'Rationale', wrap: true }
    ];
    function run() {
      var q = input.value.trim().toLowerCase();
      resultsHost.textContent = '';
      var shown = rows;
      if (q) {
        shown = rows.filter(function (r) {
          return dimKeyName(r.dimension_type, r.dimension_key).toLowerCase().indexOf(q) >= 0 ||
            (r.dimension_type || '').toLowerCase().indexOf(q) >= 0;
        });
      }
      count.textContent = q ? (shown.length.toLocaleString() + ' of ' + rows.length.toLocaleString() + ' match')
        : rows.length.toLocaleString() + ' alerts';
      tableView(resultsHost, cols, shown, { label: 'Table view — ' + shown.length + ' of ' + rows.length + ' geo-ontology alert candidates', max: 500,
        rowDrill: function (r) {
          return { view: 'alert', params: { dimension_type: r.dimension_type, dimension_key: r.dimension_key, day: day(r.day) },
            title: dimKeyName(r.dimension_type, r.dimension_key), subtitle: fmtDayFull(r.day) };
        } });
    }
    input.addEventListener('input', run);
    run();
  };

  /* ================================================================ basic == */

  /* Overview-mode filter state. `attentionOnly` narrows every Overview panel to
     just the dimensions that currently carry an alert, so "Needs attention"
     behaves like a filter (show me only these) rather than a jump. */
  var BASIC = { attentionOnly: false };

  /* Which risk / theme / entity / topic keys are currently alerting. */
  function attentionKeys() {
    var out = { risk: {}, theme: {}, entity: {}, topic: {}, n: 0 };
    T('geo_alert_candidates').forEach(function (a) {
      var b = out[a.dimension_type];
      if (b && !b[a.dimension_key]) { b[a.dimension_key] = true; out.n++; }
    });
    T('alert_candidates').forEach(function (a) {
      if (a.topic_id && !out.topic[a.topic_id]) { out.topic[a.topic_id] = true; out.n++; }
    });
    return out;
  }

  /* Re-render only the Overview panels (cheaper than a full render(), and it
     keeps the Advanced view's scroll position untouched). */
  function renderBasic() {
    document.querySelectorAll('#basic-view [data-panel]').forEach(function (host) {
      var key = host.getAttribute('data-panel');
      var fn = P[key];
      if (!fn) { return; }
      host.textContent = '';
      try { fn(host); } catch (e) {
        empty(host, 'Render error in panel "' + key + '": ' + e.message);
        if (window.console) { console.error(key, e); }
      }
    });
  }

  /* Single-screen, plain-language overview. No jargon (no "NB surprise", no
     "gray_zone", no q-values in prose), no verification caveats -- that detail
     lives one click away in Advanced. Reads the same tables as the Advanced
     view, just summarised and named for a reader seeing this for the first time. */

  P.basic_updated = function (host) {
    var from = S.from ? fmtDay(S.from) : '';
    var to = S.to ? fmtDay(S.to) : '';
    var rangeStr = (from && to) ? (from + ' – ' + to) : (DATA.window ? fmtDay(DATA.window.start) + ' – ' + fmtDay(DATA.window.end) : '');
    var gen = (DATA.generated_at || '').slice(0, 10);
    host.appendChild(h('span', { text: 'Showing activity: ' + rangeStr +
      (gen ? ' · snapshot taken ' + gen : '') }));
    if (BASIC.attentionOnly) {
      var ak = attentionKeys();
      var banner = h('div', { class: 'filter-banner' });
      banner.appendChild(h('span', { text: 'Filtered to items needing attention (' + ak.n + ')' }));
      var clear = h('button', { class: 'chip', type: 'button', text: 'Show everything' });
      clear.addEventListener('click', function () { BASIC.attentionOnly = false; renderBasic(); });
      banner.appendChild(clear);
      host.appendChild(banner);
    }
  };

  P.basic_kpis = function (host) {
    var md = (F.md && F.md.length) ? F.md : (F.days && F.days.length ? F.md : T('metrics_daily'));
    var corpus = 0; md.forEach(function (r) { corpus += (r.V || 0); });
    var fromD = S.from, toD = S.to;
    var inRange = function (r) { var d = day(r.day || r.date || ''); return (!fromD || d >= fromD) && (!toD || d <= toD); };
    var geoAlerts = T('geo_alert_candidates').filter(inRange);
    var topicAlerts = (F.alerts !== undefined) ? F.alerts : T('alert_candidates').filter(inRange);
    var alerts = geoAlerts.length + topicAlerts.length;
    var crit = geoAlerts.filter(function (a) { return a.severity === 'critical'; }).length +
      topicAlerts.filter(function (a) { return a.severity === 'critical'; }).length;
    var activePlats = {};
    md.forEach(function (r) { if ((r.V || 0) > 0) activePlats[r.platform] = true; });
    var platforms = Object.keys(activePlats).length || selPlatforms().length || T('platform_dim').length;
    var toneVals = md.filter(function (r) { return isNum(r.tone_eb); }).map(function (r) { return r.tone_eb; });
    var toneAvg = toneVals.length ? toneVals.reduce(function (a, b) { return a + b; }, 0) / toneVals.length : null;
    var toneLabel = toneAvg == null ? 'No data' : (toneAvg > 0.05 ? 'Positive' : (toneAvg < -0.05 ? 'Negative' : 'Neutral'));
    var days = F.days && F.days.length ? F.days : (WINDAYS.length ? WINDAYS : ALLDAYS);

    function tile(label, val, sub, warn, drill) {
      var t = h('div', { class: 'kpi-tile' + (warn ? ' warn' : '') }, [
        h('div', { class: 'kt-label', text: label }),
        h('div', { class: 'kt-val', text: String(val) }),
        h('div', { class: 'kt-sub', text: sub || '' })
      ]);
      attachDrill(t, drill);
      return t;
    }
    host.appendChild(tile('Posts Monitored', fmtCompact(corpus), fmtInt(corpus) + ' posts', false,
      { view: 'posts', params: {}, title: 'Recent posts', subtitle: 'Filtered posts in selected window' }));
    host.appendChild(tile('Active Alerts', fmtInt(alerts), alerts ? crit + ' need urgent review' : 'All clear', alerts > 0));
    host.appendChild(tile('Platforms Covered', fmtInt(platforms), 'social, video & forums'));
    host.appendChild(tile('Overall Tone', toneLabel, 'across labelled posts'));
    host.appendChild(tile('Time Window', fmtInt(days.length) + ' days', days.length ? (fmtDay(days[0]) + ' – ' + fmtDay(days[days.length - 1])) : ''));
  };

  P.basic_attention = function (host) {
    var fromD = S.from, toD = S.to;
    var inRange = function (d) { var x = day(d); return (!fromD || x >= fromD) && (!toD || x <= toD); };
    var geoAlerts = T('geo_alert_candidates').filter(function (a) { return inRange(a.day); });
    var topicAlerts = (F.alerts !== undefined) ? F.alerts : T('alert_candidates').filter(function (a) { return inRange(a.day); });
    var alerts = geoAlerts.map(function (a) {
      return { day: a.day, severity: a.severity, dimType: a.dimension_type, key: a.dimension_key, q: a.q_value, rationale: a.rationale };
    }).concat(topicAlerts.map(function (a) {
      return { day: a.day, severity: a.severity, dimType: 'topic', key: a.topic_id, q: a.surprise_q, rationale: null };
    }));
    if (!alerts.length) {
      host.appendChild(h('div', { class: 'attn-box' }, [
        h('div', { class: 'attn-icon', text: '✓' }),
        h('div', {}, [
          h('div', { class: 'attn-hd', text: 'Status' }),
          h('div', { class: 'attn-body', text: 'Nothing unusual detected in the selected window — all tracked signals are within normal range.' })
        ])
      ]));
      return;
    }
    var sevRank = { critical: 0, high: 1, medium: 2 };
    var sorted = alerts.slice().sort(function (a, b) {
      var oa = sevRank[a.severity] != null ? sevRank[a.severity] : 3;
      var ob = sevRank[b.severity] != null ? sevRank[b.severity] : 3;
      if (oa !== ob) { return oa - ob; }
      return (isNum(a.q) ? a.q : 1) - (isNum(b.q) ? b.q : 1);
    });
    var top = sorted[0];
    var name = dimName(top.dimType, top.key);
    var icon = top.severity === 'critical' ? '🔴' : (top.severity === 'high' ? '🟠' : '🟡');
    var box = h('div', { class: 'attn-box' });
    box.appendChild(h('div', { class: 'attn-icon', text: icon }));
    var body = h('div');
    body.appendChild(h('div', { class: 'attn-hd', text: 'Needs attention' }));
    body.appendChild(h('div', { class: 'attn-body' }, [txt('Unusual activity in '), h('b', { text: name }), txt(' on ' + fmtDayFull(top.day) + '.')]));
    body.appendChild(h('div', { class: 'attn-note', text: alerts.length + ' alert' + (alerts.length === 1 ? '' : 's') + ' flagged in the selected window.' + (top.rationale ? ' ' + top.rationale : '') }));
    box.appendChild(body);

    /* Clicking this FILTERS the Overview to the flagged items rather than
       opening a single post list -- pressing "needs attention" should narrow
       what you're looking at, not jump you somewhere else. Toggles. */
    var act = h('div', { class: 'attn-act' });
    var btn = h('button', { class: 'chip', type: 'button',
      text: BASIC.attentionOnly ? '✕ Clear filter — show everything' : 'Show only what needs attention →' });
    btn.setAttribute('aria-pressed', BASIC.attentionOnly ? 'true' : 'false');
    btn.addEventListener('click', function (ev) {
      ev.stopPropagation();
      BASIC.attentionOnly = !BASIC.attentionOnly;
      renderBasic();
    });
    act.appendChild(btn);
    body.appendChild(act);

    host.appendChild(box);
  };

  P.basic_risk_strip = function (host) {
    var fromD = S.from, toD = S.to;
    var inRange = function (r) { var x = day(r.day); return (!fromD || x >= fromD) && (!toD || x <= toD); };
    var rows = T('risk_metrics_daily').filter(inRange);
    var alertSet = {};
    T('geo_alert_candidates').filter(function (a) { return a.dimension_type === 'risk' && inRange(a.day); }).forEach(function (a) { alertSet[a.dimension_key] = true; });
    var byCat = {};
    rows.forEach(function (r) { (byCat[r.risk_category] = byCat[r.risk_category] || []).push(r); });
    var strip = h('div', { class: 'status-strip' });
    host.appendChild(strip);
    var order = TAX.orderAll('risk', byCat).filter(function (k) { return !BASIC.attentionOnly || alertSet[k]; });
    if (!order.length) { empty(host, 'No risk category is currently flagged in this window.'); return; }
    order.forEach(function (k) {
      var crows = byCat[k] || [];
      var hasData = crows.some(function (r) { return (r.V || 0) > 0; });
      var flagged = !!alertSet[k];
      var dotClass = flagged ? 'watch' : (hasData ? 'ok' : 'na');
      var state = flagged ? 'Elevated' : (hasData ? 'Normal' : 'Insufficient data');
      var row = h('div', { class: 'status-row' }, [
        h('span', { class: 'sr-dot ' + dotClass }),
        h('span', { class: 'sr-name', text: riskName(k) }),
        h('span', { class: 'sr-state', text: state })
      ]);
      if (hasData) { attachDrill(row, { view: 'risk', params: { category: k }, title: riskName(k) }); }
      strip.appendChild(row);
    });
  };

  P.basic_theme_bars = function (host) {
    var fromD = S.from, toD = S.to;
    var inRange = function (r) { var x = day(r.day); return (!fromD || x >= fromD) && (!toD || x <= toD); };
    var rows = T('theme_metrics_daily').filter(inRange);
    var totals = {};
    rows.forEach(function (r) { totals[r.theme_category] = (totals[r.theme_category] || 0) + (r.V || 0); });
    var flaggedThemes = attentionKeys().theme;
    var items = TAX.order('theme', totals)
      .filter(function (k) { return totals[k] && (!BASIC.attentionOnly || flaggedThemes[k]); })
      .map(function (k) { return { label: themeName(k), value: totals[k],
        drill: { view: 'theme', params: { category: k }, title: themeName(k) } }; })
      .sort(function (a, b) { return b.value - a.value; });
    if (!items.length) {
      empty(host, BASIC.attentionOnly ? 'No theme is currently flagged.'
        : 'No theme-labelled posts in the selected window.');
      return;
    }
    hBars(host, { items: items, width: host.clientWidth || 360, measure: 'posts',
      vFmt: fmtInt, xFmt: fmtInt, labelW: 168, rowH: 20 });
  };

  P.basic_volume_trend = function (host) {
    var md = (F.md && F.md.length) ? F.md : (F.days && F.days.length ? F.md : T('metrics_daily'));
    var byDay = {};
    md.forEach(function (r) { var d = day(r.day); byDay[d] = (byDay[d] || 0) + (r.V || 0); });
    var days = F.days && F.days.length ? F.days : (WINDAYS.length ? WINDAYS : ALLDAYS);
    if (!days.length) { empty(host, 'No time window available.'); return; }
    var pts = days.map(function (d, i) { return { i: i, v: byDay[d] || 0 }; });
    lineChart(host, {
      days: days, series: [{ label: 'Posts per day', role: 'focal', kind: 'area', pts: pts }],
      width: host.clientWidth || 360, height: 140, zeroBase: true,
      yFmt: fmtCompact, tipFmt: fmtInt, tipHead: 'Posts per day', yTicks: 3, xTicks: Math.min(4, days.length),
      margin: { t: 8, r: 10, b: 22, l: 36 }
    });
    attachDrill(host, { view: 'posts', params: {}, title: 'Recent posts', subtitle: 'Across all platforms in the selected window' });
  };

  P.basic_top_entities = function (host) {
    var flaggedEnt = attentionKeys().entity;
    var rows = T('entity_dim')
      .filter(function (r) { return !BASIC.attentionOnly || flaggedEnt[r.entity_uid]; })
      .sort(function (a, b) { return (b.mention_count || 0) - (a.mention_count || 0); })
      .slice(0, 8);
    if (!rows.length) {
      empty(host, BASIC.attentionOnly ? 'No entity is currently flagged.' : 'No entities extracted yet.');
      return;
    }
    var list = h('div', { class: 'entity-list' });
    host.appendChild(list);
    rows.forEach(function (r) {
      var row = h('div', { class: 'el-row' }, [
        h('span', { class: 'el-name', text: r.canonical_name }),
        h('span', { class: 'el-type', text: r.entity_type }),
        h('span', { class: 'el-n', text: fmtInt(r.mention_count) })
      ]);
      attachDrill(row, { view: 'entity', params: { entity_uid: r.entity_uid }, title: r.canonical_name });
      list.appendChild(row);
    });
  };

  /* "Recommended for review" -- a transparent re-ranking of the alert tables already
     loaded (no new queries), not a black-box model. Blends severity, recency, and how
     unusual the signal is (q-value) into one explainable score, merges the
     entity/risk/theme grain (geo_alert_candidates) with the topic/tag grain
     (alert_candidates) into a single feed, and dedupes so the same dimension doesn't
     show up twice. Clicking an item opens the same drill panel as everywhere else --
     which now also shows related items (see renderRelated), tying the two recommender
     pieces together. */
  function sevWeight(sev) { return sev === 'critical' ? 3 : (sev === 'high' ? 2 : (sev === 'medium' ? 1 : 0)); }
  function daysAgo(dayStr, todayStr) {
    var d = Date.parse(day(dayStr)), t = Date.parse(day(todayStr));
    if (!isFinite(d) || !isFinite(t)) { return 0; }
    return Math.max(0, Math.round((t - d) / 86400000));
  }
  function recencyText(n) { return n <= 0 ? 'today' : (n === 1 ? '1 day ago' : n + ' days ago'); }
  function magnitudeText(q) {
    if (!isNum(q)) { return 'unusual activity'; }
    if (q < 0.001) { return 'extremely unusual activity'; }
    if (q < 0.01) { return 'highly unusual activity'; }
    return 'unusual activity';
  }

  P.basic_alerts_list = function (host) {
    var geo = T('geo_alert_candidates'), metrics = T('alert_candidates');
    var maxDay = null;
    geo.concat(metrics).forEach(function (a) {
      var d = day(a.day);
      if (!maxDay || d > maxDay) { maxDay = d; }
    });
    if (!maxDay) { empty(host, 'No alerts to show — nothing has crossed its baseline recently.'); return; }

    var byKey = {};
    function consider(key, item) {
      var ago = daysAgo(item.day, maxDay);
      var score = sevWeight(item.severity) * (0.5 + 0.5 * (1 - (isNum(item.q) ? Math.min(1, item.q) : 1))) / (1 + ago);
      item.score = score;
      item.ago = ago;
      if (!byKey[key] || byKey[key].score < score) { byKey[key] = item; }
    }
    geo.forEach(function (a) {
      var name = a.dimension_type === 'risk' ? riskName(a.dimension_key)
        : a.dimension_type === 'theme' ? themeName(a.dimension_key) : a.dimension_key;
      consider('geo|' + a.dimension_type + '|' + a.dimension_key, {
        day: a.day, severity: a.severity, q: a.q_value, name: name, dimType: a.dimension_type,
        drill: { view: 'alert', params: { dimension_type: a.dimension_type, dimension_key: a.dimension_key, day: day(a.day) },
          title: name, subtitle: fmtDayFull(a.day) }
      });
    });
    metrics.forEach(function (a) {
      var name = topicLabel(a.topic_id) + ' · ' + a.platform;
      consider('metrics|' + a.topic_id + '|' + a.platform, {
        day: a.day, severity: a.severity, q: a.surprise_q, name: name, dimType: 'topic',
        drill: { view: 'topic', params: { topic_id: a.topic_id, platform: a.platform, day: day(a.day) },
          title: name, subtitle: fmtDayFull(a.day) }
      });
    });

    var ranked = Object.keys(byKey).map(function (k) { return byKey[k]; })
      .sort(function (a, b) { return b.score - a.score; })
      .slice(0, 8);
    if (!ranked.length) { empty(host, 'No alerts to show — nothing has crossed its baseline recently.'); return; }

    var list = h('div', { class: 'alert-list' });
    host.appendChild(list);
    ranked.forEach(function (a) {
      var rationale = sevChip(a.severity);
      var row = h('div', { class: 'alert-row' }, [
        rationale,
        h('span', { class: 'ar-body' }, [
          h('b', { text: a.name }),
          txt(' — ' + magnitudeText(a.q) + ' · ' + recencyText(a.ago))
        ])
      ]);
      attachDrill(row, a.drill);
      list.appendChild(row);
    });
  };

  function initModeToggle() {
    var basicBtn = document.getElementById('mode-basic-btn');
    var advBtn = document.getElementById('mode-advanced-btn');
    var basicView = document.getElementById('basic-view');
    var advView = document.getElementById('advanced-view');
    var link = document.getElementById('basic-to-advanced-link');
    if (!basicBtn || !advBtn || !basicView || !advView) { return; }
    function setMode(mode) {
      var isBasic = mode !== 'advanced';
      document.body.setAttribute('data-mode', isBasic ? 'basic' : 'advanced');
      basicView.style.display = isBasic ? '' : 'none';
      advView.style.display = isBasic ? 'none' : '';
      basicBtn.setAttribute('aria-pressed', isBasic ? 'true' : 'false');
      advBtn.setAttribute('aria-pressed', isBasic ? 'false' : 'true');
      try { localStorage.setItem('z-mode', isBasic ? 'basic' : 'advanced'); } catch (e) { /* file:// */ }
      render();
    }
    var stored = null;
    try { stored = localStorage.getItem('z-mode'); } catch (e) { stored = null; }
    setMode(stored === 'advanced' ? 'advanced' : 'basic');
    basicBtn.addEventListener('click', function () { setMode('basic'); });
    advBtn.addEventListener('click', function () { setMode('advanced'); });
    if (link) { link.addEventListener('click', function (e) { e.preventDefault(); setMode('advanced'); }); }
  }

  /* =============================================================== filters (v11 — Dashboard Filters card) */

  /* --- helpers for the new filter card --- */
  var _pendingS = {};   // staging state used by Apply button

  function _makeDropdown(triggerLabel, keys, store, labeller, onApply) {
    var wrap = document.createElement('div');
    wrap.className = 'df-dropdown';

    var trigger = document.createElement('button');
    trigger.type = 'button';
    trigger.className = 'df-dropdown-trigger';

    var trigText = document.createElement('span');
    var arrow = document.createElement('span');
    arrow.className = 'df-dd-arrow';
    arrow.textContent = '▼';
    trigger.appendChild(trigText);
    trigger.appendChild(arrow);

    var menu = document.createElement('div');
    menu.className = 'df-dropdown-menu';
    menu.style.display = 'none';

    // actions row: Select All / Clear
    var actRow = document.createElement('div');
    actRow.className = 'df-dd-actions';
    var selAllBtn = document.createElement('button');
    selAllBtn.className = 'df-dd-act-btn'; selAllBtn.type = 'button'; selAllBtn.textContent = 'Select all';
    var clearBtn = document.createElement('button');
    clearBtn.className = 'df-dd-act-btn'; clearBtn.type = 'button'; clearBtn.textContent = 'Clear';
    actRow.appendChild(selAllBtn); actRow.appendChild(clearBtn);
    menu.appendChild(actRow);

    // search
    var searchWrap = document.createElement('div');
    searchWrap.className = 'df-dd-search-wrap';
    var searchInput = document.createElement('input');
    searchInput.type = 'text'; searchInput.className = 'df-dd-search';
    searchInput.placeholder = 'Search…';
    searchWrap.appendChild(searchInput);
    menu.appendChild(searchWrap);

    // list
    var list = document.createElement('div');
    list.className = 'df-dd-list';
    menu.appendChild(list);
    wrap.appendChild(trigger); wrap.appendChild(menu);

    function getLabel(k) { return labeller ? labeller(k) : k; }

    function refreshList(filter) {
      list.textContent = '';
      keys.forEach(function (k) {
        var lbl = getLabel(k);
        if (filter && lbl.toLowerCase().indexOf(filter.toLowerCase()) === -1) { return; }
        var item = document.createElement('label');
        item.className = 'df-dd-item';
        var cb = document.createElement('input');
        cb.type = 'checkbox'; cb.checked = !!store[k];
        cb.addEventListener('change', function () {
          store[k] = cb.checked;
          if (!keys.some(function (x) { return store[x]; })) { store[k] = true; cb.checked = true; }
          refreshTriggerLabel(); onApply();
        });
        var txt = document.createElement('span'); txt.textContent = lbl;
        item.appendChild(cb); item.appendChild(txt);
        list.appendChild(item);
      });
    }

    function refreshTriggerLabel() {
      var sel = keys.filter(function (k) { return store[k]; });
      if (sel.length === keys.length) { trigText.textContent = triggerLabel + ' (' + keys.length + ')'; }
      else if (sel.length <= 3) { trigText.textContent = sel.map(getLabel).join(', '); }
      else { trigText.textContent = sel.length + ' selected'; }
    }

    selAllBtn.addEventListener('click', function () {
      keys.forEach(function (k) { store[k] = true; });
      refreshList(searchInput.value); refreshTriggerLabel(); onApply();
    });
    clearBtn.addEventListener('click', function () {
      var first = keys[0];
      keys.forEach(function (k) { store[k] = false; });
      if (first) { store[first] = true; }
      refreshList(searchInput.value); refreshTriggerLabel(); onApply();
    });
    searchInput.addEventListener('input', function () { refreshList(searchInput.value); });

    trigger.addEventListener('click', function (e) {
      e.stopPropagation();
      var open = wrap.classList.toggle('open');
      menu.style.display = open ? 'block' : 'none';
      if (open) { refreshList(''); }
    });
    document.addEventListener('click', function () {
      wrap.classList.remove('open'); menu.style.display = 'none';
    });
    menu.addEventListener('click', function (e) { e.stopPropagation(); });

    refreshTriggerLabel();
    wrap._refresh = function () { refreshTriggerLabel(); };
    return wrap;
  }

  function buildFilters() {
    var host = document.getElementById('filters-in');
    if (!host) { return; }
    host.textContent = '';

    var card = document.createElement('div');
    card.className = 'dash-filters-card';

    /* --- header --- */
    var hdr = document.createElement('div'); hdr.className = 'df-header';
    var title = document.createElement('h2'); title.className = 'df-title'; title.textContent = 'Dashboard Filters';
    hdr.appendChild(title);
    card.appendChild(hdr);

    /* --- date range (single line presets + date inputs) --- */
    var dateSection = document.createElement('div'); dateSection.className = 'df-section';
    var dateLbl = document.createElement('div'); dateLbl.className = 'df-label'; dateLbl.textContent = 'DATE RANGE';
    dateSection.appendChild(dateLbl);

    var dateLine = document.createElement('div'); dateLine.className = 'df-date-single-line';
    var presetsRow = document.createElement('div'); presetsRow.className = 'df-presets';
    var presets = [
      { l: 'Full window', f: function () { S.from = WINDAYS[0] || ALLDAYS[0]; S.to = WINDAYS[WINDAYS.length - 1] || ALLDAYS[ALLDAYS.length - 1]; S.windowOnly = true; } },
      { l: '7 days',  f: function () { S.to = WINDAYS[WINDAYS.length - 1]; S.from = WINDAYS[Math.max(0, WINDAYS.length - 7)];  S.windowOnly = true; } },
      { l: '14 days', f: function () { S.to = WINDAYS[WINDAYS.length - 1]; S.from = WINDAYS[Math.max(0, WINDAYS.length - 14)]; S.windowOnly = true; } },
      { l: '30 days', f: function () { S.to = WINDAYS[WINDAYS.length - 1]; S.from = WINDAYS[Math.max(0, WINDAYS.length - 30)]; S.windowOnly = true; } }
    ];
    var presetBtns = [];
    presets.forEach(function (p) {
      var b = document.createElement('button'); b.type = 'button'; b.className = 'df-preset-btn';
      b.textContent = p.l;
      b.addEventListener('click', function () {
        p.f();
        syncDates();
        setDirty();
        updateSummary();
        presetBtns.forEach(function (x) { x.classList.remove('active'); });
        b.classList.add('active');
      });
      presetBtns.push(b);
      presetsRow.appendChild(b);
    });
    dateLine.appendChild(presetsRow);

    var datesRow = document.createElement('div'); datesRow.className = 'df-dates-row';
    var dFrom = document.createElement('input'); dFrom.type = 'date'; dFrom.className = 'df-date-input';
    dFrom.min = ALLDAYS[0]; dFrom.max = ALLDAYS[ALLDAYS.length - 1]; dFrom.value = S.from;
    var arrow = document.createElement('span'); arrow.className = 'df-date-arrow'; arrow.textContent = '→';
    var dTo = document.createElement('input'); dTo.type = 'date'; dTo.className = 'df-date-input';
    dTo.min = ALLDAYS[0]; dTo.max = ALLDAYS[ALLDAYS.length - 1]; dTo.value = S.to;
    function syncDates() { dFrom.value = S.from; dTo.value = S.to; }

    dFrom.addEventListener('change', function () {
      if (dFrom.value) {
        S.from = dFrom.value;
        presetBtns.forEach(function (x) { x.classList.remove('active'); });
        setDirty();
        updateSummary();
      }
    });
    dTo.addEventListener('change', function () {
      if (dTo.value) {
        S.to = dTo.value;
        presetBtns.forEach(function (x) { x.classList.remove('active'); });
        setDirty();
        updateSummary();
      }
    });
    datesRow.appendChild(dFrom);
    datesRow.appendChild(arrow);
    datesRow.appendChild(dTo);
    dateLine.appendChild(datesRow);

    dateSection.appendChild(dateLine);
    card.appendChild(dateSection);

    /* --- 3-column row: PLATFORM | TOPIC | DATA SCOPE --- */
    var grid = document.createElement('div');
    grid.className = 'df-grid-3col detailed-only-filter';

    // 1. Platform
    var platSection = document.createElement('div');
    platSection.className = 'df-col';
    var platLbl = document.createElement('div');
    platLbl.className = 'df-label';
    platLbl.textContent = 'PLATFORM';
    var platDd = _makeDropdown('All platforms', PLATFORMS, S.platforms, null, function () { setDirty(); updateSummary(); });
    platSection.appendChild(platLbl);
    platSection.appendChild(platDd);

    // 2. Topic
    var topicSection = document.createElement('div');
    topicSection.className = 'df-col';
    var topicLbl = document.createElement('div');
    topicLbl.className = 'df-label';
    topicLbl.textContent = 'TOPIC';
    var topicDd = _makeDropdown('All topics', TOPIC_IDS, S.topics, function (t) { return topicLabel(t); }, function () { setDirty(); updateSummary(); });
    topicSection.appendChild(topicLbl);
    topicSection.appendChild(topicDd);

    // 3. Data Scope
    var scopeSection = document.createElement('div');
    scopeSection.className = 'df-col';
    var scopeLbl = document.createElement('div');
    scopeLbl.className = 'df-label';
    scopeLbl.textContent = 'DATA SCOPE';

    var scopeToggle = document.createElement('div');
    scopeToggle.className = 'df-scope-toggle';
    var winBtn = document.createElement('button');
    winBtn.type = 'button';
    winBtn.className = 'df-scope-btn';
    winBtn.textContent = 'In-window only';
    var backBtn = document.createElement('button');
    backBtn.type = 'button';
    backBtn.className = 'df-scope-btn';
    backBtn.textContent = 'Include backfill';

    function refreshScope() {
      winBtn.classList.toggle('active', S.windowOnly);
      backBtn.classList.toggle('active', !S.windowOnly);
    }
    winBtn.addEventListener('click', function () {
      S.windowOnly = true;
      refreshScope();
      setDirty();
      updateSummary();
    });
    backBtn.addEventListener('click', function () {
      S.windowOnly = false;
      S.from = ALLDAYS[0];
      S.to = ALLDAYS[ALLDAYS.length - 1];
      syncDates();
      presetBtns.forEach(function (x) { x.classList.remove('active'); });
      refreshScope();
      setDirty();
      updateSummary();
    });
    refreshScope();
    scopeToggle.appendChild(winBtn);
    scopeToggle.appendChild(backBtn);

    var scopeHint = document.createElement('div');
    scopeHint.className = 'df-scope-hint';
    scopeHint.textContent = 'Counts only posts published inside the selected dates.';

    scopeSection.appendChild(scopeLbl);
    scopeSection.appendChild(scopeToggle);
    scopeSection.appendChild(scopeHint);

    grid.appendChild(platSection);
    grid.appendChild(topicSection);
    grid.appendChild(scopeSection);
    card.appendChild(grid);

    /* --- footer: summary on left + (Reset to defaults, Applied) on right --- */
    var footer = document.createElement('div');
    footer.className = 'df-footer';

    var summary = document.createElement('div');
    summary.className = 'df-summary';
    summary.id = 'fsummary';

    var actions = document.createElement('div');
    actions.className = 'df-footer-actions';

    var resetBtn = document.createElement('button');
    resetBtn.type = 'button';
    resetBtn.className = 'df-reset-btn';
    resetBtn.textContent = 'Reset to defaults';

    var applyBtn = document.createElement('button');
    applyBtn.type = 'button';
    applyBtn.className = 'df-apply-btn';
    applyBtn.textContent = 'Applied';

    actions.appendChild(resetBtn);
    actions.appendChild(applyBtn);
    footer.appendChild(summary);
    footer.appendChild(actions);
    card.appendChild(footer);

    host.appendChild(card);

    function setDirty() {
      applyBtn.textContent = 'Apply filters';
      applyBtn.classList.add('dirty');
    }
    function setClean() {
      applyBtn.textContent = 'Applied';
      applyBtn.classList.remove('dirty');
    }

    applyBtn.addEventListener('click', function () {
      render();
      setClean();
    });

    function updateSummary() {
      var nDays = 0;
      var from = S.from || ALLDAYS[0] || '';
      var to = S.to || ALLDAYS[ALLDAYS.length - 1] || '';
      ALLDAYS.forEach(function (d) { if (d >= from && d <= to) { nDays++; } });
      var nPlat = PLATFORMS.filter(function (p) { return S.platforms[p]; }).length;
      var nTopic = TOPIC_IDS.filter(function (t) { return S.topics[t]; }).length;

      var platStr = (nPlat === PLATFORMS.length) ? 'all platforms' : (nPlat + ' platforms');
      var topStr  = (nTopic === TOPIC_IDS.length) ? 'all topics' : (nTopic + ' topics');

      var el = document.getElementById('fsummary');
      if (el) {
        el.innerHTML = 'Will show: <span class="df-mono-date">' + from + ' → ' + to + '</span> · ' + platStr + ' · ' + topStr;
      }
      if (platDd._refresh) { platDd._refresh(); }
      if (topicDd._refresh) { topicDd._refresh(); }
    }
    updateSummary();

    resetBtn.addEventListener('click', function () {
      S.from = WINDAYS[0] || ALLDAYS[0];
      S.to = WINDAYS[WINDAYS.length - 1] || ALLDAYS[ALLDAYS.length - 1];
      S.windowOnly = true;
      PLATFORMS.forEach(function (p) { S.platforms[p] = true; });
      TOPIC_IDS.forEach(function (t) { S.topics[t] = true; });
      syncDates();
      refreshScope();
      presetBtns.forEach(function (x) { x.classList.remove('active'); });
      presetBtns[0].classList.add('active');
      if (platDd._refresh) { platDd._refresh(); }
      if (topicDd._refresh) { topicDd._refresh(); }
      updateSummary();
      render();
      setClean();
    });

    // set Full window active on load
    if (presetBtns[0]) { presetBtns[0].classList.add('active'); }
  }

  function updateFilterChrome() {
    var fs = document.getElementById('fsummary');
    if (fs) {
      fs.textContent = F.days.length + ' days · ' + selPlatforms().length +
        ' platforms · ' + selTopics().length + ' topics';
    }
  }

  /* ================================================================ render */

  var rafId = null;
  function render() {
    readTokens();
    applyFilters();
    updateFilterChrome();
    var mode = (document.body && document.body.getAttribute('data-mode')) || 'basic';
    var selector = (mode === 'basic') ? '#basic-view [data-panel]' : '#advanced-view [data-panel]';

    document.querySelectorAll(selector).forEach(function (host) {
      var sec = host.closest ? host.closest('section.sec') : null;
      if (sec && sec.style.display === 'none') {
        return;
      }
      var key = host.getAttribute('data-panel');
      host.textContent = '';
      var fn = P[key];
      if (!fn) { empty(host, 'No renderer registered for panel "' + key + '".'); return; }
      try { fn(host); } catch (e) {
        empty(host, 'Render error in panel "' + key + '": ' + e.message);
        if (window.console) { console.error(key, e); }
      }
    });
    if (window.__SELFTEST__) { selftest(); }
  }

  function scheduleRender() {
    if (rafId) { cancelAnimationFrame(rafId); }
    rafId = requestAnimationFrame(function () { rafId = null; render(); });
  }

  /* ------------------------------------------------------------- selftest - */

  function selftest() {
    var out = [];
    var de = document.documentElement;
    out.push('theme               : ' + (de.getAttribute('data-theme') || 'dark'));
    out.push('horizontal overflow : doc scrollWidth ' + de.scrollWidth + ' vs clientWidth ' +
      de.clientWidth + '  => ' + (de.scrollWidth <= de.clientWidth + 1 ? 'OK' : 'FAIL'));
    var blank = [];
    document.querySelectorAll('[data-panel]').forEach(function (p) {
      if (!p.childNodes.length) { blank.push(p.getAttribute('data-panel')); }
    });
    out.push('panels              : ' + document.querySelectorAll('[data-panel]').length +
      ' rendered, ' + blank.length + ' blank' + (blank.length ? ' [' + blank.join(', ') + ']' : ''));
    var errs = [];
    document.querySelectorAll('.empty').forEach(function (e) {
      if (/Render error|No renderer/.test(e.textContent)) { errs.push(e.textContent.slice(0, 120)); }
    });
    out.push('render errors       : ' + errs.length + (errs.length ? '\n  ' + errs.join('\n  ') : ''));
    var emptyPanels = [];
    document.querySelectorAll('[data-panel]').forEach(function (p) {
      if (!p.querySelector('svg') && !p.querySelector('table') && !p.querySelector('dl') &&
          !p.querySelector('.kpi') && !p.querySelector('.tcard')) {
        emptyPanels.push(p.getAttribute('data-panel'));
      }
    });
    out.push('panels w/o content  : ' + emptyPanels.length + (emptyPanels.length ? ' [' + emptyPanels.join(', ') + ']' : ''));
    var over = [];
    document.querySelectorAll('svg').forEach(function (s) {
      var par = s.parentElement;
      if (!par) { return; }
      if (s.getBoundingClientRect().width > par.getBoundingClientRect().width + 2 &&
          !par.closest('.scrollx')) {
        over.push((par.getAttribute('data-panel') || par.className) + ' svg ' +
          Math.round(s.getBoundingClientRect().width) + ' > host ' +
          Math.round(par.getBoundingClientRect().width));
      }
    });
    out.push('svg wider than host : ' + over.length + (over.length ? '\n  ' + over.join('\n  ') : ''));
    out.push('sections            : ' + document.querySelectorAll('section.sec').length);
    var st = document.getElementById('selftest');
    if (st) { st.classList.add('on'); st.textContent = out.join('\n'); }
    if (window.console) { console.log('[selftest]\n' + out.join('\n')); }
  }
  window.__runSelftest = function () { window.__SELFTEST__ = true; selftest(); };

  /* ---------------------------------------------------------------- init -- */

  function initTheme() {
    var btn = document.getElementById('theme-toggle');
    var stored = null;
    try { stored = localStorage.getItem('z-theme'); } catch (e) { stored = null; }
    var t = stored === 'light' ? 'light' : 'dark';
    document.documentElement.setAttribute('data-theme', t);
    function label() {
      var cur = document.documentElement.getAttribute('data-theme');
      if (btn) {
        btn.textContent = '';
        btn.appendChild(h('span', { text: cur === 'light' ? '◑' : '◐' }));
        btn.appendChild(h('span', { text: cur === 'light' ? 'Light' : 'Dark' }));
        btn.setAttribute('aria-label', 'Theme: ' + cur + '. Click to switch.');
      }
    }
    label();
    if (btn) {
      btn.addEventListener('click', function () {
        var cur = document.documentElement.getAttribute('data-theme');
        var nxt = cur === 'light' ? 'dark' : 'light';
        document.documentElement.setAttribute('data-theme', nxt);
        try { localStorage.setItem('z-theme', nxt); } catch (e) { /* file:// */ }
        label();
        render();
      });
    }
  }

  function setStatus(msg, kind) {
    var el = document.getElementById('data-status');
    if (!el) { return; }
    el.textContent = msg || '';
    el.className = 'badge' + (kind ? ' ' + kind : ' gray');
  }

  function updateChrome() {
    if (!DATA) { return; }
    var sub = document.querySelector('.brand .sub');
    if (sub) {
      var w = DATA.window || {};
      var gen = (DATA.generated_at || '').slice(0, 19).replace('T', ' ');
      sub.textContent = String(w.start || '').slice(0, 10) + ' → ' +
        String(w.end || '').slice(0, 10) + ' · updated ' + gen.slice(0, 16) + ' UTC';
    }
    var src = document.getElementById('source-badge');
    if (src) { src.textContent = 'source: ' + (DATA.source || 'metrics.json'); }
    var mode = document.getElementById('mode-badge');
    if (mode) {
      if (String(DATA.mode || '').indexOf('real') === 0) {
        mode.textContent = 'real data';
        mode.className = 'badge';
      } else {
        mode.textContent = '⚠ fixture data — not real';
        mode.className = 'badge crit';
      }
    }
  }


  // ---- Live NER panel (polls /api/gemma/status; no API on a static snapshot) ----
  var LIVE_NER_URL = 'api/gemma/status';
  var LIVE_NER_MS = 5000;
  var liveNer = { timer: null, ticker: null, lastOk: null, dead: false };

  function lnEl(tag, cls, text) {
    var e = document.createElement(tag);
    if (cls) { e.className = cls; }
    if (text != null) { e.textContent = text; }
    return e;
  }
  function lnFmt(n) { return (n == null) ? '–' : Number(n).toLocaleString('en-US'); }

  function lnNote(msg) {
    var note = document.getElementById('ln-note'), box = document.getElementById('ln-content');
    if (!note || !box) { return; }
    if (msg) { note.textContent = msg; note.hidden = false; box.hidden = true; }
    else { note.hidden = true; box.hidden = false; }
  }

  function renderLiveNer(d) {
    var root = document.getElementById('live-ner');
    if (!root) { return; }
    if (!d || !d.available) {
      root.setAttribute('data-state', 'off');
      lnNote('Live NER is not running' + (d && d.error ? ' (' + d.error + ')' : '') +
             '. This panel updates automatically when the pipeline starts.');
      return;
    }
    root.setAttribute('data-state', 'on');
    lnNote(null);
    var strip = document.getElementById('ln-strip');
    strip.textContent = '';
    var pend = d.incoming_pending || 0;
    var stats = [
      ['Routed', lnFmt(d.routed)], ['Posts extracted', lnFmt(d.extracted_posts)],
      ['Entities', lnFmt(d.entities)],
      ['OK rate', d.ok_rate == null ? '–' : (d.ok_rate * 100).toFixed(1) + '%'],
      ['Posts / min', d.rate_per_min == null ? '–' : d.rate_per_min.toFixed(1)]
    ];
    stats.forEach(function (s) {
      var c = lnEl('div', 'ln-stat'); c.appendChild(lnEl('div', 'v', s[1])); c.appendChild(lnEl('div', 'l', s[0]));
      strip.appendChild(c);
    });
    var q = lnEl('div', 'ln-stat'), qv = lnEl('div', 'v', lnFmt(pend));
    qv.appendChild(document.createTextNode(' '));
    if (pend > 0) { qv.appendChild(lnEl('span', 'badge', 'priority')); }
    q.appendChild(qv); q.appendChild(lnEl('div', 'l', 'New posts pending'));
    strip.appendChild(q);

    var names = d.domain_names || {}, dom = d.by_domain || {};
    var keys = Object.keys(dom).sort(function (a, b) { return dom[b] - dom[a]; });
    var max = keys.length ? dom[keys[0]] : 1;
    var dbox = document.getElementById('ln-domains'); dbox.textContent = '';
    if (!keys.length) { dbox.appendChild(lnEl('div', 'fnote', 'No routed posts yet.')); }
    keys.forEach(function (k) {
      var row = lnEl('div', 'ln-bar');
      row.appendChild(lnEl('span', null, names[k] ? names[k] : (k === 'none' ? 'No domain' : k)));
      var t = lnEl('span', 't'), i = lnEl('i'); i.style.width = (100 * dom[k] / max) + '%';
      t.appendChild(i); row.appendChild(t); row.appendChild(lnEl('span', null, lnFmt(dom[k])));
      if (!names[k] && k !== 'none') { console.warn('live-ner: domain without a name: ' + k); }
      dbox.appendChild(row);
    });

    var top = document.getElementById('ln-top'); top.textContent = '';
    (d.top_entities || []).forEach(function (e) {
      top.appendChild(lnEl('li', null, e.name + ' · ' + e.entity_type + ' (' + e.level + ') × ' + e.n));
    });
    if (!(d.top_entities || []).length) { top.appendChild(lnEl('li', 'fnote', 'No entities in the last 24h.')); }

    var feed = document.getElementById('ln-feed'); feed.textContent = '';
    (d.recent || []).forEach(function (r) {
      var li = lnEl('li', 'ln-item' + (r.source === 'new' ? ' is-new' : ''));
      var meta = lnEl('div', 'meta');
      if (r.source === 'new') { meta.appendChild(lnEl('span', 'badge', 'new')); }
      var dn = function (k) { return k ? (names[k] || k) : null; };
      var bits = [r.platform, dn(r.domain_1), dn(r.domain_2), r.event_mode,
                  r.confidence_1 != null ? 'conf ' + Number(r.confidence_1).toFixed(2) : null, r.routed_at];
      meta.appendChild(document.createTextNode(bits.filter(Boolean).join(' · ')));
      li.appendChild(meta);
      li.appendChild(lnEl('div', null, r.snippet || '(text unavailable)'));
      (r.entities || []).forEach(function (e) { li.appendChild(lnEl('span', 'ln-ent', e.name + ' · ' + e.type)); });
      feed.appendChild(li);
    });
    liveNer.lastOk = Date.now();
    liveNerTick();
  }

  function liveNerTick() {
    var u = document.getElementById('ln-updated');
    if (u && liveNer.lastOk) {
      u.textContent = 'last updated ' + Math.max(0, Math.round((Date.now() - liveNer.lastOk) / 1000)) + 's ago';
    }
  }

  function liveNerPoll() {
    if (liveNer.dead || !document.getElementById('live-ner')) { return Promise.resolve(); }
    return fetch(LIVE_NER_URL, { cache: 'no-store' })
      .then(function (r) {
        if (!r.ok) { throw { status: r.status }; }
        return r.json();
      })
      .then(renderLiveNer)
      .catch(function (err) {
        if (err && (err.status === 404 || err.status === 405)) {
          liveNer.dead = true;
          document.getElementById('live-ner').setAttribute('data-state', 'static');
          lnNote('Live NER needs the API server; this static snapshot has no live endpoint.');
        } else {
          lnNote('Live NER endpoint unreachable; retrying.');
        }
      });
  }

  function initLiveNer() {
    if (!document.getElementById('live-ner')) { return; }
    var start = function () {
      if (liveNer.timer) { return; }
      liveNerPoll();
      liveNer.timer = setInterval(function () {
        if (document.visibilityState === 'visible') { liveNerPoll(); }
      }, LIVE_NER_MS);
      liveNer.ticker = setInterval(liveNerTick, 1000);
    };
    start();
    document.addEventListener('visibilitychange', function () {
      if (document.visibilityState === 'visible') { liveNerPoll(); }
    });
    window.__liveNerPoll = liveNerPoll;
  }

  /* ============================================================ Overview date chip strip (v14) */

  function buildBasicDateStrip() {
    // Remove any previously built strip (re-init guard)
    var old = document.getElementById('basic-date-strip');
    if (old) { old.parentNode.removeChild(old); }

    var strip = document.createElement('div');
    strip.id = 'basic-date-strip';

    /* ---- DATE RANGE group ---- */
    var drGroup = document.createElement('div');
    drGroup.className = 'bds-group';

    var drLabel = document.createElement('span');
    drLabel.className = 'bds-label';
    drLabel.textContent = 'DATE RANGE';
    drGroup.appendChild(drLabel);

    var drRow = document.createElement('div');
    drRow.className = 'bds-row';

    var presets = [
      { l: 'Full window', f: function () {
          S.from = WINDAYS[0] || ALLDAYS[0];
          S.to   = WINDAYS[WINDAYS.length - 1] || ALLDAYS[ALLDAYS.length - 1];
          S.windowOnly = true;
        }
      },
      { l: 'Last 7d', f: function () {
          S.to   = WINDAYS[WINDAYS.length - 1] || ALLDAYS[ALLDAYS.length - 1];
          S.from = WINDAYS[Math.max(0, WINDAYS.length - 7)] || ALLDAYS[Math.max(0, ALLDAYS.length - 7)];
          S.windowOnly = true;
        }
      },
      { l: 'Last 14d', f: function () {
          S.to   = WINDAYS[WINDAYS.length - 1] || ALLDAYS[ALLDAYS.length - 1];
          S.from = WINDAYS[Math.max(0, WINDAYS.length - 14)] || ALLDAYS[Math.max(0, ALLDAYS.length - 14)];
          S.windowOnly = true;
        }
      },
      { l: 'Last 30d', f: function () {
          S.to   = WINDAYS[WINDAYS.length - 1] || ALLDAYS[ALLDAYS.length - 1];
          S.from = WINDAYS[Math.max(0, WINDAYS.length - 30)] || ALLDAYS[Math.max(0, ALLDAYS.length - 30)];
          S.windowOnly = true;
        }
      },
      { l: 'All incl. backfill', f: function () {
          S.from = ALLDAYS[0] || '';
          S.to   = ALLDAYS[ALLDAYS.length - 1] || '';
          S.windowOnly = false;
        }
      }
    ];

    var btns = [];
    presets.forEach(function (p) {
      var b = document.createElement('button');
      b.type = 'button';
      b.className = 'df-preset-btn';
      b.textContent = p.l;
      b.addEventListener('click', function () {
        p.f();
        btns.forEach(function (x) { x.classList.remove('active'); });
        b.classList.add('active');
        updateBdsInputs();
        updateBdsSummary();
        render();
      });
      btns.push(b);
      drRow.appendChild(b);
    });

    /* date inputs */
    var dFrom = document.createElement('input');
    dFrom.type = 'date'; dFrom.className = 'bds-date-input';
    dFrom.value = S.from;
    var sep = document.createElement('span');
    sep.className = 'bds-arrow'; sep.textContent = '→';
    var dTo = document.createElement('input');
    dTo.type = 'date'; dTo.className = 'bds-date-input';
    dTo.value = S.to;

    function updateBdsInputs() {
      dFrom.value = S.from;
      dTo.value = S.to;
    }

    dFrom.addEventListener('change', function () {
      S.from = dFrom.value;
      btns.forEach(function (x) { x.classList.remove('active'); });
      updateBdsSummary();
      render();
    });
    dTo.addEventListener('change', function () {
      S.to = dTo.value;
      btns.forEach(function (x) { x.classList.remove('active'); });
      updateBdsSummary();
      render();
    });

    drRow.appendChild(dFrom);
    drRow.appendChild(sep);
    drRow.appendChild(dTo);
    drGroup.appendChild(drRow);
    strip.appendChild(drGroup);

    /* ---- divider ---- */
    var div1 = document.createElement('div'); div1.className = 'bds-divider'; strip.appendChild(div1);

    /* ---- SCOPE group ---- */
    var scGroup = document.createElement('div');
    scGroup.className = 'bds-group';

    var scLabel = document.createElement('span');
    scLabel.className = 'bds-label'; scLabel.textContent = 'SCOPE';
    scGroup.appendChild(scLabel);

    var scRow = document.createElement('div');
    scRow.className = 'bds-row bds-scope-wrap';

    var winBtn = document.createElement('button');
    winBtn.type = 'button'; winBtn.className = 'bds-scope-btn'; winBtn.textContent = 'in-window only';
    var backBtn = document.createElement('button');
    backBtn.type = 'button'; backBtn.className = 'bds-scope-btn'; backBtn.textContent = 'incl. backfill';

    function refreshScope() {
      winBtn.classList.toggle('active', S.windowOnly);
      backBtn.classList.toggle('active', !S.windowOnly);
    }
    refreshScope();

    winBtn.addEventListener('click', function () { S.windowOnly = true; refreshScope(); updateBdsSummary(); render(); });
    backBtn.addEventListener('click', function () {
      S.windowOnly = false;
      S.from = ALLDAYS[0] || S.from;
      S.to   = ALLDAYS[ALLDAYS.length - 1] || S.to;
      btns.forEach(function (x) { x.classList.remove('active'); });
      if (btns[4]) { btns[4].classList.add('active'); }
      updateBdsInputs();
      refreshScope();
      updateBdsSummary();
      render();
    });

    scRow.appendChild(winBtn);
    scRow.appendChild(backBtn);
    scGroup.appendChild(scRow);
    strip.appendChild(scGroup);

    /* ---- divider ---- */
    var div2 = document.createElement('div'); div2.className = 'bds-divider'; strip.appendChild(div2);

    /* ---- SELECTION summary ---- */
    var selGroup = document.createElement('div');
    selGroup.className = 'bds-group';

    var selLabel = document.createElement('span');
    selLabel.className = 'bds-label'; selLabel.textContent = 'SELECTION';
    selGroup.appendChild(selLabel);

    var selVal = document.createElement('span');
    selVal.className = 'bds-summary'; selVal.id = 'bds-summary-val';
    selGroup.appendChild(selVal);
    strip.appendChild(selGroup);

    /* summary updater */
    function updateBdsSummary() {
      var days = ALLDAYS.filter(function (d) { return d >= S.from && d <= S.to; });
      var nDays = days.length;
      var nPlat = Object.keys(S.platforms).filter(function (k) { return S.platforms[k]; }).length;
      var totPlat = Object.keys(S.platforms).length;
      var nTop  = Object.keys(S.topics).filter(function (k) { return S.topics[k]; }).length;
      var totTop = Object.keys(S.topics).length;
      selVal.textContent = nDays + ' days · ' + nPlat + '/' + totPlat + ' platforms · ' + nTop + '/' + totTop + ' topics';
    }
    updateBdsSummary();

    // Default to "Full window" active
    if (btns.length) { btns[0].classList.add('active'); }

    // Insert just before #basic-view
    var basicView = document.getElementById('basic-view');
    if (basicView && basicView.parentNode) {
      basicView.parentNode.insertBefore(strip, basicView);
    }
  }

  /* ============================================================ Dashboard Sections nav (v11) */

  function buildDashboardSections() {
    var advView = document.getElementById('advanced-view');
    if (!advView) { return; }

    // Remove existing dash-sections-wrap if already built (re-init guard)
    var existing = advView.querySelector('.dash-sections-wrap');
    if (existing) { existing.parentNode.removeChild(existing); }

    var sections = Array.prototype.slice.call(advView.querySelectorAll('section.sec'));
    if (!sections.length) { return; }

    var groups = [
      {
        icon: '⊞',
        title: '1. Overview & Alerts',
        subtitle: 'Header & KPI · Alerts · Acceleration · Burst episodes',
        metrics: [
          { id: 'kpi', label: '1. Header & KPI row' },
          { id: 'alerts', label: '2. Alerts (FDR-gated)' },
          { id: 'accel', label: '3. Acceleration & emergence' },
          { id: 'bursts', label: '4. Burst episodes' }
        ]
      },
      {
        icon: '📈',
        title: '2. Activity & Virality',
        subtitle: 'Volume & growth · Intensity · Persistence · Virality & reach',
        metrics: [
          { id: 'volume', label: '5. Volume & growth' },
          { id: 'intensity', label: '6. Intensity & surprise' },
          { id: 'persistence', label: '7. Persistence' },
          { id: 'virality', label: '10. Virality & reach' }
        ]
      },
      {
        icon: '👥',
        title: '3. Behaviour & Content',
        subtitle: 'Tone · Hate/toxicity · Author diversity · Readability',
        metrics: [
          { id: 'tone', label: '8. Tone' },
          { id: 'hate', label: '9. Hate / toxicity' },
          { id: 'authors', label: '11. Author diversity & coordination' },
          { id: 'readability', label: '12. Readability' }
        ]
      },
      {
        icon: '🔗',
        title: '4. Entities & Ontology',
        subtitle: 'Ontology · Entity directory · Hashtags · Live NER',
        metrics: [
          { id: 'ontology', label: '13. Ontology' },
          { id: 'hashtags', label: '14. Hashtags' },
          { id: 'entities', label: '16. Entity directory' },
          { id: 'livener', label: '20. Live NER' }
        ]
      },
      {
        icon: '🛡',
        title: '5. Risk & Intelligence',
        subtitle: 'Risk categories · Timeline & alerts · Geo-ontology · Methodology',
        metrics: [
          { id: 'methodology', label: '15. Methodology & data quality' },
          { id: 'risktheme', label: '17. Risk categories & thematic dimensions' },
          { id: 'geotimeline', label: '18. Timeline & alerts' },
          { id: 'geoverify', label: '19. Geo-ontology verification' }
        ]
      }
    ];

    groups.forEach(function (g) {
      g.ids = g.metrics.map(function (m) { return m.id; });
    });

    var wrap = document.createElement('div');
    wrap.className = 'dash-sections-wrap';

    // 1. Primary Category Bar (Top-level tabs)
    var list = document.createElement('div');
    list.className = 'dash-sections-tabs';

    var allBtn = document.createElement('button');
    allBtn.type = 'button';
    allBtn.className = 'dash-sec-pill dash-sec-all-pill';
    allBtn.innerHTML = '<span class="dsp-icon">⚡</span><span>View all (20)</span>';

    var pills = [];

    // 2. Secondary Sub-metric Bar
    var subBar = document.createElement('div');
    subBar.className = 'dash-subsections-bar';

    var currentGroup = -1;
    var currentSubId = null;

    function renderSubBar(groupIdx, activeSubId) {
      subBar.textContent = '';
      if (groupIdx === -1) {
        var lbl = document.createElement('span');
        lbl.className = 'dsub-label';
        lbl.textContent = 'ALL 20 METRICS:';
        subBar.appendChild(lbl);

        var jumpSelect = document.createElement('select');
        jumpSelect.className = 'dsub-select';
        var defOpt = document.createElement('option');
        defOpt.value = '';
        defOpt.textContent = 'Jump directly to metric…';
        jumpSelect.appendChild(defOpt);

        groups.forEach(function (g) {
          var optGrp = document.createElement('optgroup');
          optGrp.label = g.title;
          g.metrics.forEach(function (m) {
            var opt = document.createElement('option');
            opt.value = m.id;
            opt.textContent = m.label;
            optGrp.appendChild(opt);
          });
          jumpSelect.appendChild(optGrp);
        });

        jumpSelect.addEventListener('change', function () {
          if (jumpSelect.value) {
            var el = document.getElementById(jumpSelect.value);
            if (el) { el.scrollIntoView({ behavior: 'smooth', block: 'start' }); }
          }
        });
        subBar.appendChild(jumpSelect);
        return;
      }

      var g = groups[groupIdx];
      var lbl = document.createElement('span');
      lbl.className = 'dsub-label';
      lbl.textContent = 'SHOW:';
      subBar.appendChild(lbl);

      // "All in section" pill
      var allPill = document.createElement('button');
      allPill.type = 'button';
      allPill.className = 'dash-sub-pill' + (!activeSubId ? ' active' : '');
      allPill.textContent = 'All in ' + g.title.replace(/^\d+\.\s*/, '');
      allPill.addEventListener('click', function () {
        showSubMetric(groupIdx, null);
      });
      subBar.appendChild(allPill);

      // Sub-metric pills
      g.metrics.forEach(function (m) {
        var mPill = document.createElement('button');
        mPill.type = 'button';
        mPill.className = 'dash-sub-pill' + (activeSubId === m.id ? ' active' : '');
        mPill.textContent = m.label;
        mPill.addEventListener('click', function () {
          showSubMetric(groupIdx, m.id);
        });
        subBar.appendChild(mPill);
      });
    }

    function showGroup(groupIdx, isAll) {
      currentGroup = isAll ? -1 : groupIdx;
      currentSubId = null;

      pills.forEach(function (p, i) { p.classList.toggle('active', !isAll && i === groupIdx); });
      allBtn.classList.toggle('active', isAll);

      sections.forEach(function (sec) {
        if (isAll) {
          sec.style.display = '';
        } else {
          sec.style.display = (groups[groupIdx].ids.indexOf(sec.id) !== -1) ? '' : 'none';
        }
      });

      renderSubBar(currentGroup, null);
      setTimeout(function () { render(); window.dispatchEvent(new Event('resize')); }, 40);
      try { if (history.replaceState) { history.replaceState(null, null, isAll ? '#all' : '#group' + groupIdx); } } catch (e) {}
    }

    function showSubMetric(groupIdx, subId) {
      currentGroup = groupIdx;
      currentSubId = subId;

      sections.forEach(function (sec) {
        if (!subId) {
          sec.style.display = (groups[groupIdx].ids.indexOf(sec.id) !== -1) ? '' : 'none';
        } else {
          sec.style.display = (sec.id === subId) ? '' : 'none';
        }
      });

      renderSubBar(groupIdx, subId);
      setTimeout(function () {
        render();
        window.dispatchEvent(new Event('resize'));
        var target = document.getElementById(subId || groups[groupIdx].ids[0]);
        if (target) { target.scrollIntoView({ behavior: 'smooth', block: 'start' }); }
      }, 50);
      try { if (history.replaceState) { history.replaceState(null, null, '#' + (subId || groups[groupIdx].ids[0])); } } catch (e) {}
    }

    allBtn.addEventListener('click', function () { showGroup(-1, true); });
    list.appendChild(allBtn);

    groups.forEach(function (g, gi) {
      var pill = document.createElement('button');
      pill.type = 'button';
      pill.className = 'dash-sec-pill';
      pill.title = g.subtitle;
      pill.innerHTML = '<span class="dsp-icon">' + g.icon + '</span><span class="dsp-title">' + g.title + '</span>';
      pill.addEventListener('click', function () {
        showGroup(gi, false);
        var first = document.getElementById(g.ids[0]);
        if (first) {
          setTimeout(function () { first.scrollIntoView({ behavior: 'smooth', block: 'start' }); }, 80);
        }
      });
      pills.push(pill);
      list.appendChild(pill);
    });

    wrap.appendChild(list);
    wrap.appendChild(subBar);

    var firstSection = advView.querySelector('section.sec');
    if (firstSection) { advView.insertBefore(wrap, firstSection); } else { advView.appendChild(wrap); }

    var hash = (location.hash || '').replace(/^#/, '');
    if (hash && hash !== 'all') {
      var gIdx = -1, mId = null;
      groups.forEach(function (g, i) {
        if (g.ids.indexOf(hash) !== -1) {
          gIdx = i;
          mId = hash;
        }
      });
      if (gIdx !== -1) {
        showSubMetric(gIdx, mId);
        return;
      }
    }
    showGroup(-1, true);
    window.__showDashGroup = showGroup;
    window.__showSubMetric = showSubMetric;
  }

  
  function initDetailedMetricToggle() {
    var toc = document.querySelector('#advanced-view .toc');
    var advView = document.getElementById('advanced-view');
    if (!toc || !advView) { return; }

    var sections = Array.prototype.slice.call(advView.querySelectorAll('section.sec'));
    if (!sections.length) { return; }

    // Add 'View All' pill at the start of TOC if not already present
    var allBtn = toc.querySelector('.toc-all-btn');
    if (!allBtn) {
      allBtn = document.createElement('a');
      allBtn.className = 'toc-btn toc-all-btn';
      allBtn.href = '#all';
      allBtn.textContent = '⚡ View All';
      toc.insertBefore(allBtn, toc.firstChild);
    }

    var links = Array.prototype.slice.call(toc.querySelectorAll('a'));
    var validIds = sections.map(function (s) { return s.id; });

    function showSection(targetId, updateHash) {
      var isAll = !targetId || targetId === 'all';

      // Update active state on tabs
      links.forEach(function (l) {
        var href = l.getAttribute('href') || '';
        var id = href.replace(/^#/, '');
        if ((isAll && id === 'all') || (!isAll && id === targetId)) {
          l.classList.add('active');
          l.setAttribute('aria-selected', 'true');
        } else {
          l.classList.remove('active');
          l.setAttribute('aria-selected', 'false');
        }
      });

      // Show/hide sections
      sections.forEach(function (sec) {
        if (isAll || sec.id === targetId) {
          sec.style.display = '';
        } else {
          sec.style.display = 'none';
        }
      });

      // Trigger resize for newly visible charts
      setTimeout(function () {
        window.dispatchEvent(new Event('resize'));
      }, 60);

      if (updateHash !== false) {
        try {
          if (history.replaceState) {
            history.replaceState(null, null, '#' + (isAll ? 'all' : targetId));
          }
        } catch (e) {}
      }
    }

    // Attach click events
    links.forEach(function (link) {
      link.addEventListener('click', function (e) {
        e.preventDefault();
        var href = link.getAttribute('href') || '';
        var id = href.replace(/^#/, '');
        showSection(id, true);
        if (id !== 'all') {
          var targetSec = document.getElementById(id);
          if (targetSec) {
            targetSec.scrollIntoView({ behavior: 'smooth', block: 'start' });
          }
        }
      });
    });

    // Check hash on load or default to first section
    var initialHash = (location.hash || '').replace(/^#/, '');
    if (initialHash && (initialHash === 'all' || validIds.indexOf(initialHash) !== -1)) {
      showSection(initialHash, false);
    } else {
      showSection(sections[0].id, false);
    }

    window.addEventListener('hashchange', function () {
      var h = (location.hash || '').replace(/^#/, '');
      if (h && (h === 'all' || validIds.indexOf(h) !== -1)) {
        showSection(h, false);
      }
    });

    window.__showMetricTab = showSection;
  }

  function initUI() {
    if (/[?&]selftest=1/.test(location.search)) { window.__SELFTEST__ = true; }
    if (/[?&]theme=light/.test(location.search)) {
      try { localStorage.setItem('z-theme', 'light'); } catch (e) { /* noop */ }
    }
    updateChrome();
    initTheme();
    initModeToggle();
    initDrillChrome();
    buildFilters();
    render();
    initLiveNer();
    initDetailedMetricToggle();
    buildBasicDateStrip();
    buildDashboardSections();
    var t = null;
    window.addEventListener('resize', function () {
      if (t) { clearTimeout(t); }
      t = setTimeout(scheduleRender, 180);
    });
  }

  function boot(data) {
    if (!data || !data.tables) {
      setStatus('no metrics tables in payload', 'crit');
      return;
    }
    hydrate(data);
    setStatus('snapshot · interactive filters', 'gray');
    initUI();
  }

  function loadData() {
    // Prefer embedded blob only if it actually has fact tables (not a shell stub).
    var embedded = window.__METRICS__;
    if (embedded && embedded.tables && embedded.tables.metrics_daily &&
        embedded.tables.metrics_daily.rows && embedded.tables.metrics_daily.rows.length) {
      boot(embedded);
      return;
    }
    var url = window.__METRICS_URL__ || 'data/metrics.json';
    setStatus('loading metrics…', 'gray');
    fetch(url)
      .then(function (r) {
        if (!r.ok) { throw new Error(url + ' → HTTP ' + r.status); }
        return r.json();
      })
      .then(function (data) {
        boot(data);
      })
      .catch(function (err) {
        console.error(err);
        setStatus('failed to load ' + url + ' — serve UI/ over HTTP', 'crit');
        var wrap = document.querySelector('.wrap');
        if (wrap && !document.getElementById('load-error')) {
          var box = document.createElement('div');
          box.id = 'load-error';
          box.className = 'warnbox';
          box.innerHTML = '<div class="hd">Could not load metrics snapshot</div>' +
            '<p style="margin:0">Open via a local static server (not <code>file://</code>):' +
            '<br><code>cd UI && python3 -m http.server 8877</code>' +
            '<br>then <code>http://127.0.0.1:8877/</code>' +
            '<br><br>Rebuild data with <code>python UI/build_dashboard.py</code>.' +
            '<br><span class="fnote">' + String(err && err.message || err) + '</span></p>';
          wrap.insertBefore(box, wrap.firstChild);
        }
      });
  }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', loadData);
  } else { loadData(); }
})();
