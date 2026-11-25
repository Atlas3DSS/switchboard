# How Switchboard works

A single local daemon, the **hub** (`python3 -m switchboard.hub`, `127.0.0.1:8802`), owns projects, agents, messages
and the web page. There is no build step and nothing to `pip install`: Python's standard library on the server,
plain JavaScript plus a vendored xterm.js in the browser.

```
browser ──HTTP/WebSocket──▶ hub (hub.py) ──unix socket──▶ ptyholder ──pty──▶ claude / codex / gemini / switchboard.agent
   ▲                         │  SQLite: hub.db                                (one holder per agent; survives hub restarts)
   └──────── sb (bin/sb) ────┘  transcripts: ~/.claude, ~/.codex, state/api-sessions  ◀── read to confirm delivery
```

## Pieces

| Module | Role |
|---|---|
| `hub.py` | HTTP API, the page, background loops (delivery, screen polling, reply refresh, session indexing) |
| `config.py` | your accounts (`~/.config/switchboard/accounts.json`): kinds, presets, detection, endpoint tests |
| `terminals.py` | launches each agent's CLI in a detached `ptyholder`, mirrors its screen, types `sb send` messages |
| `ptyholder.py` | tiny process that owns one pty and its scrollback; the hub reconnects to it after a restart |
| `vtscreen.py` | a small VT100 screen model: is the agent busy, is its input box empty, what is the window title |
| `agent.py` | the built-in agent for OpenAI-compatible endpoints (Claude-format transcripts, Claude-style stream-json) |
| `runner.py` | headless turns for any `mode='managed'` agent: `claude -p`, `codex exec`, `agent -p` |
| `transcript.py`, `discovery.py` | read Claude / Codex / built-in transcripts: last reply, usage, model, "did my text land" |
| `sessions.py` | full-text index of past sessions (`sessions.db`), for search and resume |
| `shared.py` | `sb context` / `sb note`: project-scoped shared context, pulled on demand |
| `sources.py` | live project sources on the cards: git log, file excerpts, recently changed files, commands, URLs |
| `store.py` | SQLite (WAL) tables: projects, agents, messages, events, notes, decisions, notifications, settings |
| `hermes.py` | Hermes Agent sessions: reads `$HERMES_HOME/state.db` (read-only) as if it were a transcript |
| `gate.py`, `phone.py` | the phone gate: one paired phone in, everything else refused (see [PHONE.md](PHONE.md)) |
| `barrier.py` | the project barrier: which routes an agent may call, and who is calling (read from the caller's process, not from what it claims) |
| `fleet.py` | broadcast and wrap-up to every running agent, the executive brief, the master work queue |
| `inbox.py` | the owner's inbox: open decisions and needs per project |
| `hosts.py`, `probe.py`, `jobs.py`, `placers.py` | the fleet: host inventory, HALT, `sb run` jobs and placement (see [FLEET.md](FLEET.md)) |
| `boardwatch.py`, `leasedecay.py` | optional, with a GPU workboard: page when a host's cards go stale, and release leases that sit idle |
| `memory.py` | project memory: an outline per ended conversation and a short running record, each line citing its source |
| `retention.py` | what the hub keeps and for how long; one sweep at start and every few hours |
| `autoclear.py` | clears a Claude, Codex or Antigravity agent's context at a natural pause once it passes its model's limit, instead of compacting |
| `handoff.py`, `accounts.py` | moves a Claude agent to another account before (or when) its account runs out; limits come from an optional usage cache |
| `helpers.py` | temporary helpers: one narrow task on a smaller model, with a deadline |
| `nodes.py` | the same seats on every project (lead, architect, tasker, janitor), the lead / illustrator / architect marks the owner pins on agents, and the project wiki |
| `insights.py`, `trace.py`, `team.py` | the Insights page, one agent's context view, a project's team view |
| `rewind.py` | owner tools on one agent's history: memory files and rewinding to before a message |
| `antigravity.py` | Google Antigravity CLI sessions, read as transcripts; its usage and quota readings |
| `stuck.py` | an Antigravity terminal at rest on a compact request or an API error: typed past, relaunched, or reported |
| `claims.py` | `sb attest` and claim checks: job ids and file paths an agent names are checked before "done" is passed on |
| `ghost.py` | ghost agents: sealed to their own task, erased when removed |
| `kill.py` | removing a project entirely: agents, conversations and hub records; files stay |
| `colab.py` | Google Colab compute units on the Antigravity account card, read from the `colab` CLI |

## Accounts and providers

An agent's `provider` is its account's `kind`:

| kind | launched as | resume | delivery confirmed by |
|---|---|---|---|
| `claude` | `claude [--resume ID / --session-id ID] --model M --append-system-prompt NOTE` | Claude session id | the session jsonl |
| `codex` | `codex [resume ID / fork ID] --model M -c developer_instructions=NOTE` | Codex thread id | the rollout jsonl |
| `gemini` | `gemini --model M [--yolo]` | — | the screen (input box clears) |
| `hermes` | `hermes chat [--resume ID] -m M [--reasoning E]`, note in `HERMES_EPHEMERAL_SYSTEM_PROMPT` | Hermes session id (found in `state.db` by folder and start time) | its `messages` rows |
| `api` | `python3 -m switchboard.agent [--resume ID] --model M --append-system-prompt NOTE` | its own session id | its jsonl (Claude format) |

Account settings reach the CLI as environment: `CLAUDE_CONFIG_DIR` / `ANTHROPIC_API_KEY`, `CODEX_HOME` (an
API-key Codex account gets its own home, logged in with `codex login --with-api-key`), `GEMINI_API_KEY`,
`HERMES_HOME`, and `SB_API_BASE` / `SB_API_KEY` / `SB_API_MODEL` for the built-in agent. Several logins of the same CLI are several
accounts with different config dirs; the control bar's account switch exits the CLI and resumes the same session
under the other account.

## Delivering a message exactly once

1. `POST /api/messages {to, body, idem}` enqueues it. The same idempotency key, or the same sender + body within
   10 minutes, returns the existing message instead of a second one. Hop limit: 6 agent-to-agent hops.
2. The terminal loop waits until the agent's screen shows it **idle** with an **empty input box** (for the
   built-in agent: window title `sb:idle`), then types the text once as a bracketed paste plus Enter. Background
   shells or monitors under an idle input box don't block a message, but stopping, relaunching or switching the
   agent's account asks first, since exiting the CLI would end them.
