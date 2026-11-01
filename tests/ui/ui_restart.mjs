// A hub restart started from somewhere else (another pane, another page) must not strand an open terminal: it shows
// "hub restarting…" while the hub is down and comes back live by itself, with no "refused" overlay to click.
// Restarts the hub it runs against, so only ever point it at a test-mode hub:
//   SB_BASE=http://127.0.0.1:8899 node tests/ui/ui_restart.mjs
import { launch, reporter, sleep } from './lib.mjs';
const BASE = process.env.SB_BASE || 'http://127.0.0.1:8899';
if (/:8802\b/.test(BASE)) { console.log('refusing to restart the live hub'); process.exit(2); }
const b = await launch(); const { check, done } = reporter();
const ov = await (await fetch(BASE + '/api/overview')).json();
const a = ov.agents.find((x) => x.role !== 'desk' && x.provider !== 'shell' && x.mode === 'terminal');
await b.view(1400, 900);
await b.go(BASE + `/#/p/${encodeURIComponent(a.project)}/${encodeURIComponent(a.id)}`, 1500);
const conn = `(() => [...document.querySelectorAll('.pn-conn')].map((e) => e.textContent).join('|'))()`;
const refusedShown = `[...document.querySelectorAll('.ovl')].some((e) => !e.hidden && /refused this terminal/.test(e.textContent))`;
let s = '';
for (let i = 0; i < 60 && !/live/.test(s); i++) { await sleep(250); s = await b.ev(conn); }
check(`terminal ${a.id} live before the restart`, /live/.test(s), s);
await b.ev(`window.__beforeRestart = 1`);
const started = ov.code && ov.code.started_at;
const r = await fetch(BASE + '/api/restart', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: '{}' });
check('restart accepted', r.ok, String(r.status));
let sawDown = false, sawRefused = false, back = false, labels = new Set();
for (let i = 0; i < 160 && !back; i++) {
  await sleep(250);
  s = await b.ev(conn); labels.add(s);
  if (!/live/.test(s)) sawDown = true;
  if (await b.ev(refusedShown)) sawRefused = true;
  if (sawDown && /live/.test(s)) {
    try { back = (await (await fetch(BASE + '/api/overview')).json()).code.started_at !== started; } catch (e) { /* down */ }
  }
}
check('the terminal noticed the hub going away', sawDown, [...labels].join(' → '));
check('no "refused" overlay during the restart', !sawRefused);
check('terminal live again on the restarted hub, without a click', back, s);
check('same page (no reload), like a pane that did not press Restart', (await b.ev(`window.__beforeRestart`)) === 1);
b.close();
done(b);
