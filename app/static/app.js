/* Instant filters: any <form data-autofilter> applies on change (selects, dates, checkboxes) and after a
   300 ms pause while typing. data-autofilter="swap" + data-target="#id" replaces just that region via
   HTMX (keeps focus in the form) and updates the URL; otherwise the page navigates to the new URL. */
(function () {
  'use strict';
  const timers = new WeakMap();
  function url(form, submitter) {
    const p = new URLSearchParams();
    for (const [k, v] of new FormData(form, submitter || undefined)) if (String(v).trim() !== '') p.append(k, v);
    if (submitter && submitter.name === 'preset') { p.delete('start'); p.delete('end'); }
    const qs = p.toString();
    return (form.getAttribute('action') || location.pathname) + (qs ? '?' + qs : '');
  }
  function apply(form, submitter) {
    const u = url(form, submitter);
    if (u === location.pathname + location.search) return;
    const target = form.dataset.target;
    if (form.dataset.autofilter === 'swap' && target && window.htmx && document.querySelector(target)) {
      form.classList.add('is-loading');
      const a = document.activeElement, name = a && a.form === form ? a.name : null;
      const sel = name && a.selectionStart != null ? [a.selectionStart, a.selectionEnd] : null;
      const opts = { target, select: target, swap: 'outerHTML' };
      if (form.dataset.indicator) opts.indicator = form.dataset.indicator;
      window.htmx.ajax('GET', u, opts).then(() => {
        form.classList.remove('is-loading');
        if (name) {  // keep typing where you were
          const el = document.querySelector(`${target} [name="${name}"]`);
          if (el) { el.focus(); if (sel && el.setSelectionRange) try { el.setSelectionRange(sel[0], sel[1]); } catch (e) { /* type=date */ } }
        }
        history.pushState({ tjFilter: true }, '', u);
        document.dispatchEvent(new CustomEvent('tj:filtered', { detail: { url: u } }));
      });
    } else {
      location.assign(u);
    }
  }
  function onEvent(e) {
    const el = e.target, form = el && el.form;
    if (!form || !form.hasAttribute('data-autofilter') || el.hasAttribute('data-no-autofilter')) return;
    const typing = el.tagName === 'INPUT' && /^(text|search|number|)$/.test(el.type || '');
    if (e.type === 'input' && !typing) return;
    if (e.type === 'change' && typing) return; // handled by the debounced input event
    clearTimeout(timers.get(form));
    if (typing) timers.set(form, setTimeout(() => apply(form), 300));
    else apply(form);
  }
  document.addEventListener('input', onEvent);
  document.addEventListener('change', onEvent);
  document.addEventListener('submit', (e) => {
    const form = e.target;
    if (form.hasAttribute('data-autofilter') && form.dataset.autofilter === 'swap' && (form.method || 'get').toLowerCase() === 'get') {
      e.preventDefault(); apply(form, e.submitter);
    }
  });
  window.addEventListener('popstate', (e) => { if (e.state && e.state.tjFilter) location.reload(); });
  // Filter buttons are only needed without JavaScript.
  document.documentElement.classList.add('js');
})();

