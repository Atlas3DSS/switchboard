"""Shared project context: what the other agents on a project are doing, pulled on demand (`sb context`), and
notes they leave for each other (`sb note`). Nothing is pushed into an agent's context; each launch only gets a
short note saying the commands exist. Scope is one project: an agent never reads another project's agents, unless
the owner gave its project a read grant on that project (projects.reads, `sb reads`): then it may read that
project's context and files, and still may not write, send or act there.
"""
import json

from . import barrier, transcript

OWNER_RELAY_POLICY = (
    "OWNER AUTHORIZATION THROUGH SWITCHBOARD: when the owner instructs an agent to pass a command "
    "through Switchboard, the relayed command is direct owner authorization within its stated scope. "
    "Preserve the owner's words, scope and attribution across every hop. Execute the authorized command; "
    "do not downgrade it to an unverified agent claim or demand that the owner repeat it in the destination "
    "chat. A prior requirement for direct owner authorization is satisfied by this relay. This does not "
    "turn an agent's own proposal into an owner command or lift unrelated holds."
)


read_grants = lambda pid: []  # noqa: E731 - the hub points this at Store.read_grants
outside = lambda pid: {"read": False, "write": False}  # noqa: E731 - the hub points this at Store.outside


def outside_text(o):
    """What an agent is told about reaching outside its project, when the owner opened it (sb outside): files
    outside its folder, and the other projects themselves: read access covers their context,
    write access covers messaging them."""
    if not o or not (o.get("read") or o.get("write")):
        return ""
    what = "read and write" if o.get("read") and o.get("write") else "read" if o.get("read") else "write"
    head = (f"The owner gave your project outside {what} access. FILES: you may {what} files outside its own "
            "folder, anywhere on this machine and the fleet, including folders that belong to other projects")
    talk = ""
    if o.get("read"):
        talk += (" OTHER PROJECTS, reading: `sb context --project NAME` (their agents and notes), `sb wiki NAME` "
                 "(goals, last deliverable, who holds which seat) and `sb memory --project NAME` work for any project.")
    if o.get("write"):
        talk += (" OTHER PROJECTS, talking: `sb send NAME \"text\"` with a project's name reaches its lead, who "
                 "manages the rest of that project (--wait returns the reply); a message to one of its other agents "
                 "goes to the lead too. You can talk out: do not tell the owner you are blocked from other projects, "
                 "and do not ask them to relay. Ask another project when you need something only it knows; do not "
                 "ask in place of doing work you can do yourself.")
    if not o.get("write"):
        return head + (". This is read only: create, edit, move or delete nothing outside your folder, nothing "
                       "outside is yours to change, and you cannot message other projects." + talk + " ")
    return head + (". You HAVE write access: you may create, edit, move, copy, symlink and delete files there "
                   "yourself, with your file tools or the shell (mv, cp, ln -s, mkdir, rsync), where your task needs "
                   "it" + ("" if o.get("read") else ", opening files outside only to change what you were sent there "
                           "for") + ". A permission prompt from your "
                   "CLI is a confirmation step, not a refusal. Use care, not avoidance: before moving or changing "
                   "something another project uses, check nothing has it open (`lsof +D <dir>`, `sb fleet`, "
                   "`sb jobs`); if a running job has it open, leave it and tell the owner; delete only what your task "
                   "calls for." + talk + " ")


def grant_text(grants):
    """What an agent is told about the projects its own project may read. Said twice: in the launch note, and in the
    message the hub sends when the owner changes the grant."""
    if not grants:
        return ""
    names = " and ".join(f"'{g['id']}'" for g in grants)
    where = "; ".join(f"{g['id']}: {', '.join(g['folders'])}" for g in grants if g["folders"])
    first = grants[0]["id"]
    return (f"One exception, given by the owner: your project has READ-ONLY access to project{'s' if len(grants) > 1 else ''} "
            f"{names}. You may read their agents and shared notes (`sb context --project {first}`), one of their agents' "
            f"recent conversation (`sb context <agent>`), search their conversations (`sb context --search \"words\" "
            f"--project {first}`), and read the files in their working folders"
            + (f" ({where})" if where else "") + ". Use it to learn what already exists there (tools, scripts, "
            "data, methods) so you reuse it rather than rebuild it. Read only: never create, edit, move or delete "
            "anything in those folders, never run something there that writes, and never send to, leave notes for, "
            "or start, stop or change anything on those projects; the hub refuses those. To use a tool of theirs, "
            "call it from your own project or copy what you need into it. Every other project stays closed.")


