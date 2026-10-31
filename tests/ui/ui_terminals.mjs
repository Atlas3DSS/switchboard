// Switchboard v2 browser check: Switchboard chat (desk) on the overview, project cards without the desk, 1/2/3/4
// terminal panes over the hub WebSocket, per-pane input, keys, resize, image paste. Needs a hub in test mode whose
// agents run tests/fake_cli.py (see "Tests" in README.md). Sends exactly ONE real message to the desk.
//   SB_BASE=http://127.0.0.1:8899 node tests/ui/ui_terminals.mjs     (optional: SB_TEST_PROJECT=<id>, SB_NO_DESK=1)
import { launch, reporter, sleep } from './lib.mjs';
const BASE = process.env.SB_BASE || 'http://127.0.0.1:8899';
const b = await launch(); const { check, done } = reporter();
const J = (s) => JSON.stringify(s);
const get = async (p) => (await fetch(BASE + p)).json();
async function until(expr, ms = 15000, step = 200) {
  const t0 = Date.now();
  for (;;) { const v = await b.ev(expr); if (v && !String(v).startsWith('ERR')) return v; if (Date.now() - t0 > ms) return v; await sleep(step); }
}
const buf = (id) => `(()=>{const t=window.__sbTerms&&window.__sbTerms[${J(id)}]; if(!t) return ''; const b=t.buffer.active; let s=''; for(let i=0;i<b.length;i++){const l=b.getLine(i); if(l) s+=l.translateToString(true)+'\\n';} return s;})()`;
const has = (id, s) => `${buf(id)}.includes(${J(s)})`;
const ctrlC = async () => { for (const type of ['keyDown', 'keyUp']) await b.send('Input.dispatchKeyEvent', { type, key: 'c', code: 'KeyC', modifiers: 2, windowsVirtualKeyCode: 67 }); };
async function clickAt(sel) {
  const r = JSON.parse(await b.ev(`(()=>{const e=document.querySelector(${J(sel)}); if(!e) return 'null'; const r=e.getBoundingClientRect(); return JSON.stringify({x:r.left+r.width/2,y:r.top+r.height/2})})()`) || 'null');
  if (!r) return false;
  for (const type of ['mousePressed', 'mouseReleased']) await b.send('Input.dispatchMouseEvent', { type, x: r.x, y: r.y, button: 'left', clickCount: 1 });
  return true;
}

/* ---- pick a project and make sure it has 4 launchable (non-desk) agents ---- */
let ov = await get('/api/overview');
const router = ov.router || 'desk';
const isDesk = (a) => a.role === 'desk' || a.id === router;
const desk = ov.agents.find(isDesk);
check('hub reports the desk agent', !!desk, desk ? desk.id : 'router=' + router);
const usable = (p) => ov.agents.filter((a) => a.project === p.id && !isDesk(a) && !a.launch_block);
const live = ov.projects.filter((p) => !p.archived);
const proj = live.find((p) => p.id === process.env.SB_TEST_PROJECT) || live.slice().sort((x, y) => usable(y).length - usable(x).length)[0];
const claudeAcct = (ov.accounts.find((a) => a.kind === 'claude') || {}).name;
for (let i = usable(proj).length; i < 4; i++) {
  const r = await fetch(BASE + '/api/agents', { method: 'POST', headers: { 'Content-Type': 'application/json' },
    body: J({ project: proj.id, provider: 'claude', account: claudeAcct, cwd: '/tmp', title: `ui-extra-${i + 1}` }) });
  if (!r.ok) console.log('could not add agent:', r.status, await r.text());
}
ov = await get('/api/overview');
const agents = usable(proj).map((a) => a.id);
check(`project ${proj.id} has 4 launchable agents`, agents.length >= 4, agents.join(','));
const [A1, A2] = agents;

/* ---- overview ---- */
await b.view(1888, 954, false, true);
await b.go(BASE + '/', 500);
await b.ev(`localStorage.clear()`);
await b.go(BASE + '/', 500);
await until(`document.querySelectorAll('.pcard').length`);
check('Switchboard chat panel on the overview', await b.ev(`!!document.querySelector('.ov-chat .chat textarea')`));
check('pool panel per account', (await b.ev(`document.querySelectorAll('.pool').length`)) === ov.accounts.length);
const deskId = desk ? desk.id : 'desk';
check('desk agent not in pools or cards', await b.ev(`![...document.querySelectorAll('.alink, .arow')].some(a=>decodeURIComponent(a.getAttribute('href')||'').endsWith('/'+${J(deskId)}))`));
check('no lead UI (★, "Message the lead", Make lead)', await b.ev(`!document.querySelector('.star, .pc-send') && !document.body.textContent.includes('Message the lead') && !document.body.textContent.includes('Make lead')`));
const poolTxt = await b.ev(`[...document.querySelectorAll('.pool-kind')].map(e=>e.textContent).join(' | ')`);
check('pools name each account\'s kind', /Claude Code|Codex|Gemini|built-in/.test(poolTxt), poolTxt.slice(0, 120));
check('page does not scroll sideways', await b.ev(`document.documentElement.scrollWidth <= innerWidth + 1`));

