// The owner's Inbox: topbar button + badge, a card scoped to the open project, items crossed off by option, by text,
// by Dismiss (decision) or Acknowledge (need). Needs a test-mode hub on :8899 (temp SB_STATE):
//   SB_BASE=http://127.0.0.1:8899 node tests/ui/ui_inbox.mjs
// Files questions and needs through the API and crosses them off from the page; never point it at :8802.
import { launch, reporter, sleep } from './lib.mjs';
const BASE = process.env.SB_BASE || 'http://127.0.0.1:8899';
const b = await launch(); const { check, done } = reporter();
const J = (s) => JSON.stringify(s);
const get = async (p) => (await fetch(BASE + p)).json();
const post = async (p, body) => (await fetch(BASE + p, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) })).json();
const rowIds = () => b.ev(`[...document.querySelectorAll('#inbox-card .ib-item .ib-who')].map(e=>e.title)`);
const row = (id) => `[...document.querySelectorAll('#inbox-card .ib-item')].find(r=>r.querySelector('.ib-who').title===${J(id)})`;

const before = (await get('/api/inbox')).count;
const d1 = await post('/api/decisions', { project: 'switchboard', agent: 'fake-1', question: 'Merge the branch now, or wait for review?', options: ['merge', 'wait'] });
await sleep(1200);  // created_at has second resolution
const d2 = await post('/api/decisions', { project: null, agent: null, question: 'Unsorted free-text question' });
await sleep(1200);
const n1 = await post('/api/notifications', { agent: 'fake-2', text: 'Need: please look at the failing build', level: 'need' });
await b.view(1888, 1000);
await b.go(BASE + '/', 3000);
await b.ev(`window.confirm = () => true`);
const total = before + 3;
check('badge shows the count across all projects', (await b.ev(`document.getElementById('tb-inbox-n').textContent`)) === String(total) && await b.ev(`document.getElementById('inbox-card').hidden`) === true, await b.ev(`document.getElementById('tb-inbox-n').textContent`));
await b.ev(`document.getElementById('tb-inbox').click()`); await sleep(1500);
check('button opens the card with the total and scope', await b.ev(`document.getElementById('inbox-card').hidden`) === false && (await b.ev(`document.querySelector('.ib-title').textContent`)) === `Inbox · ${total}` && (await b.ev(`document.querySelector('.ib-scope').textContent`)) === 'all projects');
const groups = await b.ev(`[...document.querySelectorAll('#inbox-card .ib-group')].map(g=>g.querySelector('.ib-proj').textContent+':'+g.querySelector('.n').textContent)`);
check('all projects: grouped, newest group first', groups.length >= 2 && groups.some((g) => g.startsWith('Switchboard:')) && groups.some((g) => g.startsWith('No project:1')), J(groups));
const ids = await rowIds();
check('newest first', ids.indexOf(n1.id) < ids.indexOf(d1.id) && ids.includes(d2.id), J(ids.slice(0, 4)));
check('decision row: who, age, text, one button per option, answer box, Dismiss', await b.ev(`(()=>{const r=${row(d1.id)}; return r.querySelector('.ib-who').textContent==='fake-1' && /ago$/.test(r.querySelector('.ib-age').textContent) && r.querySelector('.ib-text').textContent.startsWith('Merge the branch') && [...r.querySelectorAll('.ib-opt')].map(b=>b.textContent).join()==='merge,wait' && !r.querySelector('.ib-txt').hidden && r.querySelector('.ib-dismiss').textContent==='Dismiss';})()`));
check('need row: no options, a Reply box, Acknowledge', await b.ev(`(()=>{const r=${row(n1.id)}; return r.querySelector('.ib-kind').textContent==='needs you' && r.querySelector('.ib-opts').hidden && !r.querySelector('.ib-txt').hidden && r.querySelector('.ib-dismiss').textContent==='Acknowledge';})()`));
await b.shot('inbox-1888.png');

