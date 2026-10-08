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
      window.htmx.ajax('GET', u, { target, select: target, swap: 'outerHTML' }).then(() => {
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
