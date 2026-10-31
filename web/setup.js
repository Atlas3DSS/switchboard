/* Setup: which AI subscriptions and API keys this Switchboard may use (#/setup). Opens by itself on first run.
   Loaded before app.js; uses its helpers (h, api, toast, setText, tick) at call time. */
'use strict';

const SUBS = [
  { kind: 'claude', vendor: 'Anthropic', plan: 'Claude Pro or Max subscription', cli: 'Claude Code', name: 'claude',
    login: 'claude   (then type /login)' },
  { kind: 'codex', vendor: 'OpenAI', plan: 'ChatGPT Plus, Pro or Business subscription', cli: 'Codex CLI', name: 'chatgpt',
    login: 'codex login' },
  { kind: 'gemini', vendor: 'Google', plan: 'Google account (free tier or Google AI plan)', cli: 'Gemini CLI', name: 'gemini',
    login: 'gemini   (then choose "Login with Google")' },
];
/* API presets that a vendor CLI can also run with the same key */
const CLI_FOR_PRESET = { anthropic: 'claude', openai: 'codex', google: 'gemini' };
const CLI_NAME = { claude: 'Claude Code', codex: 'Codex CLI', gemini: 'Gemini CLI' };

function setupView() {
  let data = null;
  const subBox = h('div', { class: 'su-subs' });
  const keyBox = h('div', { class: 'su-keys' });
  const addKind = h('select', { 'aria-label': 'Which API' });
  const addBtn = h('button', { class: 'btn small', type: 'button', text: '+ Add API key' });
  const ws = h('input', { type: 'text', autocomplete: 'off', spellcheck: 'false', 'aria-label': 'Workspace folder' });
  const router = h('select', { 'aria-label': 'Account for the Switchboard chat' });
  const save = h('button', { class: 'btn primary', type: 'button', text: 'Save' });
  const status = h('span', { class: 'muted small' });
  const intro = h('p', { class: 'su-intro' });
  const el = h('div', { class: 'setup' },
    h('h1', { text: 'Set up Switchboard' }), intro,
    h('section', { class: 'block' }, h('h2', { text: 'Subscriptions' }),
      h('p', { class: 'muted small', text: 'A subscription runs through that vendor\'s own command-line agent, logged in once on this computer. Switchboard opens it in a terminal for each agent.' }),
      subBox),
    h('section', { class: 'block' }, h('h2', { text: 'API keys' }),
      h('p', { class: 'muted small', text: 'Any OpenAI-compatible endpoint works: Anthropic, OpenAI, Google Gemini, OpenRouter, or your own (Ollama, vLLM, LM Studio, a company gateway). Keys run on Switchboard\'s built-in agent, which needs nothing else installed; Anthropic, OpenAI and Google keys can run on the vendor CLI instead. Keys are stored on this computer only (~/.config/switchboard/accounts.json, readable only by you).' }),
      keyBox, h('div', { class: 'su-add' }, addKind, addBtn)),
    h('section', { class: 'block' }, h('h2', { text: 'Defaults' }),
      h('label', { class: 'su-field' }, h('span', { text: 'Project folders go in' }), ws),
      h('label', { class: 'su-field' }, h('span', { text: 'Switchboard chat runs on' }), router)),
    h('div', { class: 'su-save' }, status, save));

  const subRows = [];
  const keyRows = [];

  function subRow(spec, acct, det) {
    const on = h('input', { type: 'checkbox' });
    on.checked = !!acct;
    const st = det || {};
    const badge = (ok, yes, no) => h('span', { class: 'su-badge ' + (ok ? 'ok' : 'no'), text: ok ? yes : no });
    const lines = [];
    if (!st.installed) lines.push(h('div', { class: 'su-hint' }, 'Install: ', h('code', { text: st.install || '' })));
    else if (!st.logged_in) lines.push(h('div', { class: 'su-hint' }, 'Log in once: click Shell (top right) and run ', h('code', { text: spec.login })));
    const row = h('div', { class: 'su-card' + (acct ? ' on' : '') },
      h('label', { class: 'su-check' }, on,
        h('span', null, h('strong', { text: spec.vendor }), ' · ', spec.plan)),
      h('div', { class: 'su-meta' }, h('span', { class: 'muted small', text: 'runs ' + spec.cli }),
        badge(st.installed, 'installed' + (st.version ? ' ' + st.version : ''), 'not installed'),
        st.installed ? badge(st.logged_in, 'logged in', 'not logged in yet') : null),
      ...lines);
    on.addEventListener('change', () => { row.classList.toggle('on', on.checked); refreshRouter(); });
    subRows.push({ spec, on, acct });
    return row;
  }

  function keyRow(init) {
    const presets = data.presets;
    const preset = init.preset || 'custom';
    const name = h('input', { type: 'text', value: init.name || '', 'aria-label': 'Account name', autocomplete: 'off', spellcheck: 'false' });
    const base = h('input', { type: 'text', value: init.base_url || presets[preset].base_url || '', placeholder: 'https://host/v1', 'aria-label': 'Base URL', spellcheck: 'false' });
    const key = h('input', { type: 'password', autocomplete: 'off', 'aria-label': 'API key', placeholder: init.has_key ? `stored (${init.key_hint}) — leave blank to keep` : (preset === 'custom' ? 'API key (blank if none)' : 'paste your API key') });
    const envName = presets[preset].env;
    const envHas = envName && data.detect.env[envName];
    const useEnv = h('button', { class: 'pbtn', type: 'button', text: 'use $' + envName, hidden: !envHas, title: `Use the key already in ${envName} (Switchboard's environment)` });
    let keyEnv = null;
    useEnv.addEventListener('click', () => { keyEnv = envName; key.value = ''; key.placeholder = `from $${envName}`; });
    const listId = 'su-models-' + Math.random().toString(36).slice(2);
    const models = h('datalist', { id: listId }, (init.models || presets[preset].models || []).map((m) => h('option', { value: m })));
    const model = h('input', { type: 'text', value: init.model || '', list: listId, placeholder: 'model id (Test lists them)', 'aria-label': 'Model', spellcheck: 'false' });
    const cliKind = CLI_FOR_PRESET[preset];
    const runWith = h('select', { 'aria-label': 'Run with' }, h('option', { value: 'api', text: 'built-in agent' }));
    if (cliKind) runWith.append(h('option', { value: cliKind, text: CLI_NAME[cliKind] + (data.detect.clis[cliKind].installed ? '' : ' (not installed)') }));
    runWith.value = init.kind && init.kind !== 'api' ? init.kind : 'api';
    const test = h('button', { class: 'btn small', type: 'button', text: 'Test' });
    const result = h('div', { class: 'su-result muted small' });
    const remove = h('button', { class: 'pbtn', type: 'button', text: 'Remove' });
    const lab = (t, n, cls) => h('label', { class: 'su-f ' + (cls || '') }, h('span', { text: t }), n);
    const row = h('div', { class: 'su-card on su-key' },
      h('div', { class: 'su-key-h' }, h('strong', { text: presets[preset].label }),
        presets[preset].keys_at ? h('a', { href: presets[preset].keys_at, target: '_blank', rel: 'noopener', class: 'small', text: 'get a key' }) : null,
        h('span', { class: 'spacer' }), remove),
      h('div', { class: 'su-grid' }, lab('Name', name), lab('Run with', runWith), lab('Base URL', base, 'wide'),
        lab('API key', h('span', { class: 'su-keyin' }, key, useEnv), 'wide'), lab('Model', model, 'wide')),
      h('div', { class: 'su-test' }, test, result), models);
    base.readOnly = preset !== 'custom';
    const entry = {
      row, preset,
      value() {
        const kind = runWith.value;
        const a = { name: name.value.trim(), kind, label: presets[preset].label, api_key: key.value.trim() };
        if (keyEnv && !a.api_key) a.key_env = keyEnv;
        if (kind === 'api') Object.assign(a, { preset, base_url: base.value.trim(), model: model.value.trim(), models: [...models.options].map((o) => o.value) });
        else if (model.value.trim()) a.model = model.value.trim();
        return a;
      },
    };
    remove.addEventListener('click', () => { keyRows.splice(keyRows.indexOf(entry), 1); row.remove(); refreshRouter(); });
    name.addEventListener('input', refreshRouter);
    runWith.addEventListener('change', refreshRouter);
    test.addEventListener('click', async () => {
      test.disabled = true; setText(result, 'testing…'); result.className = 'su-result muted small';
      const a = { ...entry.value(), kind: 'api', preset, base_url: base.value.trim(), model: model.value.trim(), name: init.name || '' };
      try {
        const r = await api('POST', '/setup/test', { account: a });
        if (r.models && r.models.length) {
          models.replaceChildren(...r.models.map((m) => h('option', { value: m })));
          if (!model.value && r.models.length) model.placeholder = `${r.models.length} models — start typing`;
        }
        result.className = 'su-result small ' + (r.ok ? 'ok' : 'err');
        setText(result, r.ok ? (model.value ? `works: "${(r.reply || '').trim().slice(0, 60)}"` : `key works · ${r.models.length} models listed — pick one and test again`)
          : (r.error || 'failed'));
      } catch (err) { result.className = 'su-result small err'; setText(result, err.message); }
      test.disabled = false;
    });
    keyRows.push(entry);
    return row;
  }

  function uniqueName(base) {
    const taken = new Set([...keyRows.map((r) => r.value().name), ...subRows.filter((r) => r.on.checked).map((r) => r.spec.name)]);
    let n = base, i = 2;
    while (taken.has(n)) n = `${base}-${i++}`;
    return n;
  }

  function collect() {
    const out = [];
    for (const r of subRows) {
      if (!r.on.checked) continue;
      out.push({ ...(r.acct || {}), name: (r.acct && r.acct.name) || r.spec.name, kind: r.spec.kind, label: r.spec.plan, api_key: '' });
    }
    for (const r of keyRows) out.push(r.value());
    /* extra logins kept from the file (e.g. a second Claude config dir) */
    for (const a of data.accounts) if (a.kind !== 'api' && !a.has_key && !out.some((x) => x.name === a.name) && !SUBS.some((s) => s.kind === a.kind && s.name === a.name) && !subRows.some((r) => r.acct === a)) out.push(a);
    return out;
  }

  function refreshRouter() {
    const cur = router.value || (data && data.router_account);
    const opts = collect().filter((a) => a.kind !== 'gemini' && a.name);
    router.replaceChildren(...(opts.length ? opts.map((a) => h('option', { value: a.name, text: `${a.name} (${a.kind === 'api' ? 'built-in agent' : CLI_NAME[a.kind]})` }))
      : [h('option', { value: '', text: 'add a Claude, ChatGPT or API account first' })]));
    if (opts.some((a) => a.name === cur)) router.value = cur;
  }

  async function load() {
    data = await api('GET', '/setup');
    setText(intro, data.configured
      ? 'Your accounts. Changes apply to agents the next time they start.'
      : 'Welcome. Tell Switchboard which AI subscriptions and API keys you have — tick what applies, add keys, then Save. You can change this any time from Setup in the top bar.');
    const det = data.detect.clis;
    subRows.length = 0; keyRows.length = 0;
    subBox.replaceChildren(...SUBS.map((s) => subRow(s, data.accounts.find((a) => a.kind === s.kind && !a.has_key && (a.name === s.name || !data.accounts.some((b) => b.name === s.name))), det[s.kind])));
    keyBox.replaceChildren(...data.accounts.filter((a) => a.kind === 'api' || a.has_key).map((a) => keyRow({ ...a, preset: a.preset || Object.keys(CLI_FOR_PRESET).find((p) => CLI_FOR_PRESET[p] === a.kind) || 'custom' })));
    addKind.replaceChildren(...Object.entries(data.presets).map(([k, p]) => h('option', { value: k, text: p.label })));
    ws.value = data.workspace || '';
    refreshRouter();
    /* first run: tick the subscriptions whose CLI is already logged in */
    if (!data.configured) for (const r of subRows) if (det[r.spec.kind].logged_in) { r.on.checked = true; r.on.dispatchEvent(new Event('change')); }
    if (!data.configured) for (const [p, env] of [['anthropic', 'ANTHROPIC_API_KEY'], ['openai', 'OPENAI_API_KEY'], ['google', 'GEMINI_API_KEY'], ['openrouter', 'OPENROUTER_API_KEY']]) {
      if (data.detect.env[env]) { addKind.value = p; addBtn.click(); keyRows[keyRows.length - 1].row.querySelector('.su-keyin .pbtn').click(); }
    }
  }

  addBtn.addEventListener('click', () => {
    const p = addKind.value;
    const base = { anthropic: 'anthropic-api', openai: 'openai-api', google: 'gemini-api', openrouter: 'openrouter', custom: 'local' }[p] || p;
    keyBox.append(keyRow({ preset: p, name: uniqueName(base) }));
    refreshRouter();
  });

  save.addEventListener('click', async () => {
    const accounts = collect();
    if (!accounts.length) { toast('Tick a subscription or add an API key first', 'err'); return; }
    save.disabled = true; setText(status, 'saving…');
    try {
      await api('POST', '/setup', { accounts, workspace: ws.value.trim(), router_account: router.value || null });
      toast('Saved. Add a project, then "+ Agent" on its card.');
      await tick();
      location.hash = '#/';
    } catch (err) { toast(err.message, 'err'); setText(status, ''); } finally { save.disabled = false; }
  });

  load().catch((err) => setText(intro, 'Could not load setup: ' + err.message));
  return { el, title: 'Setup', update() {}, destroy() {} };
}
