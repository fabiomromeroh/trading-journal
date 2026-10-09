/* Dashboard P&L calendar (one month, navigable) and daily realized P&L bars (navigable window).
   Data: C.days = [{d: 'YYYY-MM-DD', net, gross, fees, trades, exits, closed, partial}], C.focus, C.today. */
(function () {
  const MONTHS = ['January','February','March','April','May','June','July','August','September','October','November','December'];
  const MON3 = MONTHS.map(m => m.slice(0, 3));
  const iso = dt => dt.toISOString().slice(0, 10);
  const parse = s => { const [y, m, d] = s.split('-').map(Number); return new Date(Date.UTC(y, m - 1, d)); };
  const addDays = (dt, n) => new Date(dt.getTime() + n * 86400000);
  const money = (v, dec) => { const a = Math.abs(v); const d = dec ?? (a < 10 ? 2 : 0);
    return (v < 0 ? '-$' : '$') + a.toLocaleString(undefined, { minimumFractionDigits: d, maximumFractionDigits: d }); };
  const sgn = v => (v > 0 ? '+' : '') + money(v);
  const cls = v => v > 0 ? 'text-emerald-400' : v < 0 ? 'text-rose-400' : 'text-slate-400';
  // outcome of a day's net vs the break-even range [lo, hi] (Settings): 1 win, -1 loss, 0 BE
  const oc = (v, C) => { const be = (C && C.be) || [0, 0]; return v > be[1] + 1e-9 ? 1 : v < be[0] - 1e-9 ? -1 : 0; };
  const dayUrl = (d, C) => `/trades?realized_day=${d}` + (C && C.account ? `&account=${C.account}` : '');
  window.PnlDayUrl = dayUrl;
  const esc = s => String(s).replace(/[&<>"]/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' }[c]));
  function dayTitle(r) {
    const parts = [`${r.d}: realized ${money(r.net, 2)}`, `gross ${money(r.gross, 2)}, fees ${money(r.fees, 2)}`];
    if (r.exits) parts.push(`${r.exits} exit${r.exits > 1 ? 's' : ''} in ${r.trades} trade${r.trades > 1 ? 's' : ''}` +
      (r.partial ? ` (${r.partial} partial)` : '') + (r.closed ? `, ${r.closed} trade${r.closed > 1 ? 's' : ''} fully closed` : ''));
    else parts.push('fees only');
    return parts.join(' · ');
  }

  window.PnlCalendar = function (root, C) {
    if (!root) return;
    const by = Object.fromEntries(C.days.map(r => [r.d, r]));
    const first = C.days.length ? parse(C.days[0].d) : parse(C.today);
    const today = parse(C.today);
    let cur = parse(C.focus); cur = new Date(Date.UTC(cur.getUTCFullYear(), cur.getUTCMonth(), 1));
    const $ = k => root.querySelector(`[data-cal="${k}"]`);
    function render() {
      const y = cur.getUTCFullYear(), m = cur.getUTCMonth();
      $('title').textContent = `${MONTHS[m]} ${y}`;
      const start = addDays(cur, -((cur.getUTCDay() + 6) % 7));  // Monday on/before the 1st
      const monthDays = C.days.filter(r => r.d.startsWith(`${y}-${String(m + 1).padStart(2, '0')}`));
      const maxAbs = Math.max(1, ...monthDays.map(r => Math.abs(r.net)));
      const tot = monthDays.reduce((a, r) => a + r.net, 0);
      const g = monthDays.filter(r => oc(r.net, C) > 0).length, l = monthDays.filter(r => oc(r.net, C) < 0).length;
      $('total').innerHTML = monthDays.length
        ? `<span class="${cls(tot)} font-semibold">${sgn(tot)}</span> <span class="text-slate-500">· ${monthDays.length} day${monthDays.length > 1 ? 's' : ''} · <span class="text-emerald-400">${g} green</span> / <span class="text-rose-400">${l} red</span></span>`
        : '<span class="text-slate-500">No realized P&L this month</span>';
      let html = '<div class="grid grid-cols-8 gap-1 text-[10px] text-slate-500 mb-1">' +
        ['Mon','Tue','Wed','Thu','Fri','Sat','Sun','Week'].map(d => `<div class="text-center">${d}</div>`).join('') + '</div>';
      for (let w = 0; w < 6; w++) {
        const wkStart = addDays(start, w * 7);
        if (w > 0 && wkStart.getUTCMonth() !== m) break;
        let wkNet = 0, wkTr = 0, row = '';
        for (let i = 0; i < 7; i++) {
          const dt = addDays(wkStart, i), d = iso(dt), r = by[d];
          if (dt.getUTCMonth() !== m) { row += '<div class="cal-cell"></div>'; continue; }
          const isToday = d === C.today ? ' ring-1 ring-indigo-400' : '';
          if (!r) { row += `<div class="cal-cell bg-slate-800/30 text-slate-600${isToday}">${dt.getUTCDate()}</div>`; continue; }
          wkNet += r.net; wkTr += r.trades;
          const op = (0.18 + Math.min(1, Math.abs(r.net) / maxAbs) * 0.6).toFixed(2);
          const o = oc(r.net, C);
          const bg = o > 0 ? `rgba(16,185,129,${op})` : o < 0 ? `rgba(244,63,94,${op})` : 'rgba(100,116,139,.35)';
          row += `<a href="${dayUrl(d, C)}" class="cal-cell block${isToday}" style="background:${bg}" title="${esc(dayTitle(r))}" aria-label="${esc(dayTitle(r))}">` +
            `<div class="text-slate-200/80">${dt.getUTCDate()}</div><div class="font-semibold text-white">${money(r.net)}</div>` +
            `<div class="text-white/60">${r.exits ? r.trades + ' tr' + (r.partial ? ' · ' + r.partial + 'p' : '') : 'fees'}</div></a>`;
        }
        row += `<div class="cal-cell bg-slate-800/50 text-center"><div class="text-slate-500">wk</div><div class="${cls(wkNet)} font-semibold">${wkTr || wkNet ? money(wkNet) : ''}</div></div>`;
        html += `<div class="grid grid-cols-8 gap-1 mb-1">${row}</div>`;
      }
      $('grid').innerHTML = html;
      $('prev').disabled = cur <= new Date(Date.UTC(first.getUTCFullYear(), first.getUTCMonth(), 1));
      $('next').disabled = cur >= new Date(Date.UTC(today.getUTCFullYear(), today.getUTCMonth(), 1));
    }
    const shift = n => { cur = new Date(Date.UTC(cur.getUTCFullYear(), cur.getUTCMonth() + n, 1)); render(); };
    $('prev').addEventListener('click', () => shift(-1));
    $('next').addEventListener('click', () => shift(1));
    $('today').addEventListener('click', () => { cur = new Date(Date.UTC(today.getUTCFullYear(), today.getUTCMonth(), 1)); render(); });
    root.addEventListener('keydown', e => { if (e.key === 'ArrowLeft' && e.target === root) shift(-1); if (e.key === 'ArrowRight' && e.target === root) shift(1); });
    render();
    return { shift, get month() { return iso(cur).slice(0, 7); } };
  };

  window.DailyPnl = function (root, C, opts) {
    if (!root) return;
    const canvas = root.querySelector('canvas');
    const $ = k => root.querySelector(`[data-dw="${k}"]`);
    const today = parse(C.today);
    let end = parse(C.focus), size = Number($('size').value) || 90, chart;
    function render() {
      const start = addDays(end, -(size - 1)), s = iso(start), e = iso(end);
      const rows = C.days.filter(r => r.d >= s && r.d <= e);
      const fmt = dt => `${MON3[dt.getUTCMonth()]} ${dt.getUTCDate()}`;
      $('label').textContent = `${fmt(start)} – ${fmt(end)}, ${end.getUTCFullYear()}`;
      const tot = rows.reduce((a, r) => a + r.net, 0);
      const g = rows.filter(r => oc(r.net, C) > 0).length, l = rows.filter(r => oc(r.net, C) < 0).length;
      $('sum').innerHTML = rows.length ? `Window: <span class="${cls(tot)}">${sgn(tot)}</span> · ${rows.length} days · ${g} green / ${l} red` : 'No realized P&L in this window';
      const data = { labels: rows.map(r => r.d), datasets: [{ data: rows.map(r => r.net), borderRadius: 3,
        backgroundColor: rows.map(r => { const o = oc(r.net, C); return o > 0 ? 'rgba(16,185,129,.75)' : o < 0 ? 'rgba(244,63,94,.75)' : 'rgba(148,163,184,.6)'; }) }] };
      if (chart) { chart.data = data; chart.update('none'); }
      else chart = new Chart(canvas, { type: 'bar', data, options: { responsive: true, maintainAspectRatio: false, animation: false,
        interaction: { mode: 'index', intersect: false },
        onClick: (e, els) => { const r = els.length && chart._rows[els[0].index]; if (r) location.href = dayUrl(r.d, C); },
        onHover: (e, els) => { e.native.target.style.cursor = els.length ? 'pointer' : 'default'; },
        plugins: { legend: { display: false }, tooltip: { callbacks: {
          label: c => `Realized ${money(c.raw, 2)}`,
          afterLabel: c => { const r = rows[c.dataIndex] || chart._rows[c.dataIndex]; return r ? dayTitle(r).split(' · ').slice(1).join('\n') : ''; } } } },
        scales: { y: { ticks: { callback: v => money(v, 0) } }, x: { ticks: { autoSkip: true, maxRotation: 0, callback: function (v) { return String(this.getLabelForValue(v)).slice(5); } } } } } });
      chart._rows = rows;
      chart.options.plugins.tooltip.callbacks.afterLabel = c => { const r = chart._rows[c.dataIndex]; return r ? dayTitle(r).split(' · ').slice(1).join('\n') : ''; };
      $('next').disabled = end >= today;
      $('prev').disabled = C.days.length ? s <= C.days[0].d : true;
    }
    $('prev').addEventListener('click', () => { end = addDays(end, -size); render(); });
    $('next').addEventListener('click', () => { end = addDays(end, size); if (end > today) end = today; render(); });
    $('today').addEventListener('click', () => { end = today; render(); });
    $('size').addEventListener('change', () => { size = Number($('size').value); render(); });
    render();
    return { get window() { return [iso(addDays(end, -(size - 1))), iso(end)]; } };
  };
})();
