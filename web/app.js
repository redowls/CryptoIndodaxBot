/* CryptoIndodaxBot dashboard renderer.
 *
 * Reads dashboard.json (written hourly by `python -m cryptoindodax.dashboard`)
 * and draws the page. No build step, no dependencies, no framework.
 *
 * Two rules this file keeps:
 *   1. Untrusted text — coin symbols, exit reasons, anything out of the ledger
 *      or the exchange — reaches the DOM through textContent, never innerHTML.
 *   2. A missing number renders as an em dash. Nothing is ever back-filled with
 *      a plausible-looking value; the whole point of the page is that it does
 *      not flatter the account.
 */

const WIB_OFFSET_MS = 7 * 3600 * 1000;
const TP_R = 2.5;           // config.TP_R — the right edge of the R meter
const STOP_R = -1;          // the left edge: a full 1R loss

const state = {
  mode: localStorage.getItem('idx.mode') || 'idr',     // 'idr' | 'pct'
  theme: localStorage.getItem('idx.theme') || 'auto',
};

let D = null;
const charts = [];          // redrawn on resize and on every toggle

/* ---- formatting -------------------------------------------------------- */

const isNum = (v) => typeof v === 'number' && Number.isFinite(v);

/** Rupiah the way Indodax and the bot write it: Rp1.234.567
 *
 * `precise` keeps decimals for coin prices. PEPE trades at Rp0,0725 — rounded to
 * the rupiah it reads "Rp0", which looks like a missing price rather than a real
 * one. Money amounts stay whole; only prices get the extra digits. */
function rp(n, { sign = false, precise = false } = {}) {
  if (!isNum(n)) return '—';
  const s = n < 0 ? '−' : sign && n >= 0 ? '+' : '';
  const a = Math.abs(n);
  const d = !precise ? 0 : a >= 1000 ? 0 : a >= 1 ? 2 : a >= 0.01 ? 4 : 8;
  return `${s}Rp${a.toLocaleString('de-DE', {
    minimumFractionDigits: d, maximumFractionDigits: d,
  })}`;
}

const price = (n) => rp(n, { precise: true });

function pct(n, { sign = true, digits = 2 } = {}) {
  if (!isNum(n)) return '—';
  return `${sign && n >= 0 ? '+' : n < 0 ? '−' : ''}${Math.abs(n).toFixed(digits)}%`;
}

/** A money figure that obeys the percent-only switch. */
function money(idr, asPct, opts = {}) {
  if (state.mode === 'pct') return isNum(asPct) ? pct(asPct, opts) : '—';
  return rp(idr, opts);
}

/** Coin quantities: up to 8 decimals, trailing zeros trimmed. */
function qty(q) {
  if (!isNum(q)) return '—';
  const s = q.toFixed(8).replace(/0+$/, '').replace(/\.$/, '');
  return Number(s).toLocaleString('de-DE', { maximumFractionDigits: 8 });
}

const MONTHS = ['Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun',
                'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec'];

/** Everything is stored UTC; the account is read in Jakarta. */
function wib(iso) { return new Date(new Date(iso).getTime() + WIB_OFFSET_MS); }

function fmtDay(iso) {
  const d = wib(iso);
  return `${d.getUTCDate()} ${MONTHS[d.getUTCMonth()]}`;
}

function fmtWhen(iso) {
  const d = wib(iso);
  const hh = String(d.getUTCHours()).padStart(2, '0');
  const mm = String(d.getUTCMinutes()).padStart(2, '0');
  return `${d.getUTCDate()} ${MONTHS[d.getUTCMonth()]} ${hh}:${mm}`;
}

function fmtAge(ms) {
  const m = Math.round(ms / 60000);
  if (m < 60) return `${m}m ago`;
  const h = Math.floor(m / 60);
  return h < 24 ? `${h}h ${m % 60}m ago` : `${Math.floor(h / 24)}d ago`;
}

const signClass = (n) => (isNum(n) ? (n > 0 ? 'up' : n < 0 ? 'down' : '') : '');
const cssVar = (name) => getComputedStyle(document.documentElement).getPropertyValue(name).trim();

/* ---- tiny DOM helpers -------------------------------------------------- */

function h(tag, attrs = {}, kids = []) {
  const node = document.createElement(tag);
  applyAttrs(node, attrs);
  append(node, kids);
  return node;
}

const SVGNS = 'http://www.w3.org/2000/svg';

function s(tag, attrs = {}, kids = []) {
  const node = document.createElementNS(SVGNS, tag);
  applyAttrs(node, attrs);
  append(node, kids);
  return node;
}

function applyAttrs(node, attrs) {
  for (const [k, v] of Object.entries(attrs)) {
    if (v === null || v === undefined || v === false) continue;
    if (k === 'text') node.textContent = v;            // untrusted text lands here
    else if (k === 'class') node.setAttribute('class', v);
    else if (k.startsWith('on')) node.addEventListener(k.slice(2), v);
    else node.setAttribute(k, v === true ? '' : String(v));
  }
}

function append(node, kids) {
  for (const kid of [].concat(kids)) {
    if (kid === null || kid === undefined || kid === false) continue;
    node.appendChild(typeof kid === 'string' ? document.createTextNode(kid) : kid);
  }
}

/* ---- chart scaffolding ------------------------------------------------- */

/** Register a chart. It is NOT drawn here: at registration time the container is
 *  still detached from the document, so clientWidth is 0 and there is nothing to
 *  size against. `drawAll()` runs once the page is attached, and again whenever
 *  the container's width actually changes. */
function chart(container, draw, { aria } = {}) {
  const entry = {
    container,
    width: 0,
    draw(force = false) {
      const width = container.clientWidth;
      if (!width || (!force && width === entry.width)) return;
      entry.width = width;
      container.replaceChildren();
      const tip = h('div', { class: 'tip', role: 'status', 'aria-live': 'polite' });
      const svg = draw(width, tip);
      svg.setAttribute('role', 'img');
      if (aria) svg.setAttribute('aria-label', aria);
      container.append(svg, tip);
    },
  };
  charts.push(entry);
  observer.observe(container);
}

