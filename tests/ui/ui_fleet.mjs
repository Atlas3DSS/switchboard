// Fleet panel, HALT banner, jobs and untracked lists on the overview (docs/FLEET.md). Needs a test-mode hub on :8899
// with SB_HOSTS naming a local host, a "twin" (fake ssh to this machine) and an unreachable "ghost" (tests/fake_ssh.py):
//   SB_BASE=http://127.0.0.1:8899 node tests/ui/ui_fleet.mjs
// Raises and lowers a real HALT in that hub's SB_STATE and runs one sleep job through sb run; never point it at :8802.
import { launch, reporter, sleep } from './lib.mjs';
const BASE = process.env.SB_BASE || 'http://127.0.0.1:8899';
const b = await launch(); const { check, done } = reporter();
const J = (s) => JSON.stringify(s);
const get = async (p) => (await fetch(BASE + p)).json();
const post = async (p, body) => (await fetch(BASE + p, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) })).json();
const hostNames = () => b.ev(`[...document.querySelectorAll('.fleet .host .hname')].map(e=>e.textContent)`);

await b.view(1888, 1000);
await b.go(BASE + '/', 3000);
await b.ev(`window.confirm = () => true`);
const ov = await get('/api/overview');
const hosts = ov.fleet.hosts;
check('one row per host', J(await hostNames()) === J(hosts.map((x) => x.name)), J(await hostNames()));
check('header counts hosts and unreachable', (await b.ev(`document.querySelector('.fleet h2 .n').textContent`)) === `${hosts.length} hosts · ${hosts.filter((x) => !x.reachable).length} unreachable`);
const ghost = await b.ev(`(()=>{const r=[...document.querySelectorAll('.fleet .host')].find(x=>x.querySelector('.hname').textContent==='ghost'); return {off:r.classList.contains('off'), note:r.querySelector('.hnote').textContent, red:getComputedStyle(r.querySelector('.hmark')).backgroundColor};})()`);
check('unreachable host gets the red mark and a note', ghost.off && ghost.note.startsWith('unreachable') && ghost.red === 'rgb(209, 69, 59)', J(ghost));
const dev = await b.ev(`(()=>{const r=[...document.querySelectorAll('.fleet .host')].find(x=>x.querySelector('.hname').textContent==='homebox'); return {badge:!r.querySelector('.hbadge').hidden, xorg:r.querySelector('.hxorg').hidden?null:r.querySelector('.hxorg').textContent, stars:[...r.querySelectorAll('.gpu.display')].map(g=>g.textContent), cpu:r.querySelectorAll('.hstat')[0].textContent};})()`);
const devH = hosts.find((x) => x.name === 'homebox');
check('desktop host shows the badge and the Xorg numbers', dev.badge && dev.xorg && dev.xorg.startsWith('Xorg ') && dev.xorg.includes('wake/s'), J(dev));
check('display GPU carries a star', dev.stars.length === devH.gpus.filter((g) => g.display).length && dev.stars.every((s) => s.startsWith('★ ')), J(dev.stars));
check('free threads line', new RegExp(`^\\d+ of ${devH.threads} threads free · load \\d+\\.\\d$`).test(dev.cpu), dev.cpu);  // the load moves between the API read and the page's poll
const twinX = await b.ev(`[...document.querySelectorAll('.fleet .host')].find(x=>x.querySelector('.hname').textContent==='twin').querySelector('.hxorg').hidden`);
check('non-desktop host hides Xorg', twinX === true);
await sleep(3000);  // the detail fetch (/api/fleet) fills jobs and untracked
const fv = await get('/api/fleet');
const nUn = fv.hosts.reduce((n, x) => n + (x.untracked || []).length, 0);
const unRows = await b.ev(`document.querySelectorAll('.fleet .untracked').length`);
check('untracked list has one row per heavy process', unRows === nUn, `${unRows} vs ${nUn}`);
check('untracked rows have Adopt and Stop', nUn === 0 || await b.ev(`[...document.querySelectorAll('.fleet .untracked')].every(r=>[...r.querySelectorAll('.pbtn')].map(x=>x.textContent).join()==='Adopt,Stop')`));
check('no HALT banner while clear', await b.ev(`document.getElementById('halt-banner').hidden`) === true);
await b.shot('fleet-1888.png');

