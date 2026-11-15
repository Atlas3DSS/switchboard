"""Owner-only tools on one agent's history (none of these routes is in barrier.AGENT_ROUTES):

  memory files   the auto-memory files in a project's memory folders, and which agent wrote or edited each one
                 (read from the agents' transcripts: Write/Edit/MultiEdit calls on a memory folder), plus the
                 Switchboard outlines of the project's conversations; the owner edits or deletes them in the app
  rewind         put a Claude agent back to the moment just before one of its messages, as if nothing after it
                 happened (erase()): the CLI exits, the transcript is cut, later sessions and Claude's state for
                 them go, and so does the hub's record of that part. The same session resumes from there. It can go
                 back into an earlier session (one an auto-clear or a /clear left behind). Only real work stays:
                 files on disk, jobs, the work queue. A private copy is kept only when the owner ticks it.
"""
import json
import os
import re
import shutil
import threading
import time

from . import discovery, memory, transcript
from .store import STATE, epoch, iso_from_epoch, now
from .terminals import NeedsConfirm, memory_dir

REWINDS = os.path.join(STATE, "rewinds")
TEXT_CAP = 400
FILE_CAP = 200_000
WRITERS = ("Write", "Edit", "MultiEdit", "NotebookEdit")
_SID = re.compile(r"session ([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})")


# ---------- an agent's sessions ----------
def sessions(hub, a):
    """[session id] this agent has been on, current first, then newest to oldest."""
    seen = [a["session_id"]] if a.get("session_id") else []
    for r in hub.s.q("SELECT session_id FROM memory_sessions WHERE agent=? ORDER BY id DESC", (a["id"],)):
        seen.append(r["session_id"])
    for r in hub.s.q("SELECT text FROM events WHERE agent=? AND kind='system' AND text LIKE '%session %' "
                     "ORDER BY id DESC LIMIT 400", (a["id"],)):
        seen += _SID.findall(r["text"] or "")
    return [x for x in dict.fromkeys(seen) if x]


def _lines(path):
    """(offset, raw line) of each complete line."""
    off = 0
    with open(path, "rb") as f:
        for line in f:
            if not line.endswith(b"\n"):
                break
            yield off, line
            off += len(line)


def _moments_of(path):
    out = []
    for off, line in _lines(path):
        if b'"type":"user"' not in line and b'"type": "user"' not in line:
            continue
        try:
            e = json.loads(line)
        except ValueError:
            continue
        if e.get("type") != "user":
            continue
        got = transcript._claude_entries(e)
        if got and got[0][0] == "user":
            out.append({"offset": off, "ts": e.get("timestamp"), "text": got[0][1][:TEXT_CAP]})
    return out


def moments(hub, aid):
    a = hub.s.agent(aid)
    if not a:
        raise LookupError(aid)
    if a["provider"] != "claude":
        return {"unsupported": "rewind works on Claude agents"}
    out = []
    for sid in sessions(hub, a):
        path = discovery.transcript_path("claude", sid, a.get("account"))
        if not path or not os.path.exists(path):
            continue
        ms = _moments_of(path)
        size = os.path.getsize(path)
        for i, m in enumerate(ms):
            m["later"] = len(ms) - i - 1  # owner messages cut with it
            m["cut_bytes"] = size - m["offset"]
        out.append({"session": sid, "current": sid == a.get("session_id"), "size": size,
                    "mtime": os.path.getmtime(path), "moments": ms[::-1]})
    backups = sorted(os.listdir(os.path.join(REWINDS, aid)), reverse=True) if os.path.isdir(
        os.path.join(REWINDS, aid)) else []
    return {"agent": {"id": aid, "title": a.get("title") or aid, "project": a["project"], "mode": a["mode"]},
            "sessions": out, "backups": [os.path.join(REWINDS, aid, b) for b in backups[:20]]}