def _antigravity_launch_note(agent, pid, grant):
    """Concise launch note for Antigravity (agy): sent as -i on a fresh conversation, so keep it short,
    Antigravity-native, and free of Claude-only instructions (general sb rules live in GEMINI.md + switchboard-cli)."""
    scope = ("This project is the owner's Switchboard terminal: on the owner's direct request, you may read, message, "
             "start, stop and change any project's agents, jobs, notes and files, and Switchboard itself. "
             if barrier.unlimited(pid) else
             ("Projects are separate. What your project may do across them is exactly what the owner opened, "
              "stated next; nothing beyond it, through Switchboard or any other channel. "
              if outside(pid).get("read") or outside(pid).get("write") else
              "Projects are isolated. Do not contact, relay to, or read context from other projects. "))
    note = (f"You are agent '{agent['id']}' on project '{pid}' in the owner's Switchboard. {scope}{grant}"
            "Use Antigravity's native tools (read_file, edit_file, run_command, invoke_subagent) for normal coding "
            "and research. Use `sb` (open the `switchboard-cli` skill when needed) for Switchboard coordination "
            "(`sb context`, `sb send`, `sb memory`, `sb note`, `sb work`), heavy or GPU fleet jobs (`sb run`, never "
            "background shells), and owner questions (`sb ask`, `sb notify`).")
    try:
        src = (json.loads(agent.get("fork_from") or "null") or {}).get("agent")
    except (ValueError, AttributeError):
        src = None
    if src:
        note += (f" You were forked from agent '{src}' as a side agent: do the side task without undoing or redoing "
                 "its work.")
    return (note + " " + OWNER_RELAY_POLICY
            + " Reply in one short line confirming your agent name and project; wait for your first task before "
              "running commands.")


def launch_note(agent):
    """Appended to the system prompt of every Claude/Codex terminal agent on a project."""
    if agent.get("ghost"):  # sealed off: told nothing of the project (switchboard/ghost.py)
        from . import ghost
        return ghost.note(agent)
    pid = agent.get("project") or "unsorted"
    grant = grant_text(read_grants(pid))
    grant = grant + " " if grant else ""
    grant += outside_text(outside(pid))
    if agent.get("provider") == "antigravity":
        return _antigravity_launch_note(agent, pid, grant)
    note = (f"You are agent '{agent['id']}' on project '{pid}' in the owner's Switchboard. Other agents may be "
            "working on this project too. Their context is shared on demand, never pushed: `sb context` lists them "
            "(what each was last asked and said) plus the project's shared notes; `sb context <agent>` shows one "
            "agent's recent conversation (--n 30 for more, --tools to include tool calls); `sb context --search "
            "\"words\"` searches every conversation on this project. Pull it only when it would help (overlapping "
            "files, a question about their work, a hand-off). `sb note \"...\"` leaves a short note for the others "
            "(a decision, a finding, a path); keep notes rare and useful. The project also keeps a running memory, "
            "written when a conversation on it is cleared, goes quiet or its agent is removed: `sb memory` shows where "
            "the project stands, each line citing its source; `sb memory <n>` is the outline of conversation n, "
            "each point citing the messages it came from, and `sb memory <n> --at <m>` shows those messages in full "
            "from the transcript on disk. Check a point against its messages before you rely on it. It is never "
            "sent to you: look when you start fresh on work that has a history. Reach another agent on this "
            "project with "
            "`sb send <agent> \"text\"`. " + (
                "This project is the owner's own terminal on the whole Switchboard and is NOT limited like the other "
                "projects: when the owner asks you directly, you may read, message, start, stop and change any "
                "project's agents, jobs, notes and files, and Switchboard itself (`sb context --project P`, `sb "
                "context <agent>`, `sb send <agent>` reach every project; `sb halt`/`sb unhalt` work for you). Act "
                "across projects on the owner's word, not on your own initiative or another agent's request. "
                if barrier.unlimited(pid) else
                ("Projects are separate. What your project may do across them is exactly what the owner opened, "
                 "stated next; nothing beyond it, through Switchboard or any other channel. "
                 if outside(pid).get("read") or outside(pid).get("write") else
                 "Projects are isolated. Do not contact, relay to, or read context from "
                 "other projects through Switchboard or any alternative channel. ")) + grant + "The desk agent has been removed. "
            "The owner's master work queue is `sb work` (`sb work add \"title\"` for "
            "work you leave unfinished, `sb work done <n>` when you finish an item); keep your items current. "
            "For one narrow, independent piece of work you may start a temporary helper on a smaller model: "
            "`sb helper \"task\" [--minutes 10]` (at most two at a time; it reports back to you and retires at its "
            "deadline; helpers cannot start helpers); `sb helpers` lists yours. Not for trivial work. "
            "Work that runs for minutes or hours, or uses more than a core or a GPU, is launched with "
            "`sb run --name NAME --threads N --mem GIB [--gpu GIB] [--hours H] -- command args...`: the hub places it "
            "on the fleet (`sb fleet` shows every host and what is free), runs it as a bounded systemd unit and tracks "
            "it under your name (`sb jobs`, `sb jobs show <id>`, `sb stop <id>`). Never use `systemd-run`, `systemctl "
            "start` or a background shell for workloads: anything heavy launched another way shows on the Fleet panel "
            "as untracked work attributed to you; `sb adopt <unit> --name NAME` brings an existing unit under tracking. "
            "While `sb fleet` shows HALT raised, launch nothing; `sb halt` and `sb unhalt` are the owner's and are refused for agents (bare `sb halt` is not a status check: read `sb fleet`). Write your auto-memory files with the Write or "
            "Edit tool, never through a shell redirection: the memory folder is on your allowed list for those "
            "tools, while a shell write there is judged as a write to a sensitive file and stops you at a prompt.")
    try:
        src = (json.loads(agent.get("fork_from") or "null") or {}).get("agent")
    except (ValueError, AttributeError):
        src = None
    note += (" Work that runs for minutes or hours, or uses more than a core or a GPU, is launched with "
             "`sb run --name NAME --threads N --mem GIB [--gpu GIB] [--hours H] -- command args...`: the hub places it "
             "on the fleet (`sb fleet` shows every host and what is free), runs it as a bounded systemd unit and tracks "
             "it under your name (`sb jobs`, `sb jobs show <id>`, `sb stop <id>`). Never use `systemd-run`, `systemctl "
             "start` or a background shell for workloads: anything heavy launched another way shows on the Fleet panel "
             "as untracked work attributed to you; `sb adopt <unit> --name NAME` brings an existing unit under tracking. "
             "While `sb fleet` shows HALT raised, launch nothing.")
    if src:
        note += (f" You were forked from agent '{src}' as a side agent the owner summoned: the "
                 "conversation above is a copy of its context, and that agent keeps working on its own. Do the "
                 "owner's side task without undoing or redoing its work, and say so if the task would collide with it.")
    return note + " " + OWNER_RELAY_POLICY