/* a tracked job through sb run, then Stop from the panel */
const job = await post('/api/jobs', { name: 'ui-sleep', project: 'switchboard', agent: 'ui-test', threads: 1, mem_mib: 256, hours: 1, argv: ['sleep', '600'] });
await sleep(4000);
const jrow = await b.ev(`(()=>{const r=[...document.querySelectorAll('.fleet .job')].find(x=>x.querySelector('.jname').textContent==='ui-sleep'); return r && {state:[...r.classList].find(c=>c.startsWith('s-')), who:r.querySelector('.jwho').textContent, where:r.querySelector('.jwhere').textContent, stop:!r.querySelector('.jstop').hidden};})()`);
check('tracked job appears with owner, host and Stop', jrow && /^(switchboard|Switchboard) \/ @ui-test$/.test(jrow.who) && jrow.stop && (jrow.state === 's-running' || jrow.state === 's-queued'), J({ jrow, job: job.state, reason: job.reason }));
await b.shot('fleet-job-1888.png');
await b.ev(`[...document.querySelectorAll('.fleet .job')].find(x=>x.querySelector('.jname').textContent==='ui-sleep').querySelector('.jstop').click()`);
await sleep(3500);
const j2 = await get('/api/jobs/' + job.id);
check('Stop stops the job', j2.state === 'stopped', J({ state: j2.state, reason: j2.reason }));
check('stopped job leaves the list', !(await b.ev(`[...document.querySelectorAll('.fleet .job .jname')].map(e=>e.textContent)`)).includes('ui-sleep'));

/* a queued job no host has taken yet is on no host's list; the panel still shows it and stops it */
const qjob = await post('/api/jobs', { name: 'ui-queued', project: 'switchboard', agent: 'ui-test', threads: 4000, mem_mib: 256, hours: 1, argv: ['true'] });
await sleep(4000);
const qrow = await b.ev(`(()=>{const r=[...document.querySelectorAll('.fleet .job')].find(x=>x.querySelector('.jname').textContent==='ui-queued'); return r && {state:[...r.classList].find(c=>c.startsWith('s-')), where:r.querySelector('.jwhere').textContent, stop:!r.querySelector('.jstop').hidden};})()`);
check('unplaced queued job appears with Stop', qrow && qrow.state === 's-queued' && qrow.where.startsWith('queued') && qrow.stop, J({ qrow, job: qjob.state }));
await b.ev(`window.__asked = ''; window.confirm = (m) => { window.__asked = m; return true; }`);
await b.ev(`[...document.querySelectorAll('.fleet .job')].find(x=>x.querySelector('.jname').textContent==='ui-queued').querySelector('.jstop').click()`);
await sleep(3500);
check('the confirm names no host for it', (await b.ev(`window.__asked`)).includes('still queued') && !(await b.ev(`window.__asked`)).includes('undefined') && !(await b.ev(`window.__asked`)).includes('null'), await b.ev(`window.__asked`));
check('Stop stops the queued job', (await get('/api/jobs/' + qjob.id)).state === 'stopped');
await sleep(6000);  // the panel refetches its detail at most every 2.5 s, on the next poll
check('it leaves the list', !(await b.ev(`[...document.querySelectorAll('.fleet .job .jname')].map(e=>e.textContent)`)).includes('ui-queued'));
await b.ev(`window.confirm = () => true`);

/* HALT from the panel form, banner, Unhalt */
await b.ev(`document.querySelector('.fl-halt').click()`);
await b.ev(`(()=>{const f=document.querySelector('.fl-haltform'); f.querySelector('input').value='ui test halt'; f.requestSubmit();})()`);
await sleep(4000);
const halted = (await get('/api/overview')).fleet.halt;
check('panel form raises HALT with the reason', halted && halted.reason === 'ui test halt' && halted.by === 'owner', J(halted));
const banner = await b.ev(`(()=>{const e=document.getElementById('halt-banner'); return {hidden:e.hidden, text:e.querySelector('.halt-text').textContent, hosts:e.querySelector('.halt-hosts').textContent, btn:e.querySelector('.halt-btn').textContent};})()`);
check('HALT banner shows reason and hosts', !banner.hidden && banner.text.includes('ui test halt') && banner.hosts.includes('homebox') && banner.btn === 'Unhalt', J(banner));
check('unreachable host is listed as pending', banner.hosts.includes('not yet on ghost'), banner.hosts);
check('Halt button disabled while halted', await b.ev(`document.querySelector('.fl-halt').disabled`) === true);
await b.shot('fleet-halt-1888.png');
await b.ev(`document.querySelector('.halt-btn').click()`);
await sleep(4000);
check('Unhalt lowers HALT', (await get('/api/overview')).fleet.halt === null);
check('banner hides again', await b.ev(`document.getElementById('halt-banner').hidden`) === true);

await b.view(390, 844, true); await sleep(800);
check('fleet fits 390px', await b.ev(`[...document.querySelectorAll('.fleet, .fleet *')].filter(e=>!e.hidden).every(e=>e.getBoundingClientRect().right<=391)`));
await b.ev(`document.querySelector('.fleet').scrollIntoView()`); await sleep(300);
await b.shot('fleet-390.png');
done(b); b.close();
