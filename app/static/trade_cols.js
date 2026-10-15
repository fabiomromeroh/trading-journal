/* Trades-list column picker: tick columns on/off, drag headers to reorder, reset. Saved server-side
   (POST /trades/columns); the table region is then re-fetched with htmx (same URL, so sort/filters stay). */
(function () {
  if (window.__tradeCols) return; window.__tradeCols = true;
  const KEY = 'tj.trades.columns';
  function refresh(reopen) {
    if (reopen) sessionStorage.setItem('tj.colpicker.open', '1');
    if (window.htmx) htmx.ajax('GET', location.pathname + location.search, { target: '#trades-results', select: '#trades-results', swap: 'outerHTML' });
    else location.reload();
  }
  function save(cols, reopen) {
    fetch('/trades/columns', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ columns: cols }), credentials: 'same-origin' })
      .then(r => { if (!r.ok) throw new Error(r.status); try { localStorage.removeItem(KEY); } catch (e) {} refresh(reopen); })
      .catch(() => { try { localStorage.setItem(KEY, JSON.stringify(cols)); } catch (e) {} });
  }
  function current() { const row = document.querySelector('[data-colrow]'); return row ? row.dataset.colrow.split(',').filter(Boolean) : []; }
  document.addEventListener('change', e => {
    const box = e.target.closest && e.target.closest('[data-colpick]'); if (!box) return;
    let cols = current(); const id = box.dataset.colpick;
    if (box.checked) { if (!cols.includes(id)) cols.push(id); } else { cols = cols.filter(c => c !== id); }
    if (!cols.length) { box.checked = true; return; }
    save(cols, true);
  });
  document.addEventListener('click', e => {
    const b = e.target.closest && e.target.closest('[data-colreset]'); if (!b) return;
    fetch('/trades/columns/reset', { method: 'POST', credentials: 'same-origin' }).then(() => { try { localStorage.removeItem(KEY); } catch (er) {} refresh(false); });
  });
  function init() {
    const row = document.querySelector('[data-colrow]');
    if (!row || row.dataset.sortableOn) return;
    row.dataset.sortableOn = '1';
    if (sessionStorage.getItem('tj.colpicker.open')) { sessionStorage.removeItem('tj.colpicker.open'); const d = document.getElementById('col-picker'); if (d) d.open = true; }
    try { const pending = JSON.parse(localStorage.getItem(KEY) || 'null'); if (pending && pending.length) { localStorage.removeItem(KEY); save(pending, false); return; } } catch (e) {}
    if (!window.Sortable) return;
    Sortable.create(row, { draggable: 'th[data-col]', animation: 150, ghostClass: 'opacity-40',
      onEnd: () => { const cols = Array.from(row.querySelectorAll('th[data-col]')).map(th => th.dataset.col); if (cols.join(',') !== row.dataset.colrow) save(cols, false); } });
  }
  document.addEventListener('DOMContentLoaded', init);
  document.addEventListener('htmx:afterSettle', init);
  init();
})();