const drawAll = (force = false) => charts.forEach((c) => c.draw(force));

/** Width changes come from the viewport, from a panel reflowing, and from the
 *  webfont landing after first paint — all of them arrive here. */
const observer = new ResizeObserver(() => drawAll());

function showTip(tip, x, y, rows, when) {
  tip.replaceChildren();
  if (when) tip.append(h('div', { class: 'tip-when', text: when }));
  for (const row of rows) {
    tip.append(h('div', { class: 'tip-row' }, [
      row.color
        ? h('span', {
            class: `tip-key${row.block ? ' is-block' : ''}`,
            style: `background:${row.color}`,
          })
        : null,
      h('span', { class: 'tip-name', text: row.name }),
      h('span', { class: 'tip-val', text: row.value }),
    ]));
  }
  tip.dataset.show = '1';
  const box = tip.getBoundingClientRect();
  const wide = tip.parentElement.clientWidth;
  tip.style.left = `${Math.max(2, Math.min(wide - box.width - 2, x - box.width / 2))}px`;
  tip.style.top = `${Math.max(2, y - box.height - 12)}px`;
}

const hideTip = (tip) => { tip.dataset.show = '0'; };

/** Round axis ticks to numbers people read: 0 / 50.000 / 100.000. */
function ticks(lo, hi, count = 5) {
  const raw = (hi - lo) / count;
  const mag = 10 ** Math.floor(Math.log10(Math.abs(raw) || 1));
  const step = [1, 2, 2.5, 5, 10].map((m) => m * mag).find((v) => v >= raw) || mag * 10;
  const out = [];
  // `v === 0 ? 0 : v` scrubs negative zero — Math.ceil(-0.8) is -0, and -0
  // formats as "-0" on the axis.
  for (let v = Math.ceil(lo / step) * step; v <= hi + step * 1e-9; v += step) {
    out.push(v === 0 ? 0 : v);
  }
  return out;
}

/* ---- chart: the equity curve (the hero) -------------------------------- */

function drawCurve(width, tip) {
  const points = D.curve;
  const asPct = state.mode === 'pct';
  // In percent mode each point carries its own figure: profit over the cash
  // contributed BY THEN. Dividing by a fixed day-one balance would price the
  // whole curve against money that was not in the account for most of it.
  const val = (p, key) => (asPct
    ? (key === 'benchmark' ? p.benchmark_pct : p.roi_pct)
    : p[key]);
  const label = (p, key) => money(p[key], key === 'benchmark' ? p.benchmark_pct : p.roi_pct);

  const height = Math.max(260, Math.min(400, Math.round(width * 0.32)));
  const pad = {
    t: 16, r: Math.min(140, Math.max(64, width * 0.13)), b: 30, l: asPct ? 48 : 74,
  };
  const plotW = width - pad.l - pad.r;
  const plotH = height - pad.t - pad.b;

  const ys = points.flatMap((p) => [val(p, 'equity'), val(p, 'benchmark')]);
  let lo = Math.min(...ys);
  let hi = Math.max(...ys);
  const slack = (hi - lo) * 0.12 || 1;
  lo -= slack; hi += slack;

  const t0 = new Date(points[0].t).getTime();
  const t1 = new Date(points[points.length - 1].t).getTime();
  const X = (p) => pad.l + ((new Date(p.t).getTime() - t0) / (t1 - t0 || 1)) * plotW;
  const Y = (v) => pad.t + plotH - ((v - lo) / (hi - lo)) * plotH;

  const g = s('g');
  const svg = s('svg', { viewBox: `0 0 ${width} ${height}`, width, height }, [g]);

  // grid + y ticks
  for (const v of ticks(lo, hi, 5)) {
    const y = pad.t + plotH - ((v - lo) / (hi - lo)) * plotH;
    g.append(s('line', { class: 'grid-line', x1: pad.l, x2: pad.l + plotW, y1: y, y2: y }));
    g.append(s('text', {
      class: 'tick tick-y', x: pad.l - 9, y: y + 3.5,
      text: asPct ? `${v >= 0 ? '+' : '−'}${Math.abs(v).toFixed(0)}%` : rp(v),
    }));
  }

  // x ticks — only as many as fit without colliding at this width
  const fits = Math.max(2, Math.min(6, Math.floor(plotW / 64)));
  const every = Math.max(1, Math.floor(points.length / fits));
  for (let i = 0; i < points.length; i += every) {
    g.append(s('text', {
      class: 'tick', x: X(points[i]), y: height - 9,
      'text-anchor': i === 0 ? 'start' : 'middle', text: fmtDay(points[i].t),
    }));
  }
  g.append(s('line', {
    class: 'axis-line', x1: pad.l, x2: pad.l + plotW,
    y1: pad.t + plotH, y2: pad.t + plotH,
  }));

  // the shortfall: everything the equal-weight hold made that the bot did not
  const band = points.map((p) => `${X(p)},${Y(val(p, 'benchmark'))}`).join(' L')
    + ' L' + [...points].reverse().map((p) => `${X(p)},${Y(val(p, 'equity'))}`).join(' L');
  g.append(s('path', {
    d: `M${band} Z`, fill: cssVar('--series-2'), 'fill-opacity': .08, stroke: 'none',
  }));

  const line = (key, color) => s('path', {
    d: 'M' + points.map((p) => `${X(p)},${Y(val(p, key))}`).join(' L'),
    fill: 'none', stroke: color, 'stroke-width': 2,
    'stroke-linejoin': 'round', 'stroke-linecap': 'round',
  });
  g.append(line('benchmark', cssVar('--series-2')));
  g.append(line('equity', cssVar('--series-1')));

  // direct end labels — dropped rather than stacked if the lines converge
  const last = points[points.length - 1];
  const ends = [
    { y: Y(val(last, 'equity')), color: cssVar('--series-1'), name: 'Account',
      v: label(last, 'equity') },
    { y: Y(val(last, 'benchmark')), color: cssVar('--series-2'), name: 'Equal-weight hold',
      v: label(last, 'benchmark') },
  ];
  for (const e of ends) {
    g.append(s('circle', {
      cx: X(last), cy: e.y, r: 4.5, fill: e.color,
      stroke: cssVar('--surface'), 'stroke-width': 2,
    }));
  }
  if (Math.abs(ends[0].y - ends[1].y) >= 30 && pad.r > 72) {
    for (const e of ends) {
      g.append(s('text', {
        class: 'serieslabel', x: X(last) + 11, y: e.y - 3,
        fill: cssVar('--ink-2'), text: e.name,
      }));
      g.append(s('text', {
        class: 'serieslabel', x: X(last) + 11, y: e.y + 13,
        fill: cssVar('--ink'), text: e.v,
      }));
    }
  }

  // crosshair: the reader aims at a date, never at a 2px line
  const hair = s('line', { class: 'crosshair', y1: pad.t, y2: pad.t + plotH, opacity: 0 });
  const dots = [cssVar('--series-1'), cssVar('--series-2')].map((c) => s('circle', {
    r: 4.5, fill: c, stroke: cssVar('--surface'), 'stroke-width': 2, opacity: 0,
  }));
  g.append(hair, ...dots);

  const move = (ev) => {
    const box = svg.getBoundingClientRect();
    const px = ((ev.clientX ?? 0) - box.left) * (width / box.width);
    const ratio = Math.max(0, Math.min(1, (px - pad.l) / plotW));
    const p = points[Math.round(ratio * (points.length - 1))];
    if (!p) return;
    const x = X(p);
    hair.setAttribute('x1', x); hair.setAttribute('x2', x); hair.setAttribute('opacity', 1);
    ['equity', 'benchmark'].forEach((key, i) => {
      dots[i].setAttribute('cx', x); dots[i].setAttribute('cy', Y(val(p, key)));
      dots[i].setAttribute('opacity', 1);
    });
    const rows = [
      { name: 'Account', color: cssVar('--series-1'), value: label(p, 'equity') },
      { name: 'Equal-weight hold', color: cssVar('--series-2'), value: label(p, 'benchmark') },
      { name: p.positions === 1 ? 'Position open' : 'Positions open', value: String(p.positions) },
    ];
    if (p.deposit) rows.push({ name: p.deposit > 0 ? 'Deposited' : 'Withdrew', value: rp(Math.abs(p.deposit)) });
    showTip(tip, x * (box.width / width),
      Y(Math.max(val(p, 'equity'), val(p, 'benchmark'))) * (box.height / height),
      rows, fmtWhen(p.t));
  };
  const leave = () => {
    hair.setAttribute('opacity', 0);
    dots.forEach((d) => d.setAttribute('opacity', 0));
    hideTip(tip);
  };
  svg.append(s('rect', {
    class: 'hit', x: pad.l, y: pad.t, width: plotW, height: plotH,
    onpointermove: move, onpointerleave: leave,
  }));
  return svg;
}

