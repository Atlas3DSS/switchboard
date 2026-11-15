// Project view "Limits" panel: one card per agent with its account's limits; collapsible and remembered.
import { launch, sleep, reporter } from './lib.mjs';
const BASE = process.env.SB_BASE || 'http://127.0.0.1:8899';
const b = await launch(); const { check, done } = reporter();
await b.view(1888, 954);
await b.go(BASE + '/#/p/switchboard', 3500);
const cards = await b.ev(`[...document.querySelectorAll('.al')].map((c) => c.innerText.replace(/\\s+/g, ' ').trim())`);
check('a limits card per agent', cards.length >= 2, String(cards.length));
check('cards show account limits as "% used · resets"', cards.some((t) => /Weekly: \d+% used/.test(t)), (cards[0] || '').slice(0, 160));
await b.shot('limits-open.png');
await b.ev(`[...document.querySelectorAll('button')].find((e) => e.textContent.trim() === 'Limits').click()`);
await sleep(300);
check('Limits button collapses the panel', await b.ev(`document.querySelector('.al-list').closest('section').hidden === true`));
await b.go(BASE + '/#/p/switchboard', 2500);
check('collapsed state is remembered', await b.ev(`document.querySelector('.al-list').closest('section').hidden === true`));
await b.ev(`[...document.querySelectorAll('button')].find((e) => e.textContent.trim() === 'Limits').click()`);
check('no page errors', b.logs.length === 0, b.logs.join(' | '));
done(b); b.close();