def rewind(hub, aid, b):
    """Cut the agent's session just before the message at b['offset'] of session b['session'], and resume it."""
    a = hub.s.agent(aid)
    if not a:
        raise LookupError(aid)
    if a["provider"] != "claude":
        raise ValueError("rewind works on Claude agents")
    if a["mode"] not in ("terminal", "managed"):
        raise ValueError("this agent runs in a tmux pane the switchboard does not own; adopt it first")
    sid, off = b.get("session") or a.get("session_id"), int(b.get("offset", -1))
    if sid not in sessions(hub, a):
        raise ValueError(f"session {sid} is not one of {aid}'s")
    path = discovery.transcript_path("claude", sid, a.get("account"))
    if not path or not os.path.exists(path):
        raise LookupError(f"the transcript of session {sid}")
    hit = next((m for m in _moments_of(path) if m["offset"] == off), None)
    if not hit:
        raise ValueError("that moment is not the start of one of the agent's messages (the transcript changed?)")
    t = hub.t
    running = a["mode"] == "terminal" and t.running(aid)
    if running and t.busy(aid) and not b.get("confirm"):
        raise NeedsConfirm("the agent is working (or has unsent text or background jobs, which a rewind ends); "
                           "interrupt it and rewind?")

    order = sessions(hub, a)
    later = [x for x in order[:order.index(sid)]  # sessions the agent moved on to after this one
             if not hub.s.one("SELECT 1 n FROM agents WHERE session_id=? AND id<>?", (x, aid))]

    def cut():
        keep = None
        if b.get("keep_copy"):
            os.makedirs(os.path.join(REWINDS, aid), mode=0o700, exist_ok=True)
            keep = os.path.join(REWINDS, aid, f"{time.strftime('%Y%m%d-%H%M%S')}-{sid}.jsonl")
            shutil.copy2(path, keep)
            os.chmod(keep, 0o600)
        erase(hub, a, sid, path, off, hit["ts"], later)
        hub.s.event(aid, "system", f"rewound by the owner to {hit['ts']}" + (f"; a copy is at {keep}" if keep else ""))
        return keep

    if not running:
        keep = cut()
        return {"ok": True, "backup": keep, "message": hit["text"], "relaunch": False}

    t.switching[aid] = {"to": "rewind", "note": "exiting the CLI to rewind"}

    def run():
        try:
            if not t.exit_cli(aid, interrupt=True):
                t.switching[aid]["note"] = "the CLI did not exit; rewind abandoned"
                hub.s.event(aid, "error", "rewind abandoned: the CLI did not exit; nothing was cut")
                time.sleep(10)
                return
            cut()
            t.switching[aid]["note"] = "relaunching at the rewound moment"
            time.sleep(0.5)
            t.launch(hub.s.agent(aid), during_switch=True)
        except Exception as e:
            hub.s.event(aid, "error", f"rewind failed: {e}")
        finally:
            t.switching.pop(aid, None)

    threading.Thread(target=run, daemon=True).start()
    return {"ok": True, "message": hit["text"], "relaunch": True}


def _rm(p):
    try:
        if os.path.isdir(p) and not os.path.islink(p):
            shutil.rmtree(p)
        elif os.path.lexists(p):
            os.remove(p)
    except OSError:
        pass


def _first_ts(p):
    try:
        with open(p, "rb") as f:
            for line in f:
                ts = (json.loads(line) or {}).get("timestamp")
                if ts:
                    return epoch(ts)
    except (OSError, ValueError):
        pass
    return os.path.getmtime(p) if os.path.exists(p) else 0


def _session_files(path, sid, since=None):
    """Claude's own per-session state next to a transcript: subagent transcripts and stored tool results under
    <project>/<sid>/, file-history/<sid>/ and session-env/<sid>/ in the config dir. With `since`, only what was
    written from then on."""
    cfg = os.path.dirname(os.path.dirname(os.path.dirname(os.path.realpath(path))))
    roots = [os.path.join(os.path.dirname(path), sid), os.path.join(cfg, "file-history", sid),
             os.path.join(cfg, "session-env", sid)]
    if since is None:
        return [r for r in roots if os.path.lexists(r)]
    out = []
    for r in roots:
        for d, _, fs in os.walk(r):
            for n in fs:
                p = os.path.join(d, n)
                t = _first_ts(p) if p.endswith(".jsonl") else os.path.getmtime(p)
                if t >= since:
                    out.append(p)
    return out


