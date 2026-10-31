"""Index of every past Claude + Codex session (owner asks, title, cwd), searchable with SQLite FTS5.

Transcripts are append-only jsonl, so each file is read once and then only from the byte offset it was indexed to.
The first build reads every transcript in parallel (one core left free); later refreshes touch only files that grew.
"""
import concurrent.futures
import glob
import json
import multiprocessing
import os
import re
import sqlite3
import threading

from . import discovery
from .store import STATE, epoch

DB_PATH = os.path.join(STATE, "sessions.db")
CODEX_INDEX = os.path.expanduser("~/.codex/session_index.jsonl")
DESK_CWD = os.path.join(STATE, "desk")  # the Switchboard chat's own sessions are not worth finding
CLIP = 400

SCHEMA = """
CREATE TABLE IF NOT EXISTS sess (
  id TEXT PRIMARY KEY, provider TEXT, path TEXT, cwd TEXT, title TEXT, first_ts TEXT, last_ts TEXT,
  n_asks INTEGER DEFAULT 0, first_ask TEXT, last_ask TEXT, size INTEGER, offset INTEGER,
  interactive INTEGER DEFAULT 0);
CREATE INDEX IF NOT EXISTS sess_last ON sess(last_ts);
CREATE VIRTUAL TABLE IF NOT EXISTS sess_fts USING fts5(id UNINDEXED, title, cwd, asks, tokenize='unicode61');
"""

SKIP_PREFIXES = ("<system-reminder", "<command-", "<local-command", "Caveat:", "<task-notification",
                 "[Request interrupted", "<bash-stdout", "<bash-stderr", "# AGENTS.md", "<environment_context", "<user_instructions", "<turn_aborted")


def _text(content):
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return " ".join(c.get("text", "") for c in content if isinstance(c, dict) and
                        c.get("type") in ("text", "input_text"))
    return ""


def _keep(t):
    t = (t or "").strip()
    if not t or t.startswith(SKIP_PREFIXES):
        return None
    return " ".join(t.split())[:CLIP]


def parse(job):
    """Read one transcript from `offset` to its last complete line. Runs in a worker process."""
    provider, path, offset = job
    try:
        os.nice(10)
    except OSError:
        pass
    out = {"provider": provider, "path": path, "id": None, "cwd": None, "title": None, "first_ts": None,
           "last_ts": None, "asks": [], "offset": offset, "interactive": False}
    if provider != "codex":
        out["id"] = os.path.basename(path)[:-6]
    try:
        with open(path, "rb") as f:
            f.seek(offset)
            pos = offset
            for raw in f:
                if not raw.endswith(b"\n"):
                    break  # being written; picked up next time
                pos += len(raw)
                line = raw.decode(errors="replace")
                if provider != "codex":
                    _claude_line(line, out)
                else:
                    _codex_line(line, out)
            out["offset"] = pos
    except OSError:
        return None
    return out


def _claude_line(line, out):
    if '"type":"user"' in line:
        if '"tool_result"' in line or '"isSidechain":true' in line or '"isMeta":true' in line:
            return
    elif not any(k in line for k in ('"ai-title"', '"custom-title"', '"type":"summary"')):
        return
    try:
        e = json.loads(line)
    except ValueError:
        return
    kind = e.get("type")
    if kind == "custom-title" and e.get("customTitle"):
        out["title"] = ("!", e["customTitle"])  # a title the owner set beats a generated one
    elif kind == "ai-title" and e.get("aiTitle") and not (out["title"] or ("",))[0] == "!":
        out["title"] = ("", e["aiTitle"])
    elif kind == "summary" and e.get("summary") and not out["title"]:
        out["title"] = ("", e["summary"])
    elif kind == "user":
        out["cwd"] = out["cwd"] or e.get("cwd")
        if e.get("entrypoint") == "cli":  # "sdk-cli" is a headless `claude -p` run
            out["interactive"] = True
        ts = e.get("timestamp")
        out["first_ts"] = out["first_ts"] or ts
        out["last_ts"] = ts or out["last_ts"]
        t = _keep(_text((e.get("message") or {}).get("content")))
        if t and (not out["asks"] or out["asks"][-1] != t):
            out["asks"].append(t)


