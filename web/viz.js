/* Switchboard visualizations: usage insights, per-agent context, project team.
   Design adapted from dsh-context by bowenliang123 (https://github.com/bowenliang123/dsh-context, Apache-2.0):
   the donut, heatmap, tally rows and KPI layout follow its Context Insights page. Ported to plain SVG + DOM; no
   code or assets from that project are loaded at runtime. Uses h(), ago(), fmtDur(), api() from app.js. */
'use strict';

const VIZ = {
  palette: ['#3b82f6', '#14b8a6', '#f59e0b', '#a855f7', '#22c55e', '#ec4899', '#f97316', '#6366f1', '#06b6d4', '#84cc16'],
  comp: { cache_read: '#14b8a6', cache_write: '#f59e0b', input: '#22c55e', reasoning: '#a855f7', output: '#3b82f6' },
  compName: { cache_read: 'Cache read', cache_write: 'Cache write', input: 'Fresh input', reasoning: 'Reasoning', output: 'Output' },
};
const SVGNS = 'http://www.w3.org/2000/svg';
function sv(tag, attrs, ...kids) {
  const el = document.createElementNS(SVGNS, tag);
  for (const [k, v] of Object.entries(attrs || {})) if (v != null) el.setAttribute(k, v);
  for (const k of kids.flat()) if (k != null) el.append(k);
  return el;
}
function fmtN(n) {
  if (n == null || isNaN(n)) return '—';
  const a = Math.abs(n);
  if (a >= 1e9) return (n / 1e9).toFixed(a >= 1e10 ? 1 : 2) + 'B';
  if (a >= 1e6) return (n / 1e6).toFixed(a >= 1e8 ? 0 : 1) + 'M';
  if (a >= 1e3) return (n / 1e3).toFixed(a >= 1e5 ? 0 : 1) + 'k';
  return String(Math.round(n));
}
function fmtUsd(n) {
  if (n == null || isNaN(n)) return '—';
  if (n >= 1000) return '$' + Math.round(n).toLocaleString();
  if (n >= 10) return '$' + n.toFixed(0);
  return '$' + n.toFixed(2);
}
function fmtSec(s) {
  if (s == null || isNaN(s)) return '—';
  if (s < 60) return (s < 10 && s % 1 ? s.toFixed(1) : Math.round(s)) + 's';
  if (s < 3600) return (s / 60).toFixed(s < 600 ? 1 : 0) + 'm';
  return (s / 3600).toFixed(1) + 'h';
}
const pct = (x, d = 1) => (x == null ? '—' : (x * 100).toFixed(d) + '%');

/* one floating tooltip for every [data-tip] element */
let vizTipEl = null;
function vizTip() {
  if (vizTipEl) return;
  const el = vizTipEl = h('div', { class: 'vz-tip', role: 'tooltip' });
  document.body.append(el);
  const show = (e) => {
    const t = e.target.closest && e.target.closest('[data-tip]');
    if (!t) { el.classList.remove('on'); return; }
    el.textContent = t.getAttribute('data-tip');
    const r = t.getBoundingClientRect();
    el.classList.add('on');
    const w = el.offsetWidth, hh = el.offsetHeight;
    el.style.left = Math.max(6, Math.min(window.innerWidth - w - 6, r.left + r.width / 2 - w / 2)) + 'px';
    el.style.top = (r.top - hh - 8 < 4 ? r.bottom + 8 : r.top - hh - 8) + 'px';
  };
  document.addEventListener('mouseover', show);
  document.addEventListener('focusin', show);
  document.addEventListener('scroll', () => el.classList.remove('on'), true);
}

/* ring chart: segments [{key, label, value, color}] around a centre figure (dsh-context donut.tsx geometry) */
function donut(segments, top, sub, size = 118) {
  const total = segments.reduce((s, x) => s + (x.value > 0 ? x.value : 0), 0);
  const svg = sv('svg', { viewBox: '0 0 42 42', width: size, height: size, 'aria-hidden': 'true' });
  if (!total) svg.append(sv('circle', { class: 'vz-track', cx: 21, cy: 21, r: 15.9155 }));
  let acc = 0;
  const live = segments.filter((x) => x.value > 0);
  for (const s of live) {
    const p = s.value / total * 100;
    let len = p, off = 100 - acc + 25;
    if (live.length > 1) { const cut = Math.min(0.25, len / 4); len -= cut * 2; off -= cut; }
    acc += p;
    svg.append(sv('circle', { class: 'vz-seg', cx: 21, cy: 21, r: 15.9155, style: `stroke:${s.color}`,
      'stroke-dasharray': `${len} ${100 - len}`, 'stroke-dashoffset': off,
      'data-tip': `${s.label}: ${fmtN(s.value)} (${(s.value / total * 100).toFixed(1)}%)` }));
  }
  return h('div', { class: 'vz-donut', style: `width:${size}px;height:${size}px` }, svg,
    h('div', { class: 'vz-donut-c' }, h('b', { text: top, style: `font-size:${Math.max(11, Math.round(size * 0.14))}px` }),
      sub ? h('span', { text: sub, style: `font-size:${Math.max(9, Math.round(size * 0.095))}px` }) : null));
}

/* legend grid beside a donut: colour, label, share, value */
function legend(segments, fmt = (v) => '≈' + fmtN(v)) {
  const total = segments.reduce((s, x) => s + (x.value > 0 ? x.value : 0), 0) || 1;
  return h('div', { class: 'vz-legend' }, segments.map((s) => h('div', { class: 'vz-leg' },
    h('i', { style: `background:${s.color}` }), h('span', { class: 'vz-leg-l', text: s.label }),
    h('b', { text: (s.value / total * 100).toFixed(1) + '%' }), h('small', { text: fmt(s.value) }))));
}

/* the last 7 days as paired bars: tokens (left axis) and estimated cost (right axis) */
function weekBars(days, today) {
  const keys = [];
  const t0 = new Date(today + 'T12:00:00');
  for (let i = 6; i >= 0; i--) { const d = new Date(t0); d.setDate(d.getDate() - i); keys.push(d.toISOString().slice(0, 10)); }
  const rows = keys.map((k) => ({ k, ...(days[k] || { tokens: 0, cost: 0, agents: 0 }) }));
  const W = 560, H = 150, L = 44, R = 46, T = 14, B = 22;
  const mt = Math.max(1, ...rows.map((r) => r.tokens)), mc = Math.max(0.01, ...rows.map((r) => r.cost));
  const svg = sv('svg', { viewBox: `0 0 ${W} ${H}`, class: 'vz-week', preserveAspectRatio: 'none' });
  for (let g = 0; g <= 4; g++) {
    const y = T + (H - T - B) * (1 - g / 4);
    svg.append(sv('line', { x1: L, x2: W - R, y1: y, y2: y, class: 'vz-grid' }),
      sv('text', { x: L - 6, y: y + 3, class: 'vz-ax', 'text-anchor': 'end' }, fmtN(mt * g / 4)),
      sv('text', { x: W - R + 6, y: y + 3, class: 'vz-ax' }, fmtUsd(mc * g / 4)));
  }
  const step = (W - L - R) / 7, bw = Math.min(16, step / 3.2);
  rows.forEach((r, i) => {
    const cx = L + step * (i + 0.5), ph = H - T - B;
    const th = r.tokens / mt * ph, ch = r.cost / mc * ph;
    const tip = `${r.k}\n${fmtN(r.tokens)} tokens · ${fmtUsd(r.cost)} · ${r.agents} agents`;
    svg.append(sv('rect', { x: cx - bw - 1, y: T + ph - th, width: bw, height: Math.max(th, r.tokens ? 1 : 0), rx: 1.5, fill: VIZ.palette[0], 'data-tip': tip }),
      sv('rect', { x: cx + 1, y: T + ph - ch, width: bw, height: Math.max(ch, r.cost ? 1 : 0), rx: 1.5, fill: VIZ.palette[4], 'data-tip': tip }),
      sv('text', { x: cx, y: H - 6, class: 'vz-ax', 'text-anchor': 'middle' }, r.k.slice(5)));
  });
  return h('div', null, svg, h('div', { class: 'vz-keys' },
    h('span', null, h('i', { style: `background:${VIZ.palette[0]}` }), 'Tokens (billed)'),
    h('span', null, h('i', { style: `background:${VIZ.palette[4]}` }), 'Est. cost (API list price)')));
}

/* GitHub-style activity grid: one column per week, Sunday on top, shaded by billed tokens (dsh-context heatmap.tsx) */
function heatmap(days, today, weeks = 9) {
  const t = new Date(today + 'T12:00:00');
  const lastSun = new Date(t); lastSun.setDate(t.getDate() - t.getDay());
  const max = Math.max(1, ...Object.values(days).map((d) => d.tokens));
  const lvl = (n) => (n <= 0 ? 0 : n / max <= 0.25 ? 1 : n / max <= 0.5 ? 2 : n / max <= 0.75 ? 3 : 4);
  const MON = ['Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun', 'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec'];
  const cols = [];
  for (let w = weeks - 1; w >= 0; w--) {
    const sun = new Date(lastSun); sun.setDate(lastSun.getDate() - 7 * w);
    const cells = [];
    for (let d = 0; d < 7; d++) {
      const day = new Date(sun); day.setDate(sun.getDate() + d);
      const k = `${day.getFullYear()}-${String(day.getMonth() + 1).padStart(2, '0')}-${String(day.getDate()).padStart(2, '0')}`;
      if (k > today) { cells.push(h('span', { class: 'vz-hc vz-hf' })); continue; }
      const e = days[k];
      cells.push(h('span', { class: 'vz-hc vz-h' + (e ? lvl(e.tokens) : 0) + (k === today ? ' vz-today' : ''),
        'data-tip': e ? `${k}\n${fmtN(e.tokens)} tokens · ${fmtUsd(e.cost)} · ${e.agents} agents` : `${k}\nno activity` }));
    }
    const first = sun.getDate() <= 7 || w === weeks - 1;
    cols.push(h('div', { class: 'vz-hcol' }, h('span', { class: 'vz-hm', text: first ? MON[sun.getMonth()] : '' }), cells));
  }
  return h('div', { class: 'vz-heat' },
    h('div', { class: 'vz-hgrid' },
      h('div', { class: 'vz-hwd' }, h('span'), ['', 'Mon', '', 'Wed', '', 'Fri', ''].map((x) => h('span', { text: x }))), cols),
    h('div', { class: 'vz-hleg' }, 'less', [0, 1, 2, 3, 4].map((l) => h('span', { class: 'vz-hc vz-h' + l })), 'more'));
}