def scrub_outlines(s, pid, ids):
    """Take outlines out of a project's memory, as if they were never written: their [#id] sources go from every
    line (current text and kept versions), and a line no other source backs goes with them."""
    gone = {f"#{i}" for i in ids}
    if not gone:
        return

    def fix_line(line):
        had = memory.tags_of(line)
        if not had & gone:
            return line
        if not had - gone:
            return None

        def fix(m):
            left = [t for t in re.findall(r"(?:#|n)\d+", m.group(1)) if t not in gone]
            return "[" + ", ".join(left) + "]" if left else ""
        return memory.TAGS.sub(fix, line).rstrip()

    def fix_text(text):
        return "\n".join(x for x in map(fix_line, (text or "").splitlines()) if x is not None)

    m = s.one("SELECT text FROM memory WHERE project=?", (pid,))
    if m and m["text"]:
        s.x("UPDATE memory SET text=? WHERE project=?", (fix_text(m["text"]), pid))
    for v in s.q("SELECT id, text FROM memory_versions WHERE project=?", (pid,)):
        s.x("UPDATE memory_versions SET text=? WHERE id=?", (fix_text(v["text"]), v["id"]))


def erase(hub, a, sid, path, off, ts, later=()):
    """Everything the agent did from the message at `off` (sent at `ts`) on is taken out, as if it never happened:
    the rest of that transcript and any session after it, Claude's own state for them, and the hub's record of it
    (its events, the messages it got and sent, its notes, pages and questions, the outlines of that part and the
    memory lines they alone back). Real work stays: files on disk, jobs, the work queue."""
    s, aid = hub.s, a["id"]
    since = epoch(ts) - 3  # the hub logs a typed message a moment before the CLI writes it
    cutoff = iso_from_epoch(since)
    for p in _session_files(path, sid, since):
        _rm(p)
    os.truncate(path, off)
    dead_paths = []
    for x in later:
        p = discovery.transcript_path("claude", x, a.get("account"))
        if p:
            for q in _session_files(p, x):
                _rm(q)
            _rm(p)
            dead_paths.append(p)
    ix = getattr(hub, "_ix", None)
    if ix is not None and dead_paths:
        try:
            with ix.lock, ix.db:
                for p in dead_paths:
                    r = ix.db.execute("SELECT id FROM sess WHERE path=?", (p,)).fetchone()
                    ix.db.execute("DELETE FROM sess WHERE path=?", (p,))
                    if r:
                        ix.db.execute("DELETE FROM sess_fts WHERE id=?", (r["id"],))
        except Exception:
            pass  # the index notices a vanished file on its own
    marks = ",".join("?" * len(later))
    dead = [r["id"] for r in s.q(f"SELECT id FROM memory_sessions WHERE (session_id=? AND size>?)"
                                 + (f" OR session_id IN ({marks})" if later else ""), (sid, off, *later))]
    if dead:
        s.x(f"DELETE FROM memory_sessions WHERE id IN ({','.join('?' * len(dead))})", dead)
        scrub_outlines(s, a["project"], dead)
    s.x("DELETE FROM messages WHERE to_agent=? AND (created_at>=? OR delivered_at>=?)", (aid, cutoff, cutoff))
    s.x("DELETE FROM messages WHERE from_agent=? AND created_at>=? AND state NOT IN ('delivering','delivered')",
        (aid, cutoff))
    s.x("UPDATE messages SET state='interrupted', error='rewound by the owner', finished_at=COALESCE(finished_at,?) "
        "WHERE to_agent=? AND state IN ('delivering','delivered')", (now(), aid))
    s.x("DELETE FROM events WHERE agent=? AND ts>=?", (aid, cutoff))
    for table in ("notes", "notifications", "decisions"):
        s.x(f"DELETE FROM {table} WHERE agent=? AND created_at>=?", (aid, cutoff))
    s.set_setting(f"autoclear:{aid}", None)
    s.set_agent(aid, session_id=sid, context_tokens=None, last_text="", last_reply_at=None, last_turn=None)


