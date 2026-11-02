/* Switchboard v2 — Switchboard chat + project cards + 1-4 terminal panes. Plain vanilla JS, no build step.
 * Server data always goes through textContent (never innerHTML). Polling updates persistent DOM nodes in place,
 * so focused inputs and open forms are never clobbered. Terminals: xterm.js (web/vendor) over /ws/agents/<id>. */
'use strict';

/* ---------- small DOM helpers ---------- */
const $ = (sel, root) => (root || document).querySelector(sel);
const enc = encodeURIComponent;

function h(tag, props, ...kids) {
  const e = document.createElement(tag);
  if (props) {
    for (const [k, v] of Object.entries(props)) {
      if (v == null || v === false) continue;
      if (k === 'class') e.className = v;
      else if (k === 'text') e.textContent = v;
      else if (k.startsWith('on') && typeof v === 'function') e.addEventListener(k.slice(2), v);
      else if (v === true) e.setAttribute(k, '');
      else e.setAttribute(k, String(v));
    }
  }
  for (const kid of kids.flat(Infinity)) if (kid != null && kid !== false) e.append(kid);
  return e;
}
function setText(node, s) { s = s == null ? '' : String(s); if (node.textContent !== s) node.textContent = s; }
function setCls(node, cls) { if (node.className !== cls) node.className = cls; }
function setAttr(node, k, v) { v = v == null ? '' : String(v); if (node.getAttribute(k) !== v) node.setAttribute(k, v); }

/* keyed list: one persistent element per key; reorders without recreating */
function keyedList(container, keyFn, create) {
  const map = new Map();
  return {
    map,
    sync(items) {
      const seen = new Set();
      items.forEach((it, i) => {
        const k = keyFn(it);
        seen.add(k);
        let rec = map.get(k);
        if (!rec) { rec = create(it, k); map.set(k, rec); }
        rec.update(it);
        if (container.children[i] !== rec.el) {
          const ae = document.activeElement;
          const had = ae && ae !== document.body && rec.el.contains(ae) ? ae : null;
          container.insertBefore(rec.el, container.children[i] || null);
          if (had) { try { had.focus({ preventScroll: true }); } catch (e) { /* ignore */ } }
        }
      });
      for (const [k, rec] of map) if (!seen.has(k)) { rec.el.remove(); if (rec.destroy) rec.destroy(); map.delete(k); }
    },
  };
}

/* ---------- time ---------- */
let clockSkew = 0;
function parseTime(v) {
  if (v == null || v === '') return NaN;
  if (typeof v === 'number') return v < 1e12 ? v * 1000 : v;
  return Date.parse(v);
}
function ago(v) {
  const t = parseTime(v);
  if (isNaN(t)) return '';
  const s = Math.max(0, Math.round((Date.now() + clockSkew - t) / 1000));
  if (s < 10) return 'just now';
  if (s < 60) return s + ' s ago';
  const m = Math.floor(s / 60);
  if (m < 60) return m + ' m ago';
  const hr = Math.floor(m / 60);
  return hr < 48 ? hr + ' h ago' : Math.floor(hr / 24) + ' d ago';
}
function absTime(v) { const t = parseTime(v); return isNaN(t) ? '' : new Date(t).toLocaleString(); }
/* "3:40 PM" today, "Fri 4:59 PM" this week, "Nov 12 4:59 PM" later */
function fmtReset(v) {
  const t = parseTime(v);
  if (isNaN(t)) return '';
  const d = new Date(t), now = new Date();
  const time = d.toLocaleTimeString([], { hour: 'numeric', minute: '2-digit' });
  if (d.toDateString() === now.toDateString()) return time;
  const days = (t - now.getTime()) / 864e5;
  if (days > -1 && days < 6.5) return d.toLocaleDateString([], { weekday: 'short' }) + ' ' + time;
  return d.toLocaleDateString([], { month: 'short', day: 'numeric' }) + ' ' + time;
}

function uuid() {
  try { if (crypto.randomUUID) return crypto.randomUUID(); } catch (e) { /* insecure context */ }
  const b = new Uint8Array(16);
  crypto.getRandomValues(b);
  b[6] = (b[6] & 0x0f) | 0x40; b[8] = (b[8] & 0x3f) | 0x80;
  const x = [...b].map((n) => n.toString(16).padStart(2, '0')).join('');
  return `${x.slice(0, 8)}-${x.slice(8, 12)}-${x.slice(12, 16)}-${x.slice(16, 20)}-${x.slice(20)}`;
}
function lsGet(k) { try { return localStorage.getItem(k); } catch (e) { return null; } }
function lsSet(k, v) { try { if (v) localStorage.setItem(k, v); else localStorage.removeItem(k); } catch (e) { /* blocked */ } }

/* ---------- API ---------- */
/* a plain bash shell as a terminal "agent" on a project, opened in the focused pane */
async function openShell(pid) {
  const p = S.projects.get(pid);
  const body = { provider: 'shell', project: pid, title: 'shell' };
  if (p && p.cwd) body.cwd = p.cwd;  // otherwise the hub makes the project's folder in your workspace
  try {
    const idle = [...S.agents.values()].find((x) => x.provider === 'shell' && x.project === pid && !x.running);
    const a = idle || await api('POST', '/agents', body);
    await tick();
    location.hash = agentHref(a);
  } catch (e) { alert('Could not open a shell: ' + (e.message || e)); }
}

/* true if the hub answers HTTP at all (any status); false while it is down or restarting */
async function hubAnswers() {
  try { await fetch('/', { method: 'HEAD', cache: 'no-store' }); return true; } catch (e) { return false; }
}

async function api(method, path, body, raw) {
  const opt = { method, cache: 'no-store', headers: {} };
  if (raw) { opt.headers['Content-Type'] = raw.type || 'application/octet-stream'; opt.body = raw; }
  else if (body !== undefined) { opt.headers['Content-Type'] = 'application/json'; opt.body = JSON.stringify(body); }
  const r = await fetch('/api' + path, opt);
  if (r.status === 401 && r.headers.get('X-Gate')) {  // phone session ended: the gate signs this phone in again
    location.href = '/gate/?next=' + encodeURIComponent(location.pathname + location.search + location.hash);
  }
  const txt = await r.text();
  let data = null;
  try { data = txt ? JSON.parse(txt) : null; } catch (e) { /* non-JSON */ }
  if (!r.ok) {
    const err = new Error((data && data.error) || `HTTP ${r.status}`);
    err.status = r.status; err.data = data;
    throw err;
  }
  return data;
}
/* POST that the hub may refuse with 409 {needs_confirm} while the agent is busy */
async function postConfirm(path, body, question) {
  try { return await api('POST', path, body); } catch (e) {
    if (e.status === 409 && e.data && e.data.needs_confirm) {
      if (!confirm(question)) return null;
      return api('POST', path, { ...body, confirm: true });
    }
    throw e;
  }
}

function toast(msg, kind) {
  const t = h('div', { class: 'toast' + (kind === 'err' ? ' err' : ''), text: msg });
  $('#toasts').append(t);
  setTimeout(() => t.remove(), kind === 'err' ? 7000 : 3500);
}
function guarded(btn, fn) {
  let inflight = false;
  btn.addEventListener('click', async (ev) => {
    ev.stopPropagation();
    if (inflight) return;
    inflight = true; btn.disabled = true;
    try { await fn(); } catch (e) { toast(e.message || String(e), 'err'); }
    finally { inflight = false; btn.disabled = false; }
  });
}

/* ---------- state ---------- */
const S = { ov: null, agents: new Map(), projects: new Map(), accounts: [], desk: null, online: false, view: null };
window.__sbTerms = {};

/* model suggestions for the CLIs; API accounts suggest the models their endpoint listed during setup */
const MODEL_HINTS = { claude: ['claude-sonnet-5-5', 'claude-opus-5-5', 'claude-haiku-5-5'], codex: [], gemini: [], hermes: [] };
const KIND_LABEL = { claude: 'Claude Code', codex: 'Codex', gemini: 'Gemini CLI', hermes: 'Hermes Agent', api: 'built-in agent', shell: 'shell' };
const STATE_WORD = { busy: 'working', idle: 'idle', starting: 'starting', stopped: 'stopped', reconnecting: 'reconnecting' };
const queueWhy = (a) => `${a.paused ? 'paused' : a.queue_reason || 'waiting to type'}${a.queued_since ? ', oldest ' + ago(a.queued_since) : ''}`;

const agentName = (a) => (a && (a.title || a.id)) || '';
const agentHref = (a) => `#/p/${enc(a.project || '')}/${enc(a.id)}`;
const projName = (pid) => { const p = S.projects.get(pid); return (p && p.name) || pid || ''; };
function projectAgents(pid) {
  const p = S.projects.get(pid);
  const order = (p && p.agent_ids) || [];
  return [...S.agents.values()].filter((a) => a.project === pid)
    .sort((x, y) => { const i = order.indexOf(x.id), j = order.indexOf(y.id); return (i < 0 ? 1e9 : i) - (j < 0 ? 1e9 : j); });
}
function accountAgentIds(acc) {
  const ids = Array.isArray(acc.agents) ? acc.agents : [...S.agents.values()].filter((a) => a.account === acc.name).map((a) => a.id);
  return ids.filter((id) => S.agents.has(id));          // the Switchboard desk is not shown as an agent
}
function accountHint(acc) {
  return [KIND_LABEL[acc.kind] || acc.kind, { key: 'API key', none: 'no key' }[acc.auth] || 'login', acc.kind === 'api' ? acc.model : ''].filter(Boolean).join(' · ');
}
function modelList(acc) { return 'models-' + (acc && acc.kind === 'api' ? 'acct-' + acc.name : (acc ? acc.kind : 'claude')); }
function lsJSON(k) { try { return JSON.parse(lsGet(k) || 'null'); } catch (e) { return null; } }

