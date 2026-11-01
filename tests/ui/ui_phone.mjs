// Phone pairing end to end: the computer's Phone dialog makes a QR code; the "phone" (its own browser profile) opens
// it through the gate, lands in Switchboard and gets a live terminal; a second "phone" cannot use the same code or
// get in; pairing it instead locks the first one out. Needs a test-mode hub and a gate in front of it:
//   SB_BASE=http://127.0.0.1:8899 SB_GATE=http://127.0.0.1:8810 node tests/ui/ui_phone.mjs
import { launch, reporter, sleep } from './lib.mjs';
const BASE = process.env.SB_BASE || 'http://127.0.0.1:8899';
const GATE = process.env.SB_GATE || 'http://127.0.0.1:8810';
const { check, done } = reporter();
const until = async (b, expr, ms = 15000) => { const t0 = Date.now(); for (;;) { const v = await b.ev(expr); if (v && !String(v).startsWith('ERR')) return v; if (Date.now() - t0 > ms) return v; await sleep(250); } };

async function makeCode(pc) {
  await pc.go(BASE + '/', 2500);
  await until(pc, `!document.querySelector('#tb-phone').hidden`);
  await pc.ev(`window.__qr = null; const q0 = window.qrcode; window.qrcode = (...a) => { const q = q0(...a); const add = q.addData.bind(q); q.addData = (s) => { window.__qr = s; add(s); }; return q; }; true`);
  await pc.ev(`document.querySelector('#tb-phone').click()`);
  await until(pc, `!!document.querySelector('dialog.ph-dlg[open]')`);
  await pc.ev(`(() => { const i = document.querySelector('.ph-addr input'); if (!i.closest('.ph-addr').hidden) i.value = ${JSON.stringify(GATE)}; [...document.querySelectorAll('.ph-dlg button')].find((b) => /Pair/.test(b.textContent)).click(); })()`);
  return until(pc, `window.__qr`);
}

const pc = await launch();
await pc.view(1400, 900);
const url = await makeCode(pc);
check('Phone dialog shows a QR code for the gate', String(url).startsWith(GATE + '/gate/#pair=') && await pc.ev(`!!document.querySelector('.ph-qr svg')`), String(url).slice(0, 48) + '…');
await pc.shot('phone-dialog.png');

const a = await launch();
await a.view(390, 844, true, true);
await a.go(String(url), 4000);
check('phone A lands in Switchboard after scanning', await until(a, `location.origin + location.pathname === ${JSON.stringify(GATE + '/')} && !!document.querySelector('#topbar')`), await a.ev('location.href'));
check('phone A does not see the Phone (pairing) button', await a.ev(`document.querySelector('#tb-phone').hidden`));
check('the computer closed the code once the phone paired', await until(pc, `!document.querySelector('.ph-qr svg')`, 8000));
const ov = await (await fetch(BASE + '/api/overview')).json();
const ag = ov.agents.find((x) => x.role !== 'desk' && x.provider !== 'shell');
await a.go(GATE + `/#/p/${encodeURIComponent(ag.project)}/${encodeURIComponent(ag.id)}`, 2500);
check('phone A gets a live terminal through the gate', /live/.test(await until(a, `(() => { const t = [...document.querySelectorAll('.pn-conn')].map((e) => e.textContent).join('|'); return /live/.test(t) ? t : ''; })()`, 20000)));
await a.shot('phone-a.png');

const b = await launch();
await b.view(390, 844, true, false);
await b.go(String(url), 3000);
check('phone B cannot reuse the code', /not valid any more/.test(await until(b, `document.querySelector('#msg') && /not valid/.test(document.querySelector('#msg').textContent) && document.querySelector('#msg').textContent`)));
await b.go(GATE + '/', 2500);
check('phone B is sent to the gate, not into Switchboard', await b.ev(`location.pathname === '/gate/' && /not paired/.test(document.querySelector('#msg').textContent)`), await b.ev('location.href'));
check('phone B cannot read the API', (await b.ev(`fetch('/api/overview').then((r) => r.status)`)) === 401);
await b.shot('phone-b.png');

const url2 = await makeCode(pc);
await b.go(String(url2), 4000);
check('phone B pairs with a new code', await until(b, `location.pathname === '/' && !!document.querySelector('#topbar')`));
check('phone A is locked out at once', (await a.ev(`fetch('/api/overview').then((r) => r.status)`)) === 401);
await a.go(GATE + '/gate/', 3000);
check('phone A is told another phone was paired', /not the paired phone/.test(await until(a, `/not the paired/.test(document.querySelector('#msg').textContent) && document.querySelector('#msg').textContent`)));
for (const x of [pc, a, b]) x.close();
done({ logs: [...pc.logs, ...a.logs, ...b.logs].filter((l) => !/401|403|Failed to load resource/.test(l)) });