/* (i) metric tooltips: hover (mouse), focus (keyboard) or tap (touch) a .tip-btn. */
(function () {
  let pop, cur = null, shownAt = 0;
  function ensure() {
    if (pop) return pop;
    pop = document.createElement('div'); pop.id = 'tip-pop'; pop.setAttribute('role', 'tooltip'); pop.hidden = true;
    document.body.appendChild(pop); return pop;
  }
  function show(btn) {
    const p = ensure(); if (cur !== btn) shownAt = Date.now(); cur = btn;
    p.textContent = '';
    const b = document.createElement('b'); b.textContent = btn.dataset.tipTitle || ''; p.appendChild(b);
    p.appendChild(document.createTextNode(btn.dataset.tipDesc || ''));
    if (btn.dataset.tipCalc) { const c = document.createElement('span'); c.className = 'tip-calc'; c.textContent = btn.dataset.tipCalc; p.appendChild(c); }
    p.hidden = false; btn.setAttribute('aria-expanded', 'true');
    const r = btn.getBoundingClientRect(), w = p.offsetWidth, h = p.offsetHeight, vw = window.innerWidth, vh = window.innerHeight;
    let left = Math.min(Math.max(8, r.left + r.width / 2 - w / 2), vw - w - 8);
    let top = r.bottom + 6; if (top + h > vh - 8) top = Math.max(8, r.top - h - 6);
    p.style.left = left + 'px'; p.style.top = top + 'px';
  }
  function hide() { if (pop) pop.hidden = true; if (cur) cur.setAttribute('aria-expanded', 'false'); cur = null; }
  document.addEventListener('mouseover', e => { const b = e.target.closest && e.target.closest('.tip-btn'); if (b && b !== cur) show(b); });
  document.addEventListener('mouseout', e => { const b = e.target.closest && e.target.closest('.tip-btn'); if (b && b === cur && !b.contains(e.relatedTarget) && document.activeElement !== b) hide(); });
  document.addEventListener('focusin', e => { if (e.target.classList && e.target.classList.contains('tip-btn')) show(e.target); });
  document.addEventListener('focusout', e => { if (e.target === cur) hide(); });
  document.addEventListener('click', e => {
    const b = e.target.closest && e.target.closest('.tip-btn');
    if (b) { e.preventDefault(); e.stopPropagation(); if (cur === b && Date.now() - shownAt > 500) { hide(); } else { show(b); } return; }
    if (cur) hide();
  }, true);
  document.addEventListener('keydown', e => { if (e.key === 'Escape') hide(); });
  window.addEventListener('scroll', hide, { passive: true }); window.addEventListener('resize', hide);
})();

/* 2px top bar on full-page navigations and form submits.
   HTMX only dims the swap target (aria-busy). It never paints a bar overlay on #page,
   so headings, filters, and stat widgets stay visible. Every busy state is cleared. */
(function () {
  const bar = document.getElementById('top-progress');
  if (!bar) return;
  let hideTimer = null;
  let safety = null;
  const busy = new Set();
  function start() {
    clearTimeout(hideTimer);
    bar.classList.remove('done');
    bar.classList.add('go');
    clearTimeout(safety);
    safety = setTimeout(clearAll, 15000);
  }
  function finishBar() {
    if (!bar.classList.contains('go')) return;
    bar.classList.remove('go');
    bar.classList.add('done');
    clearTimeout(hideTimer);
    hideTimer = setTimeout(() => bar.classList.remove('done'), 400);
  }
  function mark(node) {
    if (!node || node === document.body || node === document.documentElement || node.id === 'page') return;
    node.classList.add('hx-busy');
    node.setAttribute('aria-busy', 'true');
    busy.add(node);
  }
  function clearBusy() {
    busy.forEach((node) => {
      node.classList.remove('hx-busy');
      node.removeAttribute('aria-busy');
    });
    busy.clear();
    document.querySelectorAll('.hx-busy').forEach((n) => {
      n.classList.remove('hx-busy');
      n.removeAttribute('aria-busy');
    });
    document.querySelectorAll('form.is-loading').forEach((f) => f.classList.remove('is-loading'));
  }
  function clearAll() {
    clearTimeout(safety);
    safety = null;
    clearBusy();
    finishBar();
  }
  document.addEventListener('submit', (e) => {
    const form = e.target;
    if (!form || form.hasAttribute('hx-post') || form.hasAttribute('hx-get') || form.dataset.autofilter === 'swap') return;
    start();
  });
  document.addEventListener('click', (e) => {
    const a = e.target.closest && e.target.closest('a[href]');
    if (!a || a.target || e.metaKey || e.ctrlKey || e.shiftKey || e.altKey || e.button !== 0) return;
    if (a.hasAttribute('hx-get') || a.hasAttribute('hx-post') || a.getAttribute('href').startsWith('#')) return;
    let u;
    try { u = new URL(a.href, location.href); } catch (err) { return; }
    if (u.origin !== location.origin) return;
    if (u.pathname === location.pathname && u.search === location.search) return;
    start();
  });
  document.body.addEventListener('htmx:beforeRequest', (e) => {
    start();
    const detail = e.detail || {};
    if (detail.boosted) return;  // page navigation: only the top bar, never dim the page
    const elt = detail.elt;
    const target = detail.target;
    if (target) mark(target);
    const ind = elt && elt.getAttribute && elt.getAttribute('hx-indicator');
    if (!ind) return;
    ind.split(',').forEach((s) => {
      const node = document.querySelector(s.trim());
      if (node) mark(node);
    });
  });
  ['htmx:afterSwap', 'htmx:afterSettle', 'htmx:afterRequest', 'htmx:responseError', 'htmx:sendError'].forEach((name) => {
    document.body.addEventListener(name, clearAll);
  });
  window.addEventListener('pageshow', clearAll);
})();