/* ---------- small shared pieces ---------- */
function stateDot() { return h('span', { class: 'dot' }); }
function setDot(dot, a) { setCls(dot, 'dot s-' + (a.state || 'stopped')); setAttr(dot, 'title', STATE_WORD[a.state] || a.state || ''); }
function modelTag() { return h('span', { class: 'mtag' }); }
function setModelTag(el, a) {
  if (a.provider === 'shell') { setCls(el, 'mtag'); setText(el, 'shell'); setAttr(el, 'title', 'Plain shell (' + (a.cwd || '~') + ')'); return; }
  setCls(el, 'mtag');
  setText(el, a.model ? a.model.replace(/^claude-/, '').replace(/^[\w.-]+\//, '') : 'default model');
  setAttr(el, 'title', (a.model || 'default model') + ' · ' + (KIND_LABEL[a.provider] || a.provider));
}

/* ---------- pools (accounts) ---------- */
function limitRow() {
  const txt = h('span', { class: 'lim-t' });
  const fill = h('i');
  const el = h('div', { class: 'lim' }, txt, h('span', { class: 'lim-bar' }, fill));
  return {
    el,
    update(l) {
      const used = l.used == null ? null : Math.max(0, Math.min(100, Math.round(l.used)));
      const r = fmtReset(l.resets);
      setText(txt, `${l.label}: ${used == null ? '—' : used + '% used'}${r ? ' · resets ' + r : ''}`);
      setAttr(txt, 'title', l.resets ? 'Resets ' + absTime(l.resets) : '');
      fill.style.width = (used || 0) + '%';
      setCls(el, 'lim' + (used >= 90 ? ' hi' : used >= 75 ? ' mid' : ''));
    },
  };
}
/* project view: one card per agent with its account and its context window */
function fmtTok(n) { return n >= 1e6 ? (n / 1e6).toFixed(n % 1e6 ? 1 : 0) + 'M' : n >= 1000 ? Math.round(n / 1000) + 'k' : String(n); }
function agentLimitsCard() {
  const dot = stateDot();
  const name = h('strong');
  const mt = modelTag();
  const acct = h('div', { class: 'al-acct' });
  const ctx = limitRow();
  const el = h('div', { class: 'al' }, h('div', { class: 'al-h' }, dot, name, h('span', { class: 'spacer' }), mt), acct, ctx.el);
  return {
    el,
    update(a) {
      setDot(dot, a); setText(name, agentName(a)); setModelTag(mt, a);
      const acc = S.accounts.find((x) => x.name === a.account);
      setText(acct, acc ? `${acc.name} · ${accountHint(acc)}` : (a.account || (a.provider === 'shell' ? 'shell' : 'no account')));
      const win = a.context_window, used = a.context_tokens;
      ctx.el.hidden = !(win && used != null);
      if (win && used != null) {
        ctx.update({ label: 'Context', used: Math.round((100 * used) / win), resets: null });
        setText(ctx.el.firstChild, `Context: ${fmtTok(used)} of ${fmtTok(win)} tokens (${Math.round((100 * used) / win)}% used)`);
      }
    },
  };
}

function agentLink() {
  const dot = stateDot();
  const name = h('span');
  const el = h('a', { class: 'alink' }, dot, name);
  return {
    el,
    update(id) {
      const a = S.agents.get(id) || { id, state: 'stopped' };
      setDot(dot, a); setText(name, agentName(a));
      setAttr(el, 'href', agentHref(a));
      setAttr(el, 'title', `${a.id}${a.project ? ' · ' + projName(a.project) : ''} · ${STATE_WORD[a.state] || a.state || ''}`);
    },
  };
}
function poolPanel() {
  const name = h('strong', { class: 'pool-name' });
  const kind = h('span', { class: 'pool-kind' });
  const note = h('span', { class: 'pool-email' });
  const agBox = h('div', { class: 'pool-ags' });
  const ags = keyedList(agBox, (id) => id, agentLink);
  const none = h('span', { class: 'muted small', text: 'no agents' });
  const el = h('section', { class: 'pool' }, h('div', { class: 'pool-h' }, name, kind, note), h('div', { class: 'pool-ag' }, agBox, none));
  return {
    el,
    update(acc) {
      setText(name, acc.name); setText(kind, KIND_LABEL[acc.kind] || acc.kind);
      const n = [{ key: 'API key', none: 'no key' }[acc.auth] || 'login', acc.model].filter(Boolean).join(' · ');
      setText(note, n); setAttr(note, 'title', acc.label || n);
      const ids = accountAgentIds(acc);
      ags.sync(ids); none.hidden = ids.length > 0;
    },
  };
}

/* ---------- Switchboard chat (the desk agent: the only chat window) ---------- */
const OPEN_RE = /\[\[open:([\w.-]+)(?:\/([\w.-]+))?\]\]/g;
function inlineMd(text) {
  const out = [];
  const re = /(\*\*[^*\n]+\*\*|`[^`\n]+`)/g;
  let last = 0, m;
  while ((m = re.exec(text))) {
    if (m.index > last) out.push(text.slice(last, m.index));
    const t = m[0];
    out.push(t[0] === '`' ? h('code', { text: t.slice(1, -1) }) : h('strong', { text: t.slice(2, -2) }));
    last = m.index + t.length;
  }
  if (last < text.length) out.push(text.slice(last));
  return out;
}
function openTarget(project, agent) {
  if (!project) return;
  location.hash = agent ? `#/p/${enc(project)}/${enc(agent)}` : `#/p/${enc(project)}`;
}
/* text -> element: light markdown (bold, code, lists, fenced code) + [[open:…]] tokens as buttons */
function renderChatText(text) {
  const opens = [];
  text = String(text || '').replace(OPEN_RE, (_, p, a) => { opens.push({ p, a }); return ''; }).replace(/[ \t]+$/gm, '').trim();
  const box = h('div', { class: 'cm-text' });
  const lines = text.split('\n');
  let list = null, para = null;
  for (let i = 0; i < lines.length; i++) {
    const ln = lines[i];
    if (/^```/.test(ln)) {
      const code = [];
      for (i++; i < lines.length && !/^```/.test(lines[i]); i++) code.push(lines[i]);
      box.append(h('pre', { text: code.join('\n') })); list = para = null; continue;
    }
    const li = ln.match(/^\s*(?:[-*•]|\d+[.)])\s+(.*)$/);
    if (li) {
      if (!list) { list = h(/^\s*\d/.test(ln) ? 'ol' : 'ul'); box.append(list); }
      list.append(h('li', null, inlineMd(li[1]))); para = null; continue;
    }
    list = null;
    if (!ln.trim()) { para = null; continue; }
    if (para) para.append(h('br'), ...inlineMd(ln));
    else { para = h('p', null, inlineMd(ln)); box.append(para); }
  }
  if (opens.length) {
    const row = h('div', { class: 'cm-opens' });
    for (const o of opens) {
      const a = o.a ? S.agents.get(o.a) : null;
      const label = o.a ? `Open ${a ? agentName(a) : o.a} · ${projName(o.p)}` : `Open ${projName(o.p)}`;
      const b = h('button', { class: 'btn open-btn', type: 'button', text: label, title: o.a ? `${o.p}/${o.a}` : o.p });
      b.addEventListener('click', () => openTarget(o.p, o.a));
      row.append(b);
    }
    box.append(row);
  }
  return box;
}
window.__sbRenderChat = renderChatText;     // test hook