def delete_copies(aid):
    _rm(os.path.join(REWINDS, aid))
    return {"ok": True}


# ---------- memory files: edit and delete from the app ----------
def _memfile(path):
    rp = os.path.realpath(path or "")
    roots = [os.path.realpath(r) for r in [discovery.CLAUDE_PROJECTS] + list(discovery.EXTRA_PROJECTS)]
    rel = next((os.path.relpath(rp, r) for r in roots if rp.startswith(r + os.sep)), None)
    parts = rel.split(os.sep) if rel else []
    if len(parts) < 3 or parts[1] != "memory" or not rp.endswith(".md") or ".." in parts:
        raise PermissionError("that is not a memory file")
    if not os.path.isfile(rp):
        raise LookupError(path)
    return rp


def save_memfile(path, text):
    rp = _memfile(path)
    if not isinstance(text, str):
        raise ValueError("text is the file's new content")
    tmp = rp + ".sb-tmp"
    with open(tmp, "w") as f:
        f.write(text)
    os.replace(tmp, rp)
    return {"ok": True, "path": rp}


def delete_memfile(path):
    """Delete one memory file, and its pointer line in the folder's MEMORY.md index."""
    rp = _memfile(path)
    os.remove(rp)
    name, idx, dropped = os.path.basename(rp), os.path.join(os.path.dirname(rp), "MEMORY.md"), 0
    if name != "MEMORY.md" and os.path.isfile(idx):
        with open(idx) as f:
            lines = f.read().splitlines(keepends=True)
        keep = [x for x in lines if f"]({name})" not in x and f"](./{name})" not in x]
        dropped = len(lines) - len(keep)
        if dropped:
            with open(idx, "w") as f:
                f.writelines(keep)
    return {"ok": True, "index_lines_removed": dropped}


def delete_outline(hub, eid):
    r = hub.s.one("SELECT id, project FROM memory_sessions WHERE id=?", (int(eid),))
    if not r:
        raise LookupError(f"conversation {eid}")
    hub.s.x("DELETE FROM memory_sessions WHERE id=?", (r["id"],))
    scrub_outlines(hub.s, r["project"], [r["id"]])
    return {"ok": True}


# ---------- memory files ----------
_scan = {}  # transcript path -> {"off", "hits": [(file, ts, tool)]}
_dirs = {}  # (cwd, account) -> (when, memory folder)


def _memdir(a):
    k = (a.get("cwd"), a.get("account"))
    hit = _dirs.get(k)
    if not hit or time.time() - hit[0] > 600:
        hit = _dirs[k] = (time.time(), memory_dir(a))
    return hit[1]


def _writes(path):
    """[(file_path, ts, tool)] of every Write/Edit-family call in a transcript, read on from where it stopped."""
    st = _scan.get(path)
    size = os.path.getsize(path)
    if not st or size < st["off"]:
        st = _scan[path] = {"off": 0, "hits": []}
    with open(path, "rb") as f:
        f.seek(st["off"])
        for line in f:
            if not line.endswith(b"\n"):
                break
            st["off"] += len(line)
            if b'"tool_use"' not in line or b"/memory/" not in line:
                continue
            try:
                e = json.loads(line)
            except ValueError:
                continue
            for c in (e.get("message") or {}).get("content") or []:
                if isinstance(c, dict) and c.get("type") == "tool_use" and c.get("name") in WRITERS:
                    fp = (c.get("input") or {}).get("file_path") or (c.get("input") or {}).get("notebook_path")
                    if fp and "/memory/" in fp:
                        st["hits"].append((os.path.realpath(fp), e.get("timestamp"), c["name"]))
    return st["hits"]


