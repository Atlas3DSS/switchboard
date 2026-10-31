"""Shared project context: what the other agents on a project are doing, pulled on demand (`sb context`), and
notes they leave for each other (`sb note`). Nothing is pushed into an agent's context; each launch only gets a
short note saying the commands exist. Scope is one project: an agent never reads another project's agents.
"""
import json

from . import transcript


def launch_note(agent):
    """Appended to the system prompt of every Claude/Codex terminal agent on a project."""
    pid = agent.get("project") or "unsorted"
    note = (f"You are agent '{agent['id']}' on project '{pid}' in the owner's Switchboard. Other agents may be "
            "working on this project too. Their context is shared on demand, never pushed: `sb context` lists them "
            "(what each was last asked and said) plus the project's shared notes; `sb context <agent>` shows one "
            "agent's recent conversation (--n 30 for more, --tools to include tool calls); `sb context --search "
            "\"words\"` searches every conversation on this project. Pull it only when it would help (overlapping "
            "files, a question about their work, a hand-off). `sb note \"...\"` leaves a short note for the others "
            "(a decision, a finding, a path); keep notes rare and useful. Reach another agent directly with "
            "`sb send <agent> \"text\"`.")
    try:
        src = (json.loads(agent.get("fork_from") or "null") or {}).get("agent")
    except (ValueError, AttributeError):
        src = None
    if src:
        note += (f" You were forked from agent '{src}' as a side agent the owner summoned: the "
                 "conversation above is a copy of its context, and that agent keeps working on its own. Do the "
                 "owner's side task without undoing or redoing its work, and say so if the task would collide with it.")
    return note


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


def last_exchange(path, provider):
    """(last thing the agent was asked, last thing it said), both short."""
    if not path:
        return "", ""
    asked = said = ""
    for e in reversed(_entries(path, provider, 12, False)):
        if e["role"] == "user" and not asked:
            asked = " ".join(e["text"].split())[:300]
        elif e["role"] == "assistant" and not said:
            said = " ".join(e["text"].split())[:400]
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
