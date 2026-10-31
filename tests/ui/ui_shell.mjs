// "+ Shell" opens a working bash terminal in the project view. Run against a test-mode hub (SB_BASE, default :8899).
import { launch, sleep, reporter } from './lib.mjs';
const BASE = process.env.SB_BASE || 'http://127.0.0.1:8899';
const b = await launch(); const { check, done } = reporter();
await b.view(1888, 954);
await b.go(BASE + '/#/p/switchboard', 3000);
const clicked = await b.ev(`(() => { const x = [...document.querySelectorAll('button')].find((e) => e.textContent.trim() === '+ Shell'); if (x) x.click(); return !!x; })()`);
check('+ Shell button in the project view', clicked);
let id = null;
for (let i = 0; i < 40 && !id; i++) { await sleep(250); id = await b.ev(`location.hash.split('/')[3] || null`); }
check('navigated to the shell agent', !!id, id || '');
let text = '';
for (let i = 0; i < 40; i++) { await sleep(250); text = await b.ev(`(() => { const t = window.__sbTerms[${JSON.stringify(id)}]; if (!t) return ''; const buf = t.buffer.active; let s = ''; for (let y = 0; y < buf.length; y++) s += buf.getLine(y).translateToString(true) + '\\n'; return s; })()`); if (text.includes('$')) break; }
check('shell prompt rendered', text.includes('$'));
await b.ev(`window.__sbTerms[${JSON.stringify(id)}].focus()`);
await b.send('Input.insertText', { text: 'echo shell-$((40+2))' });
await b.key('Enter');
let ok = false;
for (let i = 0; i < 40 && !ok; i++) { await sleep(250); ok = await b.ev(`(() => { const buf = window.__sbTerms[${JSON.stringify(id)}].buffer.active; for (let y = 0; y < buf.length; y++) if (buf.getLine(y).translateToString(true).trim() === 'shell-42') return true; return false; })()`); }
check('typed command ran in bash (shell-42)', ok);
const tag = await b.ev(`[...document.querySelectorAll('.mtag')].some((e) => e.textContent === 'shell')`);
check('shell tagged as "shell", no model', tag);
const xNextToTab = await b.ev(`(() => { const t = [...document.querySelectorAll('.tab')].find((e) => e.textContent.includes('shell')); const x = t && t.querySelector('.tab-x'); if (!x) return false; const a = t.getBoundingClientRect(), r = x.getBoundingClientRect(); return r.right <= a.right + 1 && r.left >= a.left; })()`);
check('remove ✕ sits inside its tab', xNextToTab);
await b.shot('shell.png');
await b.ev(`window.confirm = () => true; [...document.querySelectorAll('.tab')].find((e) => e.textContent.includes('shell')).querySelector('.tab-x').click()`);
let gone = false;
for (let i = 0; i < 60 && !gone; i++) { await sleep(500); gone = await b.ev(`fetch('/api/overview').then((r) => r.json()).then((d) => !d.agents.some((a) => a.id === ${JSON.stringify(id)}))`); }
check('tab ✕ removes the agent', gone);
check('no page errors', b.logs.length === 0, b.logs.join(' | '));
done(b); b.close();