/* ---- chart: diverging bars (profit and loss by asset) ------------------- */

/** Assets ranked by whatever the current mode plots, so bar order always matches
 *  bar length. Ranking by rupiah while drawing percentages reads as a bug. */
function sortedAssets() {
  const key = state.mode === 'pct'
    ? (a) => (isNum(a.contribution_pct) ? a.contribution_pct : -Infinity)
    : (a) => a.contribution;
  return [...D.assets].sort((a, b) => key(b) - key(a));
}

function drawAssetBars(width, tip) {
  const rows = sortedAssets();
  const asPct = state.mode === 'pct';
  const value = (a) => (asPct ? a.contribution_pct : a.contribution);
  const fmt = (a) => money(a.contribution, a.contribution_pct, { sign: true });

  const band = 34;
  const barH = Math.min(24, band - 12);
  const height = rows.length * band + 6;
  const labelW = 46;

  // Reserve exactly as much room as the longest value label needs on either side
  // of the plot, so a label rides its bar's tip instead of being clipped.
  const widest = Math.max(...rows.map((a) => fmt(a).length), 6);
  const gutter = Math.min(110, widest * 6.9 + 10);
  const plotL = labelW + 8 + gutter;
  const plotW = Math.max(50, width - plotL - gutter);

  // Zero sits where the data puts it, not in the middle: forcing it to the centre
  // would spend half the plot on a loss side that only reaches a third as far.
  const vals = rows.map(value).filter(isNum);
  const lo = Math.min(0, ...vals);
  const hi = Math.max(0, ...vals);
  const spread = hi - lo || 1;
  const X = (v) => plotL + ((v - lo) / spread) * plotW;
  const zero = X(0);

  const g = s('g');
  const svg = s('svg', { viewBox: `0 0 ${width} ${height}`, width, height }, [g]);

  g.append(s('line', {
    class: 'axis-line', x1: zero, x2: zero, y1: 2, y2: rows.length * band + 2,
  }));

  rows.forEach((a, i) => {
    const v = value(a);
    const y = i * band + (band - barH) / 2 + 2;
    const w = isNum(v) ? Math.abs(X(v) - zero) : 0;
    const positive = (v || 0) >= 0;
    const color = positive ? cssVar('--pos') : cssVar('--neg');
    // 2px surface gap at the baseline keeps neighbouring bars apart without a stroke
    const x = positive ? zero + 1 : zero - 1 - w;
    const r = Math.min(4, w);

    g.append(s('text', {
      class: 'tick', x: labelW, y: y + barH / 2 + 4, 'text-anchor': 'end',
      fill: cssVar('--ink'), style: 'font-size:12px;font-weight:600', text: a.symbol,
    }));

    if (w > 0) {
      // rounded on the data end, square against the baseline
      const d = positive
        ? `M${x},${y} H${x + w - r} a${r},${r} 0 0 1 ${r},${r} V${y + barH - r} a${r},${r} 0 0 1 ${-r},${r} H${x} Z`
        : `M${x + w},${y} H${x + r} a${r},${r} 0 0 0 ${-r},${r} V${y + barH - r} a${r},${r} 0 0 0 ${r},${r} H${x + w} Z`;
      g.append(s('path', { class: 'bar', d, fill: color }));
    }

    // the value rides its own bar tip, on the outside
    g.append(s('text', {
      class: 'tick', y: y + barH / 2 + 4,
      x: positive ? zero + w + 9 : zero - w - 9,
      'text-anchor': positive ? 'start' : 'end',
      fill: cssVar('--ink'), style: 'font-size:12px;font-weight:600', text: fmt(a),
    }));

    // hit target spans the whole band, not just the painted bar
    g.append(s('rect', {
      class: 'hit', x: 0, y: i * band + 2, width, height: band, tabindex: 0,
      onpointerenter: (ev) => showTip(tip, ev.offsetX, i * band + band, [
        { name: 'Net realised', value: money(a.net, a.cost_basis ? a.net / a.cost_basis * 100 : null, { sign: true }) },
        { name: 'Unrealised', value: a.open ? money(a.unrealised, a.cost_basis ? a.unrealised / a.cost_basis * 100 : null, { sign: true }) : '—' },
        { name: 'Closed trades', value: String(a.trades) },
        { name: 'True win rate', value: isNum(a.true_win_pct) ? pct(a.true_win_pct, { sign: false, digits: 1 }) : '—' },
      ], a.symbol),
      onpointerleave: () => hideTip(tip),
      onfocus: () => showTip(tip, zero, i * band + band, [
        { name: 'Total contribution', value: fmt(a) },
        { name: 'Closed trades', value: String(a.trades) },
      ], a.symbol),
      onblur: () => hideTip(tip),
    }));
  });

  return svg;
}