function chatEntry() {
  const who = h('span', { class: 'cm-who' });
  const when = h('span', { class: 'cm-when' });
  const note = h('span', { class: 'cm-note' });
  const body = h('div', { class: 'cm-body' });
  const el = h('div', { class: 'cm' }, h('div', { class: 'cm-h' }, who, note, when), body);
  let sig = '';
  return {
    el,
    update(e) {
      const byAgent = e.role === 'user' && e.from && e.from !== 'owner';  // another agent wrote it (sb send)
      const peer = byAgent ? S.agents.get(e.from) : null;
      setCls(el, 'cm ' + (e.role !== 'user' ? 'sb' : byAgent ? 'me peer' : 'me') + (e.pending ? ' pending' : '') + (e.failed ? ' failed' : ''));
      setText(who, e.role !== 'user' ? 'switchboard' : byAgent ? (peer ? agentName(peer) : e.from) : 'you');
      setAttr(who, 'title', byAgent ? `agent ${e.from}${peer ? ' · ' + projName(peer.project) : ''}` : '');
      setText(when, ago(e.ts)); setAttr(when, 'title', absTime(e.ts));
      setText(note, e.note || (e.reply ? 'reply' : ''));
      const s = e.text + '|' + S.agents.size;
      if (s !== sig) { sig = s; body.replaceChildren(e.role === 'user' ? h('div', { class: 'cm-text', text: e.text }) : renderChatText(e.text)); }
    },
  };
}
function makeChat() {
  const status = h('span', { class: 'chat-status' });
  const closeBtn = h('button', { class: 'pbtn chat-close', type: 'button', text: '✕', title: 'Close (Esc)', hidden: true });
  const clearBtn = h('button', { class: 'pbtn', type: 'button', text: 'New chat', title: 'Clear the Switchboard conversation and start over (or type /clear)' });
  const findBtn = h('button', { class: 'pbtn', type: 'button', text: 'Sessions', 'aria-pressed': 'false', title: 'Search past agent sessions (Claude Code, Codex, built-in) and resume one' });
  const finder = makeSessionFinder();
  const log = h('div', { class: 'chat-log', role: 'log', 'aria-live': 'polite' });
  const list = keyedList(log, (e) => e.key, chatEntry);
  const empty = h('div', { class: 'chat-empty', text: 'Tell Switchboard what you want to work on — it opens the right agent.' });
  const noDesk = h('div', { class: 'chat-empty' }, 'The chat runs on one of your accounts. ', h('a', { href: '#/setup', text: 'Set one up' }), ' to start.');
  const thinking = h('div', { class: 'chat-thinking', hidden: true }, h('span', { class: 'dot s-busy' }), ' thinking…');
  const ta = h('textarea', { rows: 2, placeholder: 'e.g. "open the payroll agent" — Enter sends, Shift+Enter new line', 'aria-label': 'Message Switchboard' });
  const send = h('button', { class: 'btn primary', type: 'submit', text: 'Send' });
  const form = h('form', { class: 'chat-form' }, ta, send);
  const el = h('section', { class: 'chat' },
    h('div', { class: 'chat-h' }, h('img', { src: 'logo.svg', width: 18, height: 18, alt: '' }), h('strong', { text: 'Switchboard' }), status, h('span', { class: 'spacer' }), findBtn, clearBtn, closeBtn),
    finder.el, h('div', { class: 'chat-scroll' }, log, empty, noDesk, thinking), form);
  const scroller = el.querySelector('.chat-scroll');
  let timer = null, entries = [], msgs = [], optimistic = [], loaded = false, onClose = null;

  function render() {
    const seenUser = entries.filter((e) => e.role === 'user').slice(-12).map((e) => e.text);
    const inTranscript = (body) => seenUser.some((t) => t.includes(body.trim().slice(0, 200)));
    const pend = [];
    for (const m of msgs) {
      if (inTranscript(m.body)) continue;
      if (m.state === 'queued' || m.state === 'delivering') pend.push({ key: 'm' + m.id, role: 'user', from: m.from, text: m.body, ts: m.created_at, pending: true, note: m.state === 'queued' ? 'queued' : 'sent' });
      else if (m.state === 'failed' && parseTime(m.created_at) > Date.now() + clockSkew - 30 * 60e3) pend.push({ key: 'm' + m.id, role: 'user', from: m.from, text: m.body, ts: m.created_at, failed: true, note: 'failed: ' + (m.error || 'not delivered') });
    }
    for (const o of optimistic) if (!pend.some((p) => p.key === 'm' + o.id) && !inTranscript(o.body)) pend.push({ key: 'm' + o.id, role: 'user', text: o.body, ts: o.ts, pending: true, note: 'sending' });
    const atBottom = scroller.scrollHeight - scroller.scrollTop - scroller.clientHeight < 40;
    const all = entries.filter((e) => e.role === 'user' || e.role === 'assistant').map((e) => ({ ...e, key: 'e' + e.id + (e.part ? '.' + e.part : '') })).concat(pend);
    list.sync(all);
    const d = S.desk;
    empty.hidden = all.length > 0 || !loaded || !d;
    noDesk.hidden = !!d || !S.ov;
    const busy = (d && d.state === 'busy') || pend.some((p) => p.pending);
    thinking.hidden = !busy;
    setText(status, !d ? (S.ov ? 'needs an account (Setup)' : '') : busy ? 'working' : (d.model || '').replace(/^claude-/, ''));
    if (atBottom || !loaded) scroller.scrollTop = scroller.scrollHeight;
    return busy;
  }
  async function poll() {
    clearTimeout(timer);
    let busy = false;
    if (el.isConnected && S.desk) {
      try {
        const [c, m] = await Promise.all([api('GET', '/agents/desk/chat?limit=30'), api('GET', '/messages?agent=desk&limit=10')]);
        entries = (c && c.entries) || [];
        msgs = ((m && m.messages) || []).filter((x) => x.to === 'desk');
        optimistic = optimistic.filter((o) => !msgs.some((x) => x.id === o.id) && Date.now() - o.at < 15000);
        loaded = true;
      } catch (e) { /* hub away: the topbar says so */ }
      busy = render();
    }
    timer = setTimeout(poll, busy ? 1500 : 5000);
  }
  async function clearChat() {
    const d = S.desk;
    if (d && d.state === 'busy' && !confirm('Switchboard is answering right now. Stop it and start a new chat?')) return;
    clearBtn.disabled = true;
    try {
      await api('POST', '/agents/desk/clear', {});
      entries = []; msgs = []; optimistic = []; ta.value = '';
      list.sync([]);
      toast('New Switchboard chat');
      tick();
      setTimeout(poll, 300);
    } catch (err) { toast(err.message, 'err'); } finally { clearBtn.disabled = false; }
  }
  function showFinder(on) {
    finder.el.hidden = !on;
    setAttr(findBtn, 'aria-pressed', on ? 'true' : 'false');
    el.classList.toggle('finding', on);
    if (on) finder.open(); else ta.focus();
  }
  async function submit() {
    const body = ta.value.trim();
    if (!body) return;
    if (/^\/(clear|new|reset)$/i.test(body)) { clearChat(); return; }
    const sm = body.match(/^\/(sessions|resume)\b\s*(.*)$/i);
    if (sm) { ta.value = ''; showFinder(true); finder.search(sm[2]); return; }
    send.disabled = true;
    try {
      const r = await api('POST', '/messages', { to: 'desk', body, idem: uuid() });
      ta.value = '';
      const m = (r && r.message) || {};
      if (m.error) toast(m.error, 'err');
      optimistic.push({ id: m.id || uuid(), body, ts: new Date().toISOString(), at: Date.now() });
      render();
      scroller.scrollTop = scroller.scrollHeight;
      setTimeout(poll, 400);
    } catch (err) { toast(err.message, 'err'); } finally { send.disabled = false; }
  }
  form.addEventListener('submit', (e) => { e.preventDefault(); submit(); });
  ta.addEventListener('keydown', (e) => {
    if (e.key === 'Enter' && !e.shiftKey && !e.isComposing) { e.preventDefault(); submit(); }
    else if (e.key === 'Escape' && onClose) { e.preventDefault(); onClose(); }
  });
  closeBtn.addEventListener('click', () => onClose && onClose());
  clearBtn.addEventListener('click', clearChat);
  findBtn.addEventListener('click', () => showFinder(finder.el.hidden));
  finder.onDone = () => showFinder(false);
  return {
    el,
    mount(parent, close) { onClose = close || null; closeBtn.hidden = !close; parent.append(el); render(); poll(); },
    focus() { ta.focus(); },
    poll,
  };
}
const CHAT = makeChat();

/* ---------- past sessions: search every Claude/Codex session, resume one as an agent ---------- */
function makeSessionFinder() {
  const q = h('input', { type: 'search', placeholder: 'words you remember, a folder, or a session id', 'aria-label': 'Search past sessions', autocomplete: 'off', spellcheck: 'false' });
  const all = h('input', { type: 'checkbox' });
  const note = h('div', { class: 'sf-note muted small' });
  const list = h('div', { class: 'sf-list', role: 'list' });
  const el = h('div', { class: 'sf', hidden: true },
    h('div', { class: 'sf-bar' }, q, h('label', { class: 'sf-all small muted', title: 'Include headless claude -p / codex exec runs' }, all, ' headless')),
    note, list);
  let timer = null, seq = 0;
  const api_ = {
    el, onDone: null,
    open() { q.focus(); q.select(); if (!list.children.length) run(); },
    search(text) { q.value = text || ''; run(); },
  };
  function row(x) {
    const what = (x.title || x.first_ask || x.id).replace(/\s+/g, ' ');
    const snip = (x.snippet || (x.title ? x.first_ask : '') || '').replace(/\s+/g, ' ').trim();
    const label = x.agent ? 'Open' : 'Resume';
    const btn = h('button', { class: 'btn small' + (x.agent ? '' : ' primary'), type: 'button', text: label,
      title: x.agent ? `Already agent ${x.agent} on ${projName(x.project)}` : x.project ? `Adds an agent to ${x.project_name || x.project}` : 'No project matches its folder: makes one' });
    btn.addEventListener('click', async () => {
      btn.disabled = true;
      try {
        const r = await api('POST', '/sessions/resume', { session_id: x.id });
        const a = r.agent;
        if (r.created) toast(r.created_project ? `New project ${a.project} with ${agentName(a)}` : `Added ${agentName(a)} to ${projName(a.project)}`);
        await tick();
        openTarget(a.project, a.id);
        if (api_.onDone) api_.onDone();
      } catch (err) { toast(err.message, 'err'); btn.disabled = false; }
    });
    const where = x.agent ? `agent ${x.agent} · ${projName(x.project)}` : x.project ? `→ ${x.project_name || x.project}` : '→ new project';
    return h('div', { class: 'sf-row', role: 'listitem' },
      h('div', { class: 'sf-main' },
        h('div', { class: 'sf-title', text: what, title: x.id }),
        h('div', { class: 'sf-meta small muted' },
          h('span', { class: 'acct', text: x.provider }), ' ', h('span', { text: ago(x.last_ts), title: absTime(x.last_ts) }),
          ` · ${x.n_asks} asks · `, h('span', { class: 'sf-cwd', text: (x.cwd || '').replace(/^\/home\/[^/]+/, '~'), title: x.cwd || '' }),
          ` · ${where}`),
        snip ? h('div', { class: 'sf-snip small', text: snip }) : null),
      btn);
  }
  async function run() {
    const my = ++seq;
    const params = new URLSearchParams({ q: q.value.trim(), limit: '40' });
    if (all.checked) params.set('headless', '1');
    setText(note, 'searching…');
    try {
      const r = await api('GET', '/sessions?' + params.toString());
      if (my !== seq) return;
      list.replaceChildren(...r.sessions.map(row));
      setText(note, r.sessions.length ? (q.value.trim() ? `${r.sessions.length} best matches` : 'most recent') + ` · ${r.indexed} sessions indexed${r.indexing ? ' (updating)' : ''}`
        : `No match among ${r.indexed} sessions${r.indexing ? ' (still indexing)' : ''}`);
    } catch (err) { if (my === seq) setText(note, err.message); }
  }
  q.addEventListener('input', () => { clearTimeout(timer); timer = setTimeout(run, 250); });
  q.addEventListener('keydown', (e) => {
    if (e.key === 'Enter') { e.preventDefault(); clearTimeout(timer); run(); }
    else if (e.key === 'Escape' && api_.onDone) { e.preventDefault(); api_.onDone(); }
  });
  all.addEventListener('change', run);
  return api_;
}

/* ---------- new agent form (card and project view) ---------- */
function newAgentForm(getPid, onDone) {
  const fAccount = h('select', { 'aria-label': 'Account' });
  const fModel = h('input', { type: 'text', list: 'models-claude', placeholder: 'default model', autocomplete: 'off', spellcheck: 'false' });
  const fCwd = h('input', { type: 'text', placeholder: 'the project\'s folder', autocomplete: 'off', spellcheck: 'false' });
  const fTitle = h('input', { type: 'text', placeholder: 'short title', autocomplete: 'off' });
  const fSid = h('input', { type: 'text', placeholder: 'optional: session / thread id to resume', autocomplete: 'off', spellcheck: 'false' });
  const ok = h('button', { class: 'btn primary', type: 'submit', text: 'Add agent' });
  const cancel = h('button', { class: 'btn ghost', type: 'button', text: 'Cancel' });
  const lab = (t, n, cls) => h('label', { class: cls || '' }, h('span', { text: t }), n);
  const el = h('form', { class: 'newagent', hidden: true },
    lab('Account', fAccount, 'wide'), lab('Model', fModel), lab('Title', fTitle),
    lab('Working directory', fCwd, 'wide'), lab('Resume session', fSid, 'wide'),
    h('div', { class: 'wide row-end' }, h('span', { class: 'muted small', text: 'Not launched until you open it.' }), cancel, ok));
  el.addEventListener('click', (e) => e.stopPropagation());
  function refresh() {
    const cur = fAccount.value;
    fAccount.replaceChildren(...S.accounts.map((a) => h('option', { value: a.name, text: `${a.name} — ${accountHint(a)}` })));
    if (S.accounts.some((a) => a.name === cur)) fAccount.value = cur;
    const acc = S.accounts.find((a) => a.name === fAccount.value);
    fModel.setAttribute('list', modelList(acc));
    fModel.placeholder = acc && acc.kind === 'api' && acc.model ? acc.model : 'default model';
  }
  fAccount.addEventListener('change', refresh);
  cancel.addEventListener('click', () => { el.hidden = true; });
  el.addEventListener('submit', async (e) => {
    e.preventDefault();
    const acc = S.accounts.find((a) => a.name === fAccount.value);
    if (!acc) { toast('No accounts yet: open Setup in the top bar', 'err'); return; }
    const body = { project: getPid(), provider: acc.kind, account: acc.name };
    if (fCwd.value.trim()) body.cwd = fCwd.value.trim();  // blank: the project's folder in your workspace
    if (fModel.value.trim()) body.model = fModel.value.trim();
    if (fTitle.value.trim()) body.title = fTitle.value.trim();
    if (fSid.value.trim()) body.session_id = fSid.value.trim();
    ok.disabled = true;
    try {
      const a = await api('POST', '/agents', body);
      toast(`Added ${agentName(a)} — open it to launch`);
      fTitle.value = ''; fSid.value = ''; el.hidden = true;
      tick();
      if (onDone) onDone(a);
    } catch (err) { toast(err.message, 'err'); } finally { ok.disabled = false; }
  });
  return {
    el,
    open() {
      refresh();
      const p = S.projects.get(getPid());
      if (!fCwd.value && p && p.cwd) fCwd.value = p.cwd;
      el.hidden = false; fTitle.focus();
    },
    toggle() { if (el.hidden) this.open(); else el.hidden = true; },
  };
}

