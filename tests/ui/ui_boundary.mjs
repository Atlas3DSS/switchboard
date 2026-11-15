// Project boundaries in the page: a project's panes list and show only its own agents, a link to another project's
// agent opens it on its own project, and only an unsorted agent shows the "assign to" control. Needs a test-mode hub
// with an agent "beta-agent" on project beta and "stray" on unsorted (POST /api/agents).
//   SB_BASE=http://127.0.0.1:8899 node tests/ui/ui_boundary.mjs
import { launch, reporter, sleep } from './lib.mjs';
const BASE = process.env.SB_BASE || 'http://127.0.0.1:8899';
const b = await launch(); const { check, done } = reporter();
await b.view(1888, 1000);

await b.go(BASE + '/#/p/switchboard/fake-1', 3000);
const opts = await b.ev(`[...document.querySelectorAll('.pn-pick option')].map(o=>o.value).filter(Boolean)`);
check('pane picker lists only this project', opts.length > 0 && opts.every((v) => v.startsWith('fake-')), JSON.stringify(opts));
check('no "Other projects" group', !(await b.ev(`!!document.querySelector('.pn-pick optgroup')`)));
check('no move control for a project agent', await b.ev(`(()=>{const s=document.querySelector('.cbar select[aria-label=Project]'); return !!s && s.parentElement.hidden})()`));

await b.go(BASE + '/#/p/switchboard/beta-agent', 3000);
check('another project\'s agent opens on its own project', (await b.ev('location.hash')) === '#/p/beta/beta-agent', await b.ev('location.hash'));
await b.ev(`window.__sbPanes && window.__sbPanes.pick(0, 'beta-agent')`);
await b.go(BASE + '/#/p/switchboard', 2500);
await b.ev(`window.__sbPanes && window.__sbPanes.pick(0, 'beta-agent')`);  // a stale pick (old layout, test hook)
await b.go(BASE + '/#/p/switchboard', 2500);
const shown = await b.ev(`window.__sbPanes.picks.slice(0, window.__sbPanes.count)`);
check('a stale pick of another project\'s agent is dropped', !shown.includes('beta-agent'), JSON.stringify(shown));

await b.go(BASE + '/#/p/unsorted/stray', 3000);
check('an unsorted agent can be assigned', await b.ev(`(()=>{const s=document.querySelector('.cbar select[aria-label=Project]'); return !!s && !s.parentElement.hidden})()`));
await b.shot('boundary-unsorted.png');
done(b); b.close();
