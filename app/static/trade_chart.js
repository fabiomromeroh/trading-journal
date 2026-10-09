/* Trade detail chart: timeframes, volume + MA(20), client-side indicators (lightweight-charts v5). */
(function () {
  'use strict';
  const C = window.TJ_CHART;
  const L = window.LightweightCharts;
  const $ = (id) => document.getElementById(id);
  const el = $('chart'), ph = $('chart-placeholder');
  const LS_IND = 'tj.chart.indicators.v1', LS_VOL = 'tj.chart.volume.v1', LS_TF = 'tj.chart.tf.' + C.tradeId;
  const UP = '#10b981', DOWN = '#f43f5e';
  const PALETTE = ['#38bdf8', '#f59e0b', '#a78bfa', '#f472b6', '#34d399', '#facc15', '#fb7185', '#60a5fa', '#e2e8f0'];
  const CATALOG = {
    SMA: { label: 'SMA', overlay: true, defaults: { period: 50 } },
    EMA: { label: 'EMA', overlay: true, defaults: { period: 20 } },
    VWAP: { label: 'VWAP', overlay: true, defaults: {}, intradayOnly: true },
    BB: { label: 'Bollinger Bands', overlay: true, defaults: { period: 20, mult: 2 } },
    RSI: { label: 'RSI', defaults: { period: 14 } },
    MACD: { label: 'MACD', defaults: { fast: 12, slow: 26, signal: 9 } },
    ATR: { label: 'ATR', defaults: { period: 14 } },
  };
  const PRESETS = [['EMA', 10], ['EMA', 20], ['EMA', 21], ['EMA', 50], ['SMA', 10], ['SMA', 20], ['SMA', 50], ['SMA', 200]];
  const DEFAULT_IND = [{ type: 'EMA', period: 10, color: '#38bdf8' }, { type: 'EMA', period: 20, color: '#f59e0b' },
                       { type: 'SMA', period: 50, color: '#a78bfa' }];

  // ------------------------------------------------------------------ state
  function loadInd() {
    try {
      const v = JSON.parse(localStorage.getItem(LS_IND));
      if (Array.isArray(v)) return v.filter((i) => i && CATALOG[i.type]);
    } catch (e) { /* ignore */ }
    return DEFAULT_IND.map((x) => ({ ...x }));
  }
  let inds = loadInd();
  let showVol = localStorage.getItem(LS_VOL) !== '0';
  let data = null, chart = null, candleSeries = null, legendSeries = [];
  const saveInd = () => localStorage.setItem(LS_IND, JSON.stringify(inds));
  const label = (i) => i.type === 'BB' ? `BB ${i.period} ${i.mult}` : i.type === 'MACD' ? `MACD ${i.fast} ${i.slow} ${i.signal}`
    : i.type === 'VWAP' ? 'VWAP' : `${i.type} ${i.period}`;
  const nextColor = () => PALETTE.find((c) => !inds.some((i) => i.color === c)) || PALETTE[inds.length % PALETTE.length];
  const posInt = (v, d) => { const n = parseInt(v, 10); return Number.isFinite(n) && n > 0 && n <= 1000 ? n : d; };

  // ------------------------------------------------------------------ math (null = not enough data yet)
  function sma(v, n) {
    const out = new Array(v.length).fill(null); let s = 0;
    for (let i = 0; i < v.length; i++) { s += v[i]; if (i >= n) s -= v[i - n]; if (i >= n - 1) out[i] = s / n; }
    return out;
  }
  function smooth(v, n, k) { // EMA (k=2/(n+1)) or Wilder RMA (k=1/n), seeded with the SMA of the first n values
    const out = new Array(v.length).fill(null); let prev = null, cnt = 0, sum = 0;
    for (let i = 0; i < v.length; i++) {
      const x = v[i]; if (x === null || x === undefined) continue;
      if (prev === null) { sum += x; cnt++; if (cnt === n) { prev = sum / n; out[i] = prev; } }
      else { prev = x * k + prev * (1 - k); out[i] = prev; }
    }
    return out;
  }
  const ema = (v, n) => smooth(v, n, 2 / (n + 1));
  const rma = (v, n) => smooth(v, n, 1 / n);
  function stdev(v, n, mean) {
    return v.map((_, i) => { if (mean[i] === null) return null; let s = 0;
      for (let j = i - n + 1; j <= i; j++) s += (v[j] - mean[i]) ** 2; return Math.sqrt(s / n); });
  }
  function rsi(c, n) {
    const g = [null], l = [null];
    for (let i = 1; i < c.length; i++) { const d = c[i] - c[i - 1]; g.push(Math.max(d, 0)); l.push(Math.max(-d, 0)); }
    const ag = rma(g, n), al = rma(l, n);
    return ag.map((x, i) => x === null || al[i] === null ? null : al[i] === 0 ? 100 : 100 - 100 / (1 + x / al[i]));
  }
  function atr(k, n) {
    const tr = k.map((b, i) => i === 0 ? b.high - b.low
      : Math.max(b.high - b.low, Math.abs(b.high - k[i - 1].close), Math.abs(b.low - k[i - 1].close)));
    return rma(tr, n);
  }
  const etDay = new Intl.DateTimeFormat('en-CA', { timeZone: 'America/New_York', year: 'numeric', month: '2-digit', day: '2-digit' });
  function vwap(k) {
    let day = null, pv = 0, vv = 0;
    return k.map((b) => {
      const d = etDay.format(new Date(b.time * 1000));
      if (d !== day) { day = d; pv = 0; vv = 0; }
      const vol = b.volume || 0; pv += ((b.high + b.low + b.close) / 3) * vol; vv += vol;
      return vv > 0 ? pv / vv : null;
    });
  }
  const pts = (k, vals) => k.map((b, i) => vals[i] === null || !Number.isFinite(vals[i]) ? { time: b.time } : { time: b.time, value: vals[i] });

  // ------------------------------------------------------------------ formatting
  const tzFor = () => (data && data.intraday ? C.tz : 'UTC');
  const fmt = (t, o) => new Date(t * 1000).toLocaleString('en-US', { timeZone: tzFor(), ...o });
  function tickFmt(t, type) {
    if (type === 0) return fmt(t, { year: 'numeric' });
    if (type === 1) return fmt(t, { month: 'short' });
    if (type === 2) return data.intraday ? fmt(t, { month: 'short', day: 'numeric' }) : fmt(t, { day: 'numeric' });
    return fmt(t, { hour: '2-digit', minute: '2-digit', hour12: false });
  }
  const crossFmt = (t) => data.intraday
    ? fmt(t, { weekday: 'short', month: 'short', day: 'numeric', hour: '2-digit', minute: '2-digit', hour12: false })
    : fmt(t, { weekday: 'short', month: 'short', day: 'numeric', year: 'numeric' });
  const num = (v, d) => v === null || v === undefined ? '—' : Number(v).toLocaleString('en-US', { minimumFractionDigits: d, maximumFractionDigits: d });
  const pxd = (v) => Math.abs(v) < 10 ? 4 : 2;
  const volFmt = (v) => v >= 1e9 ? (v / 1e9).toFixed(2) + 'B' : v >= 1e6 ? (v / 1e6).toFixed(2) + 'M' : v >= 1e3 ? (v / 1e3).toFixed(1) + 'K' : String(Math.round(v));

  // ------------------------------------------------------------------ fill markers
  // Drawn by a series primitive at the exact fill price on the fill's candle. Styles (Fills menu, saved in
  // this browser): 'h' small horizontal arrow whose tip touches the price, left of the candle pointing right
  // for buys and right of the candle pointing left for sells (never covers the body); 'v' small vertical
  // arrow pointing at the price (buys from below, sells from above); 'dot'; 'off' (hover legend only).
  // Several fills on one candle are stacked outward so they never overlap. Options are charted on the
  // underlying, so their fills are placed just beyond the candle's high/low instead of at a price.
  const LS_FILLS = 'tj.chart.fills.v1';
  const FILL_DEFAULT = { style: 'h', size: 'm', labels: false };
  const SIZES = { s: 9, m: 12, l: 16 };
  const FILL_COLOR = { buy: '#22f06a', sell: '#ff3b55', partial: '#ffa31a' };
  let fillOpts = (() => { try { return { ...FILL_DEFAULT, ...JSON.parse(localStorage.getItem(LS_FILLS) || '{}') }; } catch (e) { return { ...FILL_DEFAULT }; } })();
  const saveFills = () => localStorage.setItem(LS_FILLS, JSON.stringify(fillOpts));
  const isBuy = (m) => (m.side ? m.side === 'BUY' : m.shape === 'arrowUp');
  const fillColor = (m) => (m.kind === 'partial' ? FILL_COLOR.partial : isBuy(m) ? FILL_COLOR.buy : FILL_COLOR.sell);
  const shortLabel = (m) => `${isBuy(m) ? '+' : '−'}${Number(m.qty).toLocaleString('en-US', { maximumFractionDigits: 2 })} @${Number(m.price).toLocaleString('en-US', { minimumFractionDigits: 2, maximumFractionDigits: 4 })}`;

  class FillsPrimitive {
    constructor() { this.fills = []; this.view = { zOrder: () => 'top', renderer: () => ({ draw: (t) => this.draw(t) }) }; }
    attached(p) { this.chart = p.chart; this.series = p.series; this.request = p.requestUpdate; }
    detached() { this.chart = this.series = null; }
    setFills(f) { this.fills = f || []; if (this.request) this.request(); }
    updateAllViews() {}
    paneViews() { return [this.view]; }
    draw(target) {
      if (!this.chart || fillOpts.style === 'off' || !this.fills.length) return;
      const chart = this.chart, series = this.series, ts = chart.timeScale();
      const spacing = ts.options().barSpacing || 6;
      const L = SIZES[fillOpts.size] || 12, body = Math.max(1, spacing * 0.4);
      const bars = new Map((data.candles || []).map((b) => [b.time, b]));
      target.useMediaCoordinateSpace(({ context: ctx }) => {
        ctx.save(); ctx.lineJoin = 'round'; ctx.font = '600 10px ui-sans-serif, system-ui, sans-serif';
        const stack = new Map();  // time|side -> count, to offset fills on the same candle
        for (const m of this.fills) {
          const x = ts.timeToCoordinate(m.time); if (x === null) continue;
          const buy = isBuy(m);
          let price = m.price;
          if (!C.isStock) { const b = bars.get(m.time); if (!b) continue; price = buy ? b.low : b.high; }
          const y = series.priceToCoordinate(price); if (y === null) continue;
          const key = m.time + (buy ? 'b' : 's'), k = stack.get(key) || 0; stack.set(key, k + 1);
          const color = fillColor(m);
          ctx.fillStyle = color; ctx.strokeStyle = 'rgba(2,6,23,.95)'; ctx.lineWidth = 1.5;
          let lx, ly, align;
          if (fillOpts.style === 'h') {
            const dir = buy ? 1 : -1;                         // buys point right (sit left), sells point left
            const tip = x - dir * (body + 2 + k * (L + 3));
            const tail = tip - dir * L, hh = Math.max(3, L * 0.38), head = tip - dir * Math.min(L * 0.55, 7);
            ctx.beginPath();
            ctx.moveTo(tip, y); ctx.lineTo(head, y - hh); ctx.lineTo(head, y - hh * 0.42); ctx.lineTo(tail, y - hh * 0.42);
            ctx.lineTo(tail, y + hh * 0.42); ctx.lineTo(head, y + hh * 0.42); ctx.lineTo(head, y + hh); ctx.closePath();
            ctx.stroke(); ctx.fill();
            lx = tail - dir * 4; ly = y; align = buy ? 'right' : 'left';
          } else if (fillOpts.style === 'v') {
            const len = Math.round(L * 0.75), dir = buy ? 1 : -1;   // buys below pointing up
            const cx = x + (k ? (k % 2 ? 1 : -1) * Math.ceil(k / 2) * (len * 0.8 + 2) : 0);
            const tip = y, base = y + dir * len, hw = Math.max(3, len * 0.45);
            ctx.beginPath(); ctx.moveTo(cx, tip); ctx.lineTo(cx - hw, base); ctx.lineTo(cx + hw, base); ctx.closePath();
            ctx.stroke(); ctx.fill();
            lx = cx; ly = base + dir * 8; align = 'center';
          } else {
            const r = Math.max(2.5, L * 0.28), cx = x + (buy ? -1 : 1) * k * (r * 2 + 1);
            ctx.beginPath(); ctx.arc(cx, y, r, 0, Math.PI * 2); ctx.stroke(); ctx.fill();
            lx = cx + (buy ? -1 : 1) * (r + 4); ly = y; align = buy ? 'right' : 'left';
          }
          if (fillOpts.labels && spacing >= 10) {   // only when zoomed in enough to stay readable
            const txt = shortLabel(m), w = ctx.measureText(txt).width + 6;
            const bx = align === 'right' ? lx - w : align === 'left' ? lx : lx - w / 2;
            ctx.fillStyle = 'rgba(2,6,23,.85)'; ctx.fillRect(bx, ly - 7, w, 14);
            ctx.fillStyle = color; ctx.textBaseline = 'middle'; ctx.textAlign = 'left'; ctx.fillText(txt, bx + 3, ly);
          }
        }
        ctx.restore();
      });
    }
  }
  let fillsPrim = null;

  function renderFillsMenu() {
    const m = $('fills-menu'); if (!m) return;
    const seg = (name, opts, cur) => `<div class="flex rounded-lg border border-slate-700 overflow-hidden" role="group" aria-label="${name}">` +
      opts.map(([v, lab]) => `<button type="button" data-fo="${name}" data-v="${v}" aria-pressed="${v === cur}" class="flex-1 px-2 py-1 ${v === cur ? 'bg-indigo-600 text-white' : 'hover:bg-slate-800'}">${lab}</button>`).join('') + '</div>';
    m.innerHTML = `<div class="card-h mb-1">Fill markers</div>
      ${seg('style', [['h', '→ Horizontal'], ['v', '↑ Vertical'], ['dot', '● Dot'], ['off', 'Off']], fillOpts.style)}
      <div class="card-h mt-3 mb-1">Size</div>${seg('size', [['s', 'S'], ['m', 'M'], ['l', 'L']], fillOpts.size)}
      <label class="flex items-center gap-2 mt-3"><input type="checkbox" data-fo="labels" ${fillOpts.labels ? 'checked' : ''}> Show labels (+qty @price) when zoomed in</label>
      <div class="mt-3 flex flex-wrap gap-x-3 gap-y-1 text-[11px]"><span><b style="color:${FILL_COLOR.buy}">■</b> buy</span><span><b style="color:${FILL_COLOR.sell}">■</b> sell / final exit</span><span><b style="color:${FILL_COLOR.partial}">■</b> partial exit</span></div>
      <p class="text-[10px] text-slate-500 mt-2">The tip touches the exact fill price. Hover a candle for every fill's details. Saved in this browser.</p>`;
    m.querySelectorAll('button[data-fo]').forEach((b) => b.addEventListener('click', (e) => {
      e.stopPropagation(); fillOpts[b.dataset.fo] = b.dataset.v; saveFills(); renderFillsMenu(); if (fillsPrim) fillsPrim.setFills(data.markers);
    }));
    const lab = m.querySelector('input[data-fo=labels]');
    lab.addEventListener('change', () => { fillOpts.labels = lab.checked; saveFills(); if (fillsPrim) fillsPrim.setFills(data.markers); });
  }
  function fillsAt(t) { return (data && data.markers || []).filter((m) => m.time === t); }

  // ------------------------------------------------------------------ chart build
  function lineOpts(color, extra) {
    return { color, lineWidth: 1.5, priceLineVisible: false, lastValueVisible: false, crosshairMarkerVisible: false, ...extra };
  }
  function build() {
    const keepRange = chart ? chart.timeScale().getVisibleLogicalRange() : null;
    if (chart) { chart.remove(); chart = null; }
    legendSeries = [];
    const k = data.candles, close = k.map((b) => b.close);
    chart = L.createChart(el, {
      autoSize: true,
      layout: { background: { color: '#0f1520' }, textColor: '#94a3b8', fontSize: 11,
                panes: { separatorColor: '#1f2937', separatorHoverColor: 'rgba(99,102,241,.25)', enableResize: true } },
      grid: { vertLines: { color: 'rgba(51,65,85,.22)' }, horzLines: { color: 'rgba(51,65,85,.22)' } },
      crosshair: { mode: L.CrosshairMode.Normal },
      rightPriceScale: { borderColor: '#334155', scaleMargins: { top: 0.08, bottom: 0.06 } },
      timeScale: { borderColor: '#334155', timeVisible: data.intraday, secondsVisible: false, rightOffset: 6,
                   tickMarkFormatter: tickFmt },
      localization: { locale: 'en-US', timeFormatter: crossFmt },
    });
    candleSeries = chart.addSeries(L.CandlestickSeries, { upColor: UP, downColor: DOWN, borderVisible: false,
      wickUpColor: UP, wickDownColor: DOWN, priceLineVisible: false });
    candleSeries.setData(k.map(({ time, open, high, low, close }) => ({ time, open, high, low, close })));
    if (C.isStock) {
      candleSeries.createPriceLine({ price: C.entry, color: '#818cf8', lineWidth: 1, lineStyle: 2, title: 'avg entry' });
      if (C.exit) candleSeries.createPriceLine({ price: C.exit, color: '#f59e0b', lineWidth: 1, lineStyle: 2, title: 'avg exit' });
    }
    fillsPrim = new FillsPrimitive();
    candleSeries.attachPrimitive(fillsPrim);
    fillsPrim.setFills(data.markers || []);

    let pane = 1;
    const sub = [];
    for (const ind of inds) {
      const spec = CATALOG[ind.type];
      if (spec.intradayOnly && !data.intraday) continue;
      if (spec.overlay) {
        if (ind.type === 'SMA' || ind.type === 'EMA') {
          const s = chart.addSeries(L.LineSeries, lineOpts(ind.color));
          s.setData(pts(k, (ind.type === 'SMA' ? sma : ema)(close, ind.period)));
          legendSeries.push([label(ind), ind.color, s, 0]);
        } else if (ind.type === 'VWAP') {
          const s = chart.addSeries(L.LineSeries, lineOpts(ind.color, { lineWidth: 2 }));
          s.setData(pts(k, vwap(k)));
          legendSeries.push(['VWAP', ind.color, s, 0]);
        } else if (ind.type === 'BB') {
          const mid = sma(close, ind.period), sd = stdev(close, ind.period, mid);
          const up = mid.map((m, i) => m === null ? null : m + ind.mult * sd[i]);
          const lo = mid.map((m, i) => m === null ? null : m - ind.mult * sd[i]);
          const sm = chart.addSeries(L.LineSeries, lineOpts(ind.color, { lineWidth: 1, lineStyle: 2 }));
          sm.setData(pts(k, mid));
          for (const arr of [up, lo]) chart.addSeries(L.LineSeries, lineOpts(ind.color, { lineWidth: 1 })).setData(pts(k, arr));
          legendSeries.push([label(ind), ind.color, sm, 0]);
        }
      } else {
        sub.push(ind);
      }
    }
    if (showVol) {
      const v = chart.addSeries(L.HistogramSeries, { priceFormat: { type: 'volume' }, priceLineVisible: false,
        lastValueVisible: false }, pane);
      v.setData(k.map((b) => ({ time: b.time, value: b.volume || 0, color: b.close >= b.open ? 'rgba(16,185,129,.45)' : 'rgba(244,63,94,.45)' })));
      const ma = chart.addSeries(L.LineSeries, lineOpts('#f59e0b', { lineWidth: 1.5 }), pane);
      ma.setData(pts(k, sma(k.map((b) => b.volume || 0), 20)));
      legendSeries.push(['Vol', '#94a3b8', v, pane, volFmt], ['Vol MA 20', '#f59e0b', ma, pane, volFmt]);
      pane++;
    }
    for (const ind of sub) {
      if (ind.type === 'RSI') {
        const s = chart.addSeries(L.LineSeries, lineOpts(ind.color, { lastValueVisible: true }), pane);
        s.setData(pts(k, rsi(close, ind.period)));
        s.createPriceLine({ price: 70, color: 'rgba(148,163,184,.45)', lineWidth: 1, lineStyle: 2, axisLabelVisible: false });
        s.createPriceLine({ price: 30, color: 'rgba(148,163,184,.45)', lineWidth: 1, lineStyle: 2, axisLabelVisible: false });
        legendSeries.push([label(ind), ind.color, s, pane]);
      } else if (ind.type === 'MACD') {
        const slow = ema(close, ind.slow), m = ema(close, ind.fast).map((f, i) => f === null || slow[i] === null ? null : f - slow[i]);
        const sig = ema(m, ind.signal);
        const h = chart.addSeries(L.HistogramSeries, { priceLineVisible: false, lastValueVisible: false }, pane);
        h.setData(k.map((b, i) => m[i] === null || sig[i] === null ? { time: b.time }
          : { time: b.time, value: m[i] - sig[i], color: m[i] - sig[i] >= 0 ? 'rgba(16,185,129,.55)' : 'rgba(244,63,94,.55)' }));
        const ml = chart.addSeries(L.LineSeries, lineOpts(ind.color), pane); ml.setData(pts(k, m));
        const sl = chart.addSeries(L.LineSeries, lineOpts('#f59e0b'), pane); sl.setData(pts(k, sig));
        legendSeries.push([label(ind), ind.color, ml, pane], ['signal', '#f59e0b', sl, pane]);
      } else if (ind.type === 'ATR') {
        const s = chart.addSeries(L.LineSeries, lineOpts(ind.color, { lastValueVisible: true }), pane);
        s.setData(pts(k, atr(k, ind.period)));
        legendSeries.push([label(ind), ind.color, s, pane]);
      }
      pane++;
    }
    const panes = chart.panes();
    panes.forEach((p, i) => p.setStretchFactor(i === 0 ? (panes.length > 3 ? 3 : 4) : 1));

    // visible range: keep the user's zoom on indicator changes, otherwise frame entry -> exit
    const ts = chart.timeScale();
    if (keepRange) ts.setVisibleLogicalRange(keepRange);
    else if (data.focus) {
      const times = k.map((b) => b.time);
      const a = times.indexOf(data.focus.from), b = times.indexOf(data.focus.to);
      const [fb, fa] = data.focus_bars || [50, 25];
      if (a >= 0 && b >= 0) ts.setVisibleLogicalRange({ from: Math.max(0, a - fb), to: Math.min(times.length - 1 + 6, b + fa) });
      else ts.fitContent();
    } else ts.fitContent();
    chart.subscribeCrosshairMove(legend);
    legend(null);
  }

  function legend(param) {
    const box = $('chart-legend'); if (!box || !data) return;
    const k = data.candles;
    let bar = param && param.seriesData ? param.seriesData.get(candleSeries) : null;
    let idx = bar ? k.findIndex((b) => b.time === param.time) : k.length - 1;
    if (!bar) bar = k[k.length - 1];
    const kb = k[idx] || bar, prev = k[idx - 1];
    const chg = prev ? kb.close - prev.close : 0, pct = prev ? (chg / prev.close) * 100 : 0;
    const d = pxd(kb.close), col = chg >= 0 ? 'text-emerald-300' : 'text-rose-300';
    let html = `<div class="flex flex-wrap gap-x-2"><span class="text-slate-200 font-semibold">${C.symbol}</span><span class="text-slate-500">${data.tf}</span>` +
      `<span>O <b class="${col} font-medium">${num(kb.open, d)}</b></span><span>H <b class="${col} font-medium">${num(kb.high, d)}</b></span>` +
      `<span>L <b class="${col} font-medium">${num(kb.low, d)}</b></span><span>C <b class="${col} font-medium">${num(kb.close, d)}</b></span>` +
      `<span class="${col}">${chg >= 0 ? '+' : ''}${num(chg, d)} (${chg >= 0 ? '+' : ''}${num(pct, 2)}%)</span>` +
      `<span>V <b class="text-slate-300 font-medium">${volFmt(kb.volume || 0)}</b></span></div>`;
    const rows = legendSeries.filter((x) => x[3] === 0).map((x) => legendItem(x, param));
    if (rows.length) html += `<div class="flex flex-wrap gap-x-3">${rows.join('')}</div>`;
    const subs = legendSeries.filter((x) => x[3] > 0).map((x) => legendItem(x, param));
    if (subs.length) html += `<div class="flex flex-wrap gap-x-3 text-slate-500">${subs.join('')}</div>`;
    const fills = fillsAt(kb.time);
    if (fills.length) {
      html += `<div class="flex flex-col gap-0.5 mt-0.5" data-fill-legend>` + fills.map((m) => {
        const buy = isBuy(m), c = fillColor(m);
        return `<span><span style="color:${c}" class="font-semibold">${buy ? 'BUY' : 'SELL'} ${num(m.qty, m.qty % 1 ? 2 : 0)} @ ${Number(m.price).toLocaleString('en-US', { minimumFractionDigits: 2, maximumFractionDigits: 4 })}</span>` +
          ` <span class="text-slate-300">${m.kind || ''}</span> <span class="text-slate-500">${m.at || ''}${m.how === 'price' ? ' · bar estimated from price' : ''}</span></span>`;
      }).join('') + `</div>`;
    }
    box.innerHTML = html;
  }
  function legendItem([name, color, s, , f], param) {
    let v = param && param.seriesData ? param.seriesData.get(s) : null;
    if (!v) { const all = s.data(); for (let i = all.length - 1; i >= 0; i--) if (all[i].value !== undefined) { v = all[i]; break; } }
    const val = v && v.value !== undefined ? (f ? f(v.value) : num(v.value, pxd(v.value))) : '—';
    return `<span><span style="color:${color}">${name}</span> <span class="text-slate-300">${val}</span></span>`;
  }

  // ------------------------------------------------------------------ toolbar
  function renderTf() {
    const bar = $('tf-bar'); bar.innerHTML = '';
    for (const o of data.timeframes) {
      const b = document.createElement('button');
      b.type = 'button'; b.textContent = o.tf; b.dataset.tf = o.tf;
      b.className = 'px-2.5 py-1 rounded-md text-xs font-medium ' + (o.tf === data.tf ? 'bg-indigo-600 text-white'
        : o.available ? 'text-slate-300 hover:bg-slate-800' : 'text-slate-600 cursor-not-allowed');
      b.title = o.available ? `${o.label}${o.tf === data.default_tf ? ' (default for this trade)' : ''}` : `${o.label}: ${o.reason}`;
      b.disabled = !o.available;
      b.addEventListener('click', () => { localStorage.setItem(LS_TF, o.tf); load(o.tf); });
      bar.appendChild(b);
    }
    const notes = (data.notes || []).slice();
    const off = data.timeframes.filter((o) => !o.available);
    if (off.length) notes.push(`${off.map((o) => o.tf).join(', ')} unavailable: ${off[0].reason}.`);
    if (inds.some((i) => i.type === 'VWAP') && !data.intraday) notes.push('VWAP shows on intraday timeframes only.');
    const n = $('chart-note'); n.textContent = notes.join(' '); n.classList.toggle('hidden', !notes.length);
    $('chart-src').textContent = data.provider_label ? `${data.provider_label}` : '';
  }

  function renderChips() {
    const box = $('ind-chips'); box.innerHTML = '';
    const mk = (text, color, onRemove, title) => {
      const s = document.createElement('span');
      s.className = 'inline-flex items-center gap-1 rounded-md border border-slate-700/70 bg-slate-900/70 px-1.5 py-0.5 text-[11px] text-slate-300';
      s.innerHTML = `<span class="h-2 w-2 rounded-full" style="background:${color}"></span>${text}`;
      const x = document.createElement('button'); x.type = 'button'; x.className = 'text-slate-500 hover:text-white ml-0.5'; x.textContent = '×';
      x.title = title; x.addEventListener('click', onRemove); s.appendChild(x); box.appendChild(s);
    };
    inds.forEach((ind, i) => mk(label(ind), ind.color, () => { inds.splice(i, 1); saveInd(); refresh(); }, 'Remove ' + label(ind)));
    if (showVol) mk('Vol + MA 20', '#f59e0b', () => { showVol = false; localStorage.setItem(LS_VOL, '0'); refresh(); }, 'Hide volume');
  }

  function renderMenu() {
    const m = $('ind-menu');
    const active = inds.map((ind, i) => {
      const spec = CATALOG[ind.type];
      const fields = Object.keys(spec.defaults).map((p) =>
        `<label class="text-[10px] text-slate-500">${p}<input data-i="${i}" data-p="${p}" type="number" min="1" step="${p === 'mult' ? '0.5' : '1'}" value="${ind[p]}" class="inp ml-1 w-14 py-0.5 px-1 text-xs"></label>`).join(' ');
      return `<div class="flex items-center gap-2 py-1"><input data-c="${i}" type="color" value="${ind.color}" class="h-5 w-5 bg-transparent border-0 p-0 cursor-pointer">` +
        `<span class="text-xs text-slate-200 w-12">${ind.type}</span><span class="flex gap-1">${fields}</span>` +
        `<button data-rm="${i}" class="ml-auto text-slate-500 hover:text-rose-300 text-sm" title="Remove">×</button></div>`;
    }).join('') || '<div class="text-xs text-slate-500 py-1">No indicators yet.</div>';
    const presets = PRESETS.map(([t, p]) => `<button data-add="${t}" data-period="${p}" class="pill bg-slate-800 text-slate-300 hover:bg-indigo-500/30">${t} ${p}</button>`).join(' ');
    const others = ['VWAP', 'BB', 'RSI', 'MACD', 'ATR'].map((t) => `<button data-add="${t}" class="pill bg-slate-800 text-slate-300 hover:bg-indigo-500/30">${CATALOG[t].label}${CATALOG[t].defaults.period ? ' ' + CATALOG[t].defaults.period : ''}</button>`).join(' ');
    m.innerHTML = `<div class="card-h mb-1">Active</div>${active}
      <div class="border-t border-slate-800 my-2"></div>
      <div class="card-h mb-1">Moving averages</div><div class="flex flex-wrap gap-1">${presets}</div>
      <div class="flex items-center gap-1 mt-2"><select id="ma-type" class="inp py-0.5 text-xs"><option>EMA</option><option>SMA</option></select>
        <input id="ma-period" type="number" min="1" value="65" class="inp w-16 py-0.5 text-xs"><button id="ma-add" class="btn btn-s py-0.5 text-xs">Add</button></div>
      <div class="card-h mt-3 mb-1">Other</div><div class="flex flex-wrap gap-1">${others}</div>
      <label class="flex items-center gap-2 mt-3 text-xs text-slate-300"><input id="vol-toggle" type="checkbox" ${showVol ? 'checked' : ''}> Volume + Volume MA(20)</label>
      <div class="flex justify-between mt-3"><button id="ind-reset" class="text-[11px] text-slate-500 hover:text-slate-300">Reset to default</button>
        <span class="text-[10px] text-slate-600">Saved in this browser</span></div>`;
    m.querySelectorAll('[data-add]').forEach((b) => b.addEventListener('click', () => {
      const t = b.dataset.add, ind = { type: t, ...CATALOG[t].defaults, color: nextColor() };
      if (b.dataset.period) ind.period = posInt(b.dataset.period, ind.period);
      if (t === 'VWAP') ind.color = '#e2e8f0';
      inds.push(ind); saveInd(); refresh();
    }));
    $('ma-add').addEventListener('click', () => {
      inds.push({ type: $('ma-type').value, period: posInt($('ma-period').value, 20), color: nextColor() }); saveInd(); refresh();
    });
    m.querySelectorAll('[data-rm]').forEach((b) => b.addEventListener('click', () => { inds.splice(+b.dataset.rm, 1); saveInd(); refresh(); }));
    m.querySelectorAll('input[data-p]').forEach((inp) => inp.addEventListener('change', () => {
      const ind = inds[+inp.dataset.i], p = inp.dataset.p;
      ind[p] = p === 'mult' ? (parseFloat(inp.value) > 0 ? parseFloat(inp.value) : 2) : posInt(inp.value, CATALOG[ind.type].defaults[p]);
      saveInd(); refresh();
    }));
    m.querySelectorAll('input[data-c]').forEach((inp) => inp.addEventListener('input', () => { inds[+inp.dataset.c].color = inp.value; saveInd(); refresh({ menu: false }); }));
    $('vol-toggle').addEventListener('change', (e) => { showVol = e.target.checked; localStorage.setItem(LS_VOL, showVol ? '1' : '0'); refresh(); });
    $('ind-reset').addEventListener('click', () => { inds = DEFAULT_IND.map((x) => ({ ...x })); showVol = true; saveInd(); localStorage.setItem(LS_VOL, '1'); refresh(); });
  }

  function refresh(opts) {
    if (data && data.candles && data.candles.length) build();
    renderChips();
    if (data) renderTf();
    if (!(opts && opts.menu === false) && !menu.classList.contains('hidden')) renderMenu();
  }

  // ------------------------------------------------------------------ load
  let seq = 0;
  async function load(tf) {
    const my = ++seq;
    ph.classList.remove('hidden');
    ph.innerHTML = '<div class="text-slate-500 text-sm">Loading chart…</div>';
    let d;
    try {
      const r = await fetch(`/trades/${C.tradeId}/chart.json` + (tf ? `?tf=${encodeURIComponent(tf)}` : ''));
      d = await r.json();
    } catch (e) { d = { candles: [], timeframes: data ? data.timeframes : [], notes: ['Could not load price data.'] }; }
    if (my !== seq) return;
    const sameTf = data && data.tf === d.tf;
    data = d;
    if (!sameTf && chart) { chart.remove(); chart = null; }
    if (data.timeframes) renderTf();
    if (!data.candles || !data.candles.length) {
      if (chart) { chart.remove(); chart = null; }
      $('chart-legend').innerHTML = '';
      ph.innerHTML = '<div class="text-center px-6"><div class="text-3xl mb-2">📈</div><div class="text-slate-300">No price data available for this timeframe.</div>' +
        '<div class="text-xs mt-1 text-slate-500">Try another timeframe. Charts use Yahoo Finance (unofficial), Polygon (POLYGON_API_KEY) or Schwab market data.</div></div>';
      return;
    }
    ph.classList.add('hidden');
    build();
    renderChips();
    if (data.mfe !== null && data.mfe !== undefined && $('mfe')) {
      const f = (v) => (v < 0 ? '-$' : '$') + Math.abs(v).toLocaleString(undefined, { minimumFractionDigits: 2, maximumFractionDigits: 2 });
      $('mfe').textContent = f(data.mfe) + ' / ' + f(data.mae);
    }
    if (data.excursion && data.excursion.note && $('mfe')) $('mfe').parentElement.title = data.excursion.note;
  }

  // fills menu
  const fbtn = $('fills-btn'), fmenu = $('fills-menu');
  if (fbtn) {
    fbtn.addEventListener('click', (e) => { e.stopPropagation(); if (fmenu.classList.toggle('hidden') === false) renderFillsMenu(); });
    document.addEventListener('click', (e) => { if (e.target.isConnected && !fmenu.contains(e.target) && e.target !== fbtn) fmenu.classList.add('hidden'); });
    document.addEventListener('keydown', (e) => { if (e.key === 'Escape') fmenu.classList.add('hidden'); });
  }

  // menu open/close
  const btn = $('ind-btn'), menu = $('ind-menu');
  btn.addEventListener('click', (e) => { e.stopPropagation(); const open = menu.classList.toggle('hidden') === false; if (open) renderMenu(); });
  document.addEventListener('click', (e) => { if (e.target.isConnected && !menu.contains(e.target) && e.target !== btn) menu.classList.add('hidden'); });
  document.addEventListener('keydown', (e) => { if (e.key === 'Escape') menu.classList.add('hidden'); });

  renderChips();
  load(localStorage.getItem(LS_TF) || null);
  window.TJ_CHART_API = { load, get data() { return data; }, get indicators() { return inds; },
    get fills() { return { ...fillOpts }; }, setFills(o) { fillOpts = { ...fillOpts, ...o }; saveFills(); if (fillsPrim) fillsPrim.setFills(data.markers); },
    get chart() { return chart; } };
})();
