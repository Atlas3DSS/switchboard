// Headless-Chromium driver (CDP over WebSocket, no dependencies) for the ui_*.mjs checks against a test-mode hub.
import { spawn, execSync } from 'node:child_process';
import fs from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
const HERE = path.dirname(fileURLToPath(import.meta.url));
const HOME = process.env.HOME;
/* Any Chromium works: set CHROME=/path/to/chrome, or install one with `npx playwright install chromium`. */
function findChrome() {
  if (process.env.CHROME) return process.env.CHROME;
  const pw = path.join(HOME, '.cache', 'ms-playwright');
  const dirs = fs.existsSync(pw) ? fs.readdirSync(pw).filter((d) => d.startsWith('chromium')).sort().reverse() : [];
  for (const d of dirs) {
    for (const rel of ['chrome-headless-shell-linux64/chrome-headless-shell', 'chrome-linux64/chrome', 'chrome-linux/chrome']) {
      const p = path.join(pw, d, rel);
      if (fs.existsSync(p)) return p;
    }
  }
  for (const n of ['chromium', 'chromium-browser', 'google-chrome']) {
    try { return execSync(`command -v ${n}`).toString().trim(); } catch (e) { /* next */ }
  }
  throw new Error('no Chromium found: set CHROME=/path/to/chrome');
}
const CHROME = findChrome();
export const OUT = path.join(HERE, 'out');
export const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

export async function launch(port = 9400 + Math.floor(Math.random() * 400)) {
  const proc = spawn(CHROME, ['--headless=new', '--no-sandbox', '--disable-gpu', `--remote-debugging-port=${port}`, '--window-size=1400,1000', 'about:blank'], { stdio: 'ignore' });
  let targets;
  for (let i = 0; i < 40; i++) { try { targets = await (await fetch(`http://127.0.0.1:${port}/json`)).json(); if (targets.length) break; } catch { /* retry */ } await sleep(200); }
  const ws = new WebSocket(targets.find((t) => t.type === 'page').webSocketDebuggerUrl);
  await new Promise((r) => (ws.onopen = r));
  let id = 0; const pend = new Map(); const logs = [];
  ws.onmessage = (m) => { const d = JSON.parse(m.data); if (d.id && pend.has(d.id)) { pend.get(d.id)(d); pend.delete(d.id); } else if (d.method === 'Runtime.exceptionThrown') logs.push('EXC ' + JSON.stringify(d.params.exceptionDetails.exception?.description || d.params.exceptionDetails.text)); else if (d.method === 'Runtime.consoleAPICalled' && d.params.type === 'error') logs.push('CONSOLE ' + d.params.args.map((a) => a.value || a.description).join(' ')); };
  const send = (method, params = {}) => new Promise((r) => { const i = ++id; pend.set(i, r); ws.send(JSON.stringify({ id: i, method, params })); });
  const ev = async (expr) => { const r = await send('Runtime.evaluate', { expression: expr, awaitPromise: true, returnByValue: true }); if (r.result.exceptionDetails) return 'ERR ' + JSON.stringify(r.result.exceptionDetails.exception?.description); return r.result.result.value; };
  await send('Runtime.enable'); await send('Page.enable');
  const view = async (w, h, mobile = false, dark = false) => {
    await send('Emulation.setDeviceMetricsOverride', { width: w, height: h, deviceScaleFactor: 1, mobile });
    await send('Emulation.setEmulatedMedia', { features: [{ name: 'prefers-color-scheme', value: dark ? 'dark' : 'light' }] });
  };
  const shot = async (name) => { const r = await send('Page.captureScreenshot', { format: 'png' }); fs.mkdirSync(OUT, { recursive: true }); fs.writeFileSync(path.join(OUT, name), Buffer.from(r.result.data, 'base64')); };
  const go = async (url, wait = 1800) => { await send('Page.navigate', { url }); await sleep(wait); };
  const key = (k, mods = 0) => send('Input.dispatchKeyEvent', { type: 'keyDown', key: k, code: k, modifiers: mods, windowsVirtualKeyCode: k === 'Enter' ? 13 : 27, text: k === 'Enter' && !(mods & 2) ? '\r' : undefined })
    .then(() => send('Input.dispatchKeyEvent', { type: 'keyUp', key: k, code: k, modifiers: mods, windowsVirtualKeyCode: k === 'Enter' ? 13 : 27 }));
  return { send, ev, view, shot, go, key, logs, close() { try { ws.close(); } catch (e) { /* ignore */ } proc.kill(); } };
}
export function reporter() {
  const R = [];
  return { check: (n, ok, x = '') => R.push(`${ok ? 'PASS' : 'FAIL'} ${n} ${x}`), done(b) { console.log(R.join('\n')); console.log('page errors:', b.logs.length ? b.logs.join('\n') : 'none'); const f = R.filter((l) => l.startsWith('FAIL')).length; console.log(f ? `${f} FAILED` : 'ALL PASSED'); process.exitCode = f ? 1 : 0; } };
}