/* ---- chart: realised profit and loss by day ---------------------------- */

function drawDaily(width, tip) {
  const rows = D.daily;
  const asPct = state.mode === 'pct';
  const start = D.meta.start_equity;
  const value = (r) => (asPct ? (r.net / (r.capital || start)) * 100 : r.net);

  const height = 210;
  const pad = { t: 12, r: 4, b: 30, l: asPct ? 52 : 74 };
  const plotW = width - pad.l - pad.r;
  const plotH = height - pad.t - pad.b;

  const vals = rows.map(value);
  const lo = Math.min(0, ...vals) * 1.14;
  const hi = Math.max(0, ...vals) * 1.14 || 1;
  const Y = (v) => pad.t + plotH - ((v - lo) / (hi - lo)) * plotH;

  const slot = plotW / Math.max(rows.length, 1);
  const barW = Math.min(24, slot - 6);
  const labelEvery = Math.max(1, Math.ceil(46 / slot));

  const g = s('g');
  const svg = s('svg', { viewBox: `0 0 ${width} ${height}`, width, height }, [g]);

  for (const v of ticks(lo, hi, 5)) {
    g.append(s('line', { class: 'grid-line', x1: pad.l, x2: pad.l + plotW, y1: Y(v), y2: Y(v) }));
    g.append(s('text', {
      class: 'tick tick-y', x: pad.l - 9, y: Y(v) + 3.5,
      text: asPct ? pct(v, { digits: 1 }) : rp(v, { sign: false }),
    }));
  }
  g.append(s('line', { class: 'axis-line', x1: pad.l, x2: pad.l + plotW, y1: Y(0), y2: Y(0) }));

  rows.forEach((row, i) => {
    const v = value(row);
    const x = pad.l + i * slot + (slot - barW) / 2;
    const top = Math.min(Y(0), Y(v));
    const barH = Math.max(1, Math.abs(Y(v) - Y(0)));
    const color = v >= 0 ? cssVar('--pos') : cssVar('--neg');
    const r = Math.min(4, barW / 2, barH);
    const d = v >= 0
      ? `M${x},${top + barH} V${top + r} a${r},${r} 0 0 1 ${r},${-r} H${x + barW - r} a${r},${r} 0 0 1 ${r},${r} V${top + barH} Z`
      : `M${x},${top} V${top + barH - r} a${r},${r} 0 0 0 ${r},${r} H${x + barW - r} a${r},${r} 0 0 0 ${r},${-r} V${top} Z`;
    g.append(s('path', { class: 'bar', d, fill: color }));

    // Thin the day labels rather than let them overlap; the tooltip and the
    // table view still name every bar.
    if (i % labelEvery === 0) {
      g.append(s('text', {
        class: 'tick', x: x + barW / 2, y: height - 9, 'text-anchor': 'middle',
        text: fmtDay(`${row.date}T00:00:00Z`),
      }));
    }

    g.append(s('rect', {
      class: 'hit', x: pad.l + i * slot, y: pad.t, width: slot, height: plotH, tabindex: 0,
      onpointerenter: () => showTip(tip, pad.l + i * slot + slot / 2, top, [
        { name: 'Realised', color, block: true,
          value: money(row.net, (row.net / start) * 100, { sign: true }) },
        { name: row.trades === 1 ? 'Trade closed' : 'Trades closed', value: String(row.trades) },
      ], fmtDay(`${row.date}T00:00:00Z`)),
      onpointerleave: () => hideTip(tip),
      onfocus: () => showTip(tip, pad.l + i * slot + slot / 2, top, [
        { name: 'Realised', color, block: true,
          value: money(row.net, (row.net / start) * 100, { sign: true }) },
      ], fmtDay(`${row.date}T00:00:00Z`)),
      onblur: () => hideTip(tip),
    }));
  });
  return svg;
}

/* ---- page sections ----------------------------------------------------- */

