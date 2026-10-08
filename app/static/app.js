/* Instant filters: any <form data-autofilter> applies on change (selects, dates, checkboxes) and after a
   300 ms pause while typing. data-autofilter="swap" + data-target="#id" replaces just that region via
   HTMX (keeps focus in the form) and updates the URL; otherwise the page navigates to the new URL. */
(function () {
  'use strict';
  const timers = new WeakMap();
  function url(form) {
    const p = new URLSearchParams();
    for (const [k, v] of new FormData(form)) if (String(v).trim() !== '') p.append(k, v);
    const qs = p.toString();
    return (form.getAttribute('action') || location.pathname) + (qs ? '?' + qs : '');
  }
  function apply(form) {
    const u = url(form);
    if (u === location.pathname + location.search) return;
    const target = form.dataset.target;
    if (form.dataset.autofilter === 'swap' && target && window.htmx && document.querySelector(target)) {
      form.classList.add('is-loading');
      window.htmx.ajax('GET', u, { target, select: target, swap: 'outerHTML' }).then(() => {
        form.classList.remove('is-loading');
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
      e.preventDefault(); apply(form);
    }
  });
  window.addEventListener('popstate', (e) => { if (e.state && e.state.tjFilter) location.reload(); });
  // Filter buttons are only needed without JavaScript.
  document.documentElement.classList.add('js');
})();
