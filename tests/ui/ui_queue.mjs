// Queue UI regression: mutations use an in-page fake API; no real messages are changed.
import { launch, reporter, sleep } from './lib.mjs';
const b = await launch(); const {check, done} = reporter();
try {
  await b.go(process.env.SB_BASE || 'http://127.0.0.1:8802/', 1800);
  await b.ev(`(()=>{
    window.queueCalls=[];
    const a={id:'queue-test', title:'Queue test', paused:true, state:'idle', queued:1, account:'test', provider:'shell'};
    let ms=[{id:'test-message',to:a.id,from:'sender',body:'Full pending message\\nSecond line',state:'queued',created_at:'2026-11-01T00:00:00Z',error:'waiting: the agent is paused',attachments:[]}];
    api=async(method,path,body)=>{queueCalls.push([method,path,body]);
      if(path==='/agents/queue-test/queue') return {agent:{...a,queued:ms.length},messages:ms};
      if(path==='/messages/test-message/cancel') {ms=[];return {};}
      if(path==='/agents/queue-test') {a.paused=false;return a;}
      throw new Error('Unexpected request '+path);
    };
    const row=agentRow();row.update(a);document.body.append(row.el);row.el.id='test-queue-row';
  })()`);
  const hash = await b.ev('location.hash');
  await b.ev(`document.querySelector('#test-queue-row .q').click()`);
  check('badge opens queue without navigating', await b.ev(`!!document.querySelector('#agent-queue[open]') && location.hash===${JSON.stringify(hash)}`));
  await sleep(100);
  check('shows sender, full message, and blocking reason', await b.ev(`['From sender','Full pending message','Second line','paused'].every(t=>document.querySelector('#agent-queue').textContent.includes(t))`));
  await b.ev(`document.querySelector('#agent-queue .queue-head button').click()`);
  check('resume calls agent endpoint', await b.ev(`queueCalls.some(c=>c[0]==='PATCH'&&c[2]?.paused===false)`));
  await b.ev(`[...document.querySelectorAll('#agent-queue button')].find(e=>e.textContent==='Cancel message').click()`);
  await sleep(100);
  check('cancel removes row and shows empty state', await b.ev(`!document.querySelector('.queue-message')&&document.querySelector('#agent-queue').textContent.includes('No pending messages')`));
  await b.view(390,844,true);
  check('fits phone width', await b.ev(`document.querySelector('#agent-queue').getBoundingClientRect().right<=390`));
  await b.ev(`[...document.querySelectorAll('#agent-queue button')].find(e=>e.textContent==='Close').click()`);
  await sleep(100);
  check('close removes dialog', await b.ev(`!document.getElementById('agent-queue')`));
  done(b);
} finally { b.close(); }