function statusRail() {
  const t = D.totals;
  const snapAge = Date.now() - new Date(D.meta.generated_at).getTime();
  const stale = snapAge > 90 * 60 * 1000;

  const item = (pipClass, label, value, note) => h('div', { class: 'state' }, [
    pipClass ? h('span', { class: `pip ${pipClass}` }) : null,
    h('i', { text: label }),
    h('b', { text: value }),
    note ? h('i', { text: note }) : null,
  ]);

  return h('header', { class: 'rail' }, [
    h('div', { class: 'rail-in' }, [
      h('div', { class: 'brand' }, [
        'CryptoIndodaxBot ',
        h('span', { text: 'Indodax · IDR' }),
      ]),
      item(D.meta.trading_enabled ? 'is-live' : 'is-off',
        D.meta.trading_enabled ? 'Live' : 'Paused',
        D.meta.trading_enabled ? 'real money' : 'no orders'),
      item(stale ? 'is-warning' : null, 'Data', fmtAge(snapAge)),
      item(null, 'Positions', `${t.open_positions}/${t.max_positions}`),
      item(null, 'Closed', String(t.trades)),
      h('div', { class: 'rail-end' }, [
        h('button', {
          class: 'ctl', type: 'button', 'aria-pressed': state.mode === 'pct',
          title: 'Hide every rupiah figure and show percentages only',
          text: state.mode === 'pct' ? 'Showing %' : 'Showing Rp',
          onclick: () => {
            state.mode = state.mode === 'pct' ? 'idr' : 'pct';
            localStorage.setItem('idx.mode', state.mode);
            render();
          },
        }),
        h('button', {
          class: 'ctl', type: 'button', title: 'Switch between light and dark',
          text: 'Theme',
          onclick: () => {
            const dark = matchMedia('(prefers-color-scheme: dark)').matches;
            const now = document.documentElement.getAttribute('data-theme')
              || (dark ? 'dark' : 'light');
            state.theme = now === 'dark' ? 'light' : 'dark';
            document.documentElement.setAttribute('data-theme', state.theme);
            localStorage.setItem('idx.theme', state.theme);
            drawAll(true);              // the marks read their hues from CSS vars
          },
        }),
      ]),
    ]),
  ]);
}

function hero() {
  const t = D.totals;
  // Against a buy-and-hold percentage the fair figure is the time-weighted one:
  // a hold has no transfers to time, so return_pct (which does) would penalise
  // or flatter the bot purely for when money arrived.
  const strategyPct = isNum(t.twr_pct) ? t.twr_pct : t.return_pct;
  const behind = isNum(t.benchmark_pct) ? strategyPct - t.benchmark_pct : null;
  const verdict = D.scorecard.verdict;
  const tone = verdict.startsWith('STOP') ? 'is-critical'
    : verdict === 'CONTINUE' ? 'is-live' : 'is-off';

  const plot = h('figure', { class: 'chart' });

  const section = h('section', { class: 'hero' }, [
    h('div', { class: 'hero-head' }, [
      h('div', {}, [
        h('div', { class: 'hero-label', text: `Account since ${fmtDay(D.meta.start_date + 'T00:00:00Z')}` }),
        h('strong', { class: `hero-figure ${signClass(t.return_pct)}`, text: pct(t.return_pct) }),
        h('p', { class: 'hero-sub' }, [
          state.mode === 'pct' ? null : h('b', { text: rp(t.equity) }),
          state.mode === 'pct' ? null : ' today, ',
          `${t.days_live} days live, `,
          h('b', { text: String(t.trades) }),
          ' trades closed. ',
          t.deposits
            ? h('span', {}, [
                h('b', { text: rp(Math.abs(t.deposits)) }),
                t.deposits > 0 ? ' was paid in along the way' : ' was withdrawn along the way',
                ', counted as capital rather than profit, so the figure above is ',
                h('b', { text: money(t.net_pnl, t.return_pct, { sign: true }) }),
                ' on ', h('b', { text: rp(t.invested_capital) }), ' contributed. ',
              ])
            : null,
          isNum(behind)
            ? h('span', {}, [
                'Simply holding the watchlist would have returned ',
                h('b', { text: pct(t.benchmark_pct) }),
                ', against ',
                h('b', { text: pct(strategyPct) }),
                t.deposits ? ' for the strategy once deposit timing is removed' : ' for the bot',
                `, so it is ${Math.abs(behind).toFixed(2)} points `,
                behind < 0 ? 'behind it.' : 'ahead of it.',
              ])
            : null,
        ]),
      ]),
      h('div', { class: 'hero-verdict' }, [
        h('div', { class: 'verdict-tag' }, [
          h('span', { class: `pip ${tone}` }),
          h('span', { text: verdict === 'COLLECTING' ? 'Collecting evidence' : verdict }),
        ]),
        h('div', { class: 'verdict-note',
          text: `Verdict due ${fmtDay(D.meta.decision_date + 'T00:00:00Z')} or ${D.meta.min_trades} trades` }),
      ]),
    ]),
    h('div', { class: 'hero-plot' }, [
      plot,
      h('div', { class: 'legend' }, [
        h('span', { class: 'legend-item' }, [
          h('span', { class: 'legend-key', style: `background:${cssVar('--series-1')}` }),
          'Account',
        ]),
        h('span', { class: 'legend-item' }, [
          h('span', { class: 'legend-key', style: `background:${cssVar('--series-2')}` }),
          'Equal-weight hold of the watchlist',
        ]),
        h('span', { class: 'legend-item', style: 'color:var(--muted)' }, [
          h('span', {
            class: 'legend-key is-block',
            style: `background:color-mix(in srgb, ${cssVar('--series-2')} 20%, transparent)`,
          }),
          'Shortfall',
        ]),
      ]),
    ]),
  ]);

  chart(plot, drawCurve, {
    aria: `Account equity against an equal-weight hold of the watchlist, ${D.meta.start_date} to today. `
      + `Account ${pct(t.return_pct)}, hold ${pct(t.benchmark_pct)}.`,
  });
  return section;
}

/** Cash the account moved that no trade and no recorded transfer explains.
 *  Every percentage on this page is wrong by that much, so it leads the page
 *  rather than sitting in a footnote. */
function cashWarning() {
  const gaps = D.meta.unrecorded_cashflows || [];
  if (!gaps.length) return null;
  const total = gaps.reduce((a, g) => a + g.residual, 0);
  return h('section', { class: 'recon' }, [
    h('div', { class: 'recon-icon', text: '▲' }),
    h('div', {}, [
      h('h2', { text: 'Unrecorded cash movement' }),
      h('p', {}, [
        'The account balance moved by ', h('b', { text: rp(Math.abs(total)) }),
        ` across ${gaps.length} ${gaps.length === 1 ? 'hour' : 'hours'} with no trade behind it`,
        ' and no transfer on record. Until that is resolved every return on this page is ',
        'wrong by that amount. It is either a deposit or withdrawal that needs recording, ',
        'or a fill the ledger never saw — and the two are indistinguishable from outside, ',
        'which is why nothing here guesses.',
      ]),
    ]),
  ]);
}