/* ---- open-token rendering (stubbed, no desk call) ---- */
const rend = JSON.parse(await b.ev(`(()=>{const e=window.__sbRenderChat('[[open:${proj.id}/${A1}]] Here you go.'); window.__sbOpenBtn=e.querySelector('button'); return JSON.stringify({text:e.textContent, btn:[...e.querySelectorAll('button')].map(x=>x.textContent)})})()`));
check('[[open:p/a]] token stripped and rendered as a button', !rend.text.includes('[[open') && rend.btn.length === 1 && rend.btn[0].startsWith('Open ') && rend.btn[0].includes(' · '), J(rend));
const md = await b.ev(`(()=>{const e=window.__sbRenderChat('**bold** and \`code\`\\n- one\\n- two'); return !!e.querySelector('strong') && !!e.querySelector('code') && e.querySelectorAll('li').length===2})()`);
check('light markdown: bold, code, list', md === true);

/* ---- one real desk message ---- */
const body = `ui test ${Date.now()}: which project is ${A1} on? one short sentence.`;
if (!process.env.SB_NO_DESK) {
  await b.ev(`(()=>{const t=document.querySelector('.ov-chat textarea'); t.focus(); t.value=${J(body)};})()`);
  await b.key('Enter');
  let found = null;
  for (let i = 0; i < 25 && !found; i++) { await sleep(200); found = ((await get('/api/messages?agent=desk&limit=20')).messages || []).find((m) => m.body === body); }
  check('chat Enter creates a message to desk', !!found, found ? found.state : 'not found');
  check('chat shows the sent line / thinking', await until(`[...document.querySelectorAll('.ov-chat .cm.me')].some(e=>e.textContent.includes(${J(body.slice(0, 30))}))`, 5000) === true);
  const reply = await until(`(()=>{const ms=[...document.querySelectorAll('.ov-chat .cm')]; const i=ms.findIndex(e=>e.classList.contains('me')&&e.textContent.includes(${J(body.slice(0, 30))})); return i>=0 && ms.slice(i+1).some(e=>e.classList.contains('sb')) })()`, 120000, 1000);
  const fin = ((await get('/api/messages?agent=desk&limit=20')).messages || []).find((m) => m.body === body) || {};
  check('desk reply appears in the chat', reply === true, `message ${fin.state}${fin.error ? ': ' + fin.error : ''}`);
}
await b.ev(`document.querySelector('.ov-chat .chat-scroll').scrollTop=1e6`);
await b.shot('terminals-overview.png');

