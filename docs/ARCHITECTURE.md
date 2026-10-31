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
| `runner.py` | headless turns for the Switchboard chat (and any `mode='managed'` agent): `claude -p`, `codex exec`, `agent -p` |
| `transcript.py`, `discovery.py` | read Claude / Codex / built-in transcripts: last reply, usage, model, "did my text land" |
| `sessions.py` | full-text index of past sessions (`sessions.db`), for search and resume |
| `shared.py` | `sb context` / `sb note`: project-scoped shared context, pulled on demand |
| `sources.py` | live project sources on the cards: git log, file excerpts, recently changed files, commands, URLs |
| `store.py` | SQLite (WAL) tables: projects, agents, messages, events, notes, decisions, notifications, settings |

## Accounts and providers

An agent's `provider` is its account's `kind`:

| kind | launched as | resume | delivery confirmed by |
|---|---|---|---|
| `claude` | `claude [--resume ID / --session-id ID] --model M --append-system-prompt NOTE` | Claude session id | the session jsonl |
| `codex` | `codex [resume ID / fork ID] --model M -c developer_instructions=NOTE` | Codex thread id | the rollout jsonl |
| `gemini` | `gemini --model M [--yolo]` | — | the screen (input box clears) |
| `api` | `python3 -m switchboard.agent [--resume ID] --model M --append-system-prompt NOTE` | its own session id | its jsonl (Claude format) |

Account settings reach the CLI as environment: `CLAUDE_CONFIG_DIR` / `ANTHROPIC_API_KEY`, `CODEX_HOME` (an
API-key Codex account gets its own home, logged in with `codex login --with-api-key`), `GEMINI_API_KEY`, and
`SB_API_BASE` / `SB_API_KEY` / `SB_API_MODEL` for the built-in agent. Several logins of the same CLI are several
accounts with different config dirs; the control bar's account switch exits the CLI and resumes the same session
under the other account.

## Delivering a message exactly once

1. `POST /api/messages {to, body, idem}` enqueues it. The same idempotency key, or the same sender + body within
   10 minutes, returns the existing message instead of a second one. Hop limit: 6 agent-to-agent hops.
2. The terminal loop waits until the agent's screen shows it **idle** with an **empty input box** (for the
   built-in agent: window title `sb:idle`), then types the text once as a bracketed paste plus Enter.
3. The message is `delivered` when the text appears as a user turn in the session transcript, and `done` when a
   reply follows. Nothing is ever retyped; a message not seen within 3 minutes is marked `failed`.
4. A reply to an agent's message is routed back to the sender once (`Reply from …`); replies never trigger replies.

## HTTP API (JSON)

```
GET  /api/overview                     projects, agents, accounts (no keys), open decisions, code version
GET  /api/setup                        accounts (keys masked), presets, detected CLIs and env keys
POST /api/setup {accounts, workspace, router_account}
POST /api/setup/test {account}         list models + one tiny chat completion
GET  /api/projects/<id>                project, agents, sources, recent messages, decisions
POST /api/projects {name, ...}         PATCH /api/projects/<id> {name, goal, next_step, lead, archived, paused, ...}
POST /api/agents {project, account, model?, cwd?, title?, session_id?, prompt?}
PATCH /api/agents/<id> {project?, title?, model?, ...}       DELETE /api/agents/<id>
POST /api/agents/<id>/start|stop|account|interrupt|clear
GET  /api/agents/<id>/chat?limit=N     conversation read from the transcript
WS   /ws/agents/<id>                   the terminal: output bytes out; {"t":"i","d":keys} / {"t":"r",cols,rows} in
POST /api/messages {to, body, from?, idem?, wait?}            GET /api/messages?agent=&state=&limit=
POST /api/decisions {project, agent, question, options}       POST /api/decisions/<id>/answer {answer}
GET  /api/notifications                POST /api/notifications {agent, text, level}   POST …/<id>/ack, …/ack-all
GET  /api/sessions?q=words             POST /api/sessions/resume {session_id}
GET  /api/context?…  /api/projects/<id>/context  /api/agents/<id>/context   POST /api/notes {text}
POST /api/summon {beside, task, fork?}  POST /api/dispatch {project, task, idem?}   POST /api/restart
```

Errors are `{error: "..."}` with 4xx/5xx. Writes must be JSON (`Content-Type: application/json`); requests from
another site's page (Origin) or a host name other than `127.0.0.1` / `localhost` (DNS rebinding) get 403.