function reconciliation() {
  const t = D.totals;
  if (!isNum(t.drift)) return null;
  const material = Math.abs(t.drift) > t.equity * 0.01;
  const claimed = t.realised_net + t.unrealised;

  return h('section', { class: `recon${material ? '' : ' is-ok'}` }, [
    h('div', { class: 'recon-icon', text: material ? '▲' : '✓' }),
    h('div', {}, [
      h('h2', { text: material ? 'The ledger and the account disagree' : 'Ledger reconciles with the account' }),
      material
        ? h('p', {}, [
            'The trade ledger adds up to ', h('b', { text: money(claimed, (claimed / t.invested_capital) * 100, { sign: true }) }),
            ' of profit, but the Indodax account is only ', h('b', { text: money(t.net_pnl, t.return_pct, { sign: true }) }),
            ' up — a gap of ', h('b', { text: money(t.drift, (t.drift / t.invested_capital) * 100, { sign: true }) }),
            '. Every figure on this page that comes from the ledger is that much too kind. ',
            'The live account balance is the number to trust, and it is what the headline above shows. ',
            `Most of the gap is unrecorded fill slippage: ${t.fees_estimated_trades} of ${t.trades} closed trades `,
            'booked the hourly snapshot close as their fill price rather than what the exchange actually paid, '
            + 'and carry a modelled fee instead of the reported one.',
          ])
        : h('p', {}, [
            'The reconstructed curve lands within ', h('b', { text: rp(Math.abs(t.drift)) }),
            ' of the live Indodax balance, so the history below can be read at face value.',
          ]),
    ]),
  ]);
}

function kpis() {
  const t = D.totals;
  const tile = (label, value, cls, foot) => h('div', { class: 'kpi' }, [
    h('div', { class: 'kpi-label', text: label }),
    h('strong', { class: `kpi-value ${cls || ''}`, text: value }),
    foot ? h('div', { class: 'kpi-foot', text: foot }) : null,
  ]);

  return h('section', { class: 'kpis' }, [
    tile('Realised', money(t.realised_net, (t.realised_net / t.invested_capital) * 100, { sign: true }),
      signClass(t.realised_net), `${t.trades} closed trades`),
    tile('Unrealised', money(t.unrealised, t.invested ? (t.unrealised / (t.invested - t.unrealised)) * 100 : null, { sign: true }),
      signClass(t.unrealised), `${t.open_positions} open`),
    tile('True win rate', pct(t.true_win_pct, { sign: false, digits: 1 }),
      t.true_win_pct < 35 ? 'down' : '', `${t.wins} of ${t.trades} · take-profit only ${t.tp_only_win_pct}%`),
    tile('Stop rate', pct(t.stop_pct, { sign: false, digits: 1 }), 'down',
      `${t.stops} trades stopped out`),
    tile('Fee drag', pct(t.fee_drag_pct, { sign: false, digits: 1 }), 'down',
      state.mode === 'pct' ? 'of gross profit' : `${rp(t.fees)} of ${rp(t.realised_gross)} gross`),
    tile('Max drawdown', pct(t.max_drawdown_pct, { sign: false, digits: 2 }), 'down',
      'deepest dip from a peak'),
  ]);
}

/** A chart card with the table-view twin every chart owes its reader. */
function chartPanel({ title, note, aria, draw, table }) {
  const plot = h('figure', { class: 'chart' });
  const view = h('div', { class: 'tableview', hidden: true }, [
    h('div', { class: 'scroller' }, [table()]),
  ]);
  const toggle = h('button', {
    class: 'ctl', type: 'button', text: 'Show table', 'aria-pressed': 'false',
    onclick: () => {
      const showing = view.hidden;
      view.hidden = !showing;
      plot.hidden = showing;
      toggle.textContent = showing ? 'Show chart' : 'Show table';
      toggle.setAttribute('aria-pressed', String(showing));
    },
  });
  const panel = h('section', { class: 'panel' }, [
    h('div', { class: 'panel-head' }, [h('h2', { text: title }), toggle]),
    note ? h('p', { class: 'panel-note', text: note }) : null,
    plot, view,
  ]);
  chart(plot, draw, { aria });
  return panel;
}

function assetTable() {
  const head = ['Coin', 'Closed', 'True win', 'Stop rate', 'Avg R', 'Realised',
    'Unrealised', 'Total', 'Fee drag'];
  return h('table', {}, [
    h('thead', {}, [h('tr', {}, head.map((t) => h('th', { text: t })))]),
    h('tbody', {}, sortedAssets().map((a) => h('tr', {}, [
      h('td', {}, [h('span', { class: 'sym' }, [
        h('span', {
          class: 'sym-key',
          style: `background:${a.contribution >= 0 ? cssVar('--pos') : cssVar('--neg')}`,
        }),
        h('span', { text: a.symbol }),
      ])]),
      h('td', { text: a.trades ? String(a.trades) : '—' }),
      h('td', { text: isNum(a.true_win_pct) ? pct(a.true_win_pct, { sign: false, digits: 1 }) : '—' }),
      h('td', { text: isNum(a.stop_pct) ? pct(a.stop_pct, { sign: false, digits: 1 }) : '—' }),
      h('td', { text: isNum(a.avg_r) ? `${a.avg_r > 0 ? '+' : ''}${a.avg_r.toFixed(2)}R` : '—' }),
      h('td', { class: signClass(a.net), text: a.trades ? money(a.net, a.cost_basis ? a.net / a.cost_basis * 100 : null, { sign: true }) : '—' }),
      h('td', { class: signClass(a.unrealised), text: a.open ? money(a.unrealised, a.cost_basis ? a.unrealised / a.cost_basis * 100 : null, { sign: true }) : '—' }),
      h('td', { class: signClass(a.contribution), text: money(a.contribution, a.contribution_pct, { sign: true }) }),
      h('td', { text: isNum(a.fee_drag_pct) ? pct(a.fee_drag_pct, { sign: false, digits: 1 }) : '—' }),
    ]))),
  ]);
}