/* ranked list with a proportional underline (dsh-context overviewTally.tsx) */
function tally(data, empty, fmt = (n) => '×' + n) {
  if (!data || !data.rows.length) return h('p', { class: 'muted vz-empty', text: empty || 'Nothing in this range.' });
  const max = data.rows[0].n || 1;
  return h('div', { class: 'vz-tally' }, data.rows.map((r, i) => h('div', { class: 'vz-trow', 'data-tip': `${r.name}: ${fmt(r.n)}${r.last ? ' · last ' + ago(r.last) : ''}` },
    h('i', { style: `background:${VIZ.palette[i % VIZ.palette.length]}` }),
    h('code', { text: r.name }), h('b', { text: fmt(r.n) }), h('small', { text: r.last ? ago(r.last) : '' }),
    h('u', { style: `width:${Math.max(2, r.n / max * 100)}%;background:${VIZ.palette[i % VIZ.palette.length]}` }))));
}

function kpi(label, value, sub, tip) {
  return h('div', { class: 'vz-kpi', 'data-tip': tip || null }, h('span', { text: label }), h('b', { text: value }), h('small', { text: sub || '' }));
}
function card(title, extra, ...body) {
  return h('section', { class: 'vz-card' }, h('header', null, h('h3', { text: title }), extra || null), ...body);
}
function segButtons(opts, cur, onPick) {
  return h('div', { class: 'vz-segs' }, opts.map(([v, label]) => {
    const b = h('button', { type: 'button', class: 'vz-sb', text: label, 'aria-pressed': String(v === cur) });
    b.addEventListener('click', () => onPick(v));
    return b;
  }));
}

/* context ring for an agent: share of its window in use */
function ctxRing(a, size = 64) {
  const used = a.context_tokens || 0, win = a.context_window || (a.provider === 'codex' ? 272000 : 1000000);
  const share = Math.min(1, used / win);
  const col = share > 0.8 ? '#ef4444' : share > 0.5 ? '#f59e0b' : '#14b8a6';
  return donut([{ label: 'In context', value: used, color: col }, { label: 'Free', value: Math.max(0, win - used), color: 'var(--line)' }],
    used ? fmtN(used) : '—', used ? pct(share, 0) : '', size);
}

/* ---------- the Insights page: #/insights or #/insights/<project> ---------- */
function insightsView(pid) {
  vizTip();
  let rng = lsGet('sb.ins.range') || '7d', data = null, timer = null, q = '';
  const body = h('div', { class: 'vz-body' }, h('p', { class: 'muted pad', text: 'Reading transcripts…' }));
  const projSel = h('select', { class: 'vz-sel', 'aria-label': 'Project' });
  projSel.addEventListener('change', () => { location.hash = projSel.value ? '#/insights/' + enc(projSel.value) : '#/insights'; });
  const rngBox = h('span');
  const status = h('span', { class: 'muted vz-status' });
  const el = h('div', { class: 'vz-page' },
    h('div', { class: 'vz-head' }, h('h1', { text: 'Insights' }), projSel, h('span', { class: 'vz-grow' }), status, rngBox),
    body);
  const setRange = (v) => { rng = v; lsSet('sb.ins.range', v); load(); };
  async function load() {
    clearTimeout(timer);
    try {
      data = await api('GET', `/insights?range=${rng}${pid ? '&project=' + enc(pid) : ''}`);
      render();
    } catch (e) { body.replaceChildren(h('p', { class: 'pad err', text: 'Could not load insights: ' + e.message })); }
    timer = setTimeout(load, 30000);
  }
  function renderAgents(box) {
    const words = q.toLowerCase().split(/\s+/).filter(Boolean);
    const rows = data.agents.filter((a) => !words.length || words.every((w) => `${a.title} ${a.id} ${projName(a.project)} ${a.model || ''}`.toLowerCase().includes(w)));
    box.replaceChildren(...rows.slice(0, 60).map((a) => {
      const live = S.agents.get(a.id);
      const card = h(live ? 'a' : 'div', { class: 'vz-acard' + (a.gone ? ' gone' : ''), href: live ? agentHref(live) : null },
        h('div', { class: 'vz-ah' }, h('b', { text: a.title }), h('small', { text: ago(a.last) })),
        h('div', { class: 'vz-atags' }, h('span', { class: 'vz-tag', text: projName(a.project) || 'no project' }),
          a.model ? h('span', { class: 'vz-tag dim', text: a.model }) : null, a.gone ? h('span', { class: 'vz-tag dim', text: 'removed' }) : null),
        h('div', { class: 'vz-abody' }, ctxRing(a),
          h('dl', null, h('dt', { text: 'Turns' }), h('dd', { text: `${a.turns} · ${a.requests} req` }),
            h('dt', { text: 'Billed tokens' }), h('dd', { text: fmtN(a.billed) }),
            h('dt', { text: 'Cache hit' }), h('dd', { text: pct(a.cache_hit) }),
            h('dt', { text: 'Est. cost' }), h('dd', { text: a.provider === 'codex' ? 'unpriced' : fmtUsd(a.cost) }),
            h('dt', { text: 'Active' }), h('dd', { text: fmtDur(a.active_s) || '—' }))));
      return card;
    }), rows.length > 60 ? h('p', { class: 'muted', text: `+${rows.length - 60} more — search to narrow` }) : null);
  }
  function render() {
    const d = data, k = d.kpi;
    setText(status, d.pending ? `still reading ${fmtN(d.pending)}B of transcripts…` : d.scanned_at ? 'updated ' + ago(d.scanned_at) : '');
    const comp = ['input', 'cache_write', 'reasoning', 'output'].map((key) => ({ key, label: VIZ.compName[key], value: d.composition[key], color: VIZ.comp[key] }));
    const projSegs = d.projects.slice(0, 7).map((p, i) => ({ label: projName(p.id) || 'no project', value: p.billed, color: VIZ.palette[i] }));
    const rest = d.projects.slice(7).reduce((s, p) => s + p.billed, 0);
    if (rest) projSegs.push({ label: 'other', value: rest, color: '#6b7280' });
    const modelSegs = d.models.map((m, i) => ({ label: m.model, value: m.tokens, color: VIZ.palette[(i + 2) % VIZ.palette.length] }));
    const search = h('input', { type: 'search', class: 'vz-search', placeholder: 'Filter agents by name, project or model', value: q });
    const agentBox = h('div', { class: 'vz-agrid' });
    search.addEventListener('input', () => { q = search.value; renderAgents(agentBox); });
    const rngWord = { '1d': 'today', '7d': 'last 7 days', '30d': 'last 30 days', all: 'all time' }[rng];
    body.replaceChildren(
      h('div', { class: 'vz-top' },
        h('div', { class: 'vz-kpis' },
          kpi('Agents', String(k.agents), `${k.turns} turns · ${fmtN(k.requests)} requests`),
          kpi('Billed tokens', fmtN(k.billed), `${fmtN(k.tokens)} incl. cache reads`, 'input + cache writes + output; cache reads are cheap and re-counted on every request'),
          kpi('Est. cost', fmtUsd(k.cost), k.unpriced ? `${fmtN(k.unpriced)} requests unpriced` : 'API list price', 'What these tokens would cost at Anthropic API list prices. Subscription accounts are not billed per token.'),
          kpi('Cache hit', pct(k.cache_hit, 2), 'of input tokens'),
          kpi('Tool calls', fmtN(k.tool_calls), `${d.tools.kinds} tools`),
          kpi('Active time', fmtDur(k.active_s) || '—', 'summed across agents', 'time between transcript lines less than 5 minutes apart')),
        card('Last 7 days', null, weekBars(d.days, d.today))),
      h('div', { class: 'vz-row3' },
        card('Billed tokens', h('small', { class: 'muted', text: rngWord }), h('div', { class: 'vz-dl' }, donut(comp, fmtN(k.billed), 'billed'), legend(comp)),
          h('p', { class: 'vz-foot', 'data-tip': 'Cache reads re-send the conversation on every request at ~1/10 to 1/20 of the input price, so they are shown apart.' },
            h('i', { style: `background:${VIZ.comp.cache_read}` }), `+ ${fmtN(d.composition.cache_read)} cache reads (${pct(k.cache_hit, 1)} of input)`)),
        card(pid ? 'By model' : 'By project', h('small', { class: 'muted', text: 'billed tokens' }),
          h('div', { class: 'vz-dl' }, pid ? donut(modelSegs, String(d.models.length), 'models') : donut(projSegs, String(d.projects.length), 'projects'), legend(pid ? modelSegs : projSegs))),
        card('Activity', h('small', { class: 'muted', text: '9 weeks' }), heatmap(d.days, d.today))),
      h('div', { class: 'vz-row3' },
        card('Tool calls', h('small', { class: 'muted', text: `${d.tools.kinds} tools · ${fmtN(d.tools.total)}` }), tally(d.tools)),
        card('Commands', h('small', { class: 'muted', text: `${d.commands.kinds} programs · ${fmtN(d.commands.total)}` }), tally(d.commands)),
        card('Skills', h('small', { class: 'muted', text: `${d.skills.kinds} skills · ${d.skills.total} loads` }), tally(d.skills, 'No skills loaded in this range.'))),
      h('div', { class: 'vz-ahead' }, h('h2', null, 'Agents ', h('span', { class: 'n', text: String(d.agents.length) })), search),
      agentBox);
    renderAgents(agentBox);
  }
  return {
    el, title: 'Insights',
    update() {
      const opts = [['', 'All projects'], ...[...S.projects.values()].filter((p) => !p.archived).sort((a, b) => a.name.localeCompare(b.name)).map((p) => [p.id, p.name])];
      if (projSel.options.length !== opts.length) projSel.replaceChildren(...opts.map(([v, n]) => h('option', { value: v, text: n })));
      projSel.value = pid || '';
      if (!rngBox.firstChild || rngBox.dataset.r !== rng) {
        rngBox.dataset.r = rng;
        rngBox.replaceChildren(segButtons([['1d', 'Today'], ['7d', '7 days'], ['30d', '30 days'], ['all', 'All']], rng, setRange));
      }
    },
    start() { load(); },
    destroy() { clearTimeout(timer); },
  };
}