/* option button answers */
await b.ev(`[...${row(d1.id)}.querySelectorAll('.ib-opt')].find(x=>x.textContent==='wait').click()`); await sleep(2500);
let d = (await get('/api/projects/switchboard')).decisions.find((x) => x.id === d1.id);
check('option answers the decision and the row leaves', d && d.state === 'answered' && d.answer === 'wait' && !(await rowIds()).includes(d1.id), J(d && { state: d.state, answer: d.answer }));
/* acknowledge a need */
await b.ev(`${row(n1.id)}.querySelector('.ib-dismiss').click()`); await sleep(2500);
check('Acknowledge crosses a need off', !(await rowIds()).includes(n1.id) && !(await get('/api/inbox')).items.some((x) => x.id === n1.id));
/* typed answer on the unsorted question */
await b.ev(`(()=>{const r=${row(d2.id)}; const f=r.querySelector('.ib-form'); f.querySelector('input').value='typed answer'; f.requestSubmit();})()`); await sleep(2500);
check('typed answer crosses the question off', !(await rowIds()).includes(d2.id) && !(await get('/api/inbox')).items.some((x) => x.id === d2.id));
await sleep(3500);  // the badge comes from the overview poll
check('badge follows', (await b.ev(`document.getElementById('tb-inbox-n').textContent`)) === (before ? String(before) : ''));

/* project scope and Dismiss */
const d3 = await post('/api/decisions', { project: 'switchboard', agent: 'fake-1', question: 'Scoped to switchboard', options: ['ok'] });
const d4 = await post('/api/decisions', { project: 'unsorted', agent: null, question: 'Not on switchboard' });
await sleep(3500);
await b.ev(`location.hash = '#/p/switchboard'`); await sleep(3500);
const scoped = await rowIds();
check('project view scopes the card to that project', (await b.ev(`document.querySelector('.ib-scope').textContent`)) === 'Switchboard' && scoped.includes(d3.id) && !scoped.includes(d4.id) && await b.ev(`document.querySelectorAll('#inbox-card .ib-group').length`) === 0, J(scoped));
const sbCount = (await get('/api/inbox?project=switchboard')).count;
check('badge shows the project count on a project view', (await b.ev(`document.getElementById('tb-inbox-n').textContent`)) === String(sbCount), await b.ev(`document.getElementById('tb-inbox-n').textContent`));
await b.shot('inbox-project-1888.png');
await b.ev(`${row(d3.id)}.querySelector('.ib-dismiss').click()`); await sleep(2500);
d = (await get('/api/projects/switchboard')).decisions.find((x) => x.id === d3.id);
check('Dismiss closes a decision without an answer', d && d.state === 'dismissed' && !(await rowIds()).includes(d3.id), J(d && d.state));
await post('/api/inbox/' + d4.id + '/done', {});
/* close, Escape, phone width */
await b.ev(`document.querySelector('.ib-close').click()`);
check('close button hides the card', await b.ev(`document.getElementById('inbox-card').hidden`) === true);
await b.ev(`location.hash = '#/'`); await sleep(1500);
await b.ev(`document.getElementById('tb-inbox').click()`); await sleep(800);
await b.view(390, 844, true); await sleep(800);
await post('/api/decisions', { project: 'switchboard', agent: 'fake-1', question: 'Phone-width check', options: ['yes', 'no'] });
await sleep(3500);
check('card fits 390px', await b.ev(`[...document.querySelectorAll('#inbox-card, #inbox-card *')].filter(e=>!e.hidden).every(e=>e.getBoundingClientRect().right<=391)`));
await b.shot('inbox-390.png');
await b.ev(`[...document.querySelectorAll('#inbox-card .ib-item')].find(r=>r.querySelector('.ib-text').textContent==='Phone-width check').querySelector('.ib-dismiss').click()`); await sleep(1500);
done(b); b.close();