function dailyTable() {
  return h('table', {}, [
    h('thead', {}, [h('tr', {}, ['Day', 'Trades closed', 'Realised']
      .map((t) => h('th', { text: t })))]),
    h('tbody', {}, D.daily.map((r) => h('tr', {}, [
      h('td', { text: fmtDay(`${r.date}T00:00:00Z`) }),
      h('td', { text: String(r.trades) }),
      h('td', {
        class: signClass(r.net),
        text: money(r.net, (r.net / (r.capital || D.meta.start_equity)) * 100, { sign: true }),
      }),
    ]))),
  ]);
}

function scorecardPanel() {
  const c = D.scorecard;
  const t = D.totals;
  const left = Math.max(0, Math.ceil(
    (new Date(D.meta.decision_date + 'T00:00:00Z') - Date.now()) / 86400000));

  const crit = (n, name, value, failing, note) => h('li', { class: 'crit' }, [
    h('span', { class: 'crit-mark', text: failing ? '✕' : '✓',
      style: `color:${failing ? cssVar('--critical') : cssVar('--good')}` }),
    h('span', { class: 'crit-name', text: name }),
    h('span', { class: `crit-val ${failing ? 'down' : ''}`, text: value }),
    h('span', { class: 'crit-note', text: note }),
  ]);

  return h('section', { class: 'panel' }, [
    h('div', { class: 'panel-head' }, [h('h2', { text: 'Pre-registered decision' })]),
    h('p', { class: 'panel-note',
      text: 'Registered 17 Sep, before this window opened, so the bar cannot move to fit '
        + 'the result. Trading stops and goes back to paper only if both conditions fail '
        + 'at the decision point.' }),
    h('ul', { class: 'criteria' }, [
      crit(1, 'True win rate holds above 35%',
        pct(c.true_win_pct, { sign: false, digits: 1 }), c.fails_win_floor,
        `${t.wins} wins from ${t.trades} trades. A stop is a failed trade whatever it paid, `
        + `and a profit-lock exit counts only at or above +${D.meta.lock_win_r}R.`),
      crit(2, 'Beats an equal-weight hold',
        isNum(c.benchmark_pct) ? `${pct(t.return_pct)} vs ${pct(c.benchmark_pct)}` : '—',
        c.trails_benchmark,
        'Measured on the coins priced in the first snapshot, so a late arrival cannot '
        + 'flatter or punish the comparison.'),
    ]),
    h('div', { class: 'countdown' }, [
      h('div', {}, [
        h('div', { class: 'kpi-label', text: 'Days to the decision' }),
        h('strong', { class: 'kpi-value', text: String(left) }),
      ]),
      h('div', {}, [
        h('div', { class: 'kpi-label', text: 'Trades still needed' }),
        h('strong', { class: 'kpi-value', text: String(Math.max(0, D.meta.min_trades - t.trades)) }),
      ]),
      h('div', {}, [
        h('div', { class: 'kpi-label', text: 'Days live' }),
        h('strong', { class: 'kpi-value', text: String(t.days_live) }),
      ]),
    ]),
  ]);
}

function positionsPanel() {
  if (!D.positions.length) {
    return h('section', { class: 'panel' }, [
      h('div', { class: 'panel-head' }, [h('h2', { text: 'Open positions' })]),
      h('p', { class: 'panel-note',
        text: 'Nothing held right now — the account is all rupiah. The bot opens a position '
          + 'only when the regime gate and every entry filter agree.' }),
    ]);
  }

  const cards = D.positions.map((p) => {
    const span = TP_R - STOP_R;
    const clamp = (r) => Math.max(0, Math.min(1, (r - STOP_R) / span));
    const at = isNum(p.r_so_far) ? clamp(p.r_so_far) : null;
    const entry = clamp(0);
    const stopAt = isNum(p.stop) && isNum(p.initial_stop) && p.initial_stop !== p.entry_price
      ? clamp((p.stop - p.entry_price) / (p.entry_price - p.initial_stop))
      : null;
    const up = (p.unrealised || 0) >= 0;
    const color = up ? cssVar('--pos') : cssVar('--neg');

    return h('article', { class: 'pos' }, [
      h('div', { class: 'pos-head' }, [
        h('span', { class: 'pos-sym', text: p.symbol }),
        h('span', {
          class: `pos-pnl ${signClass(p.unrealised)}`,
          text: money(p.unrealised, p.unrealised_pct, { sign: true }),
        }),
      ]),
      // In percent mode the quantity goes too: coins are publicly priced, so a
      // quantity is an account balance written the long way round.
      h('div', { class: 'pos-meta',
        text: state.mode === 'pct'
          ? `held ${p.hours_held}h${p.half_size ? ' · half size' : ''}`
            + (isNum(p.peak_r) ? ` · peaked +${p.peak_r.toFixed(2)}R` : '')
          : `${qty(p.qty)} @ ${price(p.entry_price)} · now ${price(p.price)}` }),
      h('div', { class: 'pos-meta',
        text: `held ${p.hours_held}h${p.half_size ? ' · half size' : ''}`
          + (isNum(p.peak_r) ? ` · peaked +${p.peak_r.toFixed(2)}R` : ''),
        hidden: state.mode === 'pct' }),
      h('div', { class: 'meter' }, [
        h('div', {
          class: 'meter-track',
          role: 'img',
          'aria-label': `${p.symbol} at ${isNum(p.r_so_far) ? p.r_so_far.toFixed(2) : '—'} R, `
            + (state.mode === 'pct' ? '' : `stop at ${price(p.stop)}, `)
            + `take-profit at +${TP_R}R`,
        }, [
          at === null ? null : h('span', {
            class: 'meter-fill',
            style: `left:${Math.min(entry, at) * 100}%;width:${Math.abs(at - entry) * 100}%;`
              + `background:${color}`,
          }),
          stopAt === null ? null : h('span', {
            class: 'meter-fill',
            style: `left:calc(${stopAt * 100}% - 1px);width:2px;background:${cssVar('--ink')}`,
          }),
        ]),
        h('div', { class: 'meter-scale' }, [
          h('span', { text: 'stop −1R' }),
          h('span', { text: isNum(p.r_so_far) ? `${p.r_so_far >= 0 ? '+' : '−'}${Math.abs(p.r_so_far).toFixed(2)}R now` : '—' }),
          h('span', { text: `+${TP_R}R` }),
        ]),
      ]),
    ]);
  });

  return h('section', { class: 'panel' }, [
    h('div', { class: 'panel-head' }, [
      h('h2', { text: `Open positions (${D.positions.length})` }),
    ]),
    h('p', { class: 'panel-note',
      text: 'The bar runs from a full 1R loss to the +2.5R take-profit. The dark notch is '
        + 'where the stop currently sits — past the entry it has ratcheted into profit.' }),
    h('div', { class: 'positions' }, cards),
  ]);
}

