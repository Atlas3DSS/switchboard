"""SQLite state for the switchboard hub. One connection, one lock: the hub is small and local."""
import json
import os
import sqlite3
import threading
import time
import uuid
from datetime import datetime, timezone

STATE = os.path.expanduser(os.environ.get("SB_STATE", "~/.local/state/switchboard"))
DB_PATH = os.path.join(STATE, "hub.db")
TURNS = os.path.join(STATE, "turns")
INBOX = os.path.join(STATE, "inbox")
UPLOADS = os.path.join(STATE, "uploads")
IMAGE_TYPES = {"image/png": ".png", "image/jpeg": ".jpg", "image/gif": ".gif", "image/webp": ".webp"}

SCHEMA = """
CREATE TABLE IF NOT EXISTS projects (
  id TEXT PRIMARY KEY, name TEXT NOT NULL, goal TEXT DEFAULT '', next_step TEXT DEFAULT '',
  status_note TEXT DEFAULT '', archived INTEGER DEFAULT 0, sources TEXT DEFAULT '[]',
  match TEXT DEFAULT '{}', updated_at TEXT);
CREATE TABLE IF NOT EXISTS agents (
  id TEXT PRIMARY KEY, project TEXT, title TEXT, provider TEXT, account TEXT, model TEXT,
  mode TEXT, session_id TEXT, cwd TEXT, pane TEXT, permission_mode TEXT, effort TEXT,
  paused INTEGER DEFAULT 0, project_pinned INTEGER DEFAULT 0, state TEXT DEFAULT 'idle',
  last_activity TEXT, last_text TEXT, attached INTEGER DEFAULT 0, gone INTEGER DEFAULT 0, created_at TEXT);
CREATE TABLE IF NOT EXISTS messages (
  id TEXT PRIMARY KEY, idem TEXT UNIQUE, to_agent TEXT, from_agent TEXT, body TEXT, state TEXT,
  attempts INTEGER DEFAULT 0, hop INTEGER DEFAULT 0, is_reply INTEGER DEFAULT 0, wait_reply INTEGER DEFAULT 0,
  turn_id TEXT,
  pid INTEGER, created_at TEXT, delivered_at TEXT, finished_at TEXT, reply TEXT, error TEXT);
CREATE INDEX IF NOT EXISTS messages_to_state ON messages(to_agent, state);
CREATE TABLE IF NOT EXISTS events (
  id INTEGER PRIMARY KEY AUTOINCREMENT, agent TEXT, ts TEXT, kind TEXT, text TEXT, turn_id TEXT);
CREATE INDEX IF NOT EXISTS events_agent ON events(agent, id);
CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT);
CREATE TABLE IF NOT EXISTS notifications (
  id TEXT PRIMARY KEY, idem TEXT UNIQUE, agent TEXT, project TEXT, kind TEXT, level TEXT, text TEXT,
  created_at TEXT, acked_at TEXT);
CREATE TABLE IF NOT EXISTS notes (
  id INTEGER PRIMARY KEY AUTOINCREMENT, project TEXT, agent TEXT, text TEXT, created_at TEXT);
CREATE INDEX IF NOT EXISTS notes_project ON notes(project, id);
CREATE TABLE IF NOT EXISTS hosts (
  name TEXT PRIMARY KEY, cfg TEXT, doc TEXT, probed_at TEXT, ok INTEGER DEFAULT 0, error TEXT, ms INTEGER);
CREATE TABLE IF NOT EXISTS host_samples (
  id INTEGER PRIMARY KEY AUTOINCREMENT, host TEXT, ts TEXT, load1 REAL, available_mib INTEGER, gpus TEXT, desktop TEXT);
CREATE INDEX IF NOT EXISTS host_samples_host ON host_samples(host, id);
CREATE TABLE IF NOT EXISTS jobs (
  id TEXT PRIMARY KEY, name TEXT, project TEXT, agent TEXT, host TEXT, unit TEXT, cwd TEXT, argv TEXT,
  threads INTEGER, mem_mib INTEGER, gpu_mib INTEGER, gpu_uuid TEXT, hours REAL, state TEXT, reason TEXT,
  adopted INTEGER DEFAULT 0, collect INTEGER DEFAULT 0, pid INTEGER, invocation TEXT, cpu_s REAL, mem_cur_mib INTEGER,
  exit_code INTEGER, created_at TEXT, started_at TEXT, finished_at TEXT, last_seen TEXT, host_pref TEXT, stopped_by TEXT,
  board_id TEXT, cpus TEXT, protected INTEGER DEFAULT 0, lane TEXT, placer TEXT);
CREATE INDEX IF NOT EXISTS jobs_state ON jobs(state, host);
CREATE TABLE IF NOT EXISTS decisions (
  id TEXT PRIMARY KEY, project TEXT, agent TEXT, question TEXT, options TEXT DEFAULT '[]',
  state TEXT DEFAULT 'open', answer TEXT, created_at TEXT, answered_at TEXT);
"""