/* Boosted navigation (sidebar links): HTMX swaps only <main>; page scripts inside it are re-run here in
   document order (libraries already on the page are not loaded twice, inline scripts run in their own
   block scope so a revisit cannot clash with the last visit's constants). Any script error -> full reload,
   so a page is never left half-initialised. Back / forward always reload the page (no stale snapshots).
   Trade detail links: prefetched on hover so the full load comes from the browser cache. */
(function () {
  if (!window.htmx) return;
  htmx.config.historyCacheSize = 0;
  htmx.config.refreshOnHistoryMiss = true;
  htmx.config.scrollIntoViewOnBoost = false;
  const loaded = new Set([...document.scripts].map((s) => s.src).filter(Boolean));
  document.body.addEventListener('htmx:beforeSwap', (e) => {
    const d = e.detail;
    if (!d.boosted || !d.xhr) return;
    if (!/<main[\s>]/.test(d.serverResponse) || /data-boost="off"/.test(d.serverResponse)) {  // opt-out page: plain load
      e.preventDefault(); location.assign(d.pathInfo && d.pathInfo.finalRequestPath || d.xhr.responseURL); return;
    }
    d.serverResponse = d.serverResponse.replace(/<script(?![^>]*\btype=)(?=[\s>])/g, '<script type="tj/boost"');
  });
  function run(list, i) {
    if (i >= list.length) { document.dispatchEvent(new CustomEvent('tj:page')); return; }
    const old = list[i], s = document.createElement('script');
    if (old.src) {
      const src = new URL(old.getAttribute('src'), location.href).href;
      if (loaded.has(src)) return run(list, i + 1);
      loaded.add(src); s.src = src; s.onload = () => run(list, i + 1); s.onerror = () => location.reload();
      old.replaceWith(s);
    } else {
      s.textContent = '{\n' + old.textContent + '\n}';
      old.replaceWith(s);
      run(list, i + 1);
    }
  }
  document.body.addEventListener('htmx:afterSettle', (e) => {
    const main = document.querySelector('main');
    const list = main ? [...main.querySelectorAll('script[type="tj/boost"]')] : [];
    if (!list.length) return;
    list.forEach((sc) => sc.setAttribute('type', 'tj/boost-run'));  // afterSettle also fires for the OOB swaps
    let failed = false;
    const onErr = () => { failed = true; };
    window.addEventListener('error', onErr);
    run(list, 0);
    setTimeout(() => { window.removeEventListener('error', onErr); if (failed) location.reload(); }, 0);
  });
  const seen = new Set();
  document.addEventListener('mouseover', (e) => {
    const a = e.target.closest && e.target.closest('a[href^="/trades/"]');
    if (!a || seen.has(a.href) || !/^\/trades\/\d+/.test(a.getAttribute('href'))) return;
    seen.add(a.href);
    const l = document.createElement('link'); l.rel = 'prefetch'; l.href = a.href; l.as = 'document';
    document.head.appendChild(l);
  });
})();
