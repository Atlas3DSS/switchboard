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
const MODEL_HINTS = { claude: ['claude-opus-5-5', 'claude-sonnet-5-5', 'claude-haiku-5-5'], codex: [], gemini: [], hermes: [], antigravity: ['gemini-pro-orchestrator-neon', 'gemini-4-neon-high', 'gemini-pro-orchestrator', 'gemini-4-carbon-high', 'gemini-4-hydrogen-high', 'gemini-3.8-flash-medium'] };
const KIND_LABEL = { claude: 'Claude Code', codex: 'Codex', gemini: 'Gemini CLI', hermes: 'Hermes Agent', antigravity: 'Antigravity', api: 'built-in agent', shell: 'shell' };
const AGENT_CLIS = ['claude', 'codex', 'gemini', 'hermes', 'antigravity'];
/* why an agent's queue is not moving, and for how long (paused, a choice open, a turn running, ...) */
const queueWhy = (a) => `${a.paused ? 'paused' : a.queue_reason || 'waiting to type'}${a.queued_since ? ', oldest ' + ago(a.queued_since) : ''}`;  // interactive agent CLIs (switchboard/discovery.py AGENT_CLIS)
const KIND_NAME = { claude: 'Claude', codex: 'Codex', gemini: 'Gemini', hermes: 'Hermes', antigravity: 'Antigravity', api: 'built-in' };
const LIMIT_LABEL = { session: 'Session (5h)', weekly: 'Weekly', fable: 'Fable weekly', '5h': 'Session (5h)', wk: 'Weekly', Fable: 'Fable weekly' };
const STATE_WORD = { busy: 'working', idle: 'idle', starting: 'starting', stopped: 'stopped' };

const agentName = (a) => (a && ((a.title || a.id) + (a.ghost ? ' · ghost' : ''))) || '';
const isFable = (a) => (a.is_fable != null ? !!a.is_fable : /fable/i.test(a.model || ''));
const agentHref = (a) => `#/p/${enc(a.project || '')}/${enc(a.id)}`;
const projName = (pid) => { const p = S.projects.get(pid); return (p && p.name) || pid || ''; };
function projectAgents(pid) {
  const p = S.projects.get(pid);
  const order = (p && p.agent_ids) || [];
  return [...S.agents.values()].filter((a) => a.project === pid)
    .sort((x, y) => { const i = order.indexOf(x.id), j = order.indexOf(y.id); return (i < 0 ? 1e9 : i) - (j < 0 ? 1e9 : j); });
}
function normLimit(l) {
  const used = l.used != null ? l.used : (l.left != null ? 100 - l.left : null);
  return { key: l.key || l.label, label: LIMIT_LABEL[l.label] || l.label || LIMIT_LABEL[l.key] || l.key, used, resets: l.resets };
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
const agentState = (a) => a.paused ? 'paused' : (STATE_WORD[a.state] || a.state || '');
function setDot(dot, a) { setCls(dot, 'dot s-' + (a.paused ? 'paused' : a.state || 'stopped')); setAttr(dot, 'title', agentState(a)); }
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
  const email = h('span', { class: 'pool-email' });
  const note = h('span', { class: 'pool-note' });
  const limBox = h('div', { class: 'lims' });
  const lims = keyedList(limBox, (l) => l.key, limitRow);
  const cu = h('div', { class: 'pool-cu' });           // Colab compute units (Antigravity account only)
  const cuAt = h('div', { class: 'pool-cu-at' });     // when that balance last moved
  const agBox = h('div', { class: 'pool-ags' });
  const ags = keyedList(agBox, (id) => id, agentLink);
  const none = h('span', { class: 'muted small', text: 'no agents' });
  const gather = h('button', { class: 'btn small ghost pool-gather', type: 'button', text: 'Move agents here' });
  let cur = null;
  guarded(gather, async () => {
    const others = [...S.agents.values()].filter((a) => a.provider === cur.kind && a.account !== cur.name);
    if (!confirm(`Move all ${others.length} ${KIND_NAME[cur.kind] || cur.kind} agents on other accounts to ${cur.name}, and start new ones on it? Each keeps its conversation; one that is working moves when it is next idle.`)) return;
    const r = await api('POST', `/accounts/${enc(cur.name)}/gather`, {});
    toast(`${cur.name}: ${r.moved.length} moved` + (r.waiting.length ? `, ${r.waiting.length} moving as they go idle` : ''));
  });
  const el = h('section', { class: 'pool' }, h('div', { class: 'pool-h' }, name, kind, email, note, gather), limBox, cu, cuAt, h('div', { class: 'pool-ag' }, agBox, none));
  return {
    el,
    update(acc) {
      cur = acc;
      gather.hidden = ![...S.agents.values()].some((a) => a.provider === acc.kind && a.account !== acc.name);
      setAttr(gather, 'title', `Every ${KIND_NAME[acc.kind] || acc.kind} agent moves to ${acc.name}, which also becomes the account new agents start on`);
      setText(name, acc.name); setText(kind, KIND_LABEL[acc.kind] || acc.kind);
      const how = [{ key: 'API key', none: 'no key' }[acc.auth] || 'login', acc.model].filter(Boolean).join(' · ');
      setText(email, how); setAttr(email, 'title', acc.label || how);
      lims.sync((acc.limits || []).map(normLimit));
      const c = acc.colab;
      cu.hidden = !c;
      cuAt.hidden = !(c && c.changed_at);
      if (c && c.changed_at) {
        const d = new Date(c.changed_at);
        const day = d.toDateString() === new Date().toDateString() ? '' : d.toLocaleDateString(undefined, { weekday: 'short' }) + ' ';
        setText(cuAt, `balance last changed ${day}${d.toLocaleTimeString(undefined, { hour: '2-digit', minute: '2-digit' })} (${ago(c.changed_at)})`);
        setAttr(cuAt, 'title', 'Google moves the balance about every five minutes while a session runs');
      }
      if (c) {
        const run = c.runway_h == null ? '' : c.runway_h >= 48 ? ` · ${(c.runway_h / 24).toFixed(1)} days left` : ` · ${c.runway_h} h left`;
        const fmt = (x) => Number(x).toLocaleString(undefined, { maximumFractionDigits: 1 });
        setText(cu, `Colab: ${fmt(c.balance)} CU` + (c.rate > 0 ? ` · burning ${fmt(c.rate)}/h on ${c.assignments} session${c.assignments === 1 ? '' : 's'}${run}` : ' · nothing running'));
        setAttr(cu, 'title', ((c.sessions || []).map((x) => `${x.name} (${x.hardware})`).join('\n') || 'no active sessions') + `\nread ${ago(c.read_at)}`);
      }
      const n = acc.error ? 'read failed' : acc.stale ? 'stale' : '';
      setText(note, n);
      setAttr(note, 'title', (acc.error || '') + (acc.read_at ? ` (last read ${ago(acc.read_at)})` : ''));
      setCls(el, 'pool' + (acc.error || acc.stale ? ' stale' : ''));
      const ids = accountAgentIds(acc);
      ags.sync(ids); none.hidden = ids.length > 0;
    },
  };
}

function isLocalHost() {
  return location.hostname === '127.0.0.1' || location.hostname === 'localhost' || location.hostname === '[::1]';
}

/* Open a web URL or local file/folder path without xterm's broken "dangerous link" confirm + empty window.open().
   On localhost, routes through POST /api/open so http(s) loads in the desktop browser,
   directories open in the file manager, and files/partial paths resolve against the agent's cwd and open cleanly. */
async function openLink(raw, aid) {
  const target = String(raw || '').trim();
  if (!target) return;
  const isWeb = /^https?:\/\//i.test(target);
  if (isWeb && !isLocalHost()) {
    window.open(target, '_blank', 'noopener');
    return;
  }
  if (!isWeb && !isLocalHost()) {
    toast('Local paths can only be opened on the machine the hub runs on', 'err');
    return;
  }
  try {
    const r = await api('POST', '/open', { target, agent: aid || null });
    if (r && r.kind === 'dir') toast('Opened folder: ' + r.path);
    else if (r && r.kind === 'file') toast('Opened: ' + r.path);
    else if (r && r.kind === 'url') toast('Opened in browser');
  } catch (err) {
    toast('Could not open: ' + err.message, 'err');
  }
}

/* Wrapped-line-aware xterm link provider: detects full http(s) URLs, file:// URIs, absolute/home paths,
   and relative/partial paths (resolved server-side against the agent's cwd). */