def now():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def iso_from_epoch(t):
    return datetime.fromtimestamp(t, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def epoch(iso):
    if not iso:
        return 0.0
    try:
        return datetime.strptime(iso[:19], "%Y-%m-%dT%H:%M:%S").replace(tzinfo=timezone.utc).timestamp()
    except ValueError:
        return 0.0


def new_id(prefix):
    return f"{prefix}-{time.strftime('%m%d%H%M%S')}-{uuid.uuid4().hex[:6]}"


def private_dir(d):
    os.makedirs(d, mode=0o700, exist_ok=True)
    os.chmod(d, 0o700)


def private_db(path):
    """Owner-only database files. SQLite gives a new -wal/-shm the database file's mode."""
    os.close(os.open(path, os.O_CREAT | os.O_RDWR, 0o600))
    for p in (path, path + "-wal", path + "-shm"):
        try:
            os.chmod(p, 0o600)
        except FileNotFoundError:
            pass


class Store:
    def __init__(self, path=DB_PATH):
        private_dir(STATE)
        private_dir(os.path.dirname(path))
        private_db(path)
        os.makedirs(TURNS, exist_ok=True)
        os.makedirs(INBOX, exist_ok=True)
        os.makedirs(UPLOADS, exist_ok=True)
        self.lock = threading.RLock()
        self.db = sqlite3.connect(path, check_same_thread=False, isolation_level=None)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA busy_timeout=5000")
        self.db.executescript(SCHEMA)
        cols = {r[1] for r in self.db.execute("PRAGMA table_info(messages)")}
        if "wait_reply" not in cols:
            self.db.execute("ALTER TABLE messages ADD COLUMN wait_reply INTEGER DEFAULT 0")
        for col, decl in (("attachments", "TEXT DEFAULT '[]'"), ("mode", "TEXT DEFAULT 'queue'")):
            if col not in cols:
                self.db.execute(f"ALTER TABLE messages ADD COLUMN {col} {decl}")
        pcols = {r[1] for r in self.db.execute("PRAGMA table_info(projects)")}
        if "paused" not in pcols:
            self.db.execute("ALTER TABLE projects ADD COLUMN paused INTEGER DEFAULT 0")
        if "lead" not in pcols:
            self.db.execute("ALTER TABLE projects ADD COLUMN lead TEXT")
        acols = {r[1] for r in self.db.execute("PRAGMA table_info(agents)")}
        for col, decl in (("account_pending", "TEXT"), ("migrate_state", "TEXT"), ("migrate_note", "TEXT"),
                          ("migrate_since", "TEXT"), ("last_reply_at", "TEXT"), ("context_tokens", "INTEGER"),
                          ("context_window", "INTEGER"), ("tokens_24h", "INTEGER"), ("last_turn", "TEXT"),
                          ("role", "TEXT"), ("default_mode", "TEXT"), ("fork_from", "TEXT")):
            if col not in acols:
                self.db.execute(f"ALTER TABLE agents ADD COLUMN {col} {decl}")

    def q(self, sql, args=()):
        with self.lock:
            return [dict(r) for r in self.db.execute(sql, args).fetchall()]

    def one(self, sql, args=()):
        rows = self.q(sql, args)
        return rows[0] if rows else None

    def x(self, sql, args=()):
        with self.lock:
            return self.db.execute(sql, args)

    def tx(self):
        """Context manager for an IMMEDIATE transaction under the hub lock. Inside another tx() on the same thread it
        joins that one, and the outer block commits or rolls back everything."""
        store = self

        class _Tx:
            def __enter__(self_):
                store.lock.acquire()
                self_.outer = not store.db.in_transaction
                if self_.outer:
                    try:
                        store.db.execute("BEGIN IMMEDIATE")
                    except BaseException:
                        store.lock.release()
                        raise
                return store

            def __exit__(self_, exc_type, *_):
                try:
                    if self_.outer:
                        store.db.execute("ROLLBACK" if exc_type else "COMMIT")
                finally:
                    store.lock.release()
                return False

        return _Tx()

    # ---------- settings ----------
    def setting(self, key, default=None):
        r = self.one("SELECT value FROM settings WHERE key=?", (key,))
        return json.loads(r["value"]) if r else default

    def set_setting(self, key, value):
        self.x("INSERT INTO settings(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
               (key, json.dumps(value)))

    # ---------- projects ----------
    def project(self, pid):
        r = self.one("SELECT * FROM projects WHERE id=?", (pid,))
        if r:
            r["sources"] = json.loads(r["sources"] or "[]")
            r["match"] = json.loads(r["match"] or "{}")
            r["archived"] = bool(r["archived"])
            r["paused"] = bool(r.get("paused"))
        return r

    def projects(self):
        return [self.project(r["id"]) for r in self.q("SELECT id FROM projects ORDER BY archived, name")]

    def upsert_project(self, p, overwrite=True):
        cur = self.project(p["id"])
        if cur and not overwrite:
            return cur
        merged = dict(cur or {"goal": "", "next_step": "", "status_note": "", "archived": False,
                              "sources": [], "match": {}})
        merged.update({k: v for k, v in p.items() if v is not None})
        self.x("""INSERT INTO projects(id,name,goal,next_step,status_note,archived,sources,match,updated_at,paused,lead)
                  VALUES(?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET name=excluded.name, goal=excluded.goal,
                  next_step=excluded.next_step, status_note=excluded.status_note, archived=excluded.archived,
                  sources=excluded.sources, match=excluded.match, updated_at=excluded.updated_at,
                  paused=excluded.paused, lead=excluded.lead""",
               (merged["id"], merged.get("name") or merged["id"], merged.get("goal", ""), merged.get("next_step", ""),
                merged.get("status_note", ""), int(bool(merged.get("archived"))), json.dumps(merged.get("sources", [])),
                json.dumps(merged.get("match", {})), now(), int(bool(merged.get("paused"))),
                merged.get("lead") or None))
        return self.project(merged["id"])

    # ---------- agents ----------
    def agent(self, aid):
        r = self.one("SELECT * FROM agents WHERE id=?", (aid,))
        if r:
            for k in ("paused", "project_pinned", "attached", "gone"):
                r[k] = bool(r[k])
        return r

    def agents(self, include_gone=False):
        rows = self.q("SELECT id FROM agents" + ("" if include_gone else " WHERE gone=0") + " ORDER BY id")
        return [self.agent(r["id"]) for r in rows]

    def set_agent(self, aid, **kw):
        if not kw:
            return
        cols = ", ".join(f"{k}=?" for k in kw)
        self.x(f"UPDATE agents SET {cols} WHERE id=?", (*kw.values(), aid))

    def add_agent(self, a):
        a = dict(a)
        a.setdefault("created_at", now())
        cols = ",".join(a)
        self.x(f"INSERT INTO agents({cols}) VALUES({','.join('?' * len(a))})", tuple(a.values()))
        return self.agent(a["id"])

    # ---------- events ----------
    def event(self, agent, kind, text, turn_id=None, ts=None):
        self.x("INSERT INTO events(agent,ts,kind,text,turn_id) VALUES(?,?,?,?,?)",
               (agent, ts or now(), kind, (text or "")[:20000], turn_id))

    def events(self, agent, after=0, limit=200):
        return self.q("SELECT * FROM events WHERE agent=? AND id>? ORDER BY id LIMIT ?", (agent, after, limit))

    def recent_events(self, agents, limit=100):
        if not agents:
            return []
        ph = ",".join("?" * len(agents))
        rows = self.q(f"SELECT * FROM events WHERE agent IN ({ph}) ORDER BY id DESC LIMIT ?", (*agents, limit))
        return rows[::-1]

    # ---------- messages ----------
    def message(self, mid):
        return self.one("SELECT * FROM messages WHERE id=?", (mid,))

    def messages(self, agent=None, state=None, limit=50):
        sql, args = "SELECT * FROM messages WHERE 1=1", []
        if agent:
            sql += " AND (to_agent=? OR from_agent=?)"
            args += [agent, agent]
        if state:
            sql += " AND state=?"
            args.append(state)
        sql += " ORDER BY created_at DESC, id DESC LIMIT ?"
        args.append(limit)
        return self.q(sql, args)

    def enqueue(self, to, body, frm="owner", idem=None, hop=0, is_reply=False, wait_reply=False, attachments=None,
                mode="queue"):
        """Insert a message exactly once. Returns (message, duplicate)."""
        body = (body or "").strip()
        att = json.dumps(list(attachments or []))
        with self.tx():
            if idem:
                dup = self.one("SELECT * FROM messages WHERE idem=?", (idem,))
                if dup:
                    return dup, True
            cutoff = time.time() - 600
            for r in self.q("SELECT * FROM messages WHERE to_agent=? AND from_agent=? AND body=? AND "
                            "COALESCE(attachments,'[]')=? AND state NOT IN ('failed','interrupted') "
                            "ORDER BY created_at DESC LIMIT 1", (to, frm, body, att)):
                if epoch(r["created_at"]) >= cutoff:
                    return r, True
            mid = new_id("m")
            self.db.execute("""INSERT INTO messages(id,idem,to_agent,from_agent,body,state,hop,is_reply,wait_reply,
                               created_at,attachments,mode) VALUES(?,?,?,?,?,'queued',?,?,?,?,?,?)""",
                            (mid, idem or mid, to, frm, body, hop, int(is_reply), int(wait_reply), now(), att, mode))
        return self.message(mid), False

    # ---------- decisions ----------
    def decision(self, did):
        r = self.one("SELECT * FROM decisions WHERE id=?", (did,))
        if r:
            r["options"] = json.loads(r["options"] or "[]")
        return r

    def decisions(self, project=None, open_only=False):
        sql, args = "SELECT id FROM decisions WHERE 1=1", []
        if project:
            sql += " AND project=?"
            args.append(project)
        if open_only:
            sql += " AND state='open'"
        sql += " ORDER BY created_at DESC"
        return [self.decision(r["id"]) for r in self.q(sql, args)]


# ---------- uploads ----------
def save_upload(data, media_type):
    if media_type not in IMAGE_TYPES:
        raise ValueError(f"unsupported image type {media_type!r}")
    if not data or len(data) > 15 * 1024 * 1024:
        raise ValueError("image must be between 1 byte and 15 MB")
    uid = new_id("u")
    path = os.path.join(UPLOADS, uid + IMAGE_TYPES[media_type])
    with open(path, "wb") as f:
        f.write(data)
    return {"id": uid, "url": f"/api/uploads/{uid}", "path": path, "media_type": media_type, "bytes": len(data)}


def upload_path(uid):
    if not uid or "/" in uid or ".." in uid:
        return None, None
    for media_type, ext in IMAGE_TYPES.items():
        p = os.path.join(UPLOADS, uid + ext)
        if os.path.exists(p):
            return p, media_type
    return None, None