/* ---------- overview: project cards ---------- */
function agentRow() {
  const dot = stateDot();
  const title = h('span', { class: 'ar-title' });
  const id = h('span', { class: 'ar-id' });
  const acct = h('span', { class: 'acct' });
  const mt = modelTag();
  const st = h('span', { class: 'ar-state' });
  const q = h('span', { class: 'badge q' });
  const blk = h('span', { class: 'badge warn', text: 'blocked' });
  const el = h('a', { class: 'arow' }, dot, title, id, h('span', { class: 'spacer' }), q, blk, acct, mt, st);
  el.addEventListener('click', (e) => e.stopPropagation());
  return {
    el,
    update(a) {
      setAttr(el, 'href', agentHref(a));
      setDot(dot, a);
      setText(title, agentName(a)); setText(id, a.title && a.title !== a.id ? a.id : ''); setAttr(title, 'title', a.last_text ? a.last_text.slice(-300) : '');
      setText(acct, a.account); setAttr(acct, 'title', 'Account');
      setModelTag(mt, a);
      setText(st, a.switching ? '→ ' + a.switching.to : (STATE_WORD[a.state] || a.state));
      setText(q, a.queued ? a.queued + ' queued' : ''); q.hidden = !a.queued; setAttr(q, 'title', a.queued ? queueWhy(a) : '');
      blk.hidden = !a.launch_block; setAttr(blk, 'title', a.launch_block || '');
    },
  };
}
function projectCard(p0) {
  const pid = p0.id;
  const name = h('a', { class: 'pc-name', href: '#/p/' + enc(pid) });
  const flags = h('span', { class: 'pc-flags' });
  const last = h('span', { class: 'pc-last' });
  const goal = h('div', { class: 'pc-goal' });
  const agBox = h('div', { class: 'pc-agents' });
  const ags = keyedList(agBox, (a) => a.id, agentRow);
  const none = h('div', { class: 'muted small', text: 'No agents yet.' });
  const addBtn = h('button', { class: 'btn ghost small', type: 'button', text: '+ Agent' });
  const form = newAgentForm(() => pid);
  addBtn.addEventListener('click', (e) => { e.stopPropagation(); form.toggle(); });
  const el = h('article', { class: 'pcard' },
    h('div', { class: 'pc-head' }, name, flags, h('span', { class: 'spacer' }), last),
    goal, agBox, none, h('div', { class: 'pc-foot' }, addBtn), form.el);
  el.addEventListener('click', (e) => {
    if (e.target.closest('a,button,input,select,textarea,form,label')) return;
    if (getSelection && String(getSelection())) return;
    location.hash = '#/p/' + enc(pid);
  });
  return {
    el,
    update(p) {
      setText(name, p.name || p.id);
      setText(flags, [p.paused && 'paused', p.stale && 'quiet', p.archived && 'archived'].filter(Boolean).join(' · '));
      setText(last, ago(p.last_activity)); setAttr(last, 'title', absTime(p.last_activity));
      const g = p.next_step ? `Next: ${p.next_step}` : (p.goal || p.status_note || '');
      setText(goal, g); setAttr(goal, 'title', [p.goal, p.next_step && 'Next: ' + p.next_step, p.status_note].filter(Boolean).join('\n'));
      goal.hidden = !g;
      const list = projectAgents(pid);
      ags.sync(list); none.hidden = list.length > 0;
      setCls(el, 'pcard' + (list.some((a) => a.state === 'busy') ? ' busy' : '') + (p.archived ? ' arch' : ''));
    },
  };
}

function overviewView() {
  const poolBox = h('div', { class: 'pools' });
  const pools = keyedList(poolBox, (a) => a.name, poolPanel);
  const grid = h('div', { class: 'pgrid' });
  const cards = keyedList(grid, (p) => p.id, projectCard);
  const archGrid = h('div', { class: 'pgrid' });
  const archCards = keyedList(archGrid, (p) => p.id, projectCard);
  const archN = h('span', { class: 'n' });
  const arch = h('details', { class: 'arch' }, h('summary', null, 'Archived ', archN), archGrid);
  /* inline name box rather than window.prompt(), which some embedded browsers block */
  const addP = h('button', { class: 'btn ghost small', type: 'button', text: '+ Project' });
  const pName = h('input', { type: 'text', placeholder: 'project name', 'aria-label': 'New project name', autocomplete: 'off' });
  const pOk = h('button', { class: 'btn primary small', type: 'submit', text: 'Create' });
  const pCancel = h('button', { class: 'btn ghost small', type: 'button', text: 'Cancel' });
  const addForm = h('form', { class: 'addproj', hidden: true }, pName, pOk, pCancel);
  const closeAdd = () => { addForm.hidden = true; addP.hidden = false; pName.value = ''; };
  addP.addEventListener('click', () => { addForm.hidden = false; addP.hidden = true; pName.focus(); });
  pCancel.addEventListener('click', closeAdd);
  pName.addEventListener('keydown', (e) => { if (e.key === 'Escape') { e.preventDefault(); closeAdd(); } });
  addForm.addEventListener('submit', async (e) => {
    e.preventDefault();
    const n = pName.value.trim();
    if (!n) { pName.focus(); return; }
    pOk.disabled = true;
    try {
      const p = await api('POST', '/projects', { name: n });
      toast('Created ' + (p.name || p.id)); closeAdd(); tick();
    } catch (err) { toast(err.message, 'err'); } finally { pOk.disabled = false; }
  });
  const noProj = h('p', { class: 'muted small', text: 'No projects yet. "+ Project" makes one (with its own folder in your workspace); then "+ Agent" on its card starts an agent there.' });
  const chatCol = h('aside', { class: 'ov-chat' });
  const el = h('div', { class: 'ov' }, chatCol,
    h('div', { class: 'ov-main' },
      h('section', { class: 'block' }, h('h2', null, 'Accounts ', h('a', { class: 'btn ghost small', href: '#/setup', text: 'Setup' })), poolBox),
      h('section', { class: 'block' }, h('h2', null, 'Projects ', addP, addForm), noProj, grid, arch)));
  CHAT.mount(chatCol, null);
  return {
    el, title: '',
    update() {
      pools.sync(S.accounts);
      const ps = [...S.projects.values()];
      const act = (p) => -(parseTime(p.last_activity) || 0);
      cards.sync(ps.filter((p) => !p.archived).sort((a, b) => act(a) - act(b)));
      noProj.hidden = ps.some((p) => !p.archived);
      const ar = ps.filter((p) => p.archived);
      archCards.sync(ar); setText(archN, ar.length); arch.hidden = !ar.length;
    },
    destroy() {},
  };
}

/* ---------- terminal pane (one slot of the 1/2/3/4 layout; shows whichever agent is picked) ---------- */
const TERM_THEME = {
  background: '#1b1e20', foreground: '#e3e8e5', cursor: '#86c232', cursorAccent: '#1b1e20',
  selectionBackground: 'rgba(134,194,50,.35)',
  black: '#2a2f32', red: '#e0675d', green: '#86c232', yellow: '#e2b04a', blue: '#6aa7e0', magenta: '#c391e0', cyan: '#5cc3c0', white: '#d6dcd9',
  brightBlack: '#6b7478', brightRed: '#ff8a80', brightGreen: '#a6dc58', brightYellow: '#f3cb6b', brightBlue: '#8fc1f0', brightMagenta: '#d9b0f0', brightCyan: '#86dcd8', brightWhite: '#ffffff',
};

function agentPicker(pid) {
  const sel = h('select', { class: 'pn-pick', 'aria-label': 'Agent in this pane' });
  return {
    el: sel,
    fill(curId) {
      if (document.activeElement === sel) return;
      const mine = projectAgents(pid);
      const others = [...S.agents.values()].filter((a) => a.project !== pid)
        .sort((x, y) => projName(x.project).localeCompare(projName(y.project)) || agentName(x).localeCompare(agentName(y)));
      const sig = curId + '|' + mine.map((a) => a.id + agentName(a)).join(',') + '|' + others.map((a) => a.id + a.project).join(',');
      if (sel.dataset.sig !== sig) {
        sel.dataset.sig = sig;
        const opt = (a, label) => h('option', { value: a.id, text: label });
        const kids = [];
        if (!curId || !S.agents.has(curId)) kids.push(h('option', { value: curId || '', text: curId ? curId + ' (unknown)' : 'Pick an agent…' }));
        if (mine.length) kids.push(h('optgroup', { label: 'This project' }, mine.map((a) => opt(a, agentName(a)))));
        if (others.length) kids.push(h('optgroup', { label: 'Other projects' }, others.map((a) => opt(a, `${agentName(a)} · ${projName(a.project)}`))));
        sel.replaceChildren(...kids);
      }
      if (sel.value !== (curId || '')) sel.value = curId || '';
    },
  };
}