const TERM_URL_RE = /https?:\/\/[^\s<>"'`\])]+/g;
const TERM_PATH_RE = /(?:file:\/\/[^\s<>"'`\])]+|~\/[A-Za-z0-9._/@%+\-]+(?:#L\d+(?:-L?\d+)?|:\d+(?::\d+)?)?|(?:\/[A-Za-z0-9._@%+\-]+){2,}\/?(?:#L\d+(?:-L?\d+)?|:\d+(?::\d+)?)?|(?:\.\.?\/)?(?:[A-Za-z0-9._@+\-]+\/)+[A-Za-z0-9._@+\-]+\/?(?:#L\d+(?:-L?\d+)?|:\d+(?::\d+)?)?|\b[A-Za-z0-9_.\-]+\.(?:py|js|mjs|cjs|ts|tsx|jsx|md|json|jsonl|ya?ml|toml|sh|css|html?|xml|svg|sql|csv|txt|pdf|png|jpe?g|webp|avif|gif|mp4|mkv|mov|webm|mp3|wav|zip|tar|gz|7z|rar|patch|diff|cpp|cc|c|h|hpp|rs|go|service|env|ini|cfg|conf|log|stl|lys|lyt|kys|blend|obj|fbx|glb|gltf|ipynb|tex|xlsx|docx|pptx)\b(?:#L\d+(?:-L?\d+)?|:\d+(?::\d+)?)?)/gi;

function trimLinkEnd(s) {
  let out = s.replace(/[.,;:!?'"`]+$/, '');
  while (out.endsWith(')') && (out.split('(').length < out.split(')').length)) out = out.slice(0, -1);
  while (out.endsWith(']') && (out.split('[').length < out.split(']').length)) out = out.slice(0, -1);
  return out;
}

function isPlausibleLocalPath(s) {
  if (/^file:\/\//i.test(s) || s.startsWith('~/') || s.startsWith('./') || s.startsWith('../')) return true;
  if (s.startsWith('/')) {
    // Reject obvious URL paths or tiny fractions like /a/b unless multi-segment or known root/extension
    return /[A-Za-z]/.test(s) && (s.length >= 5 || /\.[A-Za-z0-9]{1,8}(?:$|:|#)/.test(s));
  }
  if (s.includes('/')) {
    // Reject dates/fractions (11/24, 15/32, 2026/11/24) and short A/B pairs (w/o, N/A, on/off, pass/fail)
    if (!/[A-Za-z]/.test(s)) return false;
    if (/^[A-Za-z0-9]{1,4}\/[A-Za-z0-9]{1,4}$/.test(s) && !s.includes('.')) return false;
    if (/^(?:and|or|yes|no|on|off|true|false|pass|fail|read|write|input|output|before|after|dev|test|train|idle|busy|good|bad)\//i.test(s) && !s.includes('.')) return false;
    return true;
  }
  // Bare filename with known extension: require at least one letter in basename
  return /[A-Za-z]/.test(s.split('.')[0]);
}

function registerTerminalLinks(term, getAid) {
  term.registerLinkProvider({
    provideLinks(bufferLineNumber, callback) {
      const buf = term.buffer.active;
      let startIdx = bufferLineNumber - 1;
      while (startIdx > 0 && buf.getLine(startIdx)?.isWrapped) startIdx--;
      const rows = [];
      let idx = startIdx;
      while (idx < buf.length) {
        const line = buf.getLine(idx);
        if (!line) break;
        const hasNext = idx + 1 < buf.length && !!buf.getLine(idx + 1)?.isWrapped;
        rows.push({ y: idx + 1, text: line.translateToString(!hasNext) });
        if (!hasNext) break;
        idx++;
      }
      if (!rows.length) { callback(undefined); return; }
      const full = rows.map((r) => r.text).join('');
      const offsets = [];
      let acc = 0;
      for (const r of rows) { offsets.push({ y: r.y, start: acc, end: acc + r.text.length }); acc += r.text.length; }
      const posOf = (off) => {
        for (const o of offsets) {
          if (off < o.end) return { x: off - o.start + 1, y: o.y };
        }
        const last = offsets[offsets.length - 1];
        return { x: Math.max(1, last.end - last.start), y: last.y };
      };
      const matches = [];
      const taken = [];
      const overlaps = (a, b) => taken.some(([s, e]) => a < e && b > s);
      TERM_URL_RE.lastIndex = 0;
      let m;
      while ((m = TERM_URL_RE.exec(full))) {
        const text = trimLinkEnd(m[0]);
        if (!text) continue;
        const a = m.index, b = a + text.length;
        taken.push([a, b]);
        matches.push({ a, b, text });
      }
      TERM_PATH_RE.lastIndex = 0;
      while ((m = TERM_PATH_RE.exec(full))) {
        const text = trimLinkEnd(m[0]);
        if (!text) continue;
        const a = m.index, b = a + text.length;
        if (overlaps(a, b)) continue;
        if (a > 0 && /[A-Za-z0-9_@:/]/.test(full[a - 1]) && !text.startsWith('file://')) continue;
        if (!isPlausibleLocalPath(text)) continue;
        taken.push([a, b]);
        matches.push({ a, b, text });
      }
      const links = [];
      for (const hit of matches) {
        const st = posOf(hit.a);
        const en = posOf(hit.b - 1);
        if (en.y < bufferLineNumber || st.y > bufferLineNumber) continue;
        links.push({
          range: { start: st, end: en },
          text: hit.text,
          activate: () => openLink(hit.text, getAid()),
        });
      }
      callback(links.length ? links : undefined);
    },
  });
}

/* ---------- Switchboard chat (the desk agent: the only chat window) ---------- */
const OPEN_RE = /\[\[open:([\w.-]+)(?:\/([\w.-]+))?\]\]/g;
function inlineMd(text, aid = null) {
  const out = [];
  const re = /(\*\*[^*\n]+\*\*|`[^`\n]+`|\[[^\]\n]+\]\((?:(?:https?|file):\/\/|\/|~\/|\.\.?\/|(?:[A-Za-z0-9._@+\-]+\/)+|[A-Za-z0-9_.\-]+\.[A-Za-z0-9]{1,8}(?:[:#]|(?=\))))[^)\s]*\)|(?:https?|file):\/\/[^\s<>"'`\])]+)/gi;
  let last = 0, m;
  while ((m = re.exec(text))) {
    if (m.index > last) out.push(text.slice(last, m.index));
    const t = m[0];
    if (t[0] === '`') {
      out.push(h('code', { text: t.slice(1, -1) }));
    } else if (t.startsWith('**')) {
      out.push(h('strong', { text: t.slice(2, -2) }));
    } else if (t[0] === '[') {
      const lm = t.match(/^\[([^\]\n]+)\]\(([^)\s]+)\)$/);
      const a = h('a', { href: lm[2], text: lm[1], class: 'md-link' });
      a.addEventListener('click', (ev) => { ev.preventDefault(); openLink(lm[2], aid); });
      out.push(a);
    } else {
      const url = trimLinkEnd(t);
      const a = h('a', { href: url, text: url, class: 'md-link' });
      a.addEventListener('click', (ev) => { ev.preventDefault(); openLink(url, aid); });
      out.push(a);
      if (url.length < t.length) out.push(t.slice(url.length));
    }
    last = m.index + t.length;
  }
  if (last < text.length) out.push(text.slice(last));
  return out;
}
function openTarget(project, agent) {
  if (!project) return;
  location.hash = agent ? `#/p/${enc(project)}/${enc(agent)}` : `#/p/${enc(project)}`;
}
/* text -> element: markdown (headings, bold, code, lists, tables, blockquotes, fenced code, links) + [[open:…]] tokens */
function renderChatText(text, aid = null) {
  const opens = [];
  text = String(text || '').replace(OPEN_RE, (_, p, a) => { opens.push({ p, a }); return ''; }).replace(/[ \t]+$/gm, '').trim();
  const box = h('div', { class: 'cm-text' });
  const lines = text.split('\n');
  let list = null, para = null, quote = null;
  const splitRow = (s) => s.trim().replace(/^\|/, '').replace(/\|$/, '').split('|').map((c) => c.trim());
  for (let i = 0; i < lines.length; i++) {
    const ln = lines[i];
    if (/^```/.test(ln)) {
      const code = [];
      for (i++; i < lines.length && !/^```/.test(lines[i]); i++) code.push(lines[i]);
      box.append(h('pre', { text: code.join('\n') })); list = para = quote = null; continue;
    }
    if (/^\s*---+\s*$/.test(ln)) {
      box.append(h('hr')); list = para = quote = null; continue;
    }
    const hd = ln.match(/^(#{1,4})\s+(.*)$/);
    if (hd) {
      box.append(h('div', { class: `md-h md-h${hd[1].length}` }, inlineMd(hd[2], aid)));
      list = para = quote = null; continue;
    }
    if (ln.includes('|') && i + 1 < lines.length && /^\s*\|?[\s:-]+\|[\s|:-]*$/.test(lines[i + 1])) {
      const thead = h('thead', null, h('tr', null, splitRow(ln).map((c) => h('th', null, inlineMd(c, aid)))));
      const tbody = h('tbody');
      for (i += 2; i < lines.length && lines[i].includes('|') && lines[i].trim(); i++) {
        tbody.append(h('tr', null, splitRow(lines[i]).map((c) => h('td', null, inlineMd(c, aid)))));
      }
      i--;
      box.append(h('table', { class: 'md-tbl' }, thead, tbody));
      list = para = quote = null; continue;
    }
    const bq = ln.match(/^>\s?(.*)$/);
    if (bq) {
      if (!quote) { quote = h('blockquote', { class: 'md-q' }); box.append(quote); }
      else quote.append(h('br'));
      quote.append(...inlineMd(bq[1], aid));
      list = para = null; continue;
    }
    quote = null;
    const li = ln.match(/^\s*(?:[-*•]|\d+[.)])\s+(.*)$/);
    if (li) {
      if (!list) { list = h(/^\s*\d/.test(ln) ? 'ol' : 'ul'); box.append(list); }
      list.append(h('li', null, inlineMd(li[1], aid))); para = null; continue;
    }
    list = null;
    if (!ln.trim()) { para = null; continue; }
    if (para) para.append(h('br'), ...inlineMd(ln, aid));
    else { para = h('p', null, inlineMd(ln, aid)); box.append(para); }
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

// Claude Code pointed at another endpoint: value 'claude:<model>' -> title; discovery.py CLAUDE_ENDPOINTS routes the model

/* ---------- new agent form (card and project view) ---------- */
function newAgentForm(getPid, onDone) {
  const fAccount = h('select', { 'aria-label': 'Account' });
  const fModel = h('input', { type: 'text', list: 'models-claude', placeholder: 'default model', autocomplete: 'off', spellcheck: 'false' });
  const fCwd = h('input', { type: 'text', placeholder: 'the project\'s folder', autocomplete: 'off', spellcheck: 'false' });
  const fTitle = h('input', { type: 'text', placeholder: 'short title', autocomplete: 'off' });
  const fSid = h('input', { type: 'text', placeholder: 'optional: session / thread id to resume', autocomplete: 'off', spellcheck: 'false' });
  /* ghost: sealed off inside the project (switchboard/ghost.py); erased when removed, only its file edits stay */
  const fGhost = h('input', { type: 'checkbox' });
  const ghostLab = h('label', { class: 'wide ghost-opt', title: 'Sealed off inside the project: it knows only the task you give it. It cannot read or write the project\'s memory, notes, work queue or other agents, and no agent of any project can see it. When you remove it, its conversation and every record of it is erased; only its edits to files stay.' },
    fGhost, h('span', { text: 'Ghost: sealed to its own task, erased when removed (only its file edits stay)' }));
  const ok = h('button', { class: 'btn primary', type: 'submit', text: 'Add agent' });
  const cancel = h('button', { class: 'btn ghost', type: 'button', text: 'Cancel' });
  const lab = (t, n, cls) => h('label', { class: cls || '' }, h('span', { text: t }), n);
  const el = h('form', { class: 'newagent', hidden: true },
    lab('Account', fAccount, 'wide'), lab('Model', fModel), lab('Title', fTitle),
    lab('Working directory', fCwd, 'wide'), lab('Resume session', fSid, 'wide'), ghostLab,
    h('div', { class: 'wide row-end' }, h('span', { class: 'muted small', text: 'Not launched until you open it.' }), cancel, ok));
  el.addEventListener('click', (e) => e.stopPropagation());
  function refresh() {
    const cur = fAccount.value;
    fAccount.replaceChildren(...S.accounts.map((a) => h('option', { value: a.name, text: `${a.name} — ${accountHint(a)}` })));
    if (S.accounts.some((a) => a.name === cur)) fAccount.value = cur;
    const acc = S.accounts.find((a) => a.name === fAccount.value);
    fModel.setAttribute('list', modelList(acc));
    fModel.placeholder = acc && acc.kind === 'api' && acc.model ? acc.model : 'default model';
    ghostLab.hidden = !acc || !['claude', 'codex', 'antigravity'].includes(acc.kind);
    if (ghostLab.hidden) fGhost.checked = false;
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
    if (fGhost.checked) body.ghost = true;
    ok.disabled = true;
    try {
      const a = await api('POST', '/agents', body);
      toast(`Added ${agentName(a)} — open it to launch`);
      fTitle.value = ''; fSid.value = ''; fGhost.checked = false; el.hidden = true;
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

/* Pending messages are separate from the terminal until delivery. */
function openQueue(aid) {
  document.getElementById('agent-queue')?.close();
  const title = h('strong', { text: 'Pending messages' });
  const reason = h('p');
  const list = h('div');
  const close = h('button', { class: 'pbtn', text: 'Close', type: 'button' });
  const resume = h('button', { class: 'pbtn', text: 'Resume queue', type: 'button', hidden: true });
  const dialog = h('dialog', { id: 'agent-queue', class: 'queue-dialog', 'aria-label': 'Pending messages' },
    h('div', { class: 'queue-head' }, title, h('span', { class: 'spacer' }), resume, close), reason, list);
  let timer, stopped = false, busy = false;
  close.onclick = () => dialog.close();
  dialog.addEventListener('close', () => { stopped = true; clearTimeout(timer); dialog.remove(); });
  async function refresh() {
    clearTimeout(timer);
    if (stopped || busy) return;
    busy = true;
    try {
      const data = await api('GET', `/agents/${enc(aid)}/queue`);
      if (stopped) return;
      const a = data.agent, ms = data.messages;
      setText(title, `${agentName(a)} · ${ms.length} pending`);
      setText(reason, ms.length ? queueWhy(a) + '. These messages have not reached the terminal yet.' : 'No pending messages. Delivered messages appear in the terminal.');
      resume.hidden = !a.paused;
      const sig = JSON.stringify(ms);
      if (list.dataset.sig !== sig) {
        list.dataset.sig = sig;
        list.replaceChildren(...ms.map(m => {
          const cancel = h('button', { class: 'pbtn', text: 'Cancel message', type: 'button' });
          guarded(cancel, async () => {
            try { await api('POST', `/messages/${enc(m.id)}/cancel`, {}); }
            finally { await refresh(); tick(); }
          });
          return h('article', { class: 'queue-message' },
            h('div', { class: 'queue-head' }, h('strong', { text: `From ${m.from || 'owner'}` }),
              h('span', { text: ago(m.created_at), title: absTime(m.created_at) }), h('span', { class: 'spacer' }), cancel),
            h('p', { class: 'muted', text: m.error || 'Waiting for delivery' }),
            h('div', { class: 'queue-body', text: m.body }),
            (m.attachments || []).map(x => h('a', { href: x.url, target: '_blank', rel: 'noopener', text: 'Open attachment' })));
        }));
      }
    } catch (e) { if (!stopped) setText(reason, e.message); }
    finally { busy = false; if (!stopped) timer = setTimeout(refresh, 2000); }
  }
  guarded(resume, async () => { await api('PATCH', `/agents/${enc(aid)}`, { paused: false }); await refresh(); tick(); });
  document.body.append(dialog); dialog.showModal(); refresh();
}

/* ---------- marks: who is the project's lead, illustrator, architect ---------- */
const MARKS = [
  ['lead', 'Lead', 'messages written to this project land on this agent', '<path d="M2 13h12M2.6 10.5 1.6 4.5 5 7.5l3-5 3 5 3.4-3-1 6z"/>'],
  ['illustrator', 'Illustration', 'this agent makes the project\'s illustrations; it keeps out of the other seats and a fold leaves it', '<path d="M14 2 7.5 8.5M9.5 4.5l2 2M6.5 9c-1.6 0-2.6 1-2.6 2.4 0 1-.8 1.6-2 1.6 1 .9 2 1 3 1 1.9 0 3.1-1.2 3.1-3z"/>'],
  ['architect', 'Architect', 'the project\'s general architect: design questions and reviews go here', '<circle cx="8" cy="3" r="1.4"/><path d="M7.4 4.3 4.2 14M8.6 4.3 11.8 14M5.4 10.4h5.2"/>'],
];
/* toggle=true: three buttons that pin or unpin each mark on the agent; false: only the marks it carries, read-only */
function markSet(toggle) {
  const el = h('span', { class: 'mks' });
  const btns = {};
  let cur = null;
  for (const [role, label, , path] of MARKS) {
    const b = h(toggle ? 'button' : 'span', { class: 'mk off', type: toggle ? 'button' : null });
    b.innerHTML = `<svg viewBox="0 0 16 16" width="14" height="14" fill="none" stroke="currentColor" stroke-width="1.4" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">${path}</svg>`;
    if (toggle) b.addEventListener('click', async (e) => {
      e.preventDefault(); e.stopPropagation();
      if (!cur) return;
      const on = (cur.marks || {})[role] !== 'on';
      try { await api('POST', `/projects/${enc(cur.project)}/marks`, { role, agent: cur.id, on }); toast(`${label} ${on ? 'set to' : 'unpinned from'} ${agentName(cur)}`); }
      catch (err) { toast(err.message, 'err'); }
      tick();
    });
    btns[role] = b; el.append(b);
  }
  return {
    el,
    update(a) {
      cur = a && a.id ? a : null;
      for (const [role, label, what] of MARKS) {
        const b = btns[role], m = (cur && cur.marks || {})[role] || 'off';
        setCls(b, 'mk ' + m);
        if (!toggle) b.hidden = m === 'off';
        else setAttr(b, 'aria-pressed', m === 'on' ? 'true' : 'false');
        setAttr(b, 'title', `${label}: ${what}. ` + (m === 'on' ? 'Pinned by you.' : m === 'auto' ? 'Chosen by Switchboard because nobody is pinned.' : 'Not this agent.')
          + (toggle ? (m === 'on' ? ' Click to unpin.' : ' Click to pin it here.') : ''));
      }
      el.hidden = !cur || (!toggle && !Object.keys(cur.marks || {}).length);
    },
  };
}
/* rename in place: an input takes the anchor's spot; Enter saves the title (the id other agents write to stays) */
function renameInline(anchor, a) {
  if (!a || anchor.previousSibling && anchor.previousSibling.classList && anchor.previousSibling.classList.contains('rn-in')) return;
  const inp = h('input', { class: 'rn-in', type: 'text', value: a.title || a.id, maxlength: '60', 'aria-label': 'Panel name' });
  let done = false;
  const close = async (save) => {
    if (done) return; done = true;
    const v = inp.value.trim();
    inp.remove(); anchor.hidden = false;
    if (!save || v === (a.title || a.id)) return;
    try { await api('PATCH', `/agents/${enc(a.id)}`, { title: v }); } catch (err) { toast(err.message, 'err'); }
    tick();
  };
  inp.addEventListener('keydown', (e) => { e.stopPropagation(); if (e.key === 'Enter') { e.preventDefault(); close(true); } else if (e.key === 'Escape') close(false); });
  for (const ev of ['click', 'mousedown', 'dblclick']) inp.addEventListener(ev, (e) => { e.stopPropagation(); if (ev === 'click') e.preventDefault(); });
  inp.addEventListener('dragstart', (e) => { e.preventDefault(); e.stopPropagation(); });
  inp.addEventListener('blur', () => close(true));
  anchor.hidden = true; anchor.before(inp); inp.focus(); inp.select();
}

/* ---------- overview: project cards ---------- */
function agentRow() {
  const dot = stateDot();
  const title = h('span', { class: 'ar-title' });
  const id = h('span', { class: 'ar-id' });
  const acct = h('span', { class: 'acct' });
  const mt = modelTag();
  const st = h('span', { class: 'ar-state' });
  let aid;
  const q = h('span', { class: 'badge q', role: 'button', tabindex: '0' });
  q.addEventListener('click', e => { e.preventDefault(); e.stopPropagation(); openQueue(aid); });
  q.addEventListener('keydown', e => { if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); e.stopPropagation(); openQueue(aid); } });
  const blk = h('span', { class: 'badge warn', text: 'blocked' });
  const mks = markSet(false);
  const el = h('a', { class: 'arow' }, dot, title, mks.el, id, h('span', { class: 'spacer' }), q, blk, acct, mt, st);
  el.addEventListener('click', (e) => e.stopPropagation());
  return {
    el,
    update(a) {
      aid = a.id;
      setAttr(el, 'href', agentHref(a));
      setDot(dot, a); mks.update(a);
      setText(title, agentName(a)); setText(id, a.title && a.title !== a.id ? a.id : ''); setAttr(title, 'title', a.last_text ? a.last_text.slice(-300) : '');
      setText(acct, a.account); setAttr(acct, 'title', 'Account');
      setModelTag(mt, a);
      setText(st, a.switching ? '→ ' + a.switching.to : agentState(a));
      setText(q, a.queued ? a.queued + (a.paused ? ' paused' : ' queued') : ''); q.hidden = !a.queued;
      setAttr(q, 'title', a.queued ? 'View pending messages: ' + queueWhy(a) : '');
      setAttr(q, 'aria-label', `View ${a.queued || 0} pending messages for ${agentName(a)}`);
      blk.hidden = !a.launch_block; setAttr(blk, 'title', a.launch_block || '');
    },
  };
}
const PIN_SVG = '<svg viewBox="0 0 16 16" width="13" height="13" fill="none" stroke="currentColor" stroke-width="1.4" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M5.5 2.5h5l-.8 4.2 2.3 2v1.3H4V8.7l2.3-2-.8-4.2zM8 10v4"/></svg>';
async function toggleProjectPin(pid, on) {
  const p = S.projects.get(pid);
  const res = await api('PATCH', `/projects/${enc(pid)}`, { pinned: on });
  if (p) p.pinned = !!(res && res.pinned);
  toast(`${on ? 'Pinned' : 'Unpinned'} ${(p && p.name) || pid}`);
  tick();
}
function projectCard(p0) {
  const pid = p0.id;
  const pinBtn = h('button', { class: 'pin-btn', type: 'button', 'aria-pressed': 'false' });
  pinBtn.innerHTML = PIN_SVG;
  pinBtn.hidden = pid === 'unsorted';
  pinBtn.addEventListener('click', async (e) => {
    e.preventDefault(); e.stopPropagation();
    const p = S.projects.get(pid);
    try { await toggleProjectPin(pid, !(p && p.pinned)); } catch (err) { toast(err.message, 'err'); }
  });
  const name = h('a', { class: 'pc-name', href: '#/p/' + enc(pid) });
  const flags = h('span', { class: 'pc-flags' });
  const last = h('span', { class: 'pc-last' });
  const goal = h('div', { class: 'pc-goal' });
  const agBox = h('div', { class: 'pc-agents' });
  const ags = keyedList(agBox, (a) => a.id, agentRow);
  const none = h('div', { class: 'muted small', text: 'No agents yet.' });
  const addBtn = h('button', { class: 'btn ghost small', type: 'button' });  // folds the agents outside the seats
  const nodeBox = h('div', { class: 'pc-nodes' });
  const seatBtn = {};
  for (const [role, short] of [['lead', 'Lead'], ['architect', 'Architect'], ['tasker', 'Tasker'], ['janitor', 'Janitor']]) {
    const b = h('button', { class: 'pc-node', type: 'button' });
    b.addEventListener('click', async (e) => {
      e.stopPropagation();
      const n = b._seat; if (!n) return;
      if (n.state === 'live') { location.hash = `#/p/${enc(pid)}/${enc(n.agent)}`; return; }
      const what = n.agent ? `${n.agent} has been idle ${n.idle_hours ?? '?'} h. Clear its conversation and brief it as ${short}?`
        : `Start a clean ${short} on ${n.provider} ${n.model}?`;
      if (!confirm(what)) return;
      try { const r = await api('POST', `/projects/${enc(pid)}/nodes/${role}/pickup`, {}); toast(`${short}: ${r.action} ${r.agent}`); tick(); location.hash = `#/p/${enc(pid)}/${enc(r.agent)}`; }
      catch (err) { toast(err.message, 'err'); }
    });
    seatBtn[role] = [b, short]; nodeBox.append(b);
  }
  const wikiBtn = h('button', { class: 'pc-node wiki', type: 'button', text: 'Wiki' });
  wikiBtn.addEventListener('click', async (e) => {
    e.stopPropagation();
    try {
      const r = await api('GET', `/projects/${enc(pid)}/wiki?format=md`);
      const close = h('button', { class: 'btn small', type: 'button', text: 'Close' });
      const box = h('div', { class: 'wiki-pop' }, h('div', { class: 'wiki-bar' }, h('b', { text: 'Live wiki: ' + pid }), h('span', { class: 'spacer' }), close), h('pre', { text: r.markdown }));
      close.addEventListener('click', () => box.remove());
      document.body.append(box);
    } catch (err) { toast(err.message, 'err'); }
  });
  nodeBox.append(wikiBtn);
  addBtn.addEventListener('click', async (e) => {
    e.stopPropagation();
    const ex = addBtn._extras || [];
    if (!confirm(`Retire ${ex.join(', ')}? Their conversations go to the project's memory and stay resumable; their open work passes to the lead. An agent in the middle of a turn is left alone.`)) return;
    try { const r = await api('POST', `/projects/${enc(pid)}/nodes/fold`, {}); toast(`folded ${r.folded.length}` + (r.left_busy.length ? `, left busy: ${r.left_busy.join(', ')}` : '')); tick(); }
    catch (err) { toast(err.message, 'err'); }
  });
  const el = h('article', { class: 'pcard' },
    h('div', { class: 'pc-head' }, pinBtn, name, flags, h('span', { class: 'spacer' }), last),
    goal, nodeBox, agBox, none, h('div', { class: 'pc-foot' }, addBtn));
  el.addEventListener('click', (e) => {
    if (e.target.closest('a,button,input,select,textarea,form,label')) return;
    if (getSelection && String(getSelection())) return;
    location.hash = '#/p/' + enc(pid);
  });
  return {
    el,
    update(p) {
      const pinned = !!p.pinned;
      pinBtn.classList.toggle('on', pinned);
      setAttr(pinBtn, 'aria-pressed', pinned ? 'true' : 'false');
      setAttr(pinBtn, 'title', pinned ? 'Pinned to top — click to unpin' : 'Pin project to top');
      setText(name, p.name || p.id);
      setText(flags, [p.paused && 'paused', p.outside_read && 'reads out', p.outside_write && 'writes out', p.stale && 'quiet', p.archived && 'archived'].filter(Boolean).join(' · '));
      setText(last, ago(p.last_activity)); setAttr(last, 'title', absTime(p.last_activity));
      const g = p.next_step ? `Next: ${p.next_step}` : (p.goal || p.status_note || '');
      setText(goal, g); setAttr(goal, 'title', [p.goal, p.next_step && 'Next: ' + p.next_step, p.status_note].filter(Boolean).join('\n'));
      goal.hidden = !g;
      for (const n of p.nodes || []) {
        const [b, short] = seatBtn[n.role] || []; if (!b) continue;
        b._seat = n;
        setText(b, `${short}: ${n.agent || 'empty'}`);
        setCls(b, 'pc-node ' + n.state);
        setAttr(b, 'title', n.agent ? `${n.title}: ${n.agent} on ${n.running_provider}/${n.account} ${n.running_model || ''}, idle ${n.idle_hours ?? '?'} h. ${n.state === 'live' ? 'Open it (its session is kept).' : 'Click to clear and brief it.'}` : `${n.title}: empty. Click to start a clean one on ${n.provider} ${n.model}.`);
      }
      addBtn._extras = p.node_extras || [];
      setText(addBtn, `Fold ${addBtn._extras.length} outside the seats`); addBtn.hidden = !addBtn._extras.length;
      const list = projectAgents(pid);
      ags.sync(list); none.hidden = list.length > 0;
      setCls(el, 'pcard' + (list.some((a) => a.state === 'busy') ? ' busy' : '') + (pinned ? ' pinned' : '') + (p.archived ? ' arch' : ''));
    },
  };
}

/* ---------- master work queue (sb work): the owner, the Switchboard chat and every agent edit the same list ---------- */
const WORK_STATUS = ['todo', 'doing', 'blocked', 'done', 'dropped'];
function workRow(w0) {
  const pri = h('select', { class: 'wq-pri', 'aria-label': 'Priority' }, [1, 2, 3].map((n) => h('option', { value: n, text: 'P' + n })));
  const st = h('select', { class: 'wq-st', 'aria-label': 'Status' }, WORK_STATUS.map((x) => h('option', { value: x, text: x })));
  const title = h('input', { type: 'text', class: 'wq-title', 'aria-label': 'Title', autocomplete: 'off' });
  const proj = h('a', { class: 'wq-proj' });
  const meta = h('span', { class: 'wq-meta' });
  const del = h('button', { class: 'pbtn wq-del', type: 'button', text: '✕', title: 'Delete this item' });
  const detail = h('div', { class: 'wq-detail' });
  const el = h('div', { class: 'wq-row' }, h('div', { class: 'wq-line' }, pri, st, title, proj, meta, del), detail);
  let cur = w0;
  async function save(patch) {
    try { cur = await api('PATCH', '/work/' + cur.id, patch); render(); tick(); } catch (err) { toast(err.message, 'err'); render(); }
  }
  pri.addEventListener('change', () => save({ priority: +pri.value }));
  st.addEventListener('change', () => save({ status: st.value }));
  title.addEventListener('keydown', (e) => {
    if (e.key === 'Enter') { e.preventDefault(); title.blur(); } else if (e.key === 'Escape') { title.value = cur.title; title.blur(); }
  });
  title.addEventListener('blur', () => { const t = title.value.trim(); if (t && t !== cur.title) save({ title: t }); else title.value = cur.title; });
  del.addEventListener('click', async () => {
    if (!confirm(`Delete #${cur.id} "${cur.title}"? (Marking it done or dropped keeps a record.)`)) return;
    try { await api('DELETE', '/work/' + cur.id); tick(); } catch (err) { toast(err.message, 'err'); }
  });
  function render() {
    const w = cur;
    if (document.activeElement !== pri) pri.value = String(w.priority);
    if (document.activeElement !== st) st.value = w.status;
    if (document.activeElement !== title && title.value !== w.title) title.value = w.title;
    setAttr(title, 'title', `#${w.id} · added by ${w.created_by || 'owner'} ${ago(w.created_at)}`);
    setText(proj, w.project ? projName(w.project) : ''); setAttr(proj, 'href', w.project ? '#/p/' + enc(w.project) : '#'); proj.hidden = !w.project;
    setText(meta, [w.agent && '@' + w.agent + (S.agents.has(w.agent) ? '' : ' (gone)'), ago(w.updated_at)].filter(Boolean).join(' · '));
    setText(detail, w.detail || ''); setAttr(detail, 'title', w.detail || ''); detail.hidden = !w.detail;
    setCls(el, `wq-row s-${w.status} p${w.priority}`);
  }
  return { el, update(w) { cur = w; render(); } };
}
function projectOptions(sel, first) {
  const ps = [...S.projects.values()].filter((p) => !p.archived && p.id !== 'unsorted')
    .sort((a, b) => (b.pinned ? 1 : 0) - (a.pinned ? 1 : 0) || (a.name || a.id).localeCompare(b.name || b.id));
  const sig = ps.map((p) => p.id + '=' + p.name + ':' + (p.pinned ? 1 : 0)).join('|');
  if (sel.dataset.sig === sig) return;
  const keep = sel.value;
  sel.replaceChildren(h('option', { value: '', text: first }), ...ps.map((p) => h('option', { value: p.id, text: p.name || p.id })));
  sel.value = ps.some((p) => p.id === keep) ? keep : '';
  sel.dataset.sig = sig;
}
function workPanel() {
  const count = h('span', { class: 'n' });
  const filter = h('select', { class: 'wq-filter', 'aria-label': 'Show work for project' });
  const showDone = h('input', { type: 'checkbox' });
  const doneLbl = h('label', { class: 'wq-done' }, showDone, 'finished');
  const showStale = h('input', { type: 'checkbox' });
  const staleN = h('span');
  const staleLbl = h('label', { class: 'wq-done', title: 'Open items nobody has touched for 3 days. Their agents are asked once a day to close, drop or annotate them.' }, showStale, 'stale ', staleN);
  const STALE_MS = 3 * 86400e3;
  const isStale = (w) => w.open && Date.now() - Date.parse(w.updated_at) > STALE_MS;
  const tIn = h('input', { type: 'text', placeholder: 'Add work… (Enter)', 'aria-label': 'New work item', autocomplete: 'off' });
  const pSel = h('select', { 'aria-label': 'Project for the new item' });
  const priSel = h('select', { 'aria-label': 'Priority for the new item' }, [1, 2, 3].map((n) => h('option', { value: n, text: 'P' + n })));
  priSel.value = '2';
  const add = h('button', { class: 'btn primary small', type: 'submit', text: 'Add' });
  const form = h('form', { class: 'wq-add' }, tIn, pSel, priSel, add);
  const box = h('div', { class: 'wq-list' });
  const rows = keyedList(box, (w) => w.id, workRow);
  const empty = h('div', { class: 'muted small wq-empty', text: 'Nothing queued. Add work here, or tell Switchboard; agents add what they leave unfinished.' });
  const el = h('section', { class: 'block wq' }, h('h2', null, 'Work queue ', count, h('span', { class: 'spacer' }), filter, staleLbl, doneLbl), form, box, empty);
  filter.value = lsGet('sb.wq.project') || '';
  showDone.checked = lsGet('sb.wq.done') === '1';
  showStale.checked = lsGet('sb.wq.stale') === '1';
  function update() {
    projectOptions(filter, 'All projects');
    projectOptions(pSel, 'No project');
    const all = (S.ov && S.ov.work) || [];
    const pid = filter.value;
    const mine = all.filter((w) => !pid || w.project === pid);
    const stale = mine.filter(isStale).length;
    const list = mine.filter((w) => (w.open ? showStale.checked || !isStale(w) : showDone.checked));
    rows.sync(list);
    box.hidden = !list.length; empty.hidden = list.length > 0;
    const open = mine.filter((w) => w.open).length;
    setText(count, `${open - stale} live` + (stale ? ` · ${stale} stale` : '') + (mine.some((w) => w.status === 'doing' && !isStale(w)) ? ` · ${mine.filter((w) => w.status === 'doing' && !isStale(w)).length} in progress` : ''));
    setText(staleN, stale ? `(${stale})` : ''); staleLbl.hidden = !stale && !showStale.checked;
  }
  filter.addEventListener('change', () => { lsSet('sb.wq.project', filter.value); if (filter.value) pSel.value = filter.value; update(); });
  showDone.addEventListener('change', () => { lsSet('sb.wq.done', showDone.checked ? '1' : ''); update(); });
  showStale.addEventListener('change', () => { lsSet('sb.wq.stale', showStale.checked ? '1' : ''); update(); });
  form.addEventListener('submit', async (e) => {
    e.preventDefault();
    const title = tIn.value.trim();
    if (!title) { tIn.focus(); return; }
    add.disabled = true;
    try {
      await api('POST', '/work', { title, project: pSel.value || null, priority: +priSel.value });
      tIn.value = ''; await tick(); tIn.focus();
    } catch (err) { toast(err.message, 'err'); } finally { add.disabled = false; }
  });
  return { el, update };
}

/* ---------- fleet: hosts, HALT, tracked jobs, untracked work (docs/FLEET.md) ---------- */
const JOB_STATES_OPEN = ['queued', 'running', 'stopping'];
function fmtDur(s) {
  if (s == null || isNaN(s)) return '';
  s = Math.max(0, Math.round(s));
  if (s < 60) return s + 's';
  if (s < 3600) return Math.round(s / 60) + 'm';
  if (s < 86400) return (s / 3600).toFixed(s < 36000 ? 1 : 0) + 'h';
  return (s / 86400).toFixed(1) + 'd';
}
function fmtMib(n) { return n == null ? '—' : n >= 1024 ? (n / 1024).toFixed(n >= 10240 ? 0 : 1) + 'G' : Math.round(n) + 'M'; }
function fmtGib(n) { return n == null ? '—' : (n >= 100 ? Math.round(n) : Number(n).toFixed(1).replace(/\.0$/, '')) + 'G'; }
function gpuName(s) { return String(s || 'GPU').replace(/^NVIDIA\s+/i, '').replace(/^GeForce\s+/i, '').replace(/^Tesla\s+/i, ''); }

/* one chip per GPU: a star marks the card that drives the owner's display (jobs avoid it) */
function gpuChip() {
  const el = h('span', { class: 'gpu' });
  return {
    el,
    update(g) {
      setText(el, `${g.display ? '★ ' : ''}${gpuName(g.name)} ${fmtMib(g.free_mib)} free${g.util != null ? ' · ' + g.util + '%' : ''}`);
      setAttr(el, 'title', `${g.name || 'GPU'}: ${fmtMib(g.free_mib)} of ${fmtMib(g.total_mib)} VRAM free${g.util != null ? `, ${g.util}% busy` : ''}${g.display ? '. Drives the display: jobs use it only when no other card fits.' : ''}`);
      setCls(el, 'gpu' + (g.display ? ' display' : '') + (g.util >= 90 ? ' hot' : ''));
    },
  };
}

/* one row per host: reachability mark, free threads / RAM, GPUs, the Xorg numbers on the desktop host, job counts */
function hostRow() {
  const mark = h('i', { class: 'hmark' });
  const name = h('strong', { class: 'hname' });
  const badge = h('span', { class: 'hbadge', text: 'desktop', title: 'The host whose display the owner sits at. Jobs here run at low CPU weight.' });
  const note = h('span', { class: 'hnote' });
  const cpu = h('span', { class: 'hstat' });
  const ram = h('span', { class: 'hstat' });
  const gpuBox = h('span', { class: 'hgpus' });
  const gpus = keyedList(gpuBox, (g, i) => (g.name || '') + '#' + (g.index != null ? g.index : i), gpuChip);
  const xorg = h('span', { class: 'hstat hxorg' });
  const counts = h('span', { class: 'hstat hcounts' });
  const el = h('div', { class: 'host' },
    h('div', { class: 'host-h' }, mark, name, badge, note),
    h('div', { class: 'host-s' }, cpu, ram, gpuBox, xorg, counts));
  return {
    el,
    update(x) {
      setText(name, x.name);
      badge.hidden = !x.desktop;
      const old = x.age_s != null && x.age_s > 120;
      let why = '';
      if (!x.reachable) why = 'unreachable' + (x.age_s != null ? ' · last seen ' + fmtDur(x.age_s) + ' ago' : '');
      else if (old) why = 'last probe ' + fmtDur(x.age_s) + ' ago';
      setText(note, why);
      setAttr(note, 'title', x.error || '');
      setAttr(mark, 'title', x.reachable ? `reachable · probed ${x.age_s != null ? fmtDur(x.age_s) + ' ago' : 'just now'}` : (x.error || 'unreachable'));
      setCls(el, 'host' + (x.reachable ? (old ? ' old' : '') : ' off') + (x.desktop ? ' desktop' : ''));
      const haveCap = x.threads != null;
      setCls(cpu, 'hstat' + (haveCap ? '' : ' herr'));
      setText(cpu, haveCap ? `${x.free_threads} of ${x.threads} threads free${x.load1 != null ? ' · load ' + Number(x.load1).toFixed(1) : ''}` : (x.error || 'no probe yet'));
      setAttr(cpu, 'title', haveCap ? 'Threads minus the host reserve minus the one-minute load' : (x.error || ''));
      setText(ram, haveCap ? `${fmtGib(x.free_ram_gib)} of ${fmtGib(x.ram_total_gib)} RAM free` : '');
      setAttr(ram, 'title', 'Available RAM minus the host reserve');
      ram.hidden = !haveCap;
      gpus.sync((x.gpus || []).map((g, i) => ({ ...g, index: g.index != null ? g.index : i })));
      gpuBox.hidden = !(x.gpus || []).length;
      const hasX = x.desktop && (x.xorg_cpu_pct != null || x.xorg_wakeups_s != null);
      xorg.hidden = !hasX;
      if (hasX) {
        setText(xorg, `Xorg ${x.xorg_cpu_pct != null ? x.xorg_cpu_pct + '% CPU' : ''}${x.xorg_cpu_pct != null && x.xorg_wakeups_s != null ? ' · ' : ''}${x.xorg_wakeups_s != null ? x.xorg_wakeups_s + ' wake/s' : ''}`);
        setAttr(xorg, 'title', "The X server's CPU share and voluntary wakeups per second: the honest measure of the owner's input lag");
        setCls(xorg, 'hstat hxorg' + (x.xorg_cpu_pct >= 30 ? ' bad' : x.xorg_cpu_pct >= 12 ? ' warn' : ''));
      }
      const parts = [];
      if (x.jobs) parts.push(`${x.jobs} job${x.jobs === 1 ? '' : 's'}`);
      if (x.untracked) parts.push(`${x.untracked} untracked`);
      setText(counts, parts.join(' · '));
      setCls(counts, 'hstat hcounts' + (x.untracked ? ' warn' : ''));
      counts.hidden = !parts.length;
    },
  };
}

/* a tracked job (sb run / sb adopt) with a Stop control */
function jobRow() {
  const dot = h('span', { class: 'jdot' });
  const name = h('strong', { class: 'jname' });
  const who = h('span', { class: 'jwho' });
  const where = h('span', { class: 'jwhere' });
  const res = h('span', { class: 'jres' });
  const reason = h('span', { class: 'jreason' });
  const stop = h('button', { class: 'pbtn jstop', type: 'button', text: 'Stop' });
  const el = h('div', { class: 'job' }, dot, name, who, where, res, reason, h('span', { class: 'spacer' }), stop);
  let cur = null;
  guarded(stop, async () => {
    if (!cur) return;
    if (!confirm(`Stop ${cur.name} (${cur.id})${cur.host ? ' on ' + cur.host : ', still queued'}?${cur.adopted ? ' It was adopted, not launched by Switchboard.' : ''}`)) return;
    const r = await api('POST', `/jobs/${enc(cur.id)}/stop`, { from: 'owner', reason: 'stopped from the Fleet panel' });
    toast(`${r.name}: ${r.state}${r.reason ? ' · ' + r.reason : ''}`);
    tick();
  });
  return {
    el,
    update(j) {
      cur = j;
      setCls(dot, 'jdot s-' + (j.state || 'queued'));
      setAttr(dot, 'title', j.state || '');
      setText(name, j.name || j.id);
      setAttr(name, 'title', `${j.id}${j.unit ? ' · ' + j.unit : ''}${j.argv && j.argv.length ? '\n' + j.argv.join(' ') : ''}`);
      setText(who, [j.project && projName(j.project), j.agent && '@' + j.agent].filter(Boolean).join(' / '));
      const place = j.state === 'queued' ? (j.host_pref ? 'wants ' + j.host_pref : 'queued') : j.host || '';
      setText(where, place + (j.adopted ? ' · adopted' : '') + (j.lane ? ' · ' + j.lane : ''));
      setAttr(where, 'title', [j.placer && 'placed by ' + j.placer, j.cpus && j.cpus.length && 'CPUs ' + j.cpus.join(','), j.board_id && 'workboard ' + j.board_id].filter(Boolean).join('\n'));
      const bits = [];
      if (j.threads) bits.push(j.threads + ' thr');
      if (j.mem_mib) bits.push(fmtMib(j.mem_cur_mib != null ? j.mem_cur_mib : 0) + (j.mem_mib ? ' / ' + fmtMib(j.mem_mib) : ''));
      if (j.gpu_mib) bits.push('GPU ' + fmtMib(j.gpu_mib));
      if (j.runtime_s != null) bits.push(fmtDur(j.runtime_s));
      setText(res, bits.join(' · '));
      setText(reason, j.reason || '');
      setAttr(reason, 'title', j.reason || '');
      reason.hidden = !j.reason;
      stop.hidden = !JOB_STATES_OPEN.includes(j.state);
      setCls(el, 'job s-' + (j.state || 'queued'));
    },
  };
}

/* a heavy process the hub did not launch, with its best attribution and Adopt / Stop controls.
 * Adopt brings the unit the pid lives in under tracking; Stop adopts it then stops the unit (the hub has no
 * stop-by-pid; a process outside any systemd unit cannot be adopted and the hub says so). */
function untrackedRow() {
  const pid = h('span', { class: 'upid' });
  const comm = h('strong', { class: 'ucomm' });
  const stat = h('span', { class: 'ustat' });
  const attr = h('span', { class: 'uattr' });
  const alink = agentLink();
  const host = h('span', { class: 'uhost' });
  const adoptBtn = h('button', { class: 'pbtn', type: 'button', text: 'Adopt', title: 'Track this work as a job (sb adopt): it is watched, never halted' });
  const stopBtn = h('button', { class: 'pbtn udanger', type: 'button', text: 'Stop', title: 'Adopt the unit this process lives in, then stop it' });
  const nameIn = h('input', { type: 'text', class: 'uname', 'aria-label': 'Job name', autocomplete: 'off', maxlength: 40, placeholder: 'job name' });
  const ok = h('button', { class: 'btn primary small', type: 'submit', text: 'Adopt' });
  const cancel = h('button', { class: 'btn ghost small', type: 'button', text: 'Cancel' });
  const form = h('form', { class: 'uform', hidden: true }, nameIn, ok, cancel);
  const el = h('div', { class: 'untracked' },
    h('div', { class: 'uline' }, pid, comm, stat, attr, alink.el, host, h('span', { class: 'spacer' }), h('span', { class: 'ubtns' }, adoptBtn, stopBtn)), form);
  let cur = null;
  const jobName = (p) => String(p.comm || 'proc').toLowerCase().replace(/[^a-z0-9._-]+/g, '-').replace(/^[^a-z0-9]+/, '').slice(0, 30) || 'proc';
  const adopt = (name) => api('POST', '/jobs/adopt', { host: cur.host, pid: cur.pid, name, agent: cur.agent || 'owner', project: cur.project || null });
  const close = () => { form.hidden = true; adoptBtn.hidden = stopBtn.hidden = false; };
  adoptBtn.addEventListener('click', (e) => { e.stopPropagation(); if (!cur) return; nameIn.value = jobName(cur) + '-' + cur.pid; form.hidden = false; adoptBtn.hidden = stopBtn.hidden = true; nameIn.focus(); nameIn.select(); });
  cancel.addEventListener('click', close);
  nameIn.addEventListener('keydown', (e) => { if (e.key === 'Escape') { e.preventDefault(); close(); } });
  form.addEventListener('submit', async (e) => {
    e.preventDefault();
    const name = nameIn.value.trim();
    if (!name) { nameIn.focus(); return; }
    ok.disabled = true;
    try { const j = await adopt(name); toast(`Adopted ${j.name} on ${j.host} (${j.unit})`); close(); tick(); } catch (err) { toast(err.message, 'err'); } finally { ok.disabled = false; }
  });
  guarded(stopBtn, async () => {
    if (!cur) return;
    if (!confirm(`Stop pid ${cur.pid} (${cur.comm}) on ${cur.host}? Switchboard adopts the unit it lives in, then stops that unit.`)) return;
    const j = await adopt('stop-' + jobName(cur) + '-' + cur.pid);
    const r = await api('POST', `/jobs/${enc(j.id)}/stop`, { from: 'owner', reason: 'untracked work stopped from the Fleet panel' });
    toast(`${j.unit} on ${j.host}: ${r.state}`);
    tick();
  });
  return {
    el,
    update(p) {
      cur = p;
      setText(pid, String(p.pid));
      setText(comm, p.comm || '?');
      setAttr(comm, 'title', (p.args || '') + (p.cwd ? '\ncwd ' + p.cwd : ''));
      setText(stat, `${p.cpu != null ? Math.round(p.cpu) + '% CPU' : ''}${p.rss_mib != null ? ' · ' + fmtMib(p.rss_mib) : ''}${p.age_s != null ? ' · ' + fmtDur(p.age_s) : ''}`);
      const isAgent = !!p.agent && S.agents.has(p.agent);
      alink.el.hidden = !isAgent;
      if (isAgent) alink.update(p.agent);
      setText(attr, isAgent ? 'launched by' : (p.attribution || 'unknown'));
      setAttr(attr, 'title', p.attribution || '');
      setText(host, p.host);
      host.hidden = !p.showHost;
    },
  };
}

/* the HALT banner at the top of the page (overview.fleet.halt) with an Unhalt button */
function haltBanner() {
  const text = h('span', { class: 'halt-text' });
  const hostsEl = h('span', { class: 'halt-hosts' });
  const btn = h('button', { class: 'btn small halt-btn', type: 'button', text: 'Unhalt' });
  const el = h('div', { class: 'halt-banner', id: 'halt-banner', role: 'alert', hidden: true }, h('strong', { text: 'HALT' }), text, hostsEl, btn);
  guarded(btn, async () => {
    if (!confirm('Lower the HALT on every host? sb run launches again and guarded units may start on their next trigger.')) return;
    const r = await api('POST', '/fleet/resume', { from: 'owner' });
    const bad = (r.results || []).filter((x) => !x.ok);
    toast(bad.length ? `HALT lowered, but ${bad.map((x) => x.host).join(', ')} did not answer` : 'HALT lowered on every host', bad.length ? 'err' : undefined);
    tick();
  });
  return {
    el,
    update(halt) {
      el.hidden = !halt;
      if (!halt) return;
      setText(text, `raised by ${halt.by || 'owner'}${halt.at ? ' ' + ago(halt.at) : ''}: ${halt.reason || 'no reason given'}${halt.workloads ? ' · workload units were disabled' : ''}`);
      setAttr(text, 'title', halt.at ? absTime(halt.at) : '');
      const pend = halt.pending || [];
      setText(hostsEl, `${(halt.hosts || []).length ? 'on ' + halt.hosts.join(', ') : ''}${pend.length ? ' · not yet on ' + pend.join(', ') + ' (retried every probe)' : ''}`);
    },
  };
}

function fleetPanel() {
  const count = h('span', { class: 'n' });
  const haltBtn = h('button', { class: 'pbtn fl-halt', type: 'button', text: 'Halt', title: 'Stop every tracked job and raise HALT on every host (sb halt)' });
  const reasonIn = h('input', { type: 'text', placeholder: 'why? (goes in the HALT flag)', 'aria-label': 'Halt reason', autocomplete: 'off' });
  const haltOk = h('button', { class: 'btn small danger', type: 'submit', text: 'Halt fleet' });
  const haltCancel = h('button', { class: 'btn ghost small', type: 'button', text: 'Cancel' });
  const haltForm = h('form', { class: 'fl-haltform', hidden: true }, reasonIn, haltOk, haltCancel);
  const hostBox = h('div', { class: 'hosts' });
  const hostRows = keyedList(hostBox, (x) => x.name, hostRow);
  const hint = h('div', { class: 'muted small fl-hint' });
  const jobsN = h('span', { class: 'n' });
  const jobBox = h('div', { class: 'jobs' });
  const jobRows = keyedList(jobBox, (j) => j.id, jobRow);
  const jobsEmpty = h('div', { class: 'muted small', text: 'No tracked jobs. Agents launch work with sb run; it shows up here with its owner.' });
  const unN = h('span', { class: 'n' });
  const unBox = h('div', { class: 'untracked-list' });
  const unRows = keyedList(unBox, (p) => p.host + ':' + p.pid, untrackedRow);
  const unEmpty = h('div', { class: 'muted small', text: 'Nothing heavy is running outside a tracked job.' });
  const wb = h('div', { class: 'muted small fl-wb' });
  const closeHalt = () => { haltForm.hidden = true; haltBtn.hidden = false; reasonIn.value = ''; };
  haltBtn.addEventListener('click', () => { haltForm.hidden = false; haltBtn.hidden = true; reasonIn.focus(); });
  haltCancel.addEventListener('click', closeHalt);
  reasonIn.addEventListener('keydown', (e) => { if (e.key === 'Escape') { e.preventDefault(); closeHalt(); } });
  haltForm.addEventListener('submit', async (e) => {
    e.preventDefault();
    const reason = reasonIn.value.trim();
    if (!reason) { reasonIn.focus(); return; }
    if (!confirm(`Raise HALT on every host and stop every tracked job?\n\nReason: ${reason}`)) return;
    haltOk.disabled = true;
    try {
      const r = await api('POST', '/fleet/halt', { from: 'owner', reason });
      const res = r.results || [];
      const stopped = res.reduce((n, x) => n + (x.stopped || []).length, 0);
      const bad = res.filter((x) => !x.ok).map((x) => x.host);
      toast(`HALT on ${res.length - bad.length} host${res.length - bad.length === 1 ? '' : 's'} · ${stopped} job${stopped === 1 ? '' : 's'} stopped${bad.length ? ' · pending: ' + bad.join(', ') : ''}`, bad.length ? 'err' : undefined);
      closeHalt(); tick();
    } catch (err) { toast(err.message, 'err'); } finally { haltOk.disabled = false; }
  });
  const el = h('section', { class: 'block fleet' },
    h('h2', null, 'Fleet ', count, h('span', { class: 'spacer' }), haltBtn, haltForm),
    hostBox, hint,
    h('h3', null, 'Jobs ', jobsN), jobBox, jobsEmpty,
    h('h3', null, 'Untracked ', unN), unBox, unEmpty, wb);
  let detail = null, inflight = false, lastFetch = 0;
  async function fetchDetail() {  // the full view (/api/fleet) carries the tracked and untracked lists
    if (inflight || Date.now() - lastFetch < 2500) return;
    inflight = true;
    try { detail = await api('GET', '/fleet'); lastFetch = Date.now(); renderDetail(); } catch (e) { /* the summary still shows */ } finally { inflight = false; }
  }
  function renderDetail() {
    if (!detail) return;
    const hs = detail.hosts || [];
    const many = hs.length > 1;
    const jobs = hs.flatMap((x) => (x.tracked || []).map((j) => ({ ...j, host: j.host || x.name })))
      .concat(detail.queued || [])  // queued jobs no host has taken yet belong to no host's list
      .sort((a, b) => JOB_STATES_OPEN.indexOf(a.state) - JOB_STATES_OPEN.indexOf(b.state) || (parseTime(b.started_at || b.created_at) || 0) - (parseTime(a.started_at || a.created_at) || 0));
    jobRows.sync(jobs);
    jobBox.hidden = !jobs.length; jobsEmpty.hidden = jobs.length > 0;
    setText(jobsN, jobs.length ? `${jobs.filter((j) => j.state === 'running').length} running${jobs.some((j) => j.state === 'queued') ? ` · ${jobs.filter((j) => j.state === 'queued').length} queued` : ''}` : '');
    const un = hs.flatMap((x) => (x.untracked || []).map((p) => ({ ...p, host: x.name, showHost: many }))).sort((a, b) => (b.cpu || 0) - (a.cpu || 0));
    unRows.sync(un);
    unBox.hidden = !un.length; unEmpty.hidden = un.length > 0;
    setText(unN, un.length ? `${un.length} process${un.length === 1 ? '' : 'es'} above 25% CPU` : '');
    const board = (detail.placers || {}).workboard;
    const cards = (board && board.cards) || [], leases = (board && board.jobs) || [];
    wb.hidden = !board;
    if (board) setText(wb, board.error ? 'gpu-workboard: ' + board.error : `gpu-workboard: ${cards.length} card${cards.length === 1 ? '' : 's'}, ${leases.length} open lease${leases.length === 1 ? '' : 's'}${leases.length ? ': ' + leases.map((l) => `${l.label || l.id}${l.host ? '@' + l.host : ''} (${l.status}${l.idle ? `, idle ${l.idle.idle_min}m, standing ${Math.round(l.idle.standing * 100)}%` : ""})`).join(', ') : ''}`);
  }
  return {
    el,
    update() {
      const f = (S.ov && S.ov.fleet) || {};
      const hs = f.hosts || [];
      hostRows.sync(hs);
      const off = hs.filter((x) => !x.reachable).length;
      setText(count, `${hs.length} host${hs.length === 1 ? '' : 's'}${off ? ` · ${off} unreachable` : ''}`);
      setText(hint, f.error ? 'Fleet read failed: ' + f.error
        : f.config_exists === false ? 'Only this machine. Add hosts in ~/.config/switchboard/hosts.json (see examples/hosts.example.json).' : '');
      hint.hidden = !hint.textContent;
      haltBtn.disabled = !!f.halt;
      setAttr(haltBtn, 'title', f.halt ? 'HALT is already raised; the banner at the top lowers it' : 'Stop every tracked job and raise HALT on every host (sb halt)');
      fetchDetail();
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
      toast('Created ' + (p.name || p.id)); closeAdd();
      await tick();
      location.hash = '#/p/' + enc(p.id);
    } catch (err) { toast(err.message, 'err'); } finally { pOk.disabled = false; }
  });
  const noProj = h('p', { class: 'muted small', text: 'No projects yet. "+ Project" makes one (with its own folder in your workspace); then "+ Agent" on its card starts an agent there.' });
  const work = workPanel();
  const fleet = fleetPanel();
  const el = h('div', { class: 'ov' },
    h('div', { class: 'ov-main' },
      work.el,
      h('div', { class: 'ov-row' },  // pools + projects on the left fill the height of the fleet column
        h('div', { class: 'ov-col' },
          h('section', { class: 'block' }, h('h2', null, 'Accounts ', h('a', { class: 'btn ghost small', href: '#/setup', text: 'Setup' })), poolBox),
          h('section', { class: 'block' }, h('h2', null, 'Projects ', addP, addForm), noProj, grid, arch)),
        fleet.el)));
  return {
    el, title: '',
    update() {
      work.update();
      fleet.update();
      pools.sync(S.accounts);
      const ps = [...S.projects.values()];
      const act = (p) => -(parseTime(p.last_activity) || 0);
      cards.sync(ps.filter((p) => !p.archived).sort((a, b) => (b.pinned ? 1 : 0) - (a.pinned ? 1 : 0) || act(a) - act(b)));
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
      const mine = projectAgents(pid);  // only this project's agents: another project's agents never open here
      const sig = curId + '|' + mine.map((a) => a.id + agentName(a)).join(',');
      if (sel.dataset.sig !== sig) {
        sel.dataset.sig = sig;
        const kids = [];
        if (!curId || !mine.some((a) => a.id === curId)) kids.push(h('option', { value: curId || '', text: curId ? curId + ' (unknown)' : 'Pick an agent…' }));
        kids.push(...mine.map((a) => h('option', { value: a.id, text: agentName(a) })));
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
  const queueBtn = h('button', { class: 'pbtn', type: 'button', hidden: true });
  queueBtn.onclick = () => openQueue(id);
  const ctxBtn = h('button', { class: 'pbtn', type: 'button', text: 'Context', title: 'What fills this agent\'s context, request by request', hidden: true });
  ctxBtn.onclick = () => { if (id) openContext(id); };
  const memBtn = h('button', { class: 'pbtn', type: 'button', text: 'Memory', title: 'Memory files this agent wrote, and outlines of its conversations', hidden: true });
  memBtn.onclick = () => { const a = id && S.agents.get(id); if (a) openMemFiles(a.project, id); };
  const rewBtn = h('button', { class: 'pbtn', type: 'button', text: 'Transcript', title: 'Rewind this agent to the moment before one of its messages, or edit any entry of its transcript', hidden: true });
  rewBtn.onclick = () => { if (id) openRewind(id); };
  // the attest box: this agent must back any "done" with sb attest, and every request it gets says so (claims.py)
  const attBox = h('input', { type: 'checkbox' });
  const attLbl = h('label', { class: 'pn-attest', hidden: true, title: 'Attest: this agent must run sb attest (the hub checks its evidence and asks "are you sure?") before it reports work as done. Every request it gets says so, and a report with no receipt is sent back.' }, attBox, h('span', { text: 'attest' }));
  attBox.onchange = async () => {
    if (!id) return;
    try { await api('POST', '/trust/require', { agent: id, on: attBox.checked }); } catch (e) { attBox.checked = !attBox.checked; }
    tick();
  };
  const note = h('span', { class: 'pn-note' });
  const meta = h('span', { class: 'pn-meta' });
  const num = h('span', { class: 'pn-num', text: String(idx + 1) });
  const mks = markSet(true);
  const renBtn = h('button', { class: 'pbtn rn-btn', type: 'button', text: '✎', title: 'Rename this panel (no relaunch; the id other agents write to stays the same)', hidden: true });
  renBtn.onclick = () => renameInline(picker.el, cur);
  const hdr = h('div', { class: 'pn-h' }, num, dot, picker.el, renBtn, mks.el, st, conn, queueBtn, note, h('span', { class: 'spacer' }), meta, attLbl, ctxBtn, memBtn, rewBtn);
  const rcHead = h('span', { class: 'rc-head' });
  const rcBody = h('div', { class: 'rc-body', hidden: true });
  const rcMore = h('button', { class: 'pbtn', type: 'button', text: 'Details' });
  const rcOk = h('button', { class: 'pbtn', type: 'button', text: 'Got it', title: 'Hide this until the next conversation ends' });
  const recap = h('div', { class: 'recap', hidden: true }, h('div', { class: 'rc-line' }, rcHead, h('span', { class: 'spacer' }), rcMore, rcOk), rcBody);
  let rcShown = 0;
  function syncRcMore() { setText(rcMore, rcBody.hidden ? 'Details' : 'Collapse'); }
  rcMore.onclick = () => { rcBody.hidden = !rcBody.hidden; syncRcMore(); scheduleFit(); };
  rcOk.onclick = async () => {
    const n = rcShown; recap.hidden = true; rcBody.hidden = true; scheduleFit();
    try { await api('POST', `/agents/${enc(id)}/recap/seen`, { n }); tick(); } catch (e) { toast(e.message, 'err'); }
  };
  function paintRecap(r) {
    if (!r) { if (!recap.hidden) { recap.hidden = true; rcBody.hidden = true; scheduleFit(); } rcShown = 0; return; }
    const tail = r.last_tail || r.last_said || '';
    const sig = `${r.state}|${tail.length}|${(r.summary || '').length}`;
    if (rcShown === r.n && rcHead.dataset.sig === sig) return;
    const isNew = rcShown !== r.n;
    rcShown = r.n; rcHead.dataset.sig = sig;
    const busy = r.state === 'pending' || r.state === 'working';
    const lead = r.auto ? `Auto-cleared (${ago(r.at)})` : `Before this conversation (${r.why || 'ended'}, ${ago(r.at)})`;
    const headMsg = busy ? (tail ? 'last reply below (outline being written…)' : 'summary being written…')
      : (r.summary || (tail ? 'last reply below' : (r.last_ask || r.first_ask || 'nothing recorded')));
    setText(rcHead, `${lead}: ${headMsg}`);
    rcBody.replaceChildren();
    if (tail) {
      rcBody.append(h('div', { class: 'rc-sec rc-tail' },
        h('div', { class: 'rc-lbl', text: r.n > 0 ? `LAST REPLY (#${r.n})` : 'LAST REPLY' }),
        renderChatText(tail, id)));
    }
    for (const s of r.sections || []) rcBody.append(h('div', { class: 'rc-sec' }, h('b', { text: s.h }), h('ul', {}, s.points.map((t) => h('li', { text: t })))));
    if (!(r.sections || []).length && !tail) {
      if (r.first_ask) rcBody.append(h('div', { class: 'rc-sec' }, h('b', { text: 'FIRST ASKED ' }), r.first_ask));
      if (r.last_ask) rcBody.append(h('div', { class: 'rc-sec' }, h('b', { text: 'LAST ASKED ' }), r.last_ask));
    }
    if (r.n > 0) rcBody.append(h('div', { class: 'rc-sec muted small', text: `sb memory ${r.n} has the outline with its sources` + (r.note ? ` · ${r.note}` : '') }));
    rcMore.hidden = !rcBody.childNodes.length;
    if (isNew) rcBody.hidden = !(r.auto && tail);
    recap.classList.toggle('has-tail', Boolean(tail));
    syncRcMore();
    recap.hidden = false; scheduleFit();
  }
  const box = h('div', { class: 'term' });
  const ovMsg = h('div', { class: 'ov-msg' });
  const ovBtn = h('button', { class: 'btn primary', type: 'button' });
  const overlay = h('div', { class: 'ovl', hidden: true }, h('div', { class: 'ov-box' }, ovMsg, ovBtn));
  const el = h('section', { class: 'pane', hidden: true }, hdr, recap, h('div', { class: 'termwrap' }, box, overlay));
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

  function copySelection() {
    const sel = term.getSelection();
    if (!sel) return;
    // the browser's own copy (xterm fills it from its selection) plus the async API as a fallback
    try { document.execCommand('copy'); } catch (err) { /* not allowed here */ }
    if (navigator.clipboard) navigator.clipboard.writeText(sel).catch(() => {});
    toast('Copied');
  }

  function ensureTerm() {
    if (term) return;
    term = new Terminal({
      fontFamily: '"JetBrains Mono", "DejaVu Sans Mono", monospace', fontSize: 13, lineHeight: 1.0,
      cursorBlink: true, scrollback: 10000, theme: TERM_THEME, macOptionIsMeta: true, allowProposedApi: false,
      linkHandler: {
        allowNonHttpProtocols: true,
        activate(_ev, text) { openLink(text, id); },
      },
    });
    fit = new FitAddon.FitAddon();
    term.loadAddon(fit);
    registerTerminalLinks(term, () => id);
    term.open(box);
    term.onData((d) => sendJ({ t: 'i', d }));
    /* Clipboard like a desktop terminal: Ctrl+C copies while text is selected (otherwise it still interrupts the
       CLI), Ctrl+Shift+C always copies; Ctrl+V / Ctrl+Shift+V / Shift+Insert paste through the browser's paste
       event (text goes to the CLI, images are uploaded). Returning false hands the key to the browser. */
    term.attachCustomKeyEventHandler((e) => {
      if (e.type !== 'keydown' || e.altKey || e.metaKey) return true;
      if (e.ctrlKey && e.code === 'KeyC' && (e.shiftKey || term.hasSelection())) {
        copySelection();
        return false;
      }
      if ((e.ctrlKey && e.code === 'KeyV') || (e.shiftKey && !e.ctrlKey && e.code === 'Insert')) return false;
      return true;
    });
    /* OSC 52: a CLI that draws its own selection (Claude's fullscreen view) copies by sending the text to the
       terminal as base64. xterm drops that unless handled, so the copy silently went nowhere. Write-only: a
       read request ("?") is never answered, so a program in the terminal cannot read the clipboard. */
    term.parser.registerOscHandler(52, (data) => {
      const b64 = data.slice(data.indexOf(';') + 1);
      // the hub replays scrollback on every open: a copy from earlier must not land on the clipboard again
      if (term._sbReplay || !b64 || b64 === '?') return true;
      let text;
      try { text = new TextDecoder().decode(Uint8Array.from(atob(b64), (c) => c.charCodeAt(0))); } catch (err) { return true; }
      if (!text) return true;
      (navigator.clipboard ? navigator.clipboard.writeText(text) : Promise.reject(new Error('no clipboard')))
        .then(() => toast('Copied'), () => toast('Copy blocked here; hold Shift, select and press Ctrl+C', 'err'));
      return true;
    });
    box.addEventListener('paste', onPaste, true);
    box.addEventListener('contextmenu', (e) => { e.preventDefault(); termMenu(e.clientX, e.clientY, term); });
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
      term._sbReplay = true;              // until the status line that follows the replay (OSC 52 handler)
      fitNow(true);
    };
    sock.onmessage = (ev) => {
      if (sock !== ws) return;
      if (typeof ev.data !== 'string') { term.write(new Uint8Array(ev.data)); return; }
      let m;
      try { m = JSON.parse(ev.data); } catch (e) { return; }
      if (term._sbReplay) term.write('', () => { term._sbReplay = false; });  // after the replay has been parsed
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
      if (!m.running && m.held && !ovKind) showOverlay('stopped', 'You stopped this CLI. It stays stopped until you start it (or a message arrives for it).', 'Start', startAndConnect);
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
    setText(st, !id ? '' : a.switching ? `switching to ${a.switching.to}…` : agentState(a));
    const bits = [];
    if (a.launch_block) bits.push('blocked: ' + a.launch_block);
    queueBtn.hidden = !a.queued;
    renBtn.hidden = !id; mks.update(id && AGENT_CLIS.includes(a.provider) ? a : null);
    ctxBtn.hidden = !id || !['claude', 'codex'].includes(a.provider);
    memBtn.hidden = rewBtn.hidden = !id || !AGENT_CLIS.includes(a.provider);
    attLbl.hidden = !id || !AGENT_CLIS.includes(a.provider);
    if (document.activeElement !== attBox) attBox.checked = !!a.attest;
    setText(queueBtn, `${a.queued || 0} pending · View messages`);
    if (a.queued) bits.push(queueWhy(a));
    if (a.switching && a.switching.note) bits.push(a.switching.note);
    setText(note, bits.join(' · ')); setAttr(note, 'title', bits.join('\n'));
    paintRecap(id ? a.recap : null);
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
        if (a && visible && a.running && !wasRunning && (ovKind === 'exit' || ovKind === 'stopped')) reconnect();
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

/* right-click menu on a terminal: Copy / Paste / Select all (the desktop window has no menu of its own) */
let TMENU = null;
function termMenu(x, y, term) {
  if (!TMENU) {
    TMENU = h('div', { class: 'tmenu', role: 'menu', hidden: true });
    document.body.append(TMENU);
    document.addEventListener('mousedown', (e) => { if (!TMENU.contains(e.target)) TMENU.hidden = true; }, true);
    document.addEventListener('keydown', (e) => { if (e.key === 'Escape') TMENU.hidden = true; }, true);
  }
  const close = () => { TMENU.hidden = true; term.focus(); };
  const item = (label, key, enabled, fn) => h('button', { type: 'button', role: 'menuitem', disabled: !enabled,
    onclick: () => { close(); fn(); } }, h('span', { text: label }), h('kbd', { text: key }));
  TMENU.replaceChildren(
    item('Copy', 'Ctrl+C', term.hasSelection(), () => {
      const sel = term.getSelection();
      (navigator.clipboard ? navigator.clipboard.writeText(sel) : Promise.reject(new Error('no clipboard')))
        .then(() => toast('Copied'), () => toast('Copy blocked here; select and press Ctrl+C', 'err'));
    }),
    item('Paste', 'Ctrl+V', true, () => {
      (navigator.clipboard && navigator.clipboard.readText ? navigator.clipboard.readText() : Promise.reject(new Error('no clipboard')))
        .then((t) => { if (t) term.paste(t); }, () => toast('This window does not let a menu read the clipboard; press Ctrl+V', 'err'));
    }),
    item('Select all', '', true, () => term.selectAll()));
  TMENU.hidden = false;
  const r = TMENU.getBoundingClientRect();
  TMENU.style.left = Math.min(x, innerWidth - r.width - 4) + 'px';
  TMENU.style.top = Math.min(y, innerHeight - r.height - 4) + 'px';
}

/* controls for the focused pane's agent: account, model, project, stop, remove */
function controlBar() {
  let a = null;
  const who = h('span', { class: 'cb-who' });
  const acct = h('select', { class: 'pn-sel', 'aria-label': 'Account', title: 'Account pool — switching exits the CLI and resumes the same session there' });
  const model = h('input', { class: 'pn-model', type: 'text', autocomplete: 'off', spellcheck: 'false', 'aria-label': 'Model', title: 'Model — applies on next launch', placeholder: 'default model' });
  const move = h('select', { class: 'pn-sel', 'aria-label': 'Project', title: 'Give this unsorted agent its project (agents never move between projects)' });
  const resumeBtn = h('button', { class: 'pbtn', type: 'button', text: 'Resume queue', title: 'Unpause message delivery; retained messages will be delivered' });
  const stopBtn = h('button', { class: 'pbtn', type: 'button', text: 'Stop', title: 'Exit the CLI (/exit)' });
  const toggle = h('button', { class: 'pbtn cb-toggle', type: 'button', title: 'Show or hide the controls for the focused agent (account, model, stop)' });
  const body = h('span', { class: 'cb-body' }, h('label', { class: 'pn-l' }, 'account', acct), h('label', { class: 'pn-l' }, 'model', model),
    h('label', { class: 'pn-l' }, 'assign to', move), resumeBtn, stopBtn);
  const el = h('div', { class: 'cbar' }, toggle, who, body);
  let open = lsGet('sb.ctl') === '1';  // collapsed by default: it shares the row with the tabs
  function paintToggle() { body.hidden = !open; setText(toggle, open ? 'controls ▾' : 'controls ▸'); el.classList.toggle('open', open); }
  toggle.addEventListener('click', () => { open = !open; lsSet('sb.ctl', open ? '1' : ''); paintToggle(); });
  paintToggle();

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
    if (!confirm(`Put ${agentName(a)} on ${projName(to)}? Agents stay on their project after that.`)) { move.value = from; return; }
    try { await api('PATCH', `/agents/${enc(a.id)}`, { project: to }); toast(`${agentName(a)} is now on ${projName(to)}`); tick(); }
    catch (e) { move.value = from; toast(e.message, 'err'); }
  });
  guarded(resumeBtn, async () => {
    if (!a || !a.paused) return;
    await api('PATCH', `/agents/${enc(a.id)}`, { paused: false });
    toast(`Queue resumed for ${agentName(a)}`); tick();
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
      const homeless = !a.project || a.project === 'unsorted' || !S.projects.has(a.project);
      move.parentElement.hidden = !homeless;
      if (homeless && document.activeElement !== move) {
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
      resumeBtn.hidden = !a.paused;
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
  const mks = markSet(false);
  const el = h('a', { class: 'tab', role: 'tab', draggable: 'true', title: 'Drag to reorder; double-click the name to rename' }, dot, name, mks.el, acct, mt, rm);
  let cur = null;
  name.addEventListener('dblclick', (e) => { e.preventDefault(); e.stopPropagation(); renameInline(name, cur); });
  /* drag to reorder: the drop position is saved on the hub per project, so every device shows the same order */
  el.addEventListener('dragstart', (e) => {
    if (!cur) return;
    e.dataTransfer.setData('text/sb-agent', cur.id); e.dataTransfer.effectAllowed = 'move';
    el.classList.add('dragging');
  });
  el.addEventListener('dragend', () => { el.classList.remove('dragging'); document.querySelectorAll('.tab.drop-before, .tab.drop-after').forEach((t) => t.classList.remove('drop-before', 'drop-after')); });
  el.addEventListener('dragover', (e) => {
    if (!e.dataTransfer.types.includes('text/sb-agent')) return;
    e.preventDefault(); e.dataTransfer.dropEffect = 'move';
    const r = el.getBoundingClientRect(), before = e.clientX < r.left + r.width / 2;
    el.classList.toggle('drop-before', before); el.classList.toggle('drop-after', !before);
  });
  el.addEventListener('dragleave', () => el.classList.remove('drop-before', 'drop-after'));
  el.addEventListener('drop', async (e) => {
    const id = e.dataTransfer.getData('text/sb-agent');
    if (!id || !cur) return;
    e.preventDefault();
    const before = el.classList.contains('drop-before');
    el.classList.remove('drop-before', 'drop-after');
    if (id === cur.id) return;
    const bar = el.parentElement;
    const order = [...bar.querySelectorAll('.tab')].map((t) => t.dataset.agent).filter((x) => x && x !== id);
    const at = order.indexOf(cur.id) + (before ? 0 : 1);
    order.splice(at, 0, id);
    try { await api('PATCH', `/projects/${enc(cur.project)}`, { tab_order: order }); tick(); } catch (err) { toast(err.message, 'err'); }
  });
  rm.addEventListener('click', async (e) => {
    e.preventDefault(); e.stopPropagation();
    if (!cur || !confirm(cur.ghost ? `Remove ghost ${agentName(cur)}? Its CLI is stopped, then its conversation and every record of it is erased for good. Only its edits to files stay.`
      : `Remove ${agentName(cur)} from Switchboard? Its CLI is stopped first; the session files stay on disk.`)) return;
    try { await api('DELETE', `/agents/${enc(cur.id)}`); } catch (err) { alert('Remove failed: ' + (err.message || err)); }
    tick();
  });
  return {
    el,
    update(a) { cur = a; el.dataset.agent = a.id; setAttr(el, 'href', agentHref(a)); setDot(dot, a); setText(name, agentName(a)); mks.update(a); setText(acct, a.account); setModelTag(mt, a); },
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
  function save() { lsSet('sb.panes.' + pid, JSON.stringify({ n: count, picks, f: focus })); }

  const pinBtn = h('button', { class: 'pin-btn pv-pin', type: 'button', 'aria-pressed': 'false' });
  pinBtn.innerHTML = PIN_SVG;
  pinBtn.hidden = pid === 'unsorted';
  guarded(pinBtn, async () => {
    const p = S.projects.get(pid);
    await toggleProjectPin(pid, !(p && p.pinned));
    v.update();
  });
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
  const teamBtn = h('button', { class: 'pbtn', type: 'button', text: 'Team', title: 'Messages, notes and decisions between this project\'s agents and you, on a timeline', onclick: () => openTeam(pid) });
  const memFilesBtn = h('button', { class: 'pbtn', type: 'button', text: 'Memory', title: 'This project\'s running memory, its conversation outlines and transcripts, and every memory file its agents wrote: read and edit them', onclick: () => openMemFiles(pid, null) });
  const insBtn = h('a', { class: 'pbtn', href: '#/insights/' + enc(pid), text: 'Insights', title: 'Token use, cost, tools and activity for this project' });
  /* kill the project entirely (DELETE /api/projects/<id>, owner only; switchboard/kill.py) */
  const killBtn = h('button', { class: 'pbtn kill-p', type: 'button', text: 'Kill project', title: 'Remove this project entirely: its agents, their conversations and everything Switchboard recorded about it. Files in its folder stay.' });
  guarded(killBtn, async () => {
    const p = S.projects.get(pid);
    const n = projectAgents(pid).length;
    if (!confirm(`Kill ${p ? p.name || pid : pid} entirely?\n\n${n} agent${n === 1 ? '' : 's'} exit now and are removed, with their conversations, messages, notes, work items and memory files. Its running jobs are stopped. The conversations are deleted from disk; no copy is kept.\n\nOnly the files in the project's folder stay. This cannot be undone.`)) return;
    const r = await api('DELETE', `/projects/${enc(pid)}`);
    toast(`Killed ${pid}: ${r.agents} agents, ${r.sessions} sessions` + (r.left_running.length ? ` · still running: ${r.left_running.join(', ')}` : '') + ((r.memory_shared || []).length ? ` · memory folder kept, another project's agent uses it: ${r.memory_shared.join(', ')}` : ''));
    location.hash = '#/'; tick();
  });
  killBtn.hidden = pid === 'switchboard' || pid === 'unsorted';
  /* files outside the project's folder: two owner toggles (POST /api/projects/<id>/outside, sb outside) */
  const outRead = h('button', { class: 'pbtn out-t', type: 'button', text: 'Read out', 'aria-pressed': 'false' });
  const outWrite = h('button', { class: 'pbtn out-t', type: 'button', text: 'Write out', 'aria-pressed': 'false' });
  for (const [btn, key] of [[outRead, 'read'], [outWrite, 'write']]) {
    guarded(btn, async () => {
      const p = S.projects.get(pid);
      const on = !(p && p['outside_' + key]);
      const res = await api('POST', `/projects/${enc(pid)}/outside`, { [key]: on });
      if (p) { p.outside_read = res.read; p.outside_write = res.write; }
      const busy = (res.next_start || []).length ? ` · busy, on next start: ${res.next_start.join(', ')}` : '';
      toast(`${key === 'read' ? 'Read' : 'Write'} outside the folder ${on ? 'on' : 'off'}${(res.relaunched || []).length ? ' · relaunched ' + res.relaunched.join(', ') : ''}${busy}`);
      v.update(); tick();
    });
  }
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
  const shMem = h('div', { class: 'sh-notes' });
  const shInput = h('input', { type: 'text', placeholder: 'note for every agent on this project', 'aria-label': 'Shared note', autocomplete: 'off' });
  const shForm = h('form', { class: 'sh-add' }, shInput, h('button', { class: 'btn small', type: 'submit', text: 'Add' }));
  const shareSec = h('section', { class: 'side-sec' },
    h('div', { class: 'side-h' }, h('h2', { text: 'Shared context' })),
    h('div', { class: 'muted small sh-hint', text: 'Agents pull this with sb context when they need it; nothing is pushed into them.' }),
    shBox, h('h2', { class: 'sh-nh', text: 'Notes' }), shNotes, shForm,
    h('h2', { class: 'sh-nh', text: 'Project memory' }), shMem);
  shellBtn.addEventListener('click', () => openShell(pid));

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
  const el = h('div', { class: 'pv' },
    h('div', { class: 'pv-head' }, h('a', { class: 'back', href: '#/', text: '← Projects', title: 'Back to all projects' }), pinBtn, name, goal,
      h('span', { class: 'spacer' }), seg, addBtn, summonBtn, shellBtn, outRead, outWrite, teamBtn, insBtn, memFilesBtn, shareBtn, limBtn, srcBtn, killBtn),
    form.el, summonForm,
    h('div', { class: 'pv-bar' }, tabsBox, h('span', { class: 'spacer' }), ctl.el),
    h('div', { class: 'pv-body' }, h('div', { class: 'stage-wrap' }, stage), side));
  form.el.classList.add('pv-form');
  addBtn.addEventListener('click', () => form.toggle());
  srcBtn.addEventListener('click', () => { showSrc = !showSrc; lsSet('sb.src', showSrc ? '1' : '0'); layout(); });
  limBtn.addEventListener('click', () => { showLim = !showLim; lsSet('sb.lim', showLim ? '1' : '0'); layout(); });
  shareBtn.addEventListener('click', () => { showShare = !showShare; lsSet('sb.share', showShare ? '1' : '0'); layout(); if (showShare) loadShared(); });
  const focusedAgent = () => { const a = picks[focus] && S.agents.get(picks[focus]); return a && AGENT_CLIS.includes(a.provider) ? a : null; };
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
      const d = await api('GET', `/projects/${enc(pid)}/context?notes=30`);
      shBox.replaceChildren(...d.agents.map((x) => h('div', { class: 'sh-agent' },
        h('div', { class: 'sh-name' }, h('span', { class: 'dot s-' + (x.state === 'busy' ? 'busy' : x.state === 'stopped' ? 'stopped' : 'idle') }), ' ', x.title || x.id,
          x.forked_from ? h('span', { class: 'muted small', text: ` · fork of ${x.forked_from}` }) : null),
        x.asked ? h('div', { class: 'sh-line small' }, h('span', { class: 'muted', text: 'asked ' }), x.asked) : null,
        x.said ? h('div', { class: 'sh-line small' }, h('span', { class: 'muted', text: 'said ' }), x.said) : null)));
      if (!d.agents.length) shBox.replaceChildren(h('div', { class: 'muted small', text: 'No agents yet.' }));
      shNotes.replaceChildren(...(d.notes.length ? d.notes.map((n) => h('div', { class: 'sh-note small' },
        h('span', { class: 'muted', text: `${n.agent} · ${ago(n.created_at)} ` }), n.text)) : [h('div', { class: 'muted small', text: 'No notes yet.' })]));
      const mem = await api('GET', `/projects/${enc(pid)}/memory?n=5`);
      shMem.replaceChildren(
        mem.memory ? h('div', { class: 'sh-note small sh-mem', text: mem.memory })
          : h('div', { class: 'muted small', text: 'Nothing yet. It is written when a conversation on this project is cleared or an agent is removed.' }),
        mem.updated_at ? h('div', { class: 'muted small', text: `updated ${ago(mem.updated_at)} · ${mem.total} ended conversation${mem.total === 1 ? '' : 's'} on record (sb memory)` }) : null,
        ...mem.sessions.map((x) => h('div', { class: 'sh-note small', title: x.path || '' },
          h('span', { class: 'muted', text: `${x.agent} · ${ago(x.created_at)} ` }),
          x.summary || (x.state === 'pending' || x.state === 'working' ? 'being summarized…' : (x.last_ask || x.first_ask || x.note || '')))));
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
      if (picks[i] && (!S.agents.size || (S.agents.has(picks[i]) && S.agents.get(picks[i]).project === pid))) continue;
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
      const pinned = !!(p && p.pinned);
      pinBtn.classList.toggle('on', pinned);
      setAttr(pinBtn, 'aria-pressed', pinned ? 'true' : 'false');
      setAttr(pinBtn, 'title', pinned ? 'Pinned to top of Projects — click to unpin' : 'Pin project to top of Projects');
      for (const [btn, key, verb] of [[outRead, 'outside_read', 'read'], [outWrite, 'outside_write', 'write']]) {
        const on = !!(p && p[key]);
        setAttr(btn, 'aria-pressed', on ? 'true' : 'false');
        setAttr(btn, 'title', `${on ? 'On' : 'Off'}: this project's agents may ${on ? '' : 'not '}${verb} files outside its folder. Click to turn it ${on ? 'off' : 'on'}.`);
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
      const own = aid && S.agents.get(aid);
      if (own && own.project !== pid) { location.hash = agentHref(own); return; }  // opens on its own project
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
let restarting = false, pageStartedAt = null, pageOutdated = false;
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
$('#stale-restart').addEventListener('click', () => pageOutdated ? location.reload() : restartHub());

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

/* ---------- the owner's Inbox: decisions to answer and needs to acknowledge, per project (switchboard/inbox.py) ---------- */
/* one item: who filed it, age, the text, one button per option, an inline answer box (decisions), Dismiss / Acknowledge */
function inboxRow() {
  const kind = h('span', { class: 'ib-kind' });
  const who = h('span', { class: 'ib-who' });
  const age = h('span', { class: 'ib-age' });
  const text = h('div', { class: 'ib-text' });
  const opts = h('div', { class: 'ib-opts' });
  const optBtns = keyedList(opts, (o) => o, (o) => {
    const b = h('button', { class: 'btn small ib-opt', type: 'button', text: o });
    guarded(b, () => cross(o));
    return { el: b, update() {} };
  });
  const txt = h('input', { type: 'text', class: 'ib-txt', 'aria-label': 'Your answer', autocomplete: 'off' });
  const send = h('button', { class: 'btn primary small', type: 'submit', text: 'Answer' });
  const dismiss = h('button', { class: 'pbtn ib-dismiss', type: 'button' });
  const ignore = h('button', { class: 'pbtn ib-ignore', type: 'button', text: 'Ignore', title: 'Just remove it from the inbox: the agent is told nothing and keeps doing what it was doing' });
  const form = h('form', { class: 'ib-form' }, txt, send, dismiss, ignore);
  const adoptSel = h('select', { class: 'ib-adopt-sel', 'aria-label': 'File under a project' });
  const adopt = h('label', { class: 'ib-adopt', title: 'Files this under the project. Whatever sent it is remembered: its other notices move too, and later ones land there.' }, 'File under', adoptSel);
  const el = h('div', { class: 'ib-item' }, h('div', { class: 'ib-h' }, kind, who, age), text, opts, form, adopt);
  let cur = null, onDone = null;
  adoptSel.addEventListener('change', async () => {
    const pid = adoptSel.value;
    if (!pid || !cur) return;
    try {
      const r = await api('POST', `/inbox/${enc(cur.id)}/adopt`, { project: pid });
      toast(`Filed under ${projName(pid)}` + (r.source ? `; ${r.source} lands there from now on` : ''));
      if (onDone) onDone();
    } catch (err) { toast(err.message, 'err'); adoptSel.value = ''; }
  });
  async function cross(answer, quiet) {
    const r = await api('POST', `/inbox/${enc(cur.id)}/done`, { answer: answer || undefined, from: 'owner', quiet: !!quiet });
    toast(r.state === 'answered' ? `Answered ${cur.agent || ''}: ${answer}` : r.state === 'acked' ? 'Acknowledged' : r.state === 'dismissed' ? 'Dismissed' : r.state === 'ignored' ? 'Ignored (the agent was not told)' : `Already ${r.state}`);
    if (onDone) onDone();
  }
  guarded(ignore, () => cross(null, true));
  form.addEventListener('submit', async (e) => {
    e.preventDefault();
    const a = txt.value.trim();
    if (!a) { txt.focus(); return; }
    send.disabled = true;
    try { await cross(a); txt.value = ''; } catch (err) { toast(err.message, 'err'); } finally { send.disabled = false; }
  });
  guarded(dismiss, async () => {
    if (cur.kind === 'decision' && !confirm(`Close this question from ${cur.agent || 'an agent'} without answering? The agent is told it is no longer wanted.\n\n${cur.text}`)) return;
    await cross(null);
  });
  return {
    el,
    update(it, done) {
      cur = it; onDone = done;
      const dec = it.kind === 'decision', live = !!it.choices;  // live: a dialog open in the agent's terminal
      setText(kind, dec ? 'question' : live ? 'in its terminal' : 'needs you'); setCls(kind, 'ib-kind ' + (dec ? 'dec' : 'need'));
      setText(who, it.source || it.agent || 'someone'); setAttr(who, 'title', it.id);
      adopt.hidden = it.project !== 'unsorted';
      if (!adopt.hidden) projectOptions(adoptSel, 'a project…');
      setText(age, it.age_s != null ? fmtDur(it.age_s) + ' ago' : ago(it.created_at)); setAttr(age, 'title', absTime(it.created_at));
      setText(text, it.text || '');
      optBtns.sync(it.options || []);
      opts.hidden = !(it.options || []).length;
      txt.hidden = send.hidden = live || !(dec || it.agent);  // a terminal choice takes one of its options; a notice can be replied to
      setText(send, dec ? 'Answer' : 'Reply');
      setAttr(txt, 'placeholder', dec ? ((it.options || []).length ? 'or type an answer… (Enter)' : 'Type your answer… (Enter)') : 'Reply to the agent… (Enter)');
      setText(dismiss, dec ? 'Dismiss' : live ? 'Hide' : 'Acknowledge');
      setAttr(dismiss, 'title', dec ? 'Close the question without answering; the agent is told it is no longer wanted'
        : live ? 'Cross it off here; the agent keeps waiting in its terminal until someone answers' : 'Cross it off; the agent is told you saw it');
      setCls(el, 'ib-item ' + (dec ? 'dec' : 'need'));
    },
  };
}
function inboxGroup() {
  const name = h('a', { class: 'ib-proj' });
  const n = h('span', { class: 'n' });
  const el = h('div', { class: 'ib-group' }, name, n);
  return { el, update(g) { setText(name, g.name); setAttr(name, 'href', g.pid && g.pid !== 'unsorted' ? '#/p/' + enc(g.pid) : '#/'); setText(n, g.count); } };
}
function makeInbox() {
  const btn = $('#tb-inbox');
  const badge = $('#tb-inbox-n');
  const title = h('span', { class: 'ib-title' });
  const scopeEl = h('span', { class: 'ib-scope' });
  const close = h('button', { class: 'pbtn ib-close', type: 'button', text: '✕', title: 'Close (Esc)' });
  const list = h('div', { class: 'ib-list' });
  const rows = keyedList(list, (x) => x.key, (x) => (x.kind === 'group' ? inboxGroup() : inboxRow()));
  const empty = h('div', { class: 'muted small ib-empty' });
  const card = h('div', { class: 'inbox-card', id: 'inbox-card', role: 'dialog', 'aria-label': 'Inbox', hidden: true },
    h('div', { class: 'ib-head' }, title, scopeEl, h('span', { class: 'spacer' }), close), list, empty);
  document.body.append(card);
  let open = location.hash === '#/inbox' || lsGet('sb.inbox.open') === '1', pid = parseRoute().pid, data = null, inflight = false, last = 0, lastOv = null;
  const refetch = () => fetchItems(true);
  async function fetchItems(force) {
    if (!open || inflight || (!force && Date.now() - last < 2500)) return;
    inflight = true;
    try { data = await api('GET', '/inbox' + (pid ? '?project=' + enc(pid) : '')); last = Date.now(); render(); }
    catch (e) { setText(empty, 'Inbox read failed: ' + e.message); empty.hidden = false; }
    finally { inflight = false; }
  }
  function render() {
    setText(scopeEl, pid ? projName(pid) : 'all projects');
    if (!data || data.project !== (pid || null)) { rows.sync([]); empty.hidden = true; setText(title, 'Inbox'); return; }
    const its = data.items || [];
    setText(title, `Inbox · ${its.length}`);
    const items = [];
    if (pid) for (const it of its) items.push({ ...it, kind: it.kind, key: it.id });
    else {
      const by = new Map();
      for (const it of its) { if (!by.has(it.project)) by.set(it.project, []); by.get(it.project).push(it); }
      for (const [p, ds] of by) {  // the API is newest first, so the first group holds the newest item
        items.push({ kind: 'group', key: 'g:' + p, pid: p, name: p === 'unsorted' ? 'No project' : projName(p), count: ds.length });
        for (const it of ds) items.push({ ...it, key: it.id });
      }
    }
    rows.sync(items);
    for (const [k, rec] of rows.map) if (!k.startsWith('g:')) rec.update(its.find((x) => x.id === k), refetch);
    empty.hidden = its.length > 0;
    setText(empty, pid ? `Nothing waits for you on ${projName(pid)}.` : 'Nothing waits for you.');
  }
  function show(on) {
    open = on; card.hidden = !on; setAttr(btn, 'aria-pressed', on ? 'true' : 'false'); lsSet('sb.inbox.open', on ? '1' : '');
    if (!on && location.hash === '#/inbox') history.replaceState(null, '', '#/');
    if (on) fetchItems(true);
  }
  btn.addEventListener('click', () => show(!open));
  close.addEventListener('click', () => show(false));
  document.addEventListener('keydown', (e) => { if (e.key === 'Escape' && open && !e.defaultPrevented && (document.activeElement == null || !document.activeElement.closest('.ib-txt'))) show(false); });
  card.hidden = !open; setAttr(btn, 'aria-pressed', open ? 'true' : 'false');
  return {
    el: card,
    show,
    scope(newPid) { if (newPid === pid) return; pid = newPid; data = null; render(); if (lastOv) this.update(lastOv); fetchItems(true); },
    update(ov) {  // every poll: the badge from overview.inbox, and a refetch while the card is open
      lastOv = ov;
      const ib = (ov && ov.inbox) || { count: 0, by_project: {} };
      const n = pid ? (ib.by_project || {})[pid] || 0 : ib.count || 0;
      setText(badge, n ? String(n) : ''); badge.hidden = !n;
      setAttr(btn, 'title', n ? `${n} request${n === 1 ? '' : 's'} wait${n === 1 ? 's' : ''} for you${pid ? ' on ' + projName(pid) : ''}${ib.count > n ? ` (${ib.count} across all projects)` : ''}` : 'Requests agents filed for you: questions to answer and needs to acknowledge');
      setCls(btn, 'pbtn tb-inbox' + (n ? ' has' : ''));
      fetchItems(false);
    },
  };
}

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
  const ins = location.hash.match(/^#\/insights(?:\/([^/]+))?/);
  if (ins) return { pid: null, insights: ins[1] ? decodeURIComponent(ins[1]) : '' };
  const m = location.hash.match(/^#\/p\/([^/]+)(?:\/([^/]+))?/);
  return m ? { pid: decodeURIComponent(m[1]), aid: m[2] ? decodeURIComponent(m[2]) : null } : { pid: null };
}
function route() {
  const r = parseRoute();
  if (location.hash === '#/inbox') { INBOX.scope(null); INBOX.show(true); }
  const app = $('#app');
  const own = r.aid && S.agents.get(r.aid);
  if (own && own.project !== r.pid) { location.hash = agentHref(own); return; }  // an agent opens on its own project only
  if (r.pid && S.view && S.view.pid === r.pid) { S.view.select(r.aid); return; }
  if (S.view) S.view.destroy();
  if (r.insights !== undefined) {
    S.view = insightsView(r.insights);
    app.className = 'app';
    app.replaceChildren(S.view.el);
    INBOX.scope(r.insights || null);
    if (S.ov) S.view.update();
    S.view.start();
    document.title = 'Insights · Switchboard';
    return;
  }
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
  INBOX.scope(r.pid || null);
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
const INBOX = makeInbox();
const HALT = haltBanner();
$('#stale-banner').after(HALT.el);  // the HALT banner sits under the stale-code banner, above the page
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
      if (Date.now() + clockSkew - parseTime(ov.focus.ts) < 15000) {
        if (ov.focus.inbox) { location.hash = '#/inbox'; INBOX.scope(null); INBOX.show(true); }
        else location.hash = '#/p/' + enc(ov.focus.project) + '/' + enc(ov.focus.agent);
      }
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
    if (!pageStartedAt && ov.code) pageStartedAt = ov.code.started_at;
    pageOutdated = !!(ov.code && pageStartedAt !== ov.code.started_at);
    setText($('#stale-restart'), pageOutdated ? 'Refresh page' : 'Restart now');
    const stale = pageOutdated ? 'Switchboard updated. Refresh this page to load the latest controls.' : !ov.code ? 'Switchboard is running older code than this page. Restart to update.'
      : ov.code.stale ? `New Switchboard code is ready (${ov.code.disk}; running ${ov.code.running}). Restart when it suits you; terminals keep running.` : '';
    banner.hidden = !stale || restarting;
    setText($('#stale-text'), stale);
    S.code = ov.code;
    HALT.update(ov.fleet && ov.fleet.halt);
    INBOX.update(ov);
    const agents = [...S.agents.values()];
    setText($('#tb-sum'), `${agents.filter((a) => a.running).length} running · ${agents.filter((a) => a.state === 'busy').length} working`);
    if (firstLoad) {  // a link to another project's agent opens it on its own project
      const r = parseRoute(), own = r.aid && S.agents.get(r.aid);
      if (own && own.project !== r.pid) location.hash = agentHref(own);
    }
    if (S.view) {
      S.view.update();
      if (firstLoad && S.view.pid) S.view.select(null);
      document.title = (S.view.title ? S.view.title + ' · ' : '') + 'Switchboard';
    }
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
