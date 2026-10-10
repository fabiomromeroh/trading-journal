/* Journal panel: searchable dropdowns (Setup single, Tags / Mistakes multi) with "add by typing" and
   Manage (rename / remove), plus autosave of the whole panel on change / blur. Alpine components. */
document.addEventListener('alpine:init', () => {
  const post = (url, body) => fetch(url, { method: 'POST', headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body) }).then((r) => r.json());

  Alpine.data('tjCombo', (cfg) => ({
    kind: cfg.kind, multi: cfg.multi, options: cfg.options.slice(), usage: cfg.usage || {},
    selected: cfg.selected.slice(), q: '', open: false, manage: false, editing: null, draft: '', label: cfg.label,
    get value() { return this.multi ? this.selected.join(', ') : (this.selected[0] || ''); },
    get matches() {
      const q = this.q.trim().toLowerCase();
      return this.options.filter((o) => !q || o.toLowerCase().includes(q));
    },
    get canAdd() {
      const q = this.q.trim();
      return q && !this.options.some((o) => o.toLowerCase() === q.toLowerCase());
    },
    has(o) { return this.selected.some((s) => s.toLowerCase() === o.toLowerCase()); },
    changed() { this.$dispatch('tj-change'); },
    pick(o) {
      if (this.multi) {
        this.selected = this.has(o) ? this.selected.filter((s) => s.toLowerCase() !== o.toLowerCase()) : [...this.selected, o];
      } else { this.selected = this.has(o) ? [] : [o]; this.open = false; }
      this.q = ''; this.changed();
    },
    drop(o) { this.selected = this.selected.filter((s) => s !== o); this.changed(); },
    async add() {
      const name = this.q.trim(); if (!name) return;
      const r = await post(`/options/${this.kind}`, { name });
      if (r.ok) { this.options = r.options; this.usage = this.usage || {}; this.pick(r.name); }
    },
    enter(e) {
      e.preventDefault();
      if (this.canAdd) this.add();
      else if (this.matches.length) { const o = this.matches[0]; if (this.has(o)) { this.q = ''; this.open = this.multi; } else this.pick(o); }
    },
    startEdit(o) { this.editing = o; this.draft = o; },
    async rename(o) {
      const n = this.draft.trim(); this.editing = null;
      if (!n || n === o) return;
      const r = await post(`/options/${this.kind}/rename`, { old: o, new: n });
      if (r.ok) {
        this.options = r.options; this.usage = r.usage;
        this.selected = this.selected.map((s) => (s === o ? r.name : s)).filter((s, i, a) => a.indexOf(s) === i);
        this.changed();
      }
    },
    async removeOption(o) {
      const n = this.usage[o] || 0;
      const msg = `Remove “${o}” from the ${this.label.toLowerCase()} list?\n` +
        (n ? `${n} trade${n === 1 ? '' : 's'} already use it and will keep it.` : 'No trade uses it.') +
        '\nIt just stops being offered.';
      if (!confirm(msg)) return;
      const r = await post(`/options/${this.kind}/remove`, { name: o });
      if (r.ok) { this.options = r.options; this.usage = r.usage; }
    },
  }));

  Alpine.data('tjJournal', (cfg) => ({
    rating: cfg.rating || 0, saved: cfg.saved, saving: false, error: false, plan: cfg.plan || {}, timer: null,
    async save() {
      clearTimeout(this.timer);
      this.saving = true; this.error = false;
      try {
        const r = await fetch(`/trades/${cfg.id}/journal`, { method: 'POST', headers: { 'X-Autosave': '1' },
          body: new URLSearchParams(new FormData(this.$refs.form)) });
        const j = await r.json();
        this.saved = !!j.ok; this.error = !j.ok;
      } catch (e) { this.error = true; this.saved = false; }
      this.saving = false;
    },
    soon() { this.saved = false; clearTimeout(this.timer); this.timer = setTimeout(() => this.save(), 700); },
  }));

  /* Settings > Journal questions editor */
  Alpine.data('tjQuestions', (cfg) => ({
    rows: cfg.rows, saved: false,
    add() { this.rows.push({ id: '', label: '', type: 'text' }); },
    del(i) { this.rows.splice(i, 1); },
    up(i) { if (i > 0) this.rows.splice(i - 1, 0, this.rows.splice(i, 1)[0]); },
    async save() {
      const r = await post('/settings/journal-questions', { questions: this.rows });
      if (r.ok) { this.rows = r.questions; this.saved = true; setTimeout(() => (this.saved = false), 2500); }
    },
  }));
});