/* ---------- one agent's context: a modal opened from a pane's Context button ---------- */
VIZ.cat = {
  system: ['System prompt', '#6366f1'], tool_schemas: ['Tool schemas', '#f59e0b'], injected: ['Injected context', '#a855f7'],
  user: ['User messages', '#22c55e'], assistant: ['Assistant text', '#3b82f6'], tool_calls: ['Tool calls', '#f97316'],
  tool_results: ['Tool results', '#14b8a6'], other: ['Other (thinking, unplaced)', '#6b7280'],
};

/* horizontal stacked bar: used parts, then the free part of the window */
function ctxBar(cats, mix, win) {
  const used = cats.reduce((s, k) => s + (mix[k] || 0), 0);
  const total = Math.max(win || used, used) || 1;
  return h('div', { class: 'vz-cbar' }, cats.map((k) => (mix[k] > 0 ? h('span', {
    style: `width:${mix[k] / total * 100}%;background:${VIZ.cat[k][1]}`, 'data-tip': `${VIZ.cat[k][0]}: ≈${fmtN(mix[k])} (${(mix[k] / used * 100).toFixed(1)}% of context)` }) : null)),
  win ? h('span', { class: 'vz-cfree', style: `width:${Math.max(0, (total - used) / total * 100)}%`, 'data-tip': `Free: ${fmtN(total - used)} of ${fmtN(win)}` }) : null);
}
function ctxLegend(cats, mix) {
  const used = cats.reduce((s, k) => s + (mix[k] || 0), 0) || 1;
  return h('div', { class: 'vz-legend vz-legend3' }, cats.map((k) => h('div', { class: 'vz-leg' },
    h('i', { style: `background:${VIZ.cat[k][1]}` }), h('span', { class: 'vz-leg-l', text: VIZ.cat[k][0] }),
    h('b', { text: (mix[k] / used * 100).toFixed(0) + '%' }), h('small', { text: '≈' + fmtN(mix[k]) }))));
}

/* per-request trend: stacked composition bars, latency line on the right axis, turn ticks, compaction marks */
function trendChart(d, onPick, picked) {
  const S2 = d.series, n = S2.length;
  const W = 1200, H = 250, L = 52, R = 50, T = 16, B = 30, pw = W - L - R, ph = H - T - B;
  const svg = sv('svg', { viewBox: `0 0 ${W} ${H}`, class: 'vz-trend' });
  if (!n) return h('p', { class: 'muted vz-empty', text: 'No requests yet.' });
  const maxC = Math.max(1, ...S2.map((p) => p.ctx)), maxD = Math.max(1, ...S2.map((p) => p.dur || 0));
  for (let g = 0; g <= 4; g++) {
    const y = T + ph * (1 - g / 4);
    svg.append(sv('line', { x1: L, x2: W - R, y1: y, y2: y, class: 'vz-grid' }),
      sv('text', { x: L - 6, y: y + 3, class: 'vz-ax', 'text-anchor': 'end' }, fmtN(maxC * g / 4)),
      sv('text', { x: W - R + 6, y: y + 3, class: 'vz-ax' }, fmtSec(maxD * g / 4)));
  }
  const bw = pw / n, gap = bw > 4 ? 1 : 0;
  let lastTurn = null, lastLabelX = -99;
  S2.forEach((p, i) => {
    const x = L + i * bw;
    let y = T + ph;
    const g = sv('g', { class: 'vz-tb' + (i === picked ? ' on' : ''), 'data-tip':
      `request ${p.index}${p.n > 1 ? ` (${p.n} merged)` : ''} · turn ${p.turn}\n${absTime(p.t)}\ncontext ${fmtN(p.ctx)} · out ${fmtN(p.out)}${p.think ? ` (${fmtN(p.think)} thinking)` : ''}` +
      `${p.dur ? ` · ${fmtSec(p.dur)}${p.n > 1 ? ' avg' : ''}` : ''}${p.tools && p.tools.length ? '\n' + p.tools.slice(0, 3).join('\n') : ''}` });
    d.cats.forEach((k, ci) => {
      const v = p.mix[ci] || 0;
      if (!v) return;
      const hh = v / maxC * ph;
      y -= hh;
      g.append(sv('rect', { x, y, width: Math.max(0.6, bw - gap), height: hh, style: `fill:${VIZ.cat[k][1]}` }));
    });
    g.append(sv('rect', { x, y: T, width: bw, height: ph, class: 'vz-hit' }));
    g.addEventListener('click', () => onPick(i));
    svg.append(g);
    if (p.turn !== lastTurn && x - lastLabelX > 34) {
      svg.append(sv('line', { x1: x, x2: x, y1: T + ph, y2: T + ph + 5, class: 'vz-axl' }),
        sv('text', { x: x + 1, y: H - 8, class: 'vz-ax' }, 't' + p.turn));
      lastLabelX = x;
    }
    lastTurn = p.turn;
  });
  for (const c of d.compactions || []) {
    const i = S2.findIndex((p) => p.index > c.at);
    if (i < 0) continue;
    const x = L + i * bw;
    svg.append(sv('line', { x1: x, x2: x, y1: T - 4, y2: T + ph, class: 'vz-compact' }),
      sv('text', { x: x + 3, y: T + 6, class: 'vz-ax vz-compact-t' }, 'compacted'));
  }
  const pts = S2.map((p, i) => (p.dur != null ? `${L + (i + 0.5) * bw},${T + ph - (p.dur / maxD) * ph}` : null)).filter(Boolean);
  if (pts.length > 1) svg.append(sv('polyline', { points: pts.join(' '), class: 'vz-durline' }));
  return h('div', null, svg, h('div', { class: 'vz-keys' },
    d.cats.map((k) => h('span', null, h('i', { style: `background:${VIZ.cat[k][1]}` }), VIZ.cat[k][0])),
    h('span', null, h('i', { class: 'vz-linekey' }), 'Latency (right axis)')));
}