function makePane(idx, pid, view) {
  let id = null;
  let cur = null;
  const dot = stateDot();
  const picker = agentPicker(pid);
  const st = h('span', { class: 'pn-state' });
  const conn = h('span', { class: 'pn-conn' });
  const note = h('span', { class: 'pn-note' });
  const meta = h('span', { class: 'pn-meta' });
  const num = h('span', { class: 'pn-num', text: String(idx + 1) });
  const hdr = h('div', { class: 'pn-h' }, num, dot, picker.el, st, conn, note, h('span', { class: 'spacer' }), meta);
  const box = h('div', { class: 'term' });
  const ovMsg = h('div', { class: 'ov-msg' });
  const ovBtn = h('button', { class: 'btn primary', type: 'button' });
  const overlay = h('div', { class: 'ovl', hidden: true }, h('div', { class: 'ov-box' }, ovMsg, ovBtn));
  const el = h('section', { class: 'pane', hidden: true }, hdr, h('div', { class: 'termwrap' }, box, overlay));
  el.addEventListener('mousedown', () => view.setFocus(idx));
  el.addEventListener('focusin', () => view.setFocus(idx));
  picker.el.addEventListener('change', () => { if (picker.el.value) view.pick(idx, picker.el.value); });

  let term = null, fit = null, ws = null, visible = false, halted = false, retry = 0, retryTimer = null, refused = 0;
  let ro = null, fitTimer = null, ovKind = null, lastSize = '', killed = false;

  function sendJ(o) { if (ws && ws.readyState === 1) ws.send(JSON.stringify(o)); }
  function sendSize(force) {
    if (!term) return;
    const s = term.cols + 'x' + term.rows;
    if (force || s !== lastSize) { lastSize = s; sendJ({ t: 'r', cols: term.cols, rows: term.rows }); }
  }
  function fitNow(force) {
    if (!term || !visible || box.clientWidth < 20 || box.clientHeight < 20) return;
    try { fit.fit(); } catch (e) { /* not measurable yet */ }
    sendSize(force);
  }
  function scheduleFit() { clearTimeout(fitTimer); fitTimer = setTimeout(() => fitNow(false), 60); }

  function showOverlay(kind, msg, btnText, fn) {
    ovKind = kind; setText(ovMsg, msg); setText(ovBtn, btnText || ''); ovBtn.hidden = !btnText;
    ovBtn.onclick = async () => { ovBtn.disabled = true; try { await fn(); } catch (e) { toast(e.message, 'err'); } finally { ovBtn.disabled = false; } };
    overlay.hidden = false;
  }
  function hideOverlay() { ovKind = null; overlay.hidden = true; }
  async function startAndConnect() { await api('POST', `/agents/${enc(id)}/start`, {}); reconnect(); tick(); }
  function setConn(s) { setText(conn, s); setCls(conn, 'pn-conn ' + s.replace(/\W.*/, '')); }

  function onPaste(e) {
    const items = e.clipboardData ? [...e.clipboardData.items] : [];
    const img = items.find((it) => it.kind === 'file' && /^image\//.test(it.type));
    if (!img) return;                        // text: xterm handles it (bracketed paste when the app enables 2004)
    e.preventDefault(); e.stopPropagation();
    const file = img.getAsFile();
    if (!file) return;
    if (file.size > 20 * 1024 * 1024) { toast('Image is over 20 MB', 'err'); return; }
    api('POST', '/uploads', undefined, file).then((r) => {
      if (!r || !r.path) throw new Error('upload returned no path');
      sendJ({ t: 'i', d: '\x1b[200~' + r.path + '\x1b[201~' });
      term.focus();
    }).catch((err) => toast('Image paste failed: ' + err.message, 'err'));
  }

  function ensureTerm() {
    if (term) return;
    term = new Terminal({
      fontFamily: '"JetBrains Mono", "DejaVu Sans Mono", monospace', fontSize: 13, lineHeight: 1.0,
      cursorBlink: true, scrollback: 10000, theme: TERM_THEME, macOptionIsMeta: true, allowProposedApi: false,
    });
    fit = new FitAddon.FitAddon();
    term.loadAddon(fit);
    term.open(box);
    term.onData((d) => sendJ({ t: 'i', d }));
    term.attachCustomKeyEventHandler((e) => {
      if (e.type === 'keydown' && e.ctrlKey && e.shiftKey && e.code === 'KeyC') {
        const sel = term.getSelection();
        if (sel && navigator.clipboard) navigator.clipboard.writeText(sel).catch(() => {});
        return false;
      }
      if (e.ctrlKey && e.shiftKey && e.code === 'KeyV') return false;   // let the browser paste event fire
      return true;
    });
    box.addEventListener('paste', onPaste, true);
    ro = new ResizeObserver(scheduleFit);
    ro.observe(box);
    if (id) window.__sbTerms[id] = term;
  }

  function connect() {
    clearTimeout(retryTimer);
    if (!visible || ws || killed || !id) return;
    halted = false;
    if (ovKind === 'offline' || ovKind === 'empty') hideOverlay();
    const url = `${location.protocol === 'https:' ? 'wss:' : 'ws:'}//${location.host}/ws/agents/${enc(id)}`;
    const sock = new WebSocket(url);
    sock.binaryType = 'arraybuffer';
    let opened = false;
    ws = sock;
    setConn(retry ? 'reconnecting…' : 'connecting…');
    sock.onopen = () => {
      if (sock !== ws) return;
      opened = true; retry = 0; refused = 0; setConn('live');
      term.reset();                       // the hub replays scrollback on every open
      fitNow(true);
    };
    sock.onmessage = (ev) => {
      if (sock !== ws) return;
      if (typeof ev.data !== 'string') { term.write(new Uint8Array(ev.data)); return; }
      let m;
      try { m = JSON.parse(ev.data); } catch (e) { return; }
      onStatus(m);
    };
    sock.onclose = async () => {
      if (sock !== ws) return;
      ws = null;
      if (!visible || halted || killed) { setConn(''); return; }
      // A hub that does not answer at all is restarting: keep trying. Only a hub that answers and still turns the
      // terminal away (404 / no such route) counts as a refusal.
      const down = !opened && !(await hubAnswers());
      if (ws || !visible || halted || killed) return;   // reconnected or closed while we asked
      if (!opened && !down && ++refused >= 2) {
        halted = true; setConn('refused');
        showOverlay('offline', 'The hub refused this terminal twice, so it stopped retrying. If a red banner says the hub is running old code, restart switchboard.service first.', 'Retry', () => { refused = 0; reconnect(); });
        return;
      }
      const delay = Math.min(down ? 2000 : 10000, 400 * 2 ** retry++);
      setConn(down ? 'hub restarting…' : 'reconnecting…');
      retryTimer = setTimeout(connect, delay);
    };
  }
  function disconnect() {
    clearTimeout(retryTimer);
    if (ws) { const s = ws; ws = null; try { s.close(); } catch (e) { /* ignore */ } }
    halted = false; retry = 0; refused = 0; setConn('');
  }
  function reconnect() { disconnect(); hideOverlay(); connect(); }

  function onStatus(m) {
    if (m.t === 'status') {
      if (m.state && cur) { cur = { ...cur, state: m.state, running: m.running }; paintState(); }
      if (m.running && (ovKind === 'stopped' || ovKind === 'exit')) hideOverlay();
      if (m.note) setText(note, m.note);
    } else if (m.t === 'exit') {
      showOverlay('exit', `The CLI exited${m.code != null ? ' (code ' + m.code + ')' : ''}.`, 'Start', startAndConnect);
    } else if (m.t === 'blocked') {
      halted = true; setConn('');
      showOverlay('blocked', 'Cannot launch: ' + (m.reason || 'blocked'), 'Retry', async () => reconnect());
    }
  }

  function paintState() {
    const a = cur || { state: 'stopped' };
    setDot(dot, a);
    setText(st, !id ? '' : a.switching ? `switching to ${a.switching.to}…` : (STATE_WORD[a.state] || a.state || ''));
    const bits = [];
    if (a.launch_block) bits.push('blocked: ' + a.launch_block);
    if (a.queued) bits.push(`${a.queued} queued · ${queueWhy(a)}`);
    if (a.switching && a.switching.note) bits.push(a.switching.note);
    setText(note, bits.join(' · ')); setAttr(note, 'title', bits.join('\n'));
    setText(meta, !id ? '' : [a.account, a.model ? a.model.replace(/^claude-/, '') : '', a.project !== pid && a.project ? projName(a.project) : ''].filter(Boolean).join(' · '));
  }

  return {
    el, idx,
    get id() { return id; },
    get agent() { return cur; },
    setAgent(newId) {
      newId = newId || null;
      if (newId === id) return;
      disconnect(); hideOverlay();
      if (id && window.__sbTerms[id] === term) delete window.__sbTerms[id];
      id = newId;
      cur = id ? (S.agents.get(id) || { id, state: 'stopped' }) : null;
      if (term) { term.reset(); if (id) window.__sbTerms[id] = term; }
      picker.fill(id); paintState();
      if (!id) showOverlay('empty', 'Pick an agent for this pane.', '', null);
      if (visible) connect();
    },
    update() {
      if (id) {
        const a = S.agents.get(id);
        const wasRunning = cur && cur.running;
        if (a) cur = a;
        /* relaunched elsewhere (account switch, sb send, Start) while we sat on the exit overlay */
        if (a && visible && a.running && !wasRunning && ovKind === 'exit') reconnect();
      }
      picker.fill(id); paintState();
    },
    show() {
      el.hidden = false;
      const was = visible;
      visible = true;
      ensureTerm();
      if (!was) { if (id) connect(); else showOverlay('empty', 'Pick an agent for this pane.', '', null); }
      requestAnimationFrame(() => fitNow(false));
    },
    hide() { el.hidden = true; visible = false; disconnect(); },
    focus() { if (term) term.focus(); },
    setFocused(on) { el.classList.toggle('focus', on); },
    destroy() {
      killed = true; visible = false; disconnect(); clearTimeout(fitTimer);
      if (ro) ro.disconnect();
      if (id && window.__sbTerms[id] === term) delete window.__sbTerms[id];
      if (term) { term.dispose(); term = null; }
    },
  };
}

/* controls for the focused pane's agent: account, model, project, stop, remove */
function controlBar() {
  let a = null;
  const who = h('span', { class: 'cb-who' });
  const acct = h('select', { class: 'pn-sel', 'aria-label': 'Account', title: 'Account pool — switching exits the CLI and resumes the same session there' });
  const model = h('input', { class: 'pn-model', type: 'text', autocomplete: 'off', spellcheck: 'false', 'aria-label': 'Model', title: 'Model — applies on next launch', placeholder: 'default model' });
  const move = h('select', { class: 'pn-sel', 'aria-label': 'Project', title: 'Move to another project' });
  const stopBtn = h('button', { class: 'pbtn', type: 'button', text: 'Stop', title: 'Exit the CLI (/exit)' });
  const el = h('div', { class: 'cbar' }, who, h('label', { class: 'pn-l' }, 'account', acct), h('label', { class: 'pn-l' }, 'model', model),
    h('label', { class: 'pn-l' }, 'project', move), stopBtn);

  acct.addEventListener('change', async () => {
    if (!a) return;
    const to = acct.value, from = a.account, name = agentName(a), id = a.id, running = a.running;
    if (to === from) return;
    try {
      const r = await postConfirm(`/agents/${enc(id)}/account`, { account: to }, `${name} is busy — interrupt and switch to ${to}?`);
      if (!r) { acct.value = from; return; }
      toast(`${name} → ${to}${running ? ' (relaunching on the same session)' : ''}`);
      tick();
    } catch (e) { acct.value = from; toast(e.message, 'err'); }
  });
  model.addEventListener('change', async () => {
    if (!a) return;
    try { await api('PATCH', `/agents/${enc(a.id)}`, { model: model.value.trim() || null }); toast('Model set — applies on next launch'); tick(); }
    catch (e) { toast(e.message, 'err'); }
  });
  model.addEventListener('keydown', (e) => { if (e.key === 'Enter') model.blur(); });
  move.addEventListener('change', async () => {
    if (!a) return;
    const to = move.value, from = a.project;
    if (to === from) return;
    try { await api('PATCH', `/agents/${enc(a.id)}`, { project: to }); toast(`Moved ${agentName(a)} to ${projName(to)}`); tick(); }
    catch (e) { move.value = from; toast(e.message, 'err'); }
  });
  guarded(stopBtn, async () => {
    if (!a) return;
    const r = await postConfirm(`/agents/${enc(a.id)}/stop`, {}, `${agentName(a)} is busy — interrupt and stop?`);
    if (r) { toast(`Stopping ${agentName(a)}`); tick(); }
  });
  return {
    el,
    bind(agent) {
      a = agent && S.agents.has(agent.id) ? S.agents.get(agent.id) : null;
      el.hidden = !a;
      if (!a) return;
      setText(who, agentName(a)); setAttr(who, 'title', `${a.id} · ${a.provider} · session ${a.session_id || 'new'}\n${a.cwd || ''}`);
      if (document.activeElement !== acct) {
        const list = S.accounts.filter((x) => x.kind === a.provider);
        if (!list.some((x) => x.name === a.account)) list.push({ name: a.account, kind: a.provider });
        const sig = list.map((x) => x.name + accountHint(x)).join('|');
        if (acct.dataset.sig !== sig) {
          acct.dataset.sig = sig;
          acct.replaceChildren(...list.map((x) => h('option', { value: x.name, text: x.name + (accountHint(x) ? ' — ' + accountHint(x) : '') })));
        }
        if (acct.value !== a.account) acct.value = a.account;
      }
      if (document.activeElement !== move) {
        const ps = [...S.projects.values()].filter((p) => !p.archived || p.id === a.project);
        const sig = ps.map((p) => p.id + p.name).join('|');
        if (move.dataset.sig !== sig) {
          move.dataset.sig = sig;
          move.replaceChildren(...ps.map((p) => h('option', { value: p.id, text: p.name || p.id })));
        }
        if (move.value !== a.project) move.value = a.project;
      }
      if (document.activeElement !== model && model.value !== (a.model || '')) model.value = a.model || '';
      model.setAttribute('list', 'models-' + a.provider);
      acct.parentElement.hidden = model.parentElement.hidden = a.provider === 'shell';
      stopBtn.disabled = !a.running;
    },
  };
}

/* ---------- project view ---------- */
function sourceCard() {
  const label = h('strong');
  const kind = h('span', { class: 'badge' });
  const ok = h('span', { class: 'badge' });
  const age = h('span', { class: 'muted small' });
  const path = h('div', { class: 'src-path' });
  const pre = h('pre');
  const el = h('div', { class: 'src' }, h('div', { class: 'src-h' }, label, kind, ok, h('span', { class: 'spacer' }), age), path, pre);
  return {
    el,
    update(x) {
      setText(label, x.label || x.path); setText(kind, x.kind);
      const good = x.ok !== false;
      setCls(ok, 'badge ' + (good ? 'ok' : 'fail')); setText(ok, good ? 'ok' : 'fail');
      setCls(el, 'src' + (good ? '' : ' bad'));
      setText(age, ago(x.mtime)); setAttr(age, 'title', absTime(x.mtime));
      setText(path, x.path || ''); setAttr(path, 'title', x.path || ''); path.hidden = !x.path;
      const lines = Array.isArray(x.lines) ? x.lines.slice(-12).join('\n') : '';
      setText(pre, [x.summary, lines].filter(Boolean).join('\n'));
    },
  };
}

function tabEl() {
  const dot = stateDot();
  const name = h('span', { class: 'tab-name' });
  const acct = h('span', { class: 'acct' });
  const mt = modelTag();
  const rm = h('button', { class: 'tab-x', type: 'button', text: '✕', title: 'Remove from Switchboard (stops its CLI; session files stay on disk)' });
  const el = h('a', { class: 'tab', role: 'tab' }, dot, name, acct, mt, rm);
  let cur = null;
  rm.addEventListener('click', async (e) => {
    e.preventDefault(); e.stopPropagation();
    if (!cur || !confirm(`Remove ${agentName(cur)} from Switchboard? Its CLI is stopped first; the session files stay on disk.`)) return;
    try { await api('DELETE', `/agents/${enc(cur.id)}`); } catch (err) { alert('Remove failed: ' + (err.message || err)); }
    tick();
  });
  return {
    el,
    update(a) { cur = a; setAttr(el, 'href', agentHref(a)); setDot(dot, a); setText(name, agentName(a)); setText(acct, a.account); setModelTag(mt, a); },
  };
}

function projectView(pid, initialAgent) {
  const saved = lsJSON('sb.panes.' + pid) || {};
  let count = [1, 2, 3, 4].includes(saved.n) ? saved.n : 1;
  const picks = Array.isArray(saved.picks) ? saved.picks.slice(0, 4).map((x) => (typeof x === 'string' ? x : null)) : [];
  while (picks.length < 4) picks.push(null);
  let focus = Number.isInteger(saved.f) && saved.f < count ? saved.f : 0;
  let showSrc = lsGet('sb.src') !== '0';
  let showLim = lsGet('sb.lim') !== '0';
  let drawer = false;
  function save() { lsSet('sb.panes.' + pid, JSON.stringify({ n: count, picks, f: focus })); }

  const name = h('h1', { class: 'pv-name' });
  const goal = h('div', { class: 'pv-goal' });
  const seg = h('div', { class: 'seg', role: 'group', 'aria-label': 'Panes' }, h('span', { class: 'seg-l', text: 'panes' }),
    [1, 2, 3, 4].map((n) => h('button', { class: 'pbtn seg-b', type: 'button', text: String(n), 'data-n': n, title: ['', 'One pane', 'Two side by side', 'One tall left, two stacked right', 'Two by two'][n] })));
  const srcBtn = h('button', { class: 'pbtn', type: 'button', text: 'Sources' });
  const limBtn = h('button', { class: 'pbtn', type: 'button', text: 'Context', title: "This project's agents: account and context window" });
  const addBtn = h('button', { class: 'pbtn', type: 'button', text: '+ Agent' });
  const shellBtn = h('button', { class: 'pbtn', type: 'button', text: '+ Shell', title: 'Open a plain bash shell in this project' });
  const summonBtn = h('button', { class: 'pbtn', type: 'button', text: 'Summon', title: 'Side agent beside the focused agent: a much bigger /btw that leaves it undisturbed' });
  const shareBtn = h('button', { class: 'pbtn', type: 'button', text: 'Shared', title: "What this project's agents are doing, and their shared notes" });
  let showShare = lsGet('sb.share') === '1';
  /* summon form */
  const smWho = h('strong');
  const smTask = h('textarea', { rows: 3, placeholder: 'What should the side agent do or answer? It works beside the focused agent without touching its conversation.', 'aria-label': 'Side task' });
  const smFork = h('input', { type: 'checkbox', checked: true });
  const smModel = h('input', { type: 'text', list: 'models-claude', autocomplete: 'off', spellcheck: 'false', 'aria-label': 'Model', placeholder: 'same model' });
  const smOk = h('button', { class: 'btn primary', type: 'submit', text: 'Summon' });
  const smCancel = h('button', { class: 'btn ghost', type: 'button', text: 'Cancel' });
  const summonForm = h('form', { class: 'summon', hidden: true },
    h('div', { class: 'summon-h' }, 'Summon a side agent beside ', smWho),
    smTask,
    h('div', { class: 'summon-row' },
      h('label', { class: 'summon-fork', title: 'Copy its whole conversation (Claude --fork-session / codex fork). Off: a fresh agent that pulls its context with sb context when needed.' }, smFork, ' fork its conversation'),
      h('label', { class: 'pn-l' }, 'model', smModel), h('span', { class: 'spacer' }), smCancel, smOk));
  /* shared context side section */
  const shBox = h('div', { class: 'sh-list' });
  const shNotes = h('div', { class: 'sh-notes' });
  const shInput = h('input', { type: 'text', placeholder: 'note for every agent on this project', 'aria-label': 'Shared note', autocomplete: 'off' });
  const shForm = h('form', { class: 'sh-add' }, shInput, h('button', { class: 'btn small', type: 'submit', text: 'Add' }));
  const shareSec = h('section', { class: 'side-sec' },
    h('div', { class: 'side-h' }, h('h2', { text: 'Shared context' })),
    h('div', { class: 'muted small sh-hint', text: 'Agents pull this with sb context when they need it; nothing is pushed into them.' }),
    shBox, h('h2', { class: 'sh-nh', text: 'Notes' }), shNotes, shForm);
  shellBtn.addEventListener('click', () => openShell(pid));
  const deskBtn = h('button', { class: 'pbtn desk-btn', type: 'button', text: 'Switchboard', title: 'Open the Switchboard chat' });
  const form = newAgentForm(() => pid, (a) => { location.hash = agentHref(a); });
  const tabsBox = h('nav', { class: 'tabs', role: 'tablist' });
  const tabs = keyedList(tabsBox, (a) => a.id, tabEl);
  const ctl = controlBar();
  const stage = h('div', { class: 'stage n1' });
  const view = { setFocus, pick };
  const panes = [0, 1, 2, 3].map((i) => makePane(i, pid, view));
  stage.append(...panes.map((p) => p.el));
  const srcBox = h('div', { class: 'src-list' });
  const srcs = keyedList(srcBox, (s) => `${s.kind}|${s.path}|${s.label}`, sourceCard);
  const srcEmpty = h('div', { class: 'muted small', text: 'No sources configured.' });
  const srcRefresh = h('button', { class: 'pbtn', type: 'button', text: '↻', title: 'Re-read sources' });
  const limBox = h('div', { class: 'al-list' });
  const limCards = keyedList(limBox, (a) => a.id, agentLimitsCard);
  const limEmpty = h('div', { class: 'muted small', text: 'No agents on this project.' });
  const limSec = h('section', { class: 'side-sec' }, h('div', { class: 'side-h' }, h('h2', { text: 'Agents and context' })), limBox, limEmpty);
  const srcSec = h('section', { class: 'side-sec' }, h('div', { class: 'side-h' }, h('h2', { text: 'Sources' }), h('span', { class: 'spacer' }), srcRefresh), srcBox, srcEmpty);
  const side = h('aside', { class: 'side' }, shareSec, limSec, srcSec);
  const drawerEl = h('aside', { class: 'drawer', hidden: true });
  const el = h('div', { class: 'pv' },
    h('div', { class: 'pv-head' }, h('a', { class: 'back', href: '#/', text: '← Projects', title: 'Back to all projects' }), name, goal,
      h('span', { class: 'spacer' }), seg, addBtn, summonBtn, shellBtn, shareBtn, limBtn, srcBtn, deskBtn),
    form.el, summonForm,
    h('div', { class: 'pv-bar' }, tabsBox, h('span', { class: 'spacer' }), ctl.el),
    h('div', { class: 'pv-body' }, h('div', { class: 'stage-wrap' }, stage), side, drawerEl));
  form.el.classList.add('pv-form');
  addBtn.addEventListener('click', () => form.toggle());
  srcBtn.addEventListener('click', () => { showSrc = !showSrc; lsSet('sb.src', showSrc ? '1' : '0'); layout(); });
  limBtn.addEventListener('click', () => { showLim = !showLim; lsSet('sb.lim', showLim ? '1' : '0'); layout(); });
  shareBtn.addEventListener('click', () => { showShare = !showShare; lsSet('sb.share', showShare ? '1' : '0'); layout(); if (showShare) loadShared(); });
  const focusedAgent = () => { const a = picks[focus] && S.agents.get(picks[focus]); return a && (a.provider === 'claude' || a.provider === 'codex') ? a : null; };
  summonBtn.addEventListener('click', () => {
    if (!summonForm.hidden) { summonForm.hidden = true; return; }
    const a = focusedAgent();
    if (!a) { toast('Focus a Claude or Codex agent pane first', 'err'); return; }
    summonForm.dataset.beside = a.id;
    setText(smWho, agentName(a));
    smModel.setAttribute('list', 'models-' + a.provider);
    smModel.placeholder = a.model ? `same (${a.model.replace(/^claude-/, '')})` : 'same model';
    summonForm.hidden = false; smTask.focus();
  });
  smCancel.addEventListener('click', () => { summonForm.hidden = true; });
  smTask.addEventListener('keydown', (e) => {
    if (e.key === 'Enter' && (e.ctrlKey || e.metaKey)) { e.preventDefault(); summonForm.requestSubmit(); }
    else if (e.key === 'Escape') { e.preventDefault(); summonForm.hidden = true; }
  });
  summonForm.addEventListener('submit', async (e) => {
    e.preventDefault();
    const task = smTask.value.trim();
    if (!task) { smTask.focus(); return; }
    smOk.disabled = true;
    try {
      const body = { beside: summonForm.dataset.beside, task, fork: smFork.checked, idem: uuid() };
      if (smModel.value.trim()) body.model = smModel.value.trim();
      const r = await api('POST', '/summon', body);
      const a = r.agent;
      toast(`${agentName(a)} summoned${r.forked ? ' (fork)' : ''}; it starts in the next pane`);
      smTask.value = ''; summonForm.hidden = true;
      await tick();
      if (count === 1) setCount(2);
      pick(count === 2 ? (focus === 0 ? 1 : 0) : (focus === count - 1 ? 0 : count - 1), a.id);
    } catch (err) { toast(err.message, 'err'); } finally { smOk.disabled = false; }
  });
  let shTimer = null;
  async function loadShared() {
    clearTimeout(shTimer);
    if (!showShare) return;
    try {
      const d = await api('GET', `/projects/${enc(pid)}/context`);
      shBox.replaceChildren(...d.agents.map((x) => h('div', { class: 'sh-agent' },
        h('div', { class: 'sh-name' }, h('span', { class: 'dot s-' + (x.state === 'busy' ? 'busy' : x.state === 'stopped' ? 'stopped' : 'idle') }), ' ', x.title || x.id,
          x.forked_from ? h('span', { class: 'muted small', text: ` · fork of ${x.forked_from}` }) : null),
        x.asked ? h('div', { class: 'sh-line small' }, h('span', { class: 'muted', text: 'asked ' }), x.asked) : null,
        x.said ? h('div', { class: 'sh-line small' }, h('span', { class: 'muted', text: 'said ' }), x.said) : null)));
      if (!d.agents.length) shBox.replaceChildren(h('div', { class: 'muted small', text: 'No agents yet.' }));
      shNotes.replaceChildren(...(d.notes.length ? d.notes.map((n) => h('div', { class: 'sh-note small' },
        h('span', { class: 'muted', text: `${n.agent} · ${ago(n.created_at)} ` }), n.text)) : [h('div', { class: 'muted small', text: 'No notes yet.' })]));
    } catch (err) { /* hub away */ }
    shTimer = setTimeout(loadShared, 10000);
  }
  shForm.addEventListener('submit', async (e) => {
    e.preventDefault();
    const text = shInput.value.trim();
    if (!text) return;
    try { await api('POST', '/notes', { project: pid, text }); shInput.value = ''; loadShared(); }
    catch (err) { toast(err.message, 'err'); }
  });
  seg.addEventListener('click', (e) => { const b = e.target.closest('[data-n]'); if (b) setCount(Number(b.dataset.n)); });
  function closeDrawer() { drawer = false; drawerEl.hidden = true; deskBtn.setAttribute('aria-pressed', 'false'); panes[focus].focus(); }
  deskBtn.addEventListener('click', () => {
    if (drawer) { closeDrawer(); return; }
    drawer = true; drawerEl.hidden = false; deskBtn.setAttribute('aria-pressed', 'true');
    CHAT.mount(drawerEl, closeDrawer); CHAT.focus();
  });
  let srcTimer = null, srcGone = false;
  async function loadSources() {
    clearTimeout(srcTimer);
    try {
      const d = await api('GET', `/projects/${enc(pid)}`);
      const list = (d && d.sources) || [];
      srcs.sync(list); srcEmpty.hidden = list.length > 0;
    } catch (e) { if (e.status === 404) srcGone = true; }
    if (!srcGone) srcTimer = setTimeout(loadSources, 20000);
  }
  guarded(srcRefresh, loadSources);

  /* fill empty / vanished picks of the visible panes with this project's agents not shown yet */
  function fillPicks() {
    if (!S.ov) return;
    const mine = projectAgents(pid).map((a) => a.id);
    for (let i = 0; i < count; i++) {
      if (picks[i] && (S.agents.has(picks[i]) || !S.agents.size)) continue;
      const used = new Set(picks.slice(0, count).filter(Boolean));
      picks[i] = mine.find((x) => !used.has(x)) || null;
    }
  }
  function syncReplaceUrl() {
    const a = picks[focus] && S.agents.get(picks[focus]);
    const want = a ? `#/p/${enc(pid)}/${enc(a.id)}` : `#/p/${enc(pid)}`;
    if (location.hash !== want) history.replaceState(null, '', want);
  }
  function paint() {
    for (const b of seg.querySelectorAll('[data-n]')) b.setAttribute('aria-pressed', Number(b.dataset.n) === count ? 'true' : 'false');
    const shown = new Set(picks.slice(0, count).filter(Boolean));
    for (const [id, rec] of tabs.map) {
      rec.el.classList.toggle('on', shown.has(id));
      rec.el.classList.toggle('focus', id === picks[focus]);
      setAttr(rec.el, 'aria-selected', id === picks[focus] ? 'true' : 'false');
    }
    panes.forEach((p, i) => p.setFocused(i === focus && count > 1));
    ctl.bind(panes[focus] && panes[focus].agent);
  }
  function layout() {
    srcBtn.setAttribute('aria-pressed', showSrc ? 'true' : 'false');
    limBtn.setAttribute('aria-pressed', showLim ? 'true' : 'false');
    shareBtn.setAttribute('aria-pressed', showShare ? 'true' : 'false');
    srcSec.hidden = !showSrc; limSec.hidden = !showLim; shareSec.hidden = !showShare;
    side.hidden = !showSrc && !showLim && !showShare;
    setCls(stage, 'stage n' + count);
    fillPicks();
    panes.forEach((p, i) => {
      if (i < count) { p.setAgent(picks[i]); p.show(); }
      else p.hide();
    });
    paint();
  }
  function setCount(n) {
    count = n;
    if (focus >= count) focus = count - 1;
    layout(); save();
    setTimeout(() => panes[focus].focus(), 0);
  }
  function setFocus(i) {
    if (i === focus || i >= count) return;
    focus = i; save(); paint(); syncReplaceUrl();
  }
  function pick(i, aid) {
    const j = picks.slice(0, count).indexOf(aid);
    if (j >= 0 && j !== i) { picks[j] = picks[i]; panes[j].setAgent(picks[j]); }   // already shown elsewhere: swap
    picks[i] = aid; focus = i;
    panes[i].setAgent(aid);
    if (j >= 0 && j !== i && !picks[j]) layout();
    save(); paint(); syncReplaceUrl();
    setTimeout(() => panes[i].focus(), 0);
  }
  const v = {
    el, title: '',
    update() {
      const p = S.projects.get(pid);
      if (!p) { setText(name, pid); setText(goal, 'Project not found.'); }
      else {
        setText(name, p.name || p.id); v.title = p.name || p.id;
        const g = [p.goal, p.next_step && 'Next: ' + p.next_step].filter(Boolean).join(' · ');
        setText(goal, g); setAttr(goal, 'title', [p.goal, p.next_step && 'Next: ' + p.next_step, p.status_note].filter(Boolean).join('\n'));
      }
      tabs.sync(projectAgents(pid));
      const withLimits = projectAgents(pid).filter((a) => a.provider !== 'shell');
      limCards.sync(withLimits); limEmpty.hidden = withLimits.length > 0;
      const before = picks.slice(0, count).join(',');
      fillPicks();
      if (picks.slice(0, count).join(',') !== before) { layout(); save(); }
      panes.forEach((pn) => pn.update());
      paint();
    },
    select(aid) {
      if (aid) {
        const j = picks.slice(0, count).indexOf(aid);
        if (j >= 0) focus = j; else picks[focus] = aid;
        save();
      }
      layout();
      syncReplaceUrl();
      setTimeout(() => panes[focus].focus(), 0);
    },
    destroy() { clearTimeout(srcTimer); clearTimeout(shTimer); showShare = false; panes.forEach((p) => p.destroy()); },
  };
  if (initialAgent) { const j = picks.slice(0, count).indexOf(initialAgent); if (j >= 0) focus = j; else picks[focus] = initialAgent; }
  window.__sbPanes = { get count() { return count; }, get focus() { return focus; }, picks, setCount, pick, setFocus };   // test hook
  loadSources();
  if (showShare) loadShared();
  return v;
}

/* ---------- restart the hub (owner only: agents never restart it on their own) ---------- */
let restarting = false;
async function restartHub() {
  if (restarting) return;
  if (!confirm('Restart the Switchboard hub now? Agent terminals keep running; this page reloads when it is back.')) return;
  const before = S.code && S.code.started_at;
  restarting = true;
  $('#stale-banner').hidden = true;
  try {
    await api('POST', '/restart', {});
  } catch (err) { restarting = false; toast(err.message, 'err'); return; }
  toast('Restarting Switchboard…');
  for (let i = 0; i < 120; i++) {
    await new Promise((r) => setTimeout(r, 500));
    try {
      const ov = await (await fetch('/api/overview', { cache: 'no-store' })).json();
      if (ov.code && ov.code.started_at !== before) { location.reload(); return; }
    } catch (e) { /* down while it restarts */ }
  }
  restarting = false;
  toast('Switchboard did not come back within a minute; it may need a look (journalctl --user -u switchboard)', 'err');
}
$('#tb-restart').addEventListener('click', restartHub);
$('#stale-restart').addEventListener('click', restartHub);

/* ---------- phone: pair exactly one phone through the gate (only from this computer; the gate refuses /api/phone) ---------- */
function phoneDialog() {
  const status = h('p', { class: 'ph-status' });
  const addr = h('input', { type: 'url', placeholder: 'https://sb.example.com', 'aria-label': 'Phone address', autocomplete: 'off' });
  const addrRow = h('label', { class: 'ph-addr' }, h('span', { text: 'Address the phone opens' }), addr);
  const pairBtn = h('button', { class: 'btn primary', type: 'button', text: 'Pair a phone' });
  const unpairBtn = h('button', { class: 'btn', type: 'button', text: 'Unpair' });
  const qr = h('div', { class: 'ph-qr', hidden: true });
  const qrNote = h('p', { class: 'ph-note', hidden: true });
  const closeBtn = h('button', { class: 'pbtn', type: 'button', text: '✕', title: 'Close' });
  const dlg = h('dialog', { class: 'ph-dlg', 'aria-label': 'Phone' },
    h('div', { class: 'ph-h' }, h('strong', { text: 'Phone' }), h('span', { class: 'spacer' }), closeBtn),
    status, addrRow, qr, qrNote, h('div', { class: 'ph-actions' }, pairBtn, unpairBtn));
  let countdown = null;
  const stopQr = () => { clearInterval(countdown); qr.hidden = true; qrNote.hidden = true; qr.replaceChildren(); };
  async function refresh() {
    const st = await api('GET', '/phone');
    setText(status, st.paired ? `Paired ${ago(st.paired_at)} · ${st.sessions ? 'signed in' : 'not signed in now'}. Pairing again replaces it.` : 'No phone is paired.');
    setAttr(status, 'title', st.browser || '');
    addr.value = addr.value || st.origin || '';
    addrRow.hidden = !!st.origin && !addrRow.dataset.edit;
    setText(pairBtn, st.paired ? 'Pair a new phone' : 'Pair a phone');
    unpairBtn.hidden = !st.paired;
    return st;
  }
  status.addEventListener('dblclick', () => { addrRow.dataset.edit = '1'; addrRow.hidden = false; });
  pairBtn.addEventListener('click', async () => {
    try {
      const st = await api('POST', '/phone/pair', addrRow.hidden ? {} : { origin: addr.value.trim() });
      const q = window.qrcode(0, 'M'); q.addData(st.url); q.make();
      qr.innerHTML = q.createSvgTag({ cellSize: 6, margin: 4, scalable: true });
      qr.hidden = false; qrNote.hidden = false; addrRow.hidden = true; delete addrRow.dataset.edit;
      const end = Date.now() + st.expires_in * 1000;
      const tick = () => {
        const s = Math.max(0, Math.round((end - Date.now()) / 1000));
        setText(qrNote, s ? `Scan with the phone's camera within ${Math.floor(s / 60)}:${String(s % 60).padStart(2, '0')}. The code works once. On iPhone, then tap Share → Add to Home Screen and open it from there (Safari clears sites you don't visit for a week).` : 'This code expired. Make a new one.');
        if (!s) { clearInterval(countdown); qr.replaceChildren(); }
      };
      clearInterval(countdown); tick(); countdown = setInterval(tick, 1000);
      const was = st.paired_at;
      const wait = setInterval(async () => {  // close the code once the phone has paired
        if (!dlg.open) return clearInterval(wait);
        const now2 = await refresh().catch(() => null);
        if (now2 && now2.paired && now2.paired_at !== was) { clearInterval(wait); stopQr(); toast('Phone paired'); }
      }, 2000);
    } catch (e) { toast(e.message, 'err'); }
  });
  unpairBtn.addEventListener('click', async () => {
    if (!confirm('Unpair the phone? It is signed out at once and needs a new code to get back in.')) return;
    try { await api('POST', '/phone/unpair', {}); stopQr(); await refresh(); toast('Phone unpaired'); } catch (e) { toast(e.message, 'err'); }
  });
  closeBtn.addEventListener('click', () => dlg.close());
  dlg.addEventListener('close', () => { stopQr(); dlg.remove(); });
  document.body.append(dlg);
  refresh().then(() => dlg.showModal()).catch((e) => { dlg.remove(); toast(e.message, 'err'); });
}
api('GET', '/phone').then(() => { $('#tb-phone').hidden = false; }).catch(() => {});  // hidden on the phone itself
$('#tb-phone').addEventListener('click', phoneDialog);

/* ---------- routing + polling ---------- */
function parseRoute() {
  const m = location.hash.match(/^#\/p\/([^/]+)(?:\/([^/]+))?/);
  return m ? { pid: decodeURIComponent(m[1]), aid: m[2] ? decodeURIComponent(m[2]) : null } : { pid: null };
}
function route() {
  const r = parseRoute();
  const app = $('#app');
  if (r.pid && S.view && S.view.pid === r.pid) { S.view.select(r.aid); return; }
  if (S.view) S.view.destroy();
  if (location.hash === '#/setup') {
    S.view = setupView();
    app.className = 'app';
  } else if (r.pid) {
    S.view = projectView(r.pid, r.aid);
    S.view.pid = r.pid;
    app.className = 'app full';
  } else {
    S.view = overviewView();
    app.className = 'app';
  }
  app.replaceChildren(S.view.el);
  if (S.ov) { S.view.update(); if (r.pid) S.view.select(null); }
  document.title = (S.view.title ? S.view.title + ' · ' : '') + 'Switchboard';
}

function setOnline(on) {
  S.online = on;
  const c = $('#conn');
  c.classList.toggle('off', !on);
  setText(c.lastElementChild, on ? 'live' : 'hub unreachable');
}
let tickTimer = null, ticking = false, firstLoad = true;
async function tick() {
  clearTimeout(tickTimer);
  if (ticking) { tickTimer = setTimeout(tick, 300); return; }
  ticking = true;
  try {
    const ov = await api('GET', '/overview');
    S.ov = ov;
    if (ov.ts) clockSkew = parseTime(ov.ts) - Date.now();
    // Something (POST /api/focus) asks to open an agent: follow fresh requests only (never jump on a reload).
    if (ov.focus && ov.focus.ts !== S.focusTs) {
      S.focusTs = ov.focus.ts;
      if (Date.now() + clockSkew - parseTime(ov.focus.ts) < 15000) location.hash = '#/p/' + enc(ov.focus.project) + '/' + enc(ov.focus.agent);
    }
    const router = ov.router || 'desk';
    const isDesk = (a) => a.role === 'desk' || a.id === router;
    S.desk = (ov.agents || []).find(isDesk) || null;
    S.agents = new Map((ov.agents || []).filter((a) => !isDesk(a)).map((a) => [a.id, a]));
    S.projects = new Map((ov.projects || []).map((p) => [p.id, p]));
    S.accounts = ov.accounts || [];
    syncModelLists();
    if (ov.setup_needed && firstLoad && location.hash !== '#/setup') location.hash = '#/setup';
    setOnline(true);
    const banner = $('#stale-banner');
    const stale = !ov.code ? 'Switchboard is running older code than this page. Restart to update.'
      : ov.code.stale ? `New Switchboard code is ready (${ov.code.disk}; running ${ov.code.running}). Restart when it suits you; terminals keep running.` : '';
    banner.hidden = !stale || restarting;
    setText($('#stale-text'), stale);
    S.code = ov.code;
    const agents = [...S.agents.values()];
    setText($('#tb-sum'), `${agents.filter((a) => a.running).length} running · ${agents.filter((a) => a.state === 'busy').length} working`);
    if (S.view) {
      S.view.update();
      if (firstLoad && S.view.pid) S.view.select(null);
      document.title = (S.view.title ? S.view.title + ' · ' : '') + 'Switchboard';
    }
    if (firstLoad) CHAT.poll();
    firstLoad = false;
  } catch (e) {
    setOnline(false);
  } finally {
    ticking = false;
    tickTimer = setTimeout(tick, 3000);
  }
}

/* model suggestion lists (shared by every model input): one per CLI kind, one per API account */
for (const [kind, list] of Object.entries(MODEL_HINTS)) document.body.append(h('datalist', { id: 'models-' + kind }, list.map((m) => h('option', { value: m }))));
function syncModelLists() {
  for (const acc of S.accounts) {
    if (acc.kind !== 'api') continue;
    const id = modelList(acc);
    if (document.getElementById(id)) continue;
    document.body.append(h('datalist', { id }, (acc.models || [acc.model]).filter(Boolean).map((m) => h('option', { value: m }))));
  }
}
$('#tb-shell').addEventListener('click', () => openShell(parseRoute().pid || 'unsorted'));
$('#tb-setup').addEventListener('click', () => { location.hash = '#/setup'; });
window.addEventListener('hashchange', route);
route();
tick();
makeInbox();