def _codex_line(line, out):
    if '"session_meta"' not in line and '"user_message"' not in line and '"role":"user"' not in line:
        return
    try:
        e = json.loads(line)
    except ValueError:
        return
    p = e.get("payload") or {}
    if e.get("type") == "session_meta":
        out["id"] = out["id"] or p.get("id")
        out["cwd"] = out["cwd"] or p.get("cwd")
        out["interactive"] = out["interactive"] or p.get("originator") != "codex_exec"
        return
    if p.get("type") == "user_message":
        msg = p.get("message")
    elif p.get("type") == "message" and p.get("role") == "user":
        msg = _text(p.get("content"))
    else:
        return
    t = _keep(msg)
    if not t:
        return
    ts = e.get("timestamp")
    out["first_ts"] = out["first_ts"] or ts
    out["last_ts"] = ts or out["last_ts"]
    if not out["asks"] or out["asks"][-1] != t:  # the event and the response item repeat the same text
        out["asks"].append(t)


def transcript_files():
    """[(provider, path)] for every main-thread transcript on disk."""
    out = [("claude", p) for root in [discovery.CLAUDE_PROJECTS] + discovery.EXTRA_PROJECTS
           for p in glob.glob(os.path.join(root, "*", "*.jsonl"))]
    out += [("codex", p) for p in glob.glob(os.path.join(discovery.CODEX_SESSIONS, "*", "*", "*", "rollout-*.jsonl"))]
    out += [("api", p) for p in glob.glob(os.path.join(discovery.API_SESSIONS, "*", "*.jsonl"))]
    return out


def codex_titles():
    titles = {}
    try:
        with open(CODEX_INDEX, errors="replace") as f:
            for line in f:
                try:
                    r = json.loads(line)
                except ValueError:
                    continue
                if r.get("id") and r.get("thread_name"):
                    titles[r["id"]] = r["thread_name"]  # the newest name wins
    except OSError:
        pass
    return titles