function openContext(aid) {
  vizTip();
  let data = null, timer = null, picked = null;
  const body = h('div', { class: 'vz-mbody' }, h('p', { class: 'muted pad', text: 'Reading the transcript…' }));
  const title = h('h2');
  const sub = h('span', { class: 'muted' });
  const close = h('button', { class: 'btn ghost small', type: 'button', text: 'Close', 'aria-label': 'Close context view' });
  const box = h('div', { class: 'vz-modal', role: 'dialog', 'aria-modal': 'true', 'aria-label': 'Agent context' },
    h('div', { class: 'vz-mhead' }, title, sub, h('span', { class: 'vz-grow' }), close), body);
  const back = h('div', { class: 'vz-back' }, box);
  const done = () => { clearTimeout(timer); back.remove(); document.removeEventListener('keydown', onKey, true); };
  const onKey = (e) => { if (e.key === 'Escape') { e.stopPropagation(); done(); } };
  close.addEventListener('click', done);
  back.addEventListener('mousedown', (e) => { if (e.target === back) done(); });
  document.addEventListener('keydown', onKey, true);
  document.body.append(back);
  close.focus();
  async function load() {
    clearTimeout(timer);
    try { data = await api('GET', `/agents/${enc(aid)}/trace`); render(); } catch (e) { body.replaceChildren(h('p', { class: 'pad err', text: e.message })); }
    if (back.isConnected) timer = setTimeout(load, 10000);
  }
  function stepDetail(p) {
    const mix = Object.fromEntries(data.cats.map((k, i) => [k, p.mix[i] || 0]));
    return h('div', { class: 'vz-step' },
      h('div', { class: 'vz-steph' }, h('b', { text: `Request ${p.index} of ${data.requests} · turn ${p.turn}` }),
        h('span', { class: 'muted', text: absTime(p.t) }), h('span', { class: 'vz-grow' }),
        h('span', { class: 'vz-pill', text: `Context ${fmtN(p.ctx)}` }), h('span', { class: 'vz-pill', text: `Output ${fmtN(p.out)}` }),
        h('span', { class: 'vz-pill', text: `Cache ${pct(p.ctx ? p.cr / p.ctx : null, 1)}` }), p.dur ? h('span', { class: 'vz-pill', text: fmtSec(p.dur) }) : null),
      p.tools && p.tools.length ? h('div', { class: 'vz-steptools' }, p.tools.map((t) => h('code', { text: t }))) : null,
      ctxBar(data.cats, mix, 0), ctxLegend(data.cats, mix));
  }
  function render() {
    const d = data;
    if (d.unsupported || d.empty) { body.replaceChildren(h('p', { class: 'muted pad', text: d.unsupported || d.empty })); return; }
    setText(title, d.agent.title);
    setText(sub, `${projName(d.agent.project)} · ${d.model || d.agent.model || ''}`);
    const c = d.current, win = d.window || (d.provider === 'codex' ? 272000 : 1000000), t = d.totals;
    const toolTime = d.tool_time.reduce((s, r) => s + r.s, 0);
    const timing = [{ label: 'Model (thinking + writing)', value: t.model_s, color: '#3b82f6' },
      ...d.tool_time.slice(0, 6).map((r, i) => ({ label: `${r.name} (${r.runs} runs)`, value: r.s, color: VIZ.palette[(i + 1) % VIZ.palette.length] }))];
    const restT = d.tool_time.slice(6).reduce((s, r) => s + r.s, 0);
    if (restT) timing.push({ label: 'other tools', value: restT, color: '#6b7280' });
    const sel = picked == null || picked >= d.series.length ? d.series.length - 1 : picked;
    const detail = h('div');
    const pick = (i) => { picked = i; render(); };
    if (d.series.length) detail.append(stepDetail(d.series[sel]));
    const toolRows = { total: c.tool_results.reduce((s, r) => s + r.tokens, 0), kinds: c.tool_results.length,
      rows: c.tool_results.map((r) => ({ name: r.name, n: r.tokens })) };
    body.replaceChildren(
      h('div', { class: 'vz-kpis vz-kpis7' },
        kpi('Requests', fmtN(d.requests), `${d.turns} turns`),
        kpi('Context', fmtN(c.ctx), `${pct(c.ctx / win, 1)} of ${fmtN(win)}`),
        kpi('Output', fmtN(t.output), `${fmtN(t.reasoning)} thinking`),
        kpi('Cache hit', pct(t.cache_hit, 2), 'of input'),
        kpi('Est. cost', t.priced ? fmtUsd(t.cost) : 'unpriced', 'this session, API list price'),
        kpi('Model time', fmtDur(t.model_s) || '—', `tools ${fmtDur(toolTime) || '0s'}`),
        kpi('Compactions', String(d.compactions.length), d.compactions.length ? 'last ' + ago(d.compactions[d.compactions.length - 1].t) : 'none this session')),
      h('div', { class: 'vz-row2' },
        card('Current context', h('small', { class: 'muted', text: `≈ estimated from transcript text, fitted to the real ${fmtN(c.ctx)}` }),
          h('div', { class: 'vz-cbig' }, h('b', { text: fmtN(c.ctx) }), h('span', { class: 'muted', text: ` / ${fmtN(win)} tokens` }),
            h('span', { class: 'vz-grow' }), h('b', { text: pct(c.ctx / win, 1) }), h('span', { class: 'muted', text: ' of the window' })),
          ctxBar(d.cats, c.mix, win), ctxLegend(d.cats, c.mix)),
        card('Timing', h('small', { class: 'muted', text: 'this session' }),
          h('div', { class: 'vz-dl' }, donut(timing, fmtDur(t.model_s + toolTime) || '0s', 'active'), legend(timing, (v) => fmtSec(v))))),
      card('Context trend', h('small', { class: 'muted', text: d.step > 1 ? `${d.requests} requests, ${d.step} per bar · click a bar` : 'one bar per request · click a bar' }),
        trendChart(d, pick, sel), detail),
      h('div', { class: 'vz-row2' },
        card('Tool results in context', h('small', { class: 'muted', text: '≈ tokens by tool' }), tally(toolRows, 'No tool results in context.', (n) => '≈' + fmtN(n))),
        card('Largest tool results', h('small', { class: 'muted', text: 'this session' }),
          d.biggest.length ? h('div', { class: 'vz-tally' }, d.biggest.map((b, i) => h('div', { class: 'vz-trow', 'data-tip': `${b.name}: ${b.brief}\n${absTime(b.t)}` },
            h('i', { style: `background:${VIZ.palette[i % VIZ.palette.length]}` }), h('code', { text: `${b.name} · ${b.brief || ''}` }),
            h('b', { text: '≈' + fmtN(b.tokens) }), h('small', { text: ago(b.t) })))) : h('p', { class: 'muted vz-empty', text: 'None over 2k tokens.' }))));
  }
  load();
}

/* ---------- a project's team: network, cross-agent timeline, event list (after dsh-context's Fleet tab) ---------- */
function initials(s) {
  const w = String(s || '?').replace(/[^\w\s-]/g, '').split(/[\s_-]+/).filter(Boolean);
  return ((w[0] || '?')[0] + (w[1] ? w[1][0] : (w[0] || '').slice(1, 2))).toUpperCase();
}
const KIND_WORD = { message: 'Message', reply: 'Reply', note: 'Note', work: 'Work done', decision: 'Decision' };

/* nodes on a ring (owner on top), one edge per pair, width by message count */
function teamNetwork(lanes, events, color, onPick) {
  const W = 560, H = 380, cx = W / 2, cy = H / 2 + 6, r = Math.min(150, 40 + lanes.length * 14);
  const pos = {};
  lanes.forEach((l, i) => {
    const a = -Math.PI / 2 + (2 * Math.PI * i) / lanes.length;
    pos[l.id] = [cx + r * Math.cos(a), cy + r * Math.sin(a)];
  });
  const pairs = {};
  for (const e of events) {
    if (!(e.kind === 'message' || e.kind === 'reply' || e.kind === 'decision') || !pos[e.from] || !pos[e.to] || e.from === e.to) continue;
    const k = e.from + '>' + e.to;
    pairs[k] = (pairs[k] || 0) + 1;
  }
  const max = Math.max(1, ...Object.values(pairs));
  const svg = sv('svg', { viewBox: `0 0 ${W} ${H}`, class: 'vz-net' });
  svg.append(sv('defs', null, ...lanes.map((l) => sv('marker', { id: 'ah-' + l.key, viewBox: '0 0 10 10', refX: 9, refY: 5, markerWidth: 9, markerHeight: 9, markerUnits: 'userSpaceOnUse', orient: 'auto-start-reverse' },
    sv('path', { d: 'M0,0 L10,5 L0,10 z', style: `fill:${color(l.id)}` })))));
  for (const [k, n] of Object.entries(pairs)) {
    const [f, t] = k.split('>');
    const [x1, y1] = pos[f], [x2, y2] = pos[t];
    const back = pairs[t + '>' + f] ? 22 : 0;  // two-way traffic: bend the two edges apart
    const mx = (x1 + x2) / 2 + (y2 - y1) * (back ? 0.18 : 0.08), my = (y1 + y2) / 2 - (x2 - x1) * (back ? 0.18 : 0.08);
    const d = Math.hypot(x2 - x1, y2 - y1) || 1, sh = 24 / d;
    const ex = x2 - (x2 - mx) * sh * 1.1, ey = y2 - (y2 - my) * sh * 1.1;
    const lane = lanes.find((l) => l.id === f);
    svg.append(sv('path', { d: `M${x1},${y1} Q${mx},${my} ${ex},${ey}`, class: 'vz-edge', 'marker-end': `url(#ah-${lane.key})`,
      style: `stroke:${color(f)};stroke-width:${1 + (n / max) * 5}`, 'data-tip': `${lane.label} → ${(lanes.find((l) => l.id === t) || {}).label}: ${n} message${n === 1 ? '' : 's'}` }));
  }
  for (const l of lanes) {
    const [x, y] = pos[l.id];
    const g = sv('g', { class: 'vz-node' + (l.gone ? ' gone' : ''), 'data-tip': `${l.label}${l.model ? ' · ' + l.model : ''}\n${l.sub || ''}` },
      sv('circle', { cx: x, cy: y, r: 21, style: `fill:${color(l.id)}22;stroke:${color(l.id)}` }),
      sv('text', { x, y: y + 4, class: 'vz-ninit', 'text-anchor': 'middle' }, l.id === 'owner' ? 'YOU' : initials(l.label)),
      sv('text', { x, y: y + 36, class: 'vz-nlab', 'text-anchor': 'middle' }, l.label.length > 18 ? l.label.slice(0, 17) + '…' : l.label));
    if (l.state === 'busy') g.append(sv('circle', { cx: x + 15, cy: y - 15, r: 4, class: 'vz-nbusy' }));
    g.addEventListener('click', () => onPick(l.id));
    svg.append(g);
  }
  return svg;
}

