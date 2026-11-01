"""Hermes Agent (Nous Research) as a terminal agent. Hermes keeps its conversations in SQLite ($HERMES_HOME/state.db:
`sessions`, `messages`), not in a transcript file, so a Hermes "transcript path" is `hermes:<state.db>#<session id>`
and the transcript/discovery readers hand those paths here. Read-only: Switchboard never writes Hermes' database.

Launch: `hermes chat [--resume <id>] [-m <model>] [--reasoning <effort>]`; Switchboard's note goes in
HERMES_EPHEMERAL_SYSTEM_PROMPT (sent with every call, never stored). A new session is found in state.db by its cwd.
"""
import json
import os
import sqlite3

PREFIX = "hermes:"


def home(config_dir=None):
    return os.path.expanduser(config_dir or os.environ.get("HERMES_HOME") or "~/.hermes")


def db_path(config_dir=None):
    return os.path.join(home(config_dir), "state.db")


def _connect(db):
    c = sqlite3.connect(f"file:{db}?mode=ro", uri=True, timeout=5)
    c.row_factory = sqlite3.Row
    return c


def split(path):
    db, _, sid = path[len(PREFIX):].rpartition("#")
    return db, sid


def path_for(sid, config_dir=None):
    """The transcript path of a Hermes session, or None if it is not in that state.db (yet)."""
    db = db_path(config_dir)
    if not sid or not os.path.exists(db):
        return None
    try:
        with _connect(db) as c:
            return f"{PREFIX}{db}#{sid}" if c.execute("SELECT 1 FROM sessions WHERE id=?", (sid,)).fetchone() else None
    except sqlite3.Error:
        return None


def new_session(cwd, since, taken=(), config_dir=None):
    """Newest session started in `cwd` at or after `since` (epoch) that no agent has claimed."""
    db = db_path(config_dir)
    if not os.path.exists(db):
        return None
    try:
        with _connect(db) as c:
            for r in c.execute("SELECT id FROM sessions WHERE cwd=? AND started_at>=? ORDER BY started_at DESC",
                               (os.path.realpath(cwd), since - 5)):
                if r["id"] not in taken:
                    return r["id"]
    except sqlite3.Error:
        pass
    return None


def _iso(t):
    from .store import iso_from_epoch
    return iso_from_epoch(t) if t else None


def _line(r):
    """One message as the JSON line transcript._parse(…, 'hermes') understands."""
    return json.dumps({"id": r["id"], "role": r["role"], "content": r["content"] or "", "ts": _iso(r["timestamp"]),
                       "tool_name": r["tool_name"], "tool_calls": r["tool_calls"]})


def entries(line):
    """[(role, text, ts, n_images)] for one message line; tool calls and results are summarised."""
    try:
        m = json.loads(line)
    except ValueError:
        return []
    role, text, ts = m.get("role"), (m.get("content") or "").strip(), m.get("ts")
    if role == "user":
        return [("user", text, ts, 0)] if text else []
    if role == "assistant":
        out = [("assistant", text, ts, 0)] if text else []
        try:
            calls = json.loads(m.get("tool_calls") or "[]") or []
        except ValueError:
            calls = []
        for call in calls:
            fn = (call or {}).get("function") or {}
            try:
                args = json.loads(fn.get("arguments") or "{}")
            except ValueError:
                args = {}
            brief = (args.get("command") or args.get("path") or args.get("query") or args.get("url") or "") \
                if isinstance(args, dict) else ""
            out.append(("tool", f"{fn.get('name') or 'tool'}: {' '.join(str(brief).split())[:240]}", ts, 0))
        return out
    return []  # tool results and system rows are not chat


def lines_newest_first(path, limit=5000):
    db, sid = split(path)
    try:
        with _connect(db) as c:
            rows = c.execute("SELECT id, role, content, timestamp, tool_name, tool_calls FROM messages "
                             "WHERE session_id=? ORDER BY id DESC LIMIT ?", (sid, limit)).fetchall()
    except sqlite3.Error:
        return []
    return [_line(r) for r in rows]


def read_chat(path, limit=40, before=None, after=None):
    """Same shape as transcript.read_chat; entry ids are message id * 8 + part."""
    db, sid = split(path)
    limit = max(1, min(int(limit), 400))
    q, args = "SELECT id, role, content, timestamp, tool_name, tool_calls FROM messages WHERE session_id=?", [sid]
    if after is not None:
        q += " AND id>? ORDER BY id LIMIT 2000"
        args.append(int(after) // 8)
    else:
        if before is not None:
            q += " AND id<?"
            args.append(int(before) // 8)
        q += " ORDER BY id DESC LIMIT ?"
        args.append(limit * 3 + 10)
    try:
        with _connect(db) as c:
            rows = c.execute(q, args).fetchall()
    except sqlite3.Error:
        rows = []
    if after is None:
        rows = rows[::-1]
    out = []
    for r in rows:
        for k, (role, text, ts, n_img) in enumerate(entries(_line(r))):
            out.append({"id": r["id"] * 8 + k, "role": role, "text": text[:30000], "ts": ts, "n_images": n_img})
    if after is not None:
        return {"entries": out, "before": None, "latest": out[-1]["id"] if out else int(after)}
    more = len(out) > limit
    out = out[-limit:]
    return {"entries": out, "before": out[0]["id"] if more and out else None, "latest": out[-1]["id"] if out else None}


def last_model(path):
    db, sid = split(path)
    try:
        with _connect(db) as c:
            r = c.execute("SELECT model FROM sessions WHERE id=?", (sid,)).fetchone()
            return r["model"] if r else None
    except sqlite3.Error:
        return None


def has_user_text(path, needle, since_epoch):
    db, sid = split(path)
    needle = " ".join(needle.split())[:80]
    try:
        with _connect(db) as c:
            rows = c.execute("SELECT content FROM messages WHERE session_id=? AND role='user' AND timestamp>=? "
                             "ORDER BY id DESC LIMIT 50", (sid, since_epoch - 5)).fetchall()
    except sqlite3.Error:
        return False
    return any(needle in " ".join((r["content"] or "").split()) for r in rows)


def usage_stats(path, since_epoch):
    db, sid = split(path)
    out = {"context_tokens": None, "context_window": None, "tokens_24h": 0, "last_turn": None}
    try:
        with _connect(db) as c:
            r = c.execute("SELECT COALESCE(SUM(token_count),0) n FROM messages WHERE session_id=? AND timestamp>=?",
                          (sid, since_epoch)).fetchone()
            out["tokens_24h"] = int(r["n"] or 0)
    except sqlite3.Error:
        pass
    return out