class Index:
    def __init__(self, path=DB_PATH):
        self.path = path
        self.lock = threading.Lock()
        self.busy = False
        self.db = sqlite3.connect(path, check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.executescript(SCHEMA)

    def count(self):
        with self.lock:
            return self.db.execute("SELECT COUNT(*) FROM sess WHERE n_asks>0").fetchone()[0]

    def refresh(self, workers=None, files=None):
        """Index new transcripts and the new tail of grown ones. Returns how many files were read."""
        if self.busy:
            return 0
        self.busy = True
        try:
            return self._refresh(workers, files)
        finally:
            self.busy = False

    def _refresh(self, workers, files):
        with self.lock:
            known = {r["path"]: (r["size"], r["offset"]) for r in self.db.execute("SELECT path, size, offset FROM sess")}
        jobs = []
        for provider, path in files or transcript_files():
            try:
                size = os.path.getsize(path)
            except OSError:
                continue
            prev = known.get(path)
            if prev and prev[0] == size:
                continue
            offset = prev[1] if prev and prev[1] <= size else 0
            jobs.append((provider, path, offset))
        if not jobs:
            return 0
        jobs.sort(key=lambda j: -(os.path.getsize(j[1]) - j[2]) if os.path.exists(j[1]) else 0)
        results = []
        if len(jobs) <= 4:
            results = [parse(j) for j in jobs]
        else:
            n = workers or max(1, (os.cpu_count() or 2) - 1)  # leave one core free
            with concurrent.futures.ProcessPoolExecutor(n, mp_context=multiprocessing.get_context("spawn")) as ex:
                results = list(ex.map(parse, jobs, chunksize=4))
        titles = codex_titles() if any(j[0] == "codex" for j in jobs) else {}
        with self.lock, self.db:
            for r in results:
                if r:
                    self._merge(r, titles)
        return len(jobs)

    def _merge(self, r, titles):
        size = r["offset"]
        old = self.db.execute("SELECT * FROM sess WHERE path=?", (r["path"],)).fetchone()
        if old and r["offset"] and old["offset"] and r["offset"] < old["offset"]:
            old = None  # the file was rewritten; start over
        sid = r["id"] or (old and old["id"])
        if not sid:  # a Codex rollout with no session_meta yet
            return
        title = (r["title"] or (None, None))[1] or titles.get(sid) or (old and old["title"])
        if r["provider"] == "codex" and titles.get(sid):
            title = titles[sid]
        cwd = (old and old["cwd"]) or r["cwd"]
        if cwd and os.path.realpath(cwd) == os.path.realpath(DESK_CWD):
            r["asks"] = []
        asks = r["asks"]
        old_asks = ""
        if old:
            row = self.db.execute("SELECT asks FROM sess_fts WHERE id=?", (sid,)).fetchone()
            old_asks = row["asks"] if row else ""
        all_asks = (old_asks + "\n" if old_asks and asks else old_asks) + "\n".join(asks)
        n = (old["n_asks"] if old else 0) + len(asks)
        self.db.execute("DELETE FROM sess WHERE id=? OR path=?", (sid, r["path"]))
        self.db.execute(
            "INSERT INTO sess(id,provider,path,cwd,title,first_ts,last_ts,n_asks,first_ask,last_ask,size,offset,"
            "interactive) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (sid, r["provider"], r["path"], cwd, title, (old and old["first_ts"]) or r["first_ts"],
             r["last_ts"] or (old and old["last_ts"]), n, (old and old["first_ask"]) or (asks[0] if asks else None),
             asks[-1] if asks else (old and old["last_ask"]), size, r["offset"],
             int(bool(r["interactive"] or (old and old["interactive"])))))
        self.db.execute("DELETE FROM sess_fts WHERE id=?", (sid,))
        if n:
            self.db.execute("INSERT INTO sess_fts(id,title,cwd,asks) VALUES(?,?,?,?)", (sid, title or "", cwd or "", all_asks))

    def find(self, sid):
        """One session by full id or a unique id prefix (8+ characters)."""
        with self.lock:
            row = self.db.execute("SELECT * FROM sess WHERE id=?", (sid,)).fetchone()
            if not row and len(sid) >= 8:
                rows = self.db.execute("SELECT * FROM sess WHERE id LIKE ? LIMIT 2", (sid + "%",)).fetchall()
                row = rows[0] if len(rows) == 1 else None
        if not row:  # not indexed yet: read that one file now
            path, provider = None, None
            for provider in ("claude", "codex", "api"):
                path = discovery.transcript_path(provider, sid)
                if path:
                    break
            if path:
                self.refresh(files=[(provider, path)])
                with self.lock:
                    row = self.db.execute("SELECT * FROM sess WHERE path=?", (path,)).fetchone()
        return self._view(row) if row else None

    def search(self, q="", limit=30, headless=False):
        """Sessions matching every word of q (prefixes), best first. Headless (`claude -p`, `codex exec`) runs
        are left out unless headless=True."""
        q = (q or "").strip()
        only = "" if headless else " AND s.interactive=1"
        limit = max(1, min(int(limit or 30), 200))
        with self.lock:
            if re.fullmatch(r"[0-9a-fA-F-]{8,64}", q):
                rows = self.db.execute("SELECT s.*, '' snip FROM sess s WHERE s.id LIKE ? ORDER BY last_ts DESC LIMIT ?",
                                       (q.lower() + "%", limit)).fetchall()
                if rows:
                    return [self._view(r) for r in rows]
            words = re.findall(r"\w+", q)
            if not words:
                rows = self.db.execute("SELECT s.*, '' snip FROM sess s WHERE n_asks>0" + only + " ORDER BY last_ts DESC LIMIT ?",
                                       (limit,)).fetchall()
                return [self._view(r) for r in rows]
            match = " ".join(f'"{w}"*' for w in words)
            # best text match first, but a recent session beats an old one of about the same rank
            rows = self.db.execute(
                "SELECT s.*, snippet(sess_fts, 3, '[', ']', ' … ', 18) snip, bm25(sess_fts, 8.0, 2.0, 1.0) rank "
                "FROM sess_fts JOIN sess s ON s.id = sess_fts.id WHERE sess_fts MATCH ?" + only + " "
                "ORDER BY rank LIMIT ?", (match, limit * 4)).fetchall()
        newest = max((epoch(r["last_ts"]) for r in rows if r["last_ts"]), default=0)

        def score(r):
            age_days = (newest - epoch(r["last_ts"])) / 86400 if r["last_ts"] else 365
            return r["rank"] + 0.05 * min(age_days, 120)

        return [self._view(r) for r in sorted(rows, key=score)[:limit]]

    @staticmethod
    def _view(r):
        keys = r.keys()
        return {"id": r["id"], "provider": r["provider"], "cwd": r["cwd"], "title": r["title"],
                "first_ts": r["first_ts"], "last_ts": r["last_ts"], "n_asks": r["n_asks"],
                "first_ask": r["first_ask"], "last_ask": r["last_ask"],
                "snippet": r["snip"] if "snip" in keys else "", "path": r["path"], "interactive": bool(r["interactive"])}


def main():
    """python3 -m switchboard.sessions [query]: build/refresh the index, then search it."""
    import sys
    import time
    ix = Index()
    t0 = time.time()
    n = ix.refresh()
    print(f"read {n} files in {time.time() - t0:.1f}s; {ix.count()} sessions indexed", file=sys.stderr)
    for s in ix.search(" ".join(sys.argv[1:]), 15):
        print(f"{s['id'][:13]} {s['provider']:<6} {(s['last_ts'] or '')[:10]} {(s['title'] or s['first_ask'] or '')[:60]:<60} "
              f"{s['cwd']}")


if __name__ == "__main__":
    main()
