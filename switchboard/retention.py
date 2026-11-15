"""Retention: what the hub keeps, so its state stays a fixed size however long it runs.

One sweep at start and every few hours. Rows that are finished and old go; anything still open stays whatever its age
(a queued or held message, an unanswered notification, a running job). Decisions and the work queue are the owner's
record and are never pruned. Agents' own transcripts (the CLIs' files) are not Switchboard's and are never touched:
they are the full record that project memory points at.

  events          older than events_days, and beyond the newest events_per_agent of one agent
  messages        finished (done, failed, interrupted, cancelled) and older than messages_days
  notifications   acknowledged, or FYI, and older than notifications_days
  jobs            ended and older than jobs_days
  broadcasts      finished and older than broadcasts_days
  notes           older than notes_days, but a project always keeps its newest notes_keep
  memory          a project keeps its newest memory_sessions outlines, and any older one its memory still cites
  turns/          headless turn files older than files_days that no open message points at
  pty/            scrollback of agents that are removed, with no holder alive, untouched for files_days
  uploads/        images older than messages_days that no remaining message carries
  helpers/        helper results older than messages_days

Every number is a setting (`sb retention --set events_days=60`); 0 switches that rule off.
"""
import json
import os
import time

from . import discovery, memory
from .store import STATE, TURNS, UPLOADS, iso_from_epoch, now

DEFAULTS = {"events_days": 30, "events_per_agent": 5000, "messages_days": 30, "notifications_days": 30,
            "jobs_days": 30, "broadcasts_days": 30, "notes_days": 180, "notes_keep": 30, "memory_sessions": 60,
            "files_days": 14}
EVERY_S = 6 * 3600
MESSAGES_DONE = ("done", "failed", "interrupted", "cancelled")
MESSAGES_OPEN = ("queued", "held", "delivering", "delivered")
JOBS_ENDED = ("finished", "failed", "stopped", "lost", "refused", "preempted")  # jobs.FINAL
PTY_SUFFIXES = (".spec.json", ".log.err", ".json", ".log", ".sock")  # longest first
VACUUM_FREE = 0.25  # rewrite the database file once this share of it is free pages


def settings(store):
    cfg = dict(DEFAULTS)
    cfg.update(store.setting("retention", {}) or {})
    return cfg


def set_settings(store, upd):
    cfg = settings(store)
    for k, v in (upd or {}).items():
        if k not in DEFAULTS:
            raise ValueError(f"unknown retention setting {k}; there are: {', '.join(DEFAULTS)}")
        if int(v) < 0:
            raise ValueError(f"{k} is a number of days or rows, 0 to switch the rule off")
        cfg[k] = int(v)
    store.set_setting("retention", cfg)
    return cfg


def _before(days):
    return iso_from_epoch(time.time() - days * 86400)


def _rows(store, sql, args=()):
    return store.x(sql, args).rowcount


def _old(path, days):
    try:
        return time.time() - os.path.getmtime(path) > days * 86400
    except OSError:
        return False


def _remove(paths):
    n = 0
    for p in paths:
        try:
            os.remove(p)
            n += 1
        except OSError:
            pass
    return n


def sweep_rows(store, cfg):
    s, out = store, {}
    if cfg["events_days"]:
        out["events"] = _rows(s, "DELETE FROM events WHERE ts<?", (_before(cfg["events_days"]),))
    if cfg["events_per_agent"]:
        n = 0
        for r in s.q("SELECT agent, COUNT(*) c FROM events GROUP BY agent HAVING c>?", (cfg["events_per_agent"],)):
            n += _rows(s, "DELETE FROM events WHERE agent IS ? AND id<=(SELECT id FROM events WHERE agent IS ? "
                          "ORDER BY id DESC LIMIT 1 OFFSET ?)", (r["agent"], r["agent"], cfg["events_per_agent"]))
        out["events"] = out.get("events", 0) + n
    if cfg["messages_days"]:
        out["messages"] = _rows(s, f"DELETE FROM messages WHERE state IN ({','.join('?' * len(MESSAGES_DONE))}) AND "
                                   "COALESCE(finished_at, created_at)<?", (*MESSAGES_DONE, _before(cfg["messages_days"])))
    if cfg["notifications_days"]:
        out["notifications"] = _rows(s, "DELETE FROM notifications WHERE created_at<? AND "
                                        "(acked_at IS NOT NULL OR level='info')", (_before(cfg["notifications_days"]),))
    if cfg["jobs_days"]:
        out["jobs"] = _rows(s, f"DELETE FROM jobs WHERE state IN ({','.join('?' * len(JOBS_ENDED))}) AND "
                               "COALESCE(finished_at, created_at)<?", (*JOBS_ENDED, _before(cfg["jobs_days"])))
    if cfg["broadcasts_days"]:
        out["broadcasts"] = _rows(s, "DELETE FROM broadcasts WHERE finished_at IS NOT NULL AND finished_at<?",
                                  (_before(cfg["broadcasts_days"]),))
    if cfg["notes_days"]:
        out["notes"] = _rows(s, """DELETE FROM notes WHERE created_at<? AND id NOT IN (
                                   SELECT id FROM (SELECT id, ROW_NUMBER() OVER (PARTITION BY project ORDER BY id DESC) k
                                                   FROM notes) WHERE k<=?)""",
                             (_before(cfg["notes_days"]), cfg["notes_keep"]))
    if cfg["memory_sessions"]:
        cited = sorted(memory.cited(s))  # an outline a project's memory still points at is kept whatever its age
        out["memory_sessions"] = _rows(s, f"""DELETE FROM memory_sessions WHERE state NOT IN ('pending','working') AND
                                              id NOT IN (SELECT id FROM (SELECT id, ROW_NUMBER() OVER (PARTITION BY project
                                              ORDER BY id DESC) k FROM memory_sessions) WHERE k<=?)
                                              AND id NOT IN ({','.join('?' * len(cited)) or 'NULL'})""",
                                       (cfg["memory_sessions"], *cited))
    return out