/* one lane per member, x = time; a message is an arrow from the sender's lane to the recipient's */
function teamTimeline(lanes, events, color, t0, t1, onPick, sel, filterLane) {
  const LH = 34, L = 150, R = 16, T = 22, W = 1200;
  const H = T + lanes.length * LH + 22;
  const x = (t) => L + ((parseTime(t) - t0) / Math.max(1, t1 - t0)) * (W - L - R);
  const y = Object.fromEntries(lanes.map((l, i) => [l.id, T + i * LH + LH / 2]));
  const svg = sv('svg', { viewBox: `0 0 ${W} ${H}`, class: 'vz-lanes' });
  lanes.forEach((l, i) => {
    svg.append(sv('rect', { x: 0, y: T + i * LH, width: W, height: LH, class: i % 2 ? 'vz-lane odd' : 'vz-lane' }),
      sv('circle', { cx: 14, cy: y[l.id], r: 9, style: `fill:${color(l.id)}33;stroke:${color(l.id)}` }),
      sv('text', { x: 14, y: y[l.id] + 3, class: 'vz-linit', 'text-anchor': 'middle' }, l.id === 'owner' ? 'Y' : initials(l.label)),
      sv('text', { x: 30, y: y[l.id] + 4, class: 'vz-llab' + (filterLane === l.id ? ' on' : '') }, l.label.length > 17 ? l.label.slice(0, 16) + '…' : l.label),
      sv('line', { x1: L, x2: W - R, y1: y[l.id], y2: y[l.id], class: 'vz-lline' }));
  });
  const span = t1 - t0, stepH = span <= 6 * 3600e3 ? 1 : span <= 26 * 3600e3 ? 3 : span <= 80 * 3600e3 ? 12 : 24;
  const first = new Date(t0); first.setMinutes(0, 0, 0); first.setHours(Math.ceil(first.getHours() / stepH) * stepH);
  for (let t = first.getTime(); t < t1; t += stepH * 3600e3) {
    const d = new Date(t), xx = x(t);
    if (xx < L) continue;
    svg.append(sv('line', { x1: xx, x2: xx, y1: T, y2: H - 20, class: 'vz-grid' }),
      sv('text', { x: xx, y: H - 6, class: 'vz-ax', 'text-anchor': 'middle' }, d.getHours() === 0 || stepH >= 24 ? `${d.getMonth() + 1}/${d.getDate()}` : `${d.getHours()}:00`));
  }
  svg.append(sv('defs', null, ...lanes.map((l) => sv('marker', { id: 'lh-' + l.key, viewBox: '0 0 10 10', refX: 9, refY: 5, markerWidth: 8, markerHeight: 8, markerUnits: 'userSpaceOnUse', orient: 'auto-start-reverse' },
    sv('path', { d: 'M0,0 L10,5 L0,10 z', style: `fill:${color(l.id)}` })))));
  for (const e of events) {
    if (y[e.from] == null) continue;
    const xx = x(e.t), y1 = y[e.from];
    const on = sel === e.id ? ' on' : '';
    const tip = `${KIND_WORD[e.kind]} · ${absTime(e.t)}\n${laneLabel(lanes, e.from)}${e.to ? ' → ' + laneLabel(lanes, e.to) : ''}${e.state ? ' · ' + e.state : ''}\n${(e.body || '').slice(0, 220)}`;
    let g;
    if ((e.kind === 'message' || e.kind === 'reply' || e.kind === 'decision') && y[e.to] != null && e.to !== e.from) {
      const y2 = y[e.to], dir = y2 > y1 ? 1 : -1;
      g = sv('g', { class: 'vz-ev' + on + (e.state === 'failed' ? ' failed' : ''), 'data-tip': tip },
        sv('line', { x1: xx, x2: xx, y1: y1 + dir * 4, y2: y2 - dir * 7, style: `stroke:${color(e.from)}`, 'marker-end': `url(#lh-${laneKey(lanes, e.from)})`, class: e.kind === 'reply' ? 'vz-evl dash' : 'vz-evl' }),
        sv('rect', { x: xx - 3.5, y: y1 - 3.5, width: 7, height: 7, rx: 1.5, style: `fill:${color(e.from)}` }),
        sv('line', { x1: xx, x2: xx, y1: Math.min(y1, y2), y2: Math.max(y1, y2), class: 'vz-evhit' }));
      if (e.done && e.reply && y[e.to] != null) {  // the answer came back: a hollow mark on the sender's lane
        g.append(sv('circle', { cx: x(e.done), cy: y1, r: 3, class: 'vz-evback', style: `stroke:${color(e.to)}` }));
      }
    } else {
      const shape = e.kind === 'note' ? sv('path', { d: `M${xx},${y1 - 6} L${xx + 6},${y1} L${xx},${y1 + 6} L${xx - 6},${y1} z`, style: `fill:${color(e.from)}` })
        : e.kind === 'work' ? sv('path', { d: `M${xx - 5},${y1} L${xx - 1},${y1 + 4} L${xx + 6},${y1 - 5}`, class: 'vz-check', style: `stroke:${color(e.from)}` })
          : sv('circle', { cx: xx, cy: y1, r: 4, style: `fill:${color(e.from)}` });
      g = sv('g', { class: 'vz-ev' + on, 'data-tip': tip }, shape, sv('circle', { cx: xx, cy: y1, r: 8, class: 'vz-evhit2' }));
    }
    g.addEventListener('click', () => onPick(e.id));
    svg.append(g);
  }
  return svg;
}
const laneLabel = (lanes, id) => (lanes.find((l) => l.id === id) || { label: id }).label;
const laneKey = (lanes, id) => (lanes.find((l) => l.id === id) || { key: 'x' }).key;