/* ---- project view via the open button: 1 pane ---- */
await b.ev(`window.__sbOpenBtn.click()`);
check('open button navigates to the agent', (await until(`location.hash === ${J(`#/p/${proj.id}/${A1}`)} || location.hash === ${J(`#/p/${encodeURIComponent(proj.id)}/${encodeURIComponent(A1)}`)}`, 3000)) === true);
check('1 pane by default', (await until(`document.querySelectorAll('.stage .pane:not([hidden])').length === 1`, 4000)) === true);
check('terminal renders "FAKE CLI"', (await until(has(A1, 'FAKE CLI'), 20000)) === true);
const size = await b.ev(`(()=>{const t=window.__sbTerms[${J(A1)}]; return t.cols+'x'+t.rows})()`);
check(`resize reached the pty (${size})`, (await until(has(A1, 'size ' + size), 8000)) === true);
const [cols, rows] = size.split('x').map(Number);
check('one pane uses nearly the whole window', rows >= 45 && cols >= 180, size);
check('terminal focused after open', (await until(`document.activeElement && document.activeElement.classList.contains('xterm-helper-textarea')`, 4000)) === true);
await b.send('Input.insertText', { text: 'hello world' }); await sleep(150); await b.key('Enter');
check('typed line answered "ok: hello world"', (await until(has(A1, 'ok: hello world'), 10000)) === true);
await b.send('Input.insertText', { text: 'junk line' }); await until(has(A1, 'junk line'), 3000); await ctrlC();
check('Ctrl+C reaches the CLI', (await until(`!${has(A1, 'junk line')}`, 3000)) === true);
await b.send('Input.insertText', { text: 'slow one' }); await sleep(100); await b.key('Enter');
await until(has(A1, 'Working'), 3000); await b.key('Escape');
check('Esc reaches the CLI (Interrupted)', (await until(has(A1, 'Interrupted'), 3000)) === true);
await b.ev(`(async()=>{const c=document.createElement('canvas'); c.width=4; c.height=4; const blob=await new Promise(r=>c.toBlob(r,'image/png'));
  const dt=new DataTransfer(); dt.items.add(new File([blob],'shot.png',{type:'image/png'}));
  document.activeElement.dispatchEvent(new ClipboardEvent('paste',{clipboardData:dt,bubbles:true,cancelable:true})); })()`);
await sleep(1200); await b.send('Input.insertText', { text: ' ' });   // fake CLI redraws its composer on the next key
const pathShown = await until(`(()=>{const m=${buf(A1)}.match(/\\/[^\\s]*\\.png/i); return m && m[0]})()`, 6000);
check('image paste uploads and pastes a path', !!pathShown && !String(pathShown).startsWith('ERR'), String(pathShown));
await ctrlC();
check('desk not offered in the pane picker', await b.ev(`![...document.querySelectorAll('.pn-pick option')].some(o=>o.value===${J(deskId)})`));
check('controls bar bound to the focused agent', await b.ev(`(()=>{const c=document.querySelector('.cbar'); return !!c && !c.hidden && !!c.querySelector('.pn-sel') && !!c.querySelector('.pn-model')})()`));
await sleep(300);
await b.shot('terminals-1pane.png');

/* ---- 2 panes: typing in pane 2 reaches only the pane-2 agent ---- */
await b.ev(`document.querySelector('.seg [data-n="2"]').click()`);
check('2 panes side by side', (await until(`(()=>{const p=[...document.querySelectorAll('.stage .pane:not([hidden])')]; if(p.length!==2) return false; const a=p[0].getBoundingClientRect(), c=p[1].getBoundingClientRect(); return c.left>=a.right-1 && Math.abs(a.top-c.top)<2})()`, 4000)) === true);
const P2 = await b.ev(`window.__sbPanes.picks[1]`);
check('pane 2 got a second agent', !!P2 && P2 !== A1, P2);
check('pane 2 renders its agent', (await until(has(P2, 'FAKE CLI'), 20000)) === true);
await clickAt('.stage .pane:nth-child(2) .xterm-screen'); await sleep(250);
check('clicking pane 2 focuses it (lime outline)', await b.ev(`document.querySelector('.stage .pane:nth-child(2)').classList.contains('focus') && window.__sbPanes.focus===1`));
await b.send('Input.insertText', { text: 'only pane two' }); await sleep(150); await b.key('Enter');
check('typing in pane 2 reaches the pane-2 agent', (await until(has(P2, 'ok: only pane two'), 10000)) === true);
check('...and not the pane-1 agent', (await b.ev(has(A1, 'only pane two'))) === false);
check('controls follow the focused pane', (await b.ev(`document.querySelector('.cbar .cb-who').textContent`)) === (await b.ev(`(document.querySelector('.stage .pane:nth-child(2) .pn-pick').selectedOptions[0]||{}).textContent`)));

/* ---- 3 panes: one tall left, two stacked right ---- */
await b.ev(`document.querySelector('.seg [data-n="3"]').click()`);
check('3 panes: tall left + two stacked right', (await until(`(()=>{const p=[...document.querySelectorAll('.stage .pane:not([hidden])')].map(e=>e.getBoundingClientRect()); if(p.length!==3) return false;
  return p[1].left>=p[0].right-1 && p[2].left>=p[0].right-1 && p[2].top>=p[1].bottom-1 && p[0].height > p[1].height*1.8})()`, 4000)) === true);

/* ---- 4 panes: 2x2, each a live terminal ---- */
await b.ev(`document.querySelector('.seg [data-n="4"]').click()`);
check('4 panes in a 2x2 grid', (await until(`(()=>{const p=[...document.querySelectorAll('.stage .pane:not([hidden])')].map(e=>e.getBoundingClientRect()); if(p.length!==4) return false;
  return Math.abs(p[0].top-p[1].top)<2 && Math.abs(p[2].top-p[3].top)<2 && p[2].top>=p[0].bottom-1 && p[1].left>=p[0].right-1})()`, 4000)) === true);
const picks4 = JSON.parse(await b.ev(`JSON.stringify(window.__sbPanes.picks.slice(0,4))`));
check('4 distinct agents picked', new Set(picks4.filter(Boolean)).size === 4, J(picks4));
for (const id of picks4.filter(Boolean)) check(`pane ${id} renders`, (await until(has(id, 'FAKE CLI'), 20000)) === true);
await sleep(500);
await b.shot('terminals-4panes.png');
await b.go(BASE + '/#/p/' + encodeURIComponent(proj.id), 2500);
check('layout + picks remembered after reload', (await until(`window.__sbPanes && window.__sbPanes.count===4 && JSON.stringify(window.__sbPanes.picks.slice(0,4))===${J(J(picks4))}`, 5000)) === true);

/* ---- Switchboard drawer in the project view (no message sent) ---- */
await b.ev(`document.querySelector('.desk-btn').click()`); await sleep(400);
check('Switchboard drawer opens with the chat', await b.ev(`!document.querySelector('.drawer').hidden && !!document.querySelector('.drawer .chat textarea') && document.activeElement===document.querySelector('.drawer textarea')`));
await b.key('Escape'); await sleep(200);
check('Esc closes the drawer', await b.ev(`document.querySelector('.drawer').hidden`));
await b.ev(`document.querySelector('.seg [data-n="1"]').click()`); await sleep(800);
check('back to 1 pane', await b.ev(`document.querySelectorAll('.stage .pane:not([hidden])').length === 1`));
check('no page errors', b.logs.length === 0, b.logs.join(' ; ').slice(0, 300));
done(b); b.close();