3. The message is `delivered` when the text appears as a user turn in the session transcript, and `done` when a
   reply follows. Nothing is ever retyped; a message not seen within 3 minutes is marked `failed`.
4. A reply to an agent's message is routed back to the sender once (`Reply from …`); replies never trigger replies.

## HTTP API (JSON)

```
GET  /api/overview                     projects, agents, accounts (no keys), open decisions, code version
GET  /api/setup                        accounts (keys masked), presets, detected CLIs and env keys
POST /api/setup {accounts, workspace}
POST /api/setup/test {account}         list models + one tiny chat completion
GET  /api/projects/<id>                project, agents, sources, recent messages, decisions
POST /api/projects {name, ...}         PATCH /api/projects/<id> {name, goal, next_step, lead, archived, paused, ...}
POST /api/agents {project, account, model?, cwd?, title?, session_id?, prompt?}
PATCH /api/agents/<id> {project?, title?, model?, ...}       DELETE /api/agents/<id>
POST /api/agents/<id>/start|stop|account|interrupt|clear
POST /api/agents/<id>/relaunch {model?, effort?, confirm?}   exit the CLI, start it again on the same session
GET  /api/agents/<id>/chat?limit=N     conversation read from the transcript; user turns carry `from` (which agent wrote it)
GET  /api/subagents?model=&hours=&agent=   recent Claude subagents and the agent or session that spawned each
GET  /api/phone   POST /api/phone/pair {origin?} | /api/phone/unpair      (never through the gate)
WS   /ws/agents/<id>                   the terminal: output bytes out; {"t":"i","d":keys} / {"t":"r",cols,rows} in
POST /api/messages {to, body, from?, idem?, wait?}            GET /api/messages?agent=&state=&limit=
POST /api/decisions {project, agent, question, options}       POST /api/decisions/<id>/answer {answer}
GET  /api/notifications                POST /api/notifications {agent, text, level}   POST …/<id>/ack, …/ack-all
GET  /api/sessions?q=words             POST /api/sessions/resume {session_id}
GET  /api/context?…  /api/projects/<id>/context  /api/agents/<id>/context   POST /api/notes {text}
POST /api/summon {beside, task, fork?}  POST /api/dispatch {project, task, idem?}   POST /api/restart
GET  /api/work   POST /api/work {title, project?, priority?, agent?}   PATCH|DELETE /api/work/<n>
GET  /api/inbox?project=               POST /api/inbox/<id>/done {answer?}
POST /api/broadcast {body, project?}   GET /api/brief?hours=24
GET  /api/fleet   /api/jobs            POST /api/jobs {name, argv, threads, mem_gib, ...}   POST /api/jobs/<id>/stop
GET  /api/memory?project=              GET /api/memory/<n>     POST /api/helpers {task, minutes?, model?}
GET  /api/insights?range=7d&project=   POST /api/projects/<id>/outside {read?, write?}   …/reads {reads}
```

Agents may call only the routes listed in `barrier.AGENT_ROUTES`, and only for their own project; everything else is
the owner's (the dashboard, a Switchboard shell pane, your own terminal, or the paired phone).


Errors are `{error: "..."}` with 4xx/5xx. Writes must be JSON (`Content-Type: application/json`); requests from
another site's page (Origin) or a host name other than `127.0.0.1` / `localhost` (DNS rebinding) get 403.