function tradeLog() {
  const head = ['Coin', 'Closed', 'Held', 'Exit', 'Result', 'R', 'Move', 'Net'];
  const rows = D.trades.map((t) => h('tr', {}, [
    h('td', {}, [h('span', { class: 'sym' }, [
      h('span', {
        class: 'sym-key',
        style: `background:${(t.pnl || 0) >= 0 ? cssVar('--pos') : cssVar('--neg')}`,
      }),
      h('span', { text: t.symbol }),
    ])]),
    h('td', { text: fmtWhen(t.exit_time) }),
    h('td', { text: isNum(t.hours_held) ? `${t.hours_held}h` : '—' }),
    h('td', { text: t.reason || '—' }),
    h('td', {}, [h('span', {
      class: `badge ${t.win ? 'is-win' : 'is-fail'}`,
      text: t.win ? 'Win' : 'Failed',
    })]),
    h('td', { text: isNum(t.r_multiple) ? `${t.r_multiple > 0 ? '+' : '−'}${Math.abs(t.r_multiple).toFixed(2)}R` : '—' }),
    h('td', { class: signClass(t.pnl_pct), text: pct(t.pnl_pct) }),
    h('td', { class: signClass(t.pnl), text: money(t.pnl, t.cost ? (t.pnl / t.cost) * 100 : null, { sign: true }) }),
  ]));

  return h('section', { class: 'panel' }, [
    h('div', { class: 'panel-head' }, [h('h2', { text: 'Trade log' })]),
    h('p', { class: 'panel-note',
      text: 'Newest first. "Result" follows the standing rule rather than the sign of the '
        + 'money: a stop is a failed trade even when it paid, and a profit-lock exit is a '
        + 'win only at or above +1R.' }),
    h('div', { class: 'scroller' }, [
      h('table', {}, [
        h('thead', {}, [h('tr', {}, head.map((t) => h('th', { text: t })))]),
        h('tbody', {}, rows),
      ]),
    ]),
  ]);
}

function footer() {
  const t = D.totals;
  return h('footer', { class: 'foot' }, [
    h('p', {}, [
      'Generated ', h('b', { text: fmtWhen(D.meta.generated_at) }), ' WIB from ',
      h('b', { text: String(D.meta.snapshots) }), ' hourly snapshots. The page refreshes itself '
      + 'every five minutes; the data behind it is rebuilt every hour, just after the trader runs.',
    ]),
    h('p', {}, [
      'The equity line is reconstructed from the snapshot archive and the trade ledger, not '
      + 'recorded live, and it assumes no rupiah moved in or out of the account since ',
      h('b', { text: fmtDay(D.meta.start_date + 'T00:00:00Z') }), '. ',
      `Fees on ${t.fees_estimated_trades} of ${t.trades} closed trades are modelled at the `
      + 'configured taker rate rather than reported by the exchange, so realised profit is '
      + 'optimistic by roughly that much.',
    ]),
    h('p', { text: 'Anyone with this link can read it. There is no login.' }),
  ]);
}

/* ---- boot -------------------------------------------------------------- */

function render() {
  observer.disconnect();
  charts.length = 0;
  document.body.replaceChildren(
    statusRail(),
    h('main', {}, [
      h('div', { class: 'wrap' }, [
        hero(),
        cashWarning(),
        reconciliation(),
        kpis(),
        h('div', { class: 'split' }, [
          chartPanel({
            title: 'Profit and loss by asset',
            note: 'Realised plus unrealised, per coin, since the account opened. In percent '
              + 'mode each coin is measured against the rupiah put at risk in it, so a small '
              + 'position is not flattened by the size of the account.',
            aria: 'Total profit and loss per coin, best to worst.',
            draw: drawAssetBars,
            table: assetTable,
          }),
          scorecardPanel(),
        ]),
        chartPanel({
          title: 'Realised profit and loss by day',
          note: 'Booked on the day a trade closed. Days with no closed trade are left out.',
          aria: 'Realised profit and loss for each day a trade closed.',
          draw: drawDaily,
          table: dailyTable,
        }),
        positionsPanel(),
        tradeLog(),
        footer(),
      ]),
    ]),
  );
  drawAll(true);            // now the containers have a width to measure
}

function boot() {
  if (state.theme !== 'auto') document.documentElement.setAttribute('data-theme', state.theme);

  // Label widths are measured from the rendered font, so redraw once it lands.
  if (document.fonts) document.fonts.ready.then(() => drawAll(true));

  const load = () => fetch(`dashboard.json?t=${Date.now()}`, { cache: 'no-store' })
    .then((r) => {
      if (!r.ok) throw new Error(`dashboard.json returned ${r.status}`);
      return r.json();
    })
    .then((doc) => { D = doc; render(); })
    .catch((err) => {
      if (D) return;                        // a failed refresh keeps the last good render
      document.body.replaceChildren(h('main', { class: 'wrap' }, [
        h('section', { class: 'panel', style: 'margin-top:40px' }, [
          h('h2', { text: 'No data to show yet' }),
          h('p', { class: 'panel-note',
            text: `Could not read dashboard.json (${err.message}). Run `
              + '"python -m cryptoindodax.dashboard" on the server to write it.' }),
        ]),
      ]));
    });

  load();
  setInterval(load, 5 * 60 * 1000);
}

boot();
