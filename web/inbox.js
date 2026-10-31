/* Inbox (top bar): what agents need from you. `sb ask` questions (answer in place), `sb notify` pages, and terminals
   waiting on a permission prompt. Loaded before app.js; uses its helpers (h, api, toast, setText, tick, S) at call time. */
'use strict';

function makeInbox() {
  const btn = document.getElementById('tb-inbox');
  const count = h('span', { class: 'ib-n' });
  btn.append(count);
  const list = h('div', { class: 'ib-list' });
  const empty = h('p', { class: 'muted small ib-empty', text: 'Nothing needs you right now. Agents reach you here with `sb ask` and `sb notify`.' });
  const clear = h('button', { class: 'pbtn', type: 'button', text: 'Dismiss all' });
  const panel = h('div', { class: 'ib-panel', hidden: true },
    h('div', { class: 'ib-h' }, h('strong', { text: 'Inbox' }), h('span', { class: 'spacer' }), clear), list, empty);
  document.body.append(panel);
  let items = [];

  function row(n) {
    const did = (n.idem || '').startsWith('decision:') ? n.idem.slice(9) : null;
    const d = did && ((S.ov && S.ov.decisions) || []).find((x) => x.id === did);
    const acts = h('div', { class: 'ib-acts' });
    if (d) {
      const opts = d.options && d.options.length ? d.options : [];
      for (const o of opts) acts.append(h('button', { class: 'btn small', type: 'button', text: o, onclick: () => answer(did, o) }));
      const free = h('input', { type: 'text', placeholder: opts.length ? 'or type an answer' : 'your answer', 'aria-label': 'Answer' });
      free.addEventListener('keydown', (e) => { if (e.key === 'Enter' && free.value.trim()) answer(did, free.value.trim()); });
      acts.append(free);
    }
    if (n.agent && S.agents.has(n.agent)) {
      const a = S.agents.get(n.agent);
      acts.append(h('a', { class: 'pbtn', href: `#/p/${encodeURIComponent(a.project || '')}/${encodeURIComponent(a.id)}`, text: 'Open', onclick: () => { panel.hidden = true; } }));
    }
    acts.append(h('button', { class: 'pbtn', type: 'button', text: 'Dismiss', onclick: () => dismiss(n.id) }));
    return h('div', { class: 'ib-item ' + (n.level === 'need' ? 'need' : 'info') },
      h('div', { class: 'ib-t' }, h('strong', { text: n.agent || 'Switchboard' }), ' ', d ? d.question : n.text),
      h('div', { class: 'muted small', text: [n.project, ago(n.created_at)].filter(Boolean).join(' · ') }), acts);
  }
  function render() {
    const need = items.filter((n) => n.level === 'need').length;
    setText(count, items.length ? String(items.length) : '');
    btn.classList.toggle('alert', need > 0);
    if (panel.hidden) return;
    const focused = panel.contains(document.activeElement) && document.activeElement.tagName === 'INPUT';
    if (focused) return;  // do not wipe an answer being typed
    list.replaceChildren(...items.map(row));
    empty.hidden = items.length > 0;
  }
  async function poll() {
    try { items = (await api('GET', '/notifications')).items || []; } catch (e) { /* hub away */ }
    render();
  }
  async function answer(did, text) {
    try { await api('POST', `/decisions/${encodeURIComponent(did)}/answer`, { answer: text }); toast('Answer sent'); await tick(); poll(); } catch (err) { toast(err.message, 'err'); }
  }
  async function dismiss(id) {
    try { await api('POST', `/notifications/${encodeURIComponent(id)}/ack`, {}); poll(); } catch (err) { toast(err.message, 'err'); }
  }
  clear.addEventListener('click', async () => { try { await api('POST', '/notifications/ack-all', {}); poll(); } catch (err) { toast(err.message, 'err'); } });
  btn.addEventListener('click', (e) => { e.stopPropagation(); panel.hidden = !panel.hidden; render(); });
  document.addEventListener('click', (e) => { if (!panel.hidden && !panel.contains(e.target) && e.target !== btn) panel.hidden = true; });
  poll();
  setInterval(poll, 4000);
}
