// Work queue panel + chat Brief/Wrap up buttons on the overview. Needs a test-mode hub (tasks/terminals/BACKEND_READY)
// with a few items (sb work add ...). Never presses Brief or Wrap up (those start a real desk turn).
//   SB_BASE=http://127.0.0.1:8899 node tests/ui/ui_work.mjs
import { launch, reporter, sleep } from './lib.mjs';
const BASE = process.env.SB_BASE || 'http://127.0.0.1:8899';
const b = await launch(); const { check, done } = reporter();
const J = (s) => JSON.stringify(s);
const get = async (p) => (await fetch(BASE + p)).json();
const rows = () => b.ev(`[...document.querySelectorAll('.wq-row')].map(r=>r.querySelector('.wq-title').value)`);

await b.view(1888, 1000);
await b.go(BASE + '/', 2500);
await b.ev(`window.confirm = () => true`);
check('chat has Brief and Wrap up', await b.ev(`[...document.querySelectorAll('.chat-h .pbtn')].filter(x=>!x.hidden).map(x=>x.textContent).join(',')`) === 'Brief,Wrap up,Sessions,New chat');
const open = (await get('/api/work')).items;
check('panel lists the open items', JSON.stringify(await rows()) === JSON.stringify(open.map((w) => w.title)), J(await rows()));
check('count shows open items', (await b.ev(`document.querySelector('.wq h2 .n').textContent`)).startsWith(open.length + ' open'));
await b.shot('work-1888.png');

/* add through the form */
await b.ev(`(()=>{const f=document.querySelector('.wq-add'); f.querySelector('input').value='Added from the page'; f.querySelectorAll('select')[0].value='switchboard'; f.querySelectorAll('select')[1].value='1'; f.requestSubmit();})()`);
await sleep(1500);
let all = (await get('/api/work?status=all')).items;
const added = all.find((w) => w.title === 'Added from the page');
check('form adds an item with project and priority', added && added.project === 'switchboard' && added.priority === 1, J(added));
check('new item shows in the list', (await rows()).includes('Added from the page'));

/* status select, inline title edit */
await b.ev(`(()=>{const r=[...document.querySelectorAll('.wq-row')].find(r=>r.querySelector('.wq-title').value==='Added from the page'); const s=r.querySelector('.wq-st'); s.value='blocked'; s.dispatchEvent(new Event('change')); const t=r.querySelector('.wq-title'); t.focus(); t.value='Renamed on the page'; t.blur();})()`);
await sleep(1500);
const w2 = (await get('/api/work/' + added.id));
check('status and title edits save', w2.status === 'blocked' && w2.title === 'Renamed on the page', J(w2));

/* finished toggle, project filter */
const nAll = (await get('/api/work?status=all')).items.length;
await b.ev(`(()=>{const c=document.querySelector('.wq-done input'); c.checked=true; c.dispatchEvent(new Event('change'));})()`);
await sleep(300);
check('"finished" shows done items too', (await rows()).length === nAll, `${(await rows()).length} vs ${nAll}`);
await b.ev(`(()=>{const s=document.querySelector('.wq-filter'); s.value='switchboard'; s.dispatchEvent(new Event('change'));})()`);
await sleep(300);
const nSb = (await get('/api/work?status=all&project=switchboard')).items.length;
check('project filter', (await rows()).length === nSb, `${(await rows()).length} vs ${nSb}`);
await b.ev(`(()=>{const s=document.querySelector('.wq-filter'); s.value=''; s.dispatchEvent(new Event('change')); const c=document.querySelector('.wq-done input'); c.checked=false; c.dispatchEvent(new Event('change'));})()`);

/* delete */
await b.ev(`[...document.querySelectorAll('.wq-row')].find(r=>r.querySelector('.wq-title').value==='Renamed on the page').querySelector('.wq-del').click()`);
await sleep(1500);
check('delete removes it', !(await rows()).includes('Renamed on the page') && !(await get('/api/work?status=all')).items.some((w) => w.id === added.id));

await b.view(1888, 1000, false, true); await sleep(400); await b.shot('work-1888-dark.png');
await b.view(390, 844, true); await sleep(600);
check('work queue fits 390px', await b.ev(`[...document.querySelectorAll('.wq, .wq *')].every(e=>e.getBoundingClientRect().right<=390)`));
await b.ev(`document.querySelector('.wq').scrollIntoView()`); await sleep(300);
await b.shot('work-390.png');
done(b); b.close();