def _front(text):
    out = {}
    m = re.match(r"---\n(.*?)\n---\n", text, re.S)
    for line in (m.group(1).splitlines() if m else []):
        k, _, v = line.partition(":")
        if k.strip() in ("name", "description", "type"):
            out[k.strip()] = v.strip().strip('"')
    if "type" not in out:
        t = re.search(r"^\s+type:\s*(\S+)", m.group(1), re.M) if m else None
        if t:
            out["type"] = t.group(1)
    return out


def memory_files(hub, pid, aid=None):
    s = hub.s
    if not s.project(pid):
        raise LookupError(pid)
    agents = [a for a in s.agents(include_gone=True) if a["project"] == pid]
    if aid:
        agents = [a for a in agents if a["id"] == aid]
        if not agents:
            raise LookupError(aid)
    claude = [a for a in agents if a["provider"] == "claude" and a.get("role") != "desk"]
    dirs = {}
    live = [a for a in claude if not a.get("gone")] or claude  # a removed agent's stray folder (~, /tmp) is not listed;
    for a in live:                                              # what it wrote still shows, through its transcripts
        try:
            dirs.setdefault(os.path.realpath(_memdir(a)), []).append(a["id"])
        except Exception:
            continue
    writers = {}  # file -> {agent: {"n", "last", "tools"}}
    for a in claude:
        for sid in sessions(hub, a):
            p = discovery.transcript_path("claude", sid, a.get("account"))
            if not p or not os.path.exists(p):
                continue
            for fp, ts, tool in _writes(p):
                w = writers.setdefault(fp, {}).setdefault(a["id"], {"n": 0, "first": ts, "last": ts, "tools": []})
                w["n"] += 1
                w["last"] = max(w["last"] or "", ts or "")
                w["first"] = min(w["first"] or ts or "", ts or w["first"] or "")
                if tool not in w["tools"]:
                    w["tools"].append(tool)
    files, shown = [], set()

    def add(fp, folder):
        shown.add(fp)
        on = os.path.exists(fp)
        text = ""
        if on:
            with open(fp, errors="replace") as f:
                text = f.read(FILE_CAP)
        by = writers.get(fp, {})
        files.append({"path": fp, "name": os.path.basename(fp), "folder": folder, "exists": on,
                      "size": os.path.getsize(fp) if on else None,
                      "mtime": os.path.getmtime(fp) if on else None, "front": _front(text) if on else {},
                      "text": text, "by": [{"agent": k, **v} for k, v in sorted(by.items(), key=lambda x: x[1]["last"] or "",
                                                                              reverse=True)]})

    for folder in dirs:
        if not os.path.isdir(folder):
            continue
        for n in sorted(os.listdir(folder)):
            fp = os.path.join(folder, n)
            if os.path.isfile(fp) and (not aid or fp in writers):
                add(fp, folder)
    for fp in writers:  # written by these agents but elsewhere, or deleted since
        if fp not in shown:
            add(fp, os.path.dirname(fp))
    files.sort(key=lambda f: (f["name"] != "MEMORY.md", -(f["mtime"] or 0)))
    rows = s.q("SELECT * FROM memory_sessions WHERE project=?" + (" AND agent=?" if aid else "") +
               " AND state<>'skipped' ORDER BY id DESC LIMIT 200", (pid, aid) if aid else (pid,))
    return {"project": pid, "agent": aid, "folders": [{"path": k, "agents": v} for k, v in dirs.items()],
            "agents": [{"id": a["id"], "title": a.get("title") or a["id"], "gone": bool(a.get("gone"))} for a in agents],
            "files": files, "outlines": [memory._entry(r) for r in rows]}
