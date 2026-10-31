// First-run setup in the browser: a hub with no accounts opens #/setup; adding an OpenAI-compatible endpoint, testing
// it and saving makes it an account on the overview. Needs a FRESH test hub (empty SB_CONFIG) and tests/fake_openai.py:
//   python3 tests/fake_openai.py 8791 &
//   SB_STATE=$(mktemp -d) SB_CONFIG=$(mktemp -d)/accounts.json SB_WORKSPACE=$(mktemp -d) SB_PORT=8898 python3 -m switchboard.hub &
//   SB_BASE=http://127.0.0.1:8898 FAKE_API=http://127.0.0.1:8791/v1 node tests/ui/ui_setup.mjs
import { launch, reporter, sleep } from './lib.mjs';
const BASE = process.env.SB_BASE || 'http://127.0.0.1:8898';
const API = process.env.FAKE_API || 'http://127.0.0.1:8791/v1';
const b = await launch(); const { check, done } = reporter();
const J = (s) => JSON.stringify(s);
async function until(expr, ms = 10000) {
  const t0 = Date.now();
  for (;;) { const v = await b.ev(expr); if (v && !String(v).startsWith('ERR')) return v; if (Date.now() - t0 > ms) return v; await sleep(200); }
}
await b.view(1300, 950);
await b.go(BASE + '/', 2500);
check('first run opens the setup page', await until(`location.hash === '#/setup' && !!document.querySelector('.setup .su-card')`));
check('three subscription cards', (await b.ev(`document.querySelectorAll('.su-subs .su-card').length`)) === 3);
await b.shot('setup-empty.png');
// add an OpenAI-compatible endpoint
await b.ev(`(() => { const s = document.querySelector('.su-add select'); s.value = 'custom'; document.querySelector('.su-add button').click(); })()`);
const row = `[...document.querySelectorAll('.su-key')].pop()`;
const field = (label) => `[...${row}.querySelectorAll('.su-f')].find((f) => f.firstChild.textContent === ${J(label)}).querySelector('input,select')`;
const set = (label, v) => b.ev(`(() => { const i = ${field(label)}; i.value = ${J(v)}; i.dispatchEvent(new Event('input')); return i.value; })()`);
await set('Base URL', API);
await b.ev(`${row}.querySelector('input[type=password]').value = 'test-key'`);
await b.ev(`${row}.querySelector('.su-test button').click()`);
check('Test lists the endpoint models', await until(`${row}.querySelector('.su-result').textContent.includes('2 models')`), await b.ev(`${row}.querySelector('.su-result').textContent`));
await set('Model', 'fake-small');
await b.ev(`${row}.querySelector('.su-test button').click()`);
check('Test with a model gets a reply', await until(`${row}.querySelector('.su-result').textContent.startsWith('works')`), await b.ev(`${row}.querySelector('.su-result').textContent`));
await b.shot('setup-filled.png');
// keep the test hermetic: untick any subscription the machine happens to be logged in to
await b.ev(`document.querySelectorAll('.su-subs input[type=checkbox]').forEach((c) => { if (c.checked) c.click(); })`);
check('chat account offers the new endpoint', (await b.ev(`document.querySelector('.su-field select').value`)) === 'local');
await b.ev(`[...document.querySelectorAll('button')].find((x) => x.textContent === 'Save').click()`);
check('saving returns to the overview', await until(`location.hash === '#/' && !!document.querySelector('.pools .pool')`));
check('the account is listed', await until(`[...document.querySelectorAll('.pool-name')].some((e) => e.textContent === 'local')`));
const ov = await (await fetch(BASE + '/api/overview')).json();
check('hub: configured, chat runs on the new account', !ov.setup_needed && ov.router === 'desk' && ov.accounts[0].kind === 'api', J({ s: ov.setup_needed, r: ov.router }));
const setup = await (await fetch(BASE + '/api/setup')).json();
check('the key is never sent back', !JSON.stringify(setup).includes('test-key'));
await b.shot('overview-after-setup.png');
done(b);
b.close();