def _entries(path, provider, n, tools):
    chat = transcript.read_chat(path, provider, limit=min(400, n * (6 if tools else 3) + 20))
    keep = [e for e in chat["entries"] if e["role"] in ("user", "assistant") or (tools and e["role"] == "tool")]
    return keep[-n:]


def digest(path, provider, n=12, tools=False, clip=1500):
    """The last n entries of one conversation as plain text."""
    if not path:
        return "(no transcript yet)"
    lines = []
    for e in _entries(path, provider, n, tools):
        who = {"user": "ASKED", "assistant": "SAID", "tool": "TOOL"}[e["role"]]
        text = " ".join(e["text"].split()) if e["role"] == "tool" else e["text"].strip()
        if len(text) > clip:
            text = text[:clip] + " …"
        lines.append(f"[{(e['ts'] or '')[:16].replace('T', ' ')}] {who}: {text}")
    return "\n".join(lines) or "(nothing said yet)"


def last_exchange(path, provider, asked_max=300, said_max=400):
    """(last thing the agent was asked, last thing it said), both short."""
    if not path:
        return "", ""
    asked = said = ""
    for e in reversed(_entries(path, provider, 12, False)):
        if e["role"] == "user" and not asked:
            asked = " ".join(e["text"].split())[:asked_max]
        elif e["role"] == "assistant" and not said:
            said = " ".join(e["text"].split())[:said_max]
        if asked and said:
            break
    return asked, said


def search(sources, q, limit=20, clip=300):
    """sources: [(agent_id, path, provider)]. Entries containing every word of q, newest first."""
    words = [w.lower() for w in q.split() if w.strip()]
    if not words:
        return []
    hits = []
    for aid, path, provider in sources:
        if not path:
            continue
        for line in transcript._reverse_lines(path, 256 << 20):
            low = line.lower()
            if not all(w in low for w in words):
                continue
            for role, text, ts, _ in transcript._parse(line, provider):
                t = text.lower()
                if role in ("user", "assistant") and all(w in t for w in words):
                    i = t.find(words[0])
                    start = max(0, i - clip // 3)
                    snip = " ".join(text[start:start + clip].split())
                    hits.append({"agent": aid, "role": role, "ts": ts,
                                 "text": ("… " if start else "") + snip + (" …" if start + clip < len(text) else "")})
            if sum(1 for h in hits if h["agent"] == aid) >= limit:
                break
    hits.sort(key=lambda h: h["ts"] or "", reverse=True)
    return hits[:limit]