function openTeam(pid) {
  vizTip();
  let data = null, timer = null, hours = +(lsGet('sb.team.hours') || 24), kind = 'all', sel = null, lane = null, q = '';
  const body = h('div', { class: 'vz-mbody' }, h('p', { class: 'muted pad', text: 'Loading the team…' }));
  const rngBox = h('span');
  const close = h('button', { class: 'btn ghost small', type: 'button', text: 'Close' });
  const box = h('div', { class: 'vz-modal', role: 'dialog', 'aria-modal': 'true', 'aria-label': 'Project team' },
    h('div', { class: 'vz-mhead' }, h('h2', { text: `${projName(pid)} · Team` }), h('span', { class: 'muted', text: 'messages, notes, finished work and decisions between this project\'s agents and you' }),
      h('span', { class: 'vz-grow' }), rngBox, close), body);
  const back = h('div', { class: 'vz-back' }, box);
  const done = () => { clearTimeout(timer); back.remove(); document.removeEventListener('keydown', onKey, true); };
  const onKey = (e) => { if (e.key === 'Escape') { e.stopPropagation(); done(); } };
  close.addEventListener('click', done);
  back.addEventListener('mousedown', (e) => { if (e.target === back) done(); });
  document.addEventListener('keydown', onKey, true);
  document.body.append(back);
  const setHours = (v) => { hours = v; lsSet('sb.team.hours', String(v)); load(); };
  async function load() {
    clearTimeout(timer);
    rngBox.replaceChildren(segButtons([[6, '6 h'], [24, '24 h'], [72, '3 days'], [168, '7 days']], hours, setHours));
    try { data = await api('GET', `/projects/${enc(pid)}/team?hours=${hours}`); render(); } catch (e) { body.replaceChildren(h('p', { class: 'pad err', text: e.message })); }
    if (back.isConnected) timer = setTimeout(load, 15000);
  }
  function render() {
    const d = data;
    const lanes = [{ id: 'owner', label: 'You', key: 'owner' }, ...d.agents.map((a, i) => ({ id: a.id, label: a.title, key: 'a' + i, model: a.model, gone: a.gone, state: a.state,
      sub: `${a.sent} sent · ${a.received} received${a.gone ? ' · removed' : ''}${a.outside ? ' · other project' : ''}` }))];
    const cmap = Object.fromEntries(lanes.map((l, i) => [l.id, l.id === 'owner' ? '#86c232' : VIZ.palette[(i - 1) % VIZ.palette.length]]));
    const color = (id) => cmap[id] || '#6b7280';
    const t1 = parseTime(d.now), t0 = t1 - hours * 3600e3;
    const msgs = d.events.filter((e) => e.kind === 'message' || e.kind === 'reply');
    const counts = { all: d.events.length, message: msgs.length, note: d.events.filter((e) => e.kind === 'note').length,
      work: d.events.filter((e) => e.kind === 'work').length, decision: d.events.filter((e) => e.kind === 'decision').length };
    const words = q.toLowerCase().split(/\s+/).filter(Boolean);
    const shown = d.events.filter((e) => (kind === 'all' || (kind === 'message' ? e.kind === 'message' || e.kind === 'reply' : e.kind === kind))
      && (!lane || e.from === lane || e.to === lane)
      && (!words.length || words.every((w) => `${e.body} ${e.reply || ''} ${laneLabel(lanes, e.from)} ${laneLabel(lanes, e.to)}`.toLowerCase().includes(w))));
    const pickEvent = (id) => { sel = sel === id ? null : id; render(); };
    const pickLane = (id) => { lane = lane === id ? null : id; render(); };
    const picked = d.events.find((e) => e.id === sel) || (sel === null ? shown[shown.length - 1] : null);
    const search = h('input', { type: 'search', class: 'vz-search', placeholder: 'Search events', value: q });
    search.addEventListener('input', () => { q = search.value; const pos = search.selectionStart; render(); const s2 = body.querySelector('.vz-evsearch'); if (s2) { s2.focus(); s2.setSelectionRange(pos, pos); } });
    search.classList.add('vz-evsearch');
    const list = h('div', { class: 'vz-evlist' }, shown.slice().reverse().slice(0, 300).map((e) => {
      const row = h('button', { type: 'button', class: 'vz-evrow' + (e.id === sel ? ' on' : ''), style: `border-left-color:${color(e.from)}` },
        h('div', { class: 'vz-evh' }, h('span', { class: 'muted', text: new Date(parseTime(e.t)).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' }) }),
          h('b', { text: laneLabel(lanes, e.from) }), e.to ? h('span', { class: 'muted', text: '→' }) : null, e.to ? h('b', { text: laneLabel(lanes, e.to) }) : null,
          h('span', { class: 'vz-grow' }), h('span', { class: 'vz-kind k-' + e.kind, text: KIND_WORD[e.kind] }), e.state ? h('span', { class: 'vz-st s-' + e.state, text: e.state }) : null),
        h('div', { class: 'vz-evb', text: (e.body || '').slice(0, 260) }));
      row.addEventListener('click', () => pickEvent(e.id));
      return row;
    }));
    const detail = picked ? h('div', { class: 'vz-evdetail' },
      h('div', { class: 'vz-evh' }, h('span', { class: 'vz-kind k-' + picked.kind, text: KIND_WORD[picked.kind] }), picked.state ? h('span', { class: 'vz-st s-' + picked.state, text: picked.state }) : null,
        h('span', { class: 'muted', text: absTime(picked.t) }), h('span', { class: 'vz-grow' }), h('button', { class: 'btn ghost small', type: 'button', text: '×', onclick: () => pickEvent(picked.id) })),
      h('div', { class: 'vz-parties' }, h('div', null, h('small', { text: 'FROM' }), h('b', { text: laneLabel(lanes, picked.from) })),
        picked.to ? h('span', { class: 'muted', text: '→' }) : null, picked.to ? h('div', null, h('small', { text: 'TO' }), h('b', { text: laneLabel(lanes, picked.to) })) : null,
        picked.delivered ? h('small', { class: 'muted', text: 'delivered ' + absTime(picked.delivered) }) : null),
      h('pre', { class: 'vz-pre', text: picked.body || '' }),
      picked.reply ? h('div', null, h('small', { class: 'muted', text: `REPLY${picked.done ? ' · ' + absTime(picked.done) : ''}` }), h('pre', { class: 'vz-pre', text: picked.reply })) : null,
      picked.error ? h('p', { class: 'err', text: picked.error }) : null) : h('p', { class: 'muted vz-empty', text: 'Click an arrow, a node or an event to see it here.' });
    const st = (a) => (S.agents.get(a.id) || a);
    const lead = d.lead;
    body.replaceChildren(
      h('div', { class: 'vz-kpis vz-kpis7' },
        kpi('Agents', String(d.agents.filter((a) => !a.gone && !a.outside).length), `${d.agents.filter((a) => st(a).running).length} running`),
        kpi('Working now', String(d.agents.filter((a) => st(a).state === 'busy').length), 'busy terminals'),
        kpi('Messages', String(counts.message), `${msgs.filter((e) => e.from === 'owner').length} from you`),
        kpi('Failed', String(msgs.filter((e) => e.state === 'failed').length), 'not delivered'),
        kpi('Notes', String(counts.note), 'shared'),
        kpi('Work done', String(counts.work), 'items closed'),
        kpi('Decisions', String(counts.decision), `${d.events.filter((e) => e.kind === 'decision' && e.state === 'open').length} open`)),
      h('div', { class: 'vz-row2 vz-teamrow' },
        card('Agent network', h('small', { class: 'muted', text: 'edge width = messages · click a node to filter' }),
          teamNetwork(lanes, d.events, color, pickLane),
          h('div', { class: 'vz-tcards' }, d.agents.filter((a) => !a.outside).map((a) => {
            const live = S.agents.get(a.id);
            return h(live ? 'a' : 'div', { class: 'vz-tcard' + (a.gone ? ' gone' : '') + (lane === a.id ? ' on' : ''), href: live ? agentHref(live) : null, style: `border-top-color:${color(a.id)}` },
              h('div', { class: 'vz-ah' }, h('b', { text: a.title }), a.id === lead ? h('span', { class: 'vz-tag', text: 'lead' }) : null),
              h('small', { class: 'muted', text: `${a.model || a.provider || ''}${a.gone ? ' · removed' : ''}` }),
              h('div', { class: 'vz-tstats' }, h('span', { text: `↑${a.sent} ↓${a.received}` }),
                h('span', { text: a.context_tokens ? `ctx ${fmtN(a.context_tokens)}` : '' }), h('span', { text: a.tokens_24h ? `${fmtN(a.tokens_24h)}/24h` : '' })));
          }))),
        card('Event', null, detail)),
      card('Cross-agent timeline', h('small', { class: 'muted', text: `${d.events.length} events · last ${hours >= 48 ? hours / 24 + ' days' : hours + ' h'} · ■ sent  ▶ received  ○ answer back  ◆ note  ✓ work` }),
        teamTimeline(lanes, d.events, color, t0, t1, pickEvent, sel, lane)),
      card('Events', h('div', { class: 'vz-evtools' },
        segButtons([['all', `All ${counts.all}`], ['message', `Messages ${counts.message}`], ['note', `Notes ${counts.note}`], ['work', `Work ${counts.work}`], ['decision', `Decisions ${counts.decision}`]], kind, (v) => { kind = v; render(); }),
        lane ? h('button', { class: 'btn ghost small', type: 'button', text: `${laneLabel(lanes, lane)} ×`, onclick: () => pickLane(lane) }) : null, search),
      shown.length ? list : h('p', { class: 'muted vz-empty', text: 'No events in this window.' })));
  }
  load();
}

/* ---------- memory files and outlines of a project or one agent (owner only: GET /api/projects/<id>/memfiles) ---------- */
function vzModal(label) {
  const body = h('div', { class: 'vz-mbody' }, h('p', { class: 'muted pad', text: 'Reading…' }));
  const title = h('h2'), sub = h('span', { class: 'muted' }), tools = h('span', { class: 'mf-tools' });
  const close = h('button', { class: 'btn ghost small', type: 'button', text: 'Close', 'aria-label': 'Close ' + label });
  const box = h('div', { class: 'vz-modal', role: 'dialog', 'aria-modal': 'true', 'aria-label': label },
    h('div', { class: 'vz-mhead' }, title, sub, h('span', { class: 'vz-grow' }), tools, close), body);
  const back = h('div', { class: 'vz-back' }, box);
  const onKey = (e) => { if (e.key === 'Escape') { e.stopPropagation(); done(); } };
  const done = () => { back.remove(); document.removeEventListener('keydown', onKey, true); };
  close.addEventListener('click', done);
  back.addEventListener('mousedown', (e) => { if (e.target === back) done(); });
  document.addEventListener('keydown', onKey, true);
  document.body.append(back);
  close.focus();
  return { body, title, sub, tools, done, live: () => back.isConnected };
}

function openMemFiles(pid, aid) {
  const m = vzModal('Memory files');
  let data = null, mem = null, vers = null, sel = null, outlineCache = {}, editing = false, askDel = false;
  const pick = (x) => { sel = x; editing = askDel = false; render(); };
  async function act(path, body, msg, keepSel) {
    try { await api('POST', path, body); toast(msg); if (!keepSel) sel = null; editing = askDel = false; outlineCache = {}; await load(); }
    catch (e) { toast(e.message, 'err'); }
  }
  function delBar(label, what, onYes) {
    return askDel ? h('span', { class: 'rw-confirm' }, h('small', { text: what }),
      h('button', { class: 'btn small rw-go', type: 'button', text: 'Delete', onclick: onYes }),
      h('button', { class: 'btn ghost small', type: 'button', text: 'Cancel', onclick: () => { askDel = false; render(); } }))
      : h('button', { class: 'btn ghost small', type: 'button', text: label, onclick: () => { askDel = true; editing = false; render(); } });
  }
  async function load() {
    try {
      [data, mem] = await Promise.all([api('GET', `/projects/${enc(pid)}/memfiles${aid ? '?agent=' + enc(aid) : ''}`), api('GET', `/projects/${enc(pid)}/memory?n=1`)]);
      render();
    } catch (e) { m.body.replaceChildren(h('p', { class: 'pad err', text: e.message })); }
  }
  /* the project's running memory (sb memory): read, correct by hand, or go back to an earlier version */
  function memDetail() {
    const ta = editing ? h('textarea', { class: 'mf-edit', spellcheck: 'false' }) : null;
    if (ta) ta.value = mem.memory;
    const put = async (body, msg) => { try { await api('POST', `/projects/${enc(pid)}/memory`, body); toast(msg); editing = false; vers = null; await load(); } catch (e) { toast(e.message, 'err'); } };
    const tools = editing ? h('span', { class: 'rw-confirm' },
      h('button', { class: 'btn small', type: 'button', text: 'Save', onclick: () => put({ text: ta.value }, 'project memory saved') }),
      h('button', { class: 'btn ghost small', type: 'button', text: 'Cancel', onclick: () => { editing = false; render(); } }))
      : h('span', { class: 'rw-confirm' },
        h('button', { class: 'btn ghost small', type: 'button', text: 'Edit', onclick: () => { editing = true; render(); } }),
        h('button', { class: 'btn ghost small', type: 'button', text: vers ? 'Hide versions' : 'Earlier versions', onclick: async () => {
          if (vers) { vers = null; render(); return; }
          try { vers = (await api('GET', `/projects/${enc(pid)}/memory/versions`)).versions; render(); } catch (e) { toast(e.message, 'err'); } } }));
    return h('div', { class: 'mf-detail' },
      h('div', { class: 'mf-dh' }, h('b', { text: 'Project memory' }), h('span', { class: 'vz-pill', text: 'sb memory' }), h('span', { class: 'vz-grow' }), tools),
      h('p', { class: 'mf-desc', text: 'Where the project stands, rewritten when a conversation ends. Every agent of the project reads it with sb memory; [#n] cites the outline of conversation n.' }),
      h('div', { class: 'muted small', text: mem.updated_at ? `updated ${ago(mem.updated_at)} (${absTime(mem.updated_at)}) · ${mem.total} ended conversation${mem.total === 1 ? '' : 's'} on record` : 'nothing written yet' }),
      ta || h('pre', { class: 'mf-text', text: mem.memory || '(empty)' }),
      vers ? h('div', {}, vers.length ? vers.map((v) => h('details', { class: 'small' },
        h('summary', {}, `version ${v.id} · replaced ${absTime(v.replaced_at || v.created_at)}${v.replaced_by ? ' by ' + v.replaced_by : ''} `,
          h('button', { class: 'btn ghost small', type: 'button', text: 'Go back to this', onclick: (e) => { e.preventDefault(); put({ restore: v.id }, `project memory put back to version ${v.id}`); } })),
        h('pre', { class: 'mf-text', text: v.text || '(empty)' }))) : h('p', { class: 'muted small', text: 'No earlier versions.' })) : null);
  }
  const who = (b) => b.map((x) => `${x.agent}${x.n > 1 ? ' ×' + x.n : ''}`).join(', ');
  function fileDetail(f) {
    const ta = editing && f.exists ? h('textarea', { class: 'mf-edit', spellcheck: 'false' }) : null;
    if (ta) ta.value = f.text;
    const tools = !f.exists ? null : editing ? h('span', { class: 'rw-confirm' },
      h('button', { class: 'btn small', type: 'button', text: 'Save', onclick: () => act('/memfiles/save', { path: f.path, text: ta.value }, `${f.name} saved`, true) }),
      h('button', { class: 'btn ghost small', type: 'button', text: 'Cancel', onclick: () => { editing = false; render(); } }))
      : h('span', { class: 'rw-confirm' },
        askDel ? null : h('button', { class: 'btn ghost small', type: 'button', text: 'Edit', onclick: () => { editing = true; askDel = false; render(); } }),
        delBar('Delete', f.name === 'MEMORY.md' ? 'Delete this index file?' : 'Delete this file and its line in MEMORY.md?',
          () => act('/memfiles/delete', { path: f.path }, `${f.name} deleted`)));
    return h('div', { class: 'mf-detail' },
      h('div', { class: 'mf-dh' }, h('b', { text: f.name }), f.exists ? null : h('span', { class: 'vz-pill mf-gone', text: 'deleted' }),
        f.front.type ? h('span', { class: 'vz-pill', text: f.front.type }) : null, h('span', { class: 'vz-grow' }), tools),
      f.front.description ? h('p', { class: 'mf-desc', text: f.front.description }) : null,
      h('div', { class: 'muted small mf-path', text: f.path }),
      h('div', { class: 'muted small', text: f.exists ? `${fmtN(f.size)} bytes · changed ${ago(f.mtime * 1000)} (${absTime(f.mtime * 1000)})` : 'no longer on disk' }),
      f.by.length ? h('table', { class: 'mf-by' }, h('tr', {}, h('th', { text: 'Written by' }), h('th', { text: 'Writes' }), h('th', { text: 'First' }), h('th', { text: 'Last' })),
        f.by.map((b) => h('tr', {}, h('td', { text: b.agent }), h('td', { text: `${b.n} (${b.tools.join(', ')})` }), h('td', { text: absTime(b.first) }), h('td', { text: absTime(b.last) }))))
        : h('p', { class: 'muted small', text: 'No agent transcript on record shows who wrote it (written before its agent joined Switchboard, or from a shell).' }),
      ta || (f.exists ? h('pre', { class: 'mf-text', text: f.text }) : null));
  }
  async function outlineDetail(o) {
    const box = h('div', { class: 'mf-detail' }, h('p', { class: 'muted', text: 'Reading the outline…' }));
    (async () => {
      try {
        const e = outlineCache[o.id] || (outlineCache[o.id] = await api('GET', `/memory/${o.id}`));
        const secs = (e.outline && e.outline.sections) || [];
        const ta = editing ? h('textarea', { class: 'mf-edit', spellcheck: 'false' }) : null;
        if (ta) ta.value = JSON.stringify({ summary: e.summary || '', outline: e.outline || { sections: [] } }, null, 1);
        const save = () => { let v; try { v = JSON.parse(ta.value); } catch (err) { toast('Not valid JSON: ' + err.message, 'err'); return; }
          act(`/memory/${e.id}`, { summary: v.summary, outline: v.outline }, `outline #${e.id} saved`, true); };
        box.replaceChildren(
          h('div', { class: 'mf-dh' }, h('b', { text: `Conversation #${e.id}` }), h('span', { class: 'vz-pill', text: e.state }), h('span', { class: 'vz-grow' }),
            editing ? h('span', { class: 'rw-confirm' }, h('button', { class: 'btn small', type: 'button', text: 'Save', onclick: save }),
              h('button', { class: 'btn ghost small', type: 'button', text: 'Cancel', onclick: () => { editing = false; render(); } }))
              : h('span', { class: 'rw-confirm' },
                askDel ? null : h('button', { class: 'btn ghost small', type: 'button', text: 'Edit', onclick: () => { editing = true; render(); } }),
                askDel || !e.transcript_on_disk ? null : h('button', { class: 'btn ghost small', type: 'button', text: 'Transcript', title: 'Every entry of this conversation; edit any of them',
                  onclick: () => openRewind(e.agent, { session: e.session_id }) }),
                delBar('Delete outline', 'Delete this outline and the project-memory lines only it backs?', () => act(`/memory/${e.id}/delete`, {}, `outline #${e.id} deleted`)))),
          h('div', { class: 'muted small', text: `${e.agent} · ${e.model || ''} · ${absTime(e.created_at)} · ${e.why || ''}` }),
          ...(ta ? [h('small', { class: 'muted', text: 'The summary and the sections of points; m lists the message numbers a point cites.' }), ta] : [
          e.summary ? h('p', { class: 'mf-desc', text: e.summary }) : null,
          secs.length ? h('div', { class: 'mf-outline' }, secs.map((s) => h('div', {}, h('h4', { text: s.h }),
            h('ul', {}, s.points.map((p) => h('li', { text: `${p.t}  [${p.m.join(', ')}]` })))))) : h('p', { class: 'muted', text: e.note || 'No points yet.' }),
          h('div', { class: 'muted small', text: `sb memory ${e.id} · sb memory ${e.id} --at <m> for the messages` })]).filter(Boolean));
      } catch (err) { box.replaceChildren(h('p', { class: 'err', text: err.message })); }
    })();
    return box;
  }
  function render() {
    const d = data;
    setText(m.title, aid ? `Memory · ${aid}` : `Memory · ${projName(pid)}`);
    setText(m.sub, `${d.files.length} file${d.files.length === 1 ? '' : 's'} · ${d.outlines.length} outline${d.outlines.length === 1 ? '' : 's'}`);
    const segs = h('div', { class: 'vz-segs' }, [{ id: null, title: 'Whole project' }, ...d.agents].map((a) =>
      h('button', { class: 'vz-sb', type: 'button', 'aria-pressed': String((a.id || null) === (aid || null)), text: a.id ? a.id + (a.gone ? ' (removed)' : '') : a.title,
        onclick: () => { aid = a.id; sel = null; load(); } })));
    m.tools.replaceChildren(segs);
    if (!sel) sel = aid ? (d.files[0] ? { kind: 'f', i: 0 } : d.outlines[0] ? { kind: 'o', i: 0 } : null) : { kind: 'm', i: 0 };
    const row = (kind, i, main, small, extra) => h('button', { class: 'mf-row', type: 'button', 'aria-pressed': String(!!sel && sel.kind === kind && sel.i === i),
      onclick: () => pick({ kind, i }) }, h('span', { class: 'mf-main', text: main }), extra || null, h('small', { class: 'muted', text: small }));
    const list = h('div', { class: 'mf-list' },
      aid ? null : h('h3', { text: 'Project memory' }),
      aid ? null : row('m', 0, 'Running memory (sb memory)', mem.updated_at ? `updated ${ago(mem.updated_at)} · ${mem.memory.length} characters` : 'nothing written yet'),
      h('h3', { text: 'Auto-memory files' }),
      d.files.length ? d.files.map((f, i) => row('f', i, f.name, [f.front.description, f.by.length ? 'by ' + who(f.by) : '', f.mtime ? ago(f.mtime * 1000) : 'deleted'].filter(Boolean).join(' · '),
        f.exists ? null : h('span', { class: 'vz-pill mf-gone', text: 'deleted' })))
        : h('p', { class: 'muted small', text: aid ? 'This agent has not written a memory file.' : 'No memory files in this project\'s memory folders.' }),
      d.folders.length ? h('div', { class: 'muted small mf-path', text: d.folders.map((x) => x.path).join('\n') }) : null,
      h('h3', { text: 'Conversation outlines (project memory)' }),
      d.outlines.length ? d.outlines.map((o, i) => row('o', i, `#${o.id} · ${o.agent}`, `${(o.created_at || '').slice(0, 16).replace('T', ' ')} · ${o.state}${o.summary ? ' · ' + o.summary : ''}`))
        : h('p', { class: 'muted small', text: 'No ended conversations on record.' }));
    const detail = !sel ? h('p', { class: 'muted', text: 'Nothing to show.' })
      : sel.kind === 'm' ? memDetail() : sel.kind === 'f' ? fileDetail(d.files[sel.i]) : null;
    const pane = h('div', { class: 'mf-pane' }, detail);
    if (sel && sel.kind === 'o') outlineDetail(d.outlines[sel.i]).then((x) => pane.replaceChildren(x));
    m.body.replaceChildren(h('div', { class: 'mf-grid' }, list, pane));
  }
  load();
}

/* ---------- an agent's transcript (owner only): rewind it to just before one of its messages, as if the rest never
   happened, or edit any entry of it. `openAt`: {session} opens on that conversation with every entry listed. ---------- */
function openRewind(aid, openAt) {
  const m = vzModal('Transcript');
  let data = null, armed = null, busyAsk = false, keepCopy = false;
  let showAll = !!openAt, ed = null;
  const all = {};  // session -> {entries, before} (every entry: asks, replies, tool calls)
  async function load() {
    try { data = await api('GET', `/agents/${enc(aid)}/moments`); render(); if (showAll) loadAll(); }
    catch (e) { m.body.replaceChildren(h('p', { class: 'pad err', text: e.message })); }
  }
  async function more(sid, fresh) {
    const have = fresh ? null : all[sid];
    try {
      const r = await api('GET', `/agents/${enc(aid)}/records?session=${enc(sid)}&limit=80${have && have.before != null ? '&before=' + have.before : ''}`);
      all[sid] = { entries: r.entries.concat(have ? have.entries : []), before: r.before };
    } catch (e) { all[sid] = { entries: [], before: null, err: e.message }; }
    if (m.live()) render();
  }
  function loadAll() { for (const s of data.sessions) if (!all[s.session] && (!openAt || openAt.session === s.session || s.current)) more(s.session, true); }
  async function go(sess, mo, confirm) {
    try {
      const r = await api('POST', `/agents/${enc(aid)}/rewind`, { session: sess.session, offset: mo.offset, confirm: !!confirm, keep_copy: keepCopy });
      toast(r.relaunch ? 'Rewinding: the CLI exits, everything after that moment is erased, and it resumes there' : 'Rewound; the agent resumes there on its next start');
      m.done();
    } catch (e) {
      if (e.status === 409 && e.data && e.data.needs_confirm) { busyAsk = true; render(); return; }
      toast(e.message, 'err');
    }
  }
  async function startEdit(sid, at) {
    ed = { s: sid, at, rec: null, vals: [], raw: false, rawVal: '', ask: false };
    armed = null; render();
    try {
      const rec = await api('GET', `/agents/${enc(aid)}/record?session=${enc(sid)}&at=${at}`);
      if (!ed || ed.s !== sid || ed.at !== at) return;
      ed.rec = rec; ed.vals = rec.parts.map((p) => p.text); ed.rawVal = rec.raw || ''; ed.raw = !rec.parts.length;
    } catch (e) { toast(e.message, 'err'); ed = null; }
    if (m.live()) render();
  }
  async function saveEdit(confirm) {
    const body = { session: ed.s, at: ed.at, sig: ed.rec.sig, confirm: !!confirm };
    if (ed.raw) body.raw = ed.rawVal;
    else {
      body.parts = {};
      ed.rec.parts.forEach((p, i) => { if (ed.vals[i] !== p.text) body.parts[i] = ed.vals[i]; });
      if (!Object.keys(body.parts).length) { toast('Nothing changed'); return; }
    }
    try {
      const r = await api('POST', `/agents/${enc(aid)}/edit`, body);
      if (r.relaunch) { toast('Editing: the CLI exits, the entry is changed, and the agent resumes with it in place'); m.done(); return; }
      toast('Edited');
      const sid = ed.s; ed = null;
      await load(); if (all[sid]) more(sid, true);
    } catch (e) {
      if (e.status === 409 && e.data && e.data.needs_confirm) { ed.ask = true; render(); return; }
      toast(e.message, 'err');
    }
  }
  function editor() {
    if (!ed.rec) return h('div', { class: 'rw-ed' }, h('p', { class: 'muted small', text: 'Reading the record…' }));
    const area = (val, set, cls) => { const t = h('textarea', { class: 'mf-edit ' + cls, spellcheck: 'false' }); t.value = val; t.oninput = () => set(t.value); return t; };
    const rawBox = h('input', { type: 'checkbox' });
    rawBox.checked = ed.raw; rawBox.disabled = !ed.rec.parts.length;
    rawBox.onchange = () => { ed.raw = rawBox.checked; render(); };
    return h('div', { class: 'rw-ed' },
      ed.raw ? [h('small', { class: 'muted', text: 'The whole record, as JSON. Keep ids and the tool call / tool output pairing intact, or the CLI may refuse to resume.' }),
        area(ed.rawVal, (v) => { ed.rawVal = v; }, 'rw-ta-raw')]
        : ed.rec.parts.length ? ed.rec.parts.map((p, i) => h('label', { class: 'rw-part' }, h('small', { class: 'muted', text: p.label }), area(ed.vals[i], (v) => { ed.vals[i] = v; }, 'rw-ta')))
          : h('p', { class: 'muted small', text: 'This record holds no text to edit.' }),
      h('div', { class: 'rw-confirm' },
        ed.rec.raw != null ? h('label', { class: 'small rw-keep' }, rawBox, ' Whole record (raw JSON)') : null,
        h('span', { class: 'vz-grow' }),
        ed.ask ? h('small', { text: 'The agent is working. Interrupt it and save the edit?' }) :
          h('small', { class: 'muted', text: 'If the agent is on this conversation, its CLI exits and resumes with the edit.' }),
        h('button', { class: 'btn small' + (ed.ask ? ' rw-go' : ''), type: 'button', text: ed.ask ? 'Interrupt and save' : 'Save', onclick: () => saveEdit(ed.ask) }),
        h('button', { class: 'btn ghost small', type: 'button', text: 'Cancel', onclick: () => { ed = null; render(); } })));
  }
  function render() {
    const d = data;
    const keepScroll = m.body.scrollTop;
    setText(m.title, `Transcript · ${d.agent.title}`);
    setText(m.sub, `${projName(d.agent.project)} · ${d.rewind ? 'rewind to just before a message, or edit any entry' : 'edit any entry'}`);
    const cli = KIND_NAME[d.agent.provider] || d.agent.provider;
    const warn = d.rewind ? h('p', { class: 'rw-warn', text: 'Rewinding erases everything after that moment as if it never happened: the rest of the conversation, any later sessions, ' +
      `${cli}'s own session state for them, and Switchboard's record of it (chat, messages, notes, pages, questions, outlines and the memory lines they alone back). ` +
      'Only real work stays: files on disk, jobs and the work queue. A working agent is interrupted.' })
      : h('p', { class: 'muted small', text: `Rewind works on Claude and Antigravity agents. ${d.edit ? 'Entries of this transcript can be edited.' : 'This transcript cannot be edited here yet.'}` });
    const box = h('input', { type: 'checkbox' });
    box.checked = keepCopy;
    box.onchange = () => { keepCopy = box.checked; };
    const keep = d.rewind ? h('label', { class: 'small rw-keep' }, box, ' Keep a private copy of the transcript before a rewind (off: nothing is kept)') : null;
    const view = h('div', { class: 'vz-segs' }, [[false, 'Your messages'], [true, 'Every entry']].map(([v, t]) =>
      h('button', { class: 'vz-sb', type: 'button', 'aria-pressed': String(showAll === v), text: t, onclick: () => { showAll = v; ed = null; armed = null; render(); if (v) loadAll(); } })));
    m.tools.replaceChildren(d.rewind && d.edit ? view : '');
    const everything = showAll || !d.rewind;
    const editBtn = (sid, at) => d.edit ? h('button', { class: 'btn ghost small', type: 'button', text: 'Edit', onclick: () => startEdit(sid, at) }) : null;
    const editing = (sid, at) => ed && ed.s === sid && ed.at === at;
    const momentRow = (s, mo) => {
      const on = armed && armed.s === s.session && armed.o === mo.offset;
      return [h('div', { class: 'rw-row' + (on ? ' rw-on' : '') },
        h('span', { class: 'rw-t', text: absTime(mo.ts), title: ago(mo.ts) }),
        h('span', { class: 'rw-text', text: mo.text }),
        on ? h('span', { class: 'rw-confirm' },
          h('small', { text: busyAsk ? 'The agent is working. Interrupt it and rewind?' :
            `Cut this and ${mo.later} later message${mo.later === 1 ? '' : 's'} (${fmtN(mo.cut_bytes)} bytes)${s.current ? '' : '; the agent moves back to this session'}?` }),
          h('button', { class: 'btn small rw-go', type: 'button', text: busyAsk ? 'Interrupt and rewind' : 'Rewind here', onclick: () => go(s, mo, busyAsk) }),
          h('button', { class: 'btn ghost small', type: 'button', text: 'Cancel', onclick: () => { armed = null; busyAsk = false; render(); } }))
          : h('span', { class: 'rw-confirm' }, editing(s.session, mo.offset) ? null : editBtn(s.session, mo.offset),
            h('button', { class: 'btn ghost small', type: 'button', text: 'Rewind to before this', onclick: () => { armed = { s: s.session, o: mo.offset }; busyAsk = false; ed = null; render(); } }))),
      editing(s.session, mo.offset) ? editor() : null];
    };
    const entryRows = (s) => {
      const a = all[s.session];
      if (!a) return [h('p', { class: 'muted small' }, h('button', { class: 'btn ghost small', type: 'button', text: 'Show every entry of this conversation', onclick: () => more(s.session, true) }))];
      if (a.err) return [h('p', { class: 'err small', text: a.err })];
      const rows = [], seen = new Set();
      for (let i = a.entries.length - 1; i >= 0; i--) {  // newest first, as the messages are
        const e = a.entries[i], first = !seen.has(e.at);
        seen.add(e.at);
        rows.push(h('div', { class: 'rw-row rw-e-' + e.role },
          h('span', { class: 'rw-t' }, h('b', { class: 'rw-role', text: e.role === 'user' ? 'you' : e.role === 'assistant' ? 'agent' : e.role }), ' ', absTime(e.ts)),
          h('span', { class: 'rw-text', text: e.text.slice(0, 600) }),
          h('span', { class: 'rw-confirm' }, editing(s.session, e.at) ? null : editBtn(s.session, e.at))));
        if (first && editing(s.session, e.at)) rows.push(editor());
      }
      if (!rows.length) rows.push(h('p', { class: 'muted small', text: 'Nothing shown in this conversation.' }));
      if (a.before != null) rows.push(h('p', {}, h('button', { class: 'btn ghost small', type: 'button', text: 'Load older entries', onclick: () => more(s.session) })));
      return rows;
    };
    const secs = d.sessions.map((s) => h('div', { class: 'rw-sess' },
      h('h3', {}, `Session ${s.session.slice(0, 8)}`, s.current ? h('span', { class: 'vz-pill', text: 'current' }) : h('span', { class: 'vz-pill', text: 'earlier (cleared)' }),
        h('small', { class: 'muted', text: ` · ${d.rewind ? s.moments.length + ' messages · ' : ''}${fmtN(s.size)} bytes · last written ${ago(s.mtime * 1000)}` })),
      everything ? entryRows(s) : s.moments.map((mo) => momentRow(s, mo))));
    m.body.replaceChildren(...[warn, keep, ...(secs.length ? secs : [h('p', { class: 'muted', text: 'No conversation on disk.' })]),
      d.backups.length ? h('details', { class: 'muted small' }, h('summary', { text: `${d.backups.length} earlier copies kept before rewinds` }),
        h('pre', { class: 'mf-path', text: d.backups.join('\n') }),
        h('button', { class: 'btn ghost small', type: 'button', text: 'Delete these copies', onclick: async () => {
          try { await api('POST', `/agents/${enc(aid)}/rewind/copies/delete`, {}); toast('copies deleted'); load(); } catch (e) { toast(e.message, 'err'); } } })) : null].filter(Boolean));
    m.body.scrollTop = keepScroll;
  }
  load();
}
