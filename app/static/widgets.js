/* Add / remove / reorder widgets in a grid; the layout is saved server-side (/layout/<page>).
   Markup: grid[data-page] > [data-wid][data-title][data-group] (hidden attr = not in layout),
   each with a .we-tools bar; toolbar buttons [data-we=edit|save|cancel|reset] and a [data-we=catalog]
   container. Drag & drop via SortableJS (touch friendly), plus up/down buttons. */
(function () {
  const css = `.we-tools{display:none}
  body.we-editing .we-tools{display:flex}
  body.we-editing [data-wid]{outline:1px dashed rgba(129,140,248,.55);outline-offset:2px;border-radius:.9rem}
  body.we-editing [data-wid] a, body.we-editing [data-wid] canvas{pointer-events:none}
  .we-ghost{opacity:.35}`;
  const st = document.createElement('style'); st.textContent = css; document.head.appendChild(st);

  window.WidgetEditor = function (opts) {
    const grid = document.querySelector(opts.grid);
    const bar = document.querySelector(opts.toolbar);
    if (!grid || !bar) return;
    const page = grid.dataset.page;
    const onShow = opts.onShow || function () {};
    const $ = s => bar.querySelector(`[data-we="${s}"]`);
    const items = () => Array.from(grid.children).filter(e => e.dataset && e.dataset.wid);
    let sortable = null;

    function renderCatalog() {
      const box = $('catalog'); if (!box) return;
      const hidden = items().filter(e => e.hidden);
      const groups = {};
      hidden.forEach(e => (groups[e.dataset.group] = groups[e.dataset.group] || []).push(e));
      box.innerHTML = hidden.length ? '' : '<div class="text-xs text-slate-500">Every widget is on the page. Remove one with ✕ to get it back here.</div>';
      Object.keys(groups).forEach(g => {
        const row = document.createElement('div'); row.className = 'flex flex-wrap items-center gap-1 mb-1';
        row.innerHTML = `<span class="text-[10px] uppercase tracking-wide text-slate-500 w-full sm:w-auto sm:mr-1">${g}</span>`;
        groups[g].forEach(e => {
          const b = document.createElement('button'); b.type = 'button';
          b.className = 'pill bg-slate-800 text-slate-200 hover:bg-indigo-500/30 !py-1';
          b.textContent = '+ ' + e.dataset.title;
          b.onclick = () => show(e); row.appendChild(b);
        });
        box.appendChild(row);
      });
    }
    function show(el) {
      const vis = items().filter(e => !e.hidden);
      el.hidden = false;
      if (vis.length) vis[vis.length - 1].after(el); else grid.prepend(el);
      onShow(el.dataset.wid); renderCatalog();
      el.scrollIntoView({ behavior: 'smooth', block: 'nearest' });
    }
    function hide(el) { el.hidden = true; grid.appendChild(el); renderCatalog(); }
    function move(el, dir) {
      const vis = items().filter(e => !e.hidden); const i = vis.indexOf(el); const j = i + dir;
      if (j < 0 || j >= vis.length) return;
      if (dir < 0) vis[j].before(el); else vis[j].after(el);
    }
    grid.addEventListener('click', ev => {
      const b = ev.target.closest('[data-we-act]'); if (!b) return;
      ev.preventDefault(); ev.stopPropagation();
      const el = b.closest('[data-wid]');
      ({ remove: () => hide(el), up: () => move(el, -1), down: () => move(el, 1) })[b.dataset.weAct]();
    }, true);
    function setEditing(on) {
      document.body.classList.toggle('we-editing', on);
      ['save', 'cancel', 'reset', 'panel'].forEach(k => { const e = $(k); if (e) e.hidden = !on; });
      const ed = $('edit'); if (ed) ed.hidden = on;
      if (on) { renderCatalog(); if (window.Sortable && !sortable) sortable = Sortable.create(grid, { handle: '.we-handle', animation: 150, ghostClass: 'we-ghost', filter: '[hidden]' }); }
      else if (sortable) { sortable.destroy(); sortable = null; }
    }
    async function post(url, body) {
      const r = await fetch(url, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body || {}) });
      if (!r.ok) throw new Error('HTTP ' + r.status);
      return r.json();
    }
    $('edit') && ($('edit').onclick = () => setEditing(true));
    $('cancel') && ($('cancel').onclick = () => location.reload());
    $('save') && ($('save').onclick = async () => {
      const ids = items().filter(e => !e.hidden).map(e => e.dataset.wid);
      try { await post(`/layout/${page}`, { widgets: ids }); setEditing(false); flash('Layout saved'); }
      catch (e) { flash('Could not save: ' + e.message, true); }
    });
    $('reset') && ($('reset').onclick = async () => {
      if (!confirm('Reset to the default widgets?')) return;
      try { await post(`/layout/${page}/reset`); location.reload(); } catch (e) { flash('Could not reset: ' + e.message, true); }
    });
    function flash(msg, bad) {
      const f = $('msg'); if (!f) return; f.textContent = msg; f.className = 'text-xs ' + (bad ? 'text-rose-300' : 'text-emerald-300');
      setTimeout(() => { f.textContent = ''; }, 2500);
    }
    items().filter(e => !e.hidden).forEach(e => onShow(e.dataset.wid));
  };
})();