def sweep_files(store, cfg):
    s, out = store, {}
    if cfg["files_days"]:
        marks = ",".join("?" * len(MESSAGES_OPEN))
        open_turns = {r["turn_id"] for r in s.q(f"SELECT DISTINCT turn_id FROM messages WHERE turn_id IS NOT NULL AND "
                                                f"state IN ({marks})", MESSAGES_OPEN)}
        out["turns"] = _remove(os.path.join(TURNS, n) for n in _ls(TURNS)
                               if n.split(".", 1)[0] not in open_turns and _old(os.path.join(TURNS, n), cfg["files_days"]))
        pty = os.path.join(STATE, "pty")
        live = {a["id"] for a in s.agents()}
        by_agent = {}
        for n in _ls(pty):
            suf = next((x for x in PTY_SUFFIXES if n.endswith(x)), None)
            if suf:
                by_agent.setdefault(n[:-len(suf)], []).append(os.path.join(pty, n))
        gone = []
        for aid, paths in by_agent.items():
            if aid in live or not all(_old(p, cfg["files_days"]) for p in paths):
                continue
            try:
                with open(os.path.join(pty, aid + ".json")) as f:
                    holder = json.load(f).get("holder_pid")
            except (OSError, ValueError, AttributeError):
                holder = None
            if not (holder and discovery.pid_alive(holder)):
                gone += paths
        out["pty"] = _remove(gone)
    if cfg["messages_days"]:
        carried = set()
        for r in s.q("SELECT attachments FROM messages WHERE attachments IS NOT NULL AND attachments<>'[]'"):
            try:
                carried.update(json.loads(r["attachments"]))
            except ValueError:
                pass
        out["uploads"] = _remove(os.path.join(UPLOADS, n) for n in _ls(UPLOADS)
                                 if n.rsplit(".", 1)[0] not in carried and _old(os.path.join(UPLOADS, n), cfg["messages_days"]))
        helpers = os.path.join(STATE, "helpers")
        out["helpers"] = _remove(os.path.join(helpers, n) for n in _ls(helpers)
                                 if n.endswith(".md") and _old(os.path.join(helpers, n), cfg["messages_days"]))
    return out


def _ls(d):
    try:
        return os.listdir(d)
    except OSError:
        return []


def compact(store):
    """Hand freed pages back to the disk. VACUUM holds the hub's one connection for about a second per 50 MB, so it
    runs only when a quarter of the file is free."""
    with store.lock:
        pages = store.db.execute("PRAGMA page_count").fetchone()[0]
        free = store.db.execute("PRAGMA freelist_count").fetchone()[0]
        if pages and free / pages >= VACUUM_FREE and not store.db.in_transaction:
            store.db.execute("VACUUM")
        store.db.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        return {"pages": pages, "free_pages": free}


def sizes(store):
    def tree(d):
        n = b = 0
        for root, _, files in os.walk(d):
            for f in files:
                try:
                    b += os.path.getsize(os.path.join(root, f))
                    n += 1
                except OSError:
                    pass
        return {"files": n, "bytes": b}
    tables = ("events", "messages", "notifications", "jobs", "broadcasts", "notes", "memory_sessions", "decisions", "work")
    out = {"rows": {t: store.one(f"SELECT COUNT(*) n FROM {t}")["n"] for t in tables}, "folders": {}}
    for name in ("turns", "pty", "uploads", "inbox", "helpers"):
        out["folders"][name] = tree(os.path.join(STATE, name))
    db = store.one("PRAGMA database_list")["file"]
    out["hub_db_bytes"] = sum(os.path.getsize(p) for p in (db, db + "-wal") if os.path.exists(p))
    return out


def sweep(hub):
    cfg = settings(hub.s)
    removed = {**sweep_rows(hub.s, cfg), **sweep_files(hub.s, cfg)}
    last = {"at": now(), "removed": {k: v for k, v in removed.items() if v}}
    if any(removed.get(k) for k in ("events", "messages", "jobs", "notifications", "notes")):
        last["db"] = compact(hub.s)
    hub.s.set_setting("retention_last", last)
    return last


def view(hub):
    return {"settings": settings(hub.s), "last": hub.s.setting("retention_last"), "sizes": sizes(hub.s),
            "every_hours": EVERY_S // 3600}
