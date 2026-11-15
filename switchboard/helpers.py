"""Temporary helpers: an agent may hand one narrow, independent task to a smaller model
beside it, with a deadline, and get the result back. Not a fork: the helper starts fresh on the parent's project,
folder and account and pulls context with `sb context` if it needs it. At most two per parent; helpers cannot start
helpers. When the helper answers, or when its deadline passes (one "time's up, report what you have" message, then a
short grace), the result is saved to STATE/helpers/<helper>.md (owner-only), sent to the parent once, and the helper
goes to 'retiring' until its CLI has really exited (retried every tick, across hub restarts); only then is it hidden.
"""
import json
import os
import threading
import time
import uuid

from .store import STATE, epoch, iso_from_epoch, now

MAX_PER_PARENT = 2
DEFAULT_MINUTES, MAX_MINUTES = 10, 60
GRACE_S = 120
RESULTS = os.path.join(STATE, "helpers")
SCHEMA = """CREATE TABLE IF NOT EXISTS helpers (
  id TEXT PRIMARY KEY, parent TEXT, task TEXT, model TEXT, task_msg TEXT, deadline TEXT, state TEXT,
  expire_msg TEXT, grace_until TEXT, result TEXT, outcome TEXT, created_at TEXT, finished_at TEXT)"""
LIVE = ("starting", "running", "expiring", "retiring")  # all count toward the limit until the CLI has exited
RANK = {"haiku": 1, "sonnet": 2, "opus": 3, "fable": 3}  # Claude sizes; anything unranked is refused


def rank(model):
    m = (model or "").lower()
    return next((r for name, r in RANK.items() if name in m), None)


def smaller_model(parent, asked=None):
    """A strictly smaller model of the parent's own CLI: the one asked for, if it is ranked below the parent's, else
    the next size down. Unranked models and other CLIs are refused rather than guessed."""
    if parent["provider"] != "claude":
        raise ValueError(f"helpers need a known smaller model; {parent['provider']} sizes are not ranked here")
    mine = rank(parent.get("model"))
    if mine is None:
        raise ValueError(f"the parent's model ({parent.get('model') or 'default'}) has no known size; set it first")
    if asked:
        r = rank(asked)
        if r is None:
            raise ValueError(f"{asked} is not a known Claude size (haiku, sonnet, opus)")
        if r >= mine:
            raise PermissionError(f"a helper must be smaller than its parent ({parent.get('model') or 'default'})")
        return asked
    if mine <= RANK["haiku"]:
        raise ValueError("a Haiku agent has no smaller Claude model; do the task yourself")
    return "claude-haiku-5-5" if mine == RANK["sonnet"] else "claude-sonnet-5-5"


def start(hub, b):
    s = hub.s
    s.x(SCHEMA)
    parent = s.agent(b.get("from") or "")
    if not parent or parent["gone"]:
        raise ValueError("helpers are started by an agent (sb helper inside its terminal)")
    if parent.get("role") == "helper":
        raise PermissionError("a helper cannot start helpers")
    task = (b.get("task") or "").strip()
    if not task:
        raise ValueError("task is required: one narrow, independent piece of work")
    minutes = max(1, min(int(b.get("minutes") or DEFAULT_MINUTES), MAX_MINUTES))
    model = smaller_model(parent, b.get("model"))
    hid = f"{parent['id'][:30]}-helper-{uuid.uuid4().hex[:6]}"  # unique: concurrent starts never clash
    deadline = time.time() + minutes * 60
    with s.tx():  # limit + reservation in one step; the row already names the helper, so a crash can be recovered
        live = s.one(f"SELECT COUNT(*) n FROM helpers WHERE parent=? AND state IN {LIVE}", (parent["id"],))["n"]
        if live >= MAX_PER_PARENT:
            raise PermissionError(f"{parent['id']} already has {live} helpers running (at most {MAX_PER_PARENT})")
        s.db.execute("INSERT INTO helpers(id,parent,task,model,deadline,state,created_at) VALUES(?,?,?,?,?,'starting',?)",
                     (hid, parent["id"], task, model, iso_from_epoch(deadline), now()))
    try:
        view = hub.create_agent({"id": hid, "project": parent["project"], "provider": parent["provider"],
                                 "account": parent["account"], "cwd": parent["cwd"],
                                 "title": f"helper of {parent['id']}", "model": model,
                                 "permission_mode": parent.get("permission_mode"), "effort": None})
        if view["id"] != hid:
            raise RuntimeError(f"helper id {hid} was taken")
    except Exception:
        if not s.agent(hid):
            s.x("DELETE FROM helpers WHERE id=?", (hid,))  # nothing was made: release the reservation
        raise
    publish(hub, s.one("SELECT * FROM helpers WHERE id=?", (hid,)))
    return {"helper": hid, "model": model, "deadline": iso_from_epoch(deadline), "project": parent["project"]}


def publish(hub, h):
    """Mark the helper as one (role), give it its task as a message from the parent, start its clock. Idempotent:
    a start that died half-way is finished by tick."""
    s, hid = hub.s, h["id"]
    s.set_agent(hid, role="helper", fork_from=json.dumps({"helper_of": h["parent"]}))
    deadline = epoch(h["deadline"])
    minutes = max(1, round((deadline - epoch(h["created_at"])) / 60))
    brief = (f"You are a temporary helper of agent '{h['parent']}', running {h['model']}. Do only this one task and "
             f"answer it in your final reply; that reply is your result. You have until "
             f"{time.strftime('%H:%M', time.localtime(deadline))} ({minutes} min); at the deadline you will be asked "
             "to report what you have and then stopped. Do not start helpers, do not message other agents unless the "
             "task says so, and do not change things outside the task. Pull context with `sb context "
             f"{h['parent']}` only if you need it.\n\nTask:\n{h['task']}")
    m, _ = s.enqueue(hid, brief, frm=h["parent"], idem=f"helper-task:{hid}")
    s.x("UPDATE helpers SET task_msg=?, state='running' WHERE id=? AND state='starting'", (m["id"], hid))
    s.event(h["parent"], "system", f"started helper {hid} ({h['model']}, {minutes} min): {h['task'][:200]}")


def tick(hub):
    s = hub.s
    s.x(SCHEMA)
    for h in s.q("SELECT * FROM helpers WHERE state='starting' AND created_at < ?",
                 (iso_from_epoch(time.time() - 120),)):  # a start that died half-way
        if s.agent(h["id"]):
            publish(hub, h)  # the agent exists: give it its task (once) and track it
        else:
            s.x("DELETE FROM helpers WHERE id=? AND state='starting'", (h["id"],))
    for h in s.q("SELECT * FROM helpers WHERE state IN ('running','expiring','retiring')"):
        try:
            _tick_one(hub, h)
        except Exception as e:  # one helper's trouble never stops the others
            s.event(h["id"], "error", f"helper lifecycle: {e}")


def _tick_one(hub, h):
    s, hid = hub.s, h["id"]
    if h["state"] == "retiring":
        return retire(hub, h)
    task = s.message(h["task_msg"]) or {}
    if task.get("state") == "done":
        return finish(hub, h, "done", task.get("reply") or "")
    if h["state"] == "running" and time.time() > epoch(h["deadline"]):
        m, _ = s.enqueue(hid, "Time is up. Stop now and reply with what you have: your findings so far, what is "
                              "unfinished, and anything the parent must not assume is done.",
                         frm=h["parent"], idem=f"helper-expire:{hid}")
        t = hub.t.term(hid)
        if t and t.view()[1]:
            t.write("\x1b")  # one Esc: the time's-up message lands at the prompt
        s.x("UPDATE helpers SET state='expiring', expire_msg=?, grace_until=? WHERE id=?",
            (m["id"], iso_from_epoch(time.time() + GRACE_S), hid))
        return
    if h["state"] == "expiring":
        late = s.message(h["expire_msg"]) or {}
        if late.get("state") == "done":
            return finish(hub, h, "expired", late.get("reply") or "")
        if time.time() > epoch(h["grace_until"]):
            a = s.agent(hid) or {}
            return finish(hub, h, "expired without a report", a.get("last_text") or "")


def finish(hub, h, how, reply):
    """Keep the result (owner-only file), tell the parent once, then retire."""
    s, hid = hub.s, h["id"]
    os.makedirs(RESULTS, mode=0o700, exist_ok=True)
    os.chmod(RESULTS, 0o700)
    path = os.path.join(RESULTS, f"{hid}.md")
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        f.write(f"# Helper {hid} of {h['parent']}: {how}\n\nModel: {h['model']} · started {h['created_at']} · "
                f"deadline {h['deadline']} · finished {now()}\n\n## Task\n\n{h['task']}\n\n## Result\n\n"
                f"{reply.strip() or '(no reply)'}\n")
    clip = reply.strip()[:3000] + (" …" if len(reply.strip()) > 3000 else "")
    s.enqueue(h["parent"], f"Helper {hid} ({h['model']}) {how}. Result kept in {path}\n\n{clip or '(no reply)'}",
              frm=hid, idem=f"helper-result:{hid}", is_reply=True)  # idem: told once, even if this runs again
    s.x("UPDATE helpers SET state='retiring', result=?, outcome=?, finished_at=? WHERE id=?", (path, how, now(), hid))
    s.event(h["parent"], "system", f"helper {hid} {how}; result in {path}")
    s.set_agent(hid, paused=1)  # nothing more is typed into it
    retire(hub, s.one("SELECT * FROM helpers WHERE id=?", (hid,)))


PROC = "/proc"


def _is(pid, start, argv=None, parent=None):
    """pid is still the very process the holder recorded: same start time (so not a reused pid), not a zombie,
    and, when given, the exact command line and parent."""
    from .ptyholder import start_ticks
    try:
        if start is None or start_ticks(pid, PROC) != int(start):
            return False
        with open(f"{PROC}/{int(pid)}/status") as f:
            status = f.read()
        if "\nState:\tZ" in status:
            return False
        if parent is not None and f"\nPPid:\t{int(parent)}\n" not in status:
            return False
        if argv is not None:
            with open(f"{PROC}/{int(pid)}/cmdline", "rb") as f:
                if f.read().split(b"\0")[1:-1] != argv:
                    return False
        return True
    except (OSError, TypeError, ValueError, IndexError):
        return False


def owned_pids(hub, hid):
    """[(pid, start, argv, parent)] for the helper's own live CLI and pty holder. The holder must be the process
    started for this helper (its exact command line names this helper's spec file, and its start time is the one in
    the state file); the CLI must be that holder's child with its recorded start time. A reused pid, or any other
    agent's holder, never counts."""
    t = hub.t.terms.get(hid)
    st = t.state if t else None
    if st is None:
        try:
            st = json.load(open(hub.t.paths(hid)["state"]))
        except (OSError, ValueError):
            return []
    holder = (st.get("holder_pid"), st.get("holder_start"),
              [b"-m", b"switchboard.ptyholder", hub.t.paths(hid)["spec"].encode()], None)
    if not _is(*holder):
        return []
    cli = (st.get("pid"), st.get("pid_start"), None, holder[0])
    return ([cli] if _is(*cli) else []) + [holder]


def _pin(who):
    """A pidfd for the process if it is still the one checked: the check runs on the pinned process, and from then
    on the handle can only ever reach that process, even if its pid is reused."""
    try:
        fd = os.pidfd_open(int(who[0]))
    except (OSError, TypeError, ValueError):
        return None
    if _is(*who):
        return fd
    os.close(fd)
    return None


def _gone(fd, wait_s):
    """Has the pinned process exited (its pidfd turns readable), waiting up to wait_s?"""
    import select
    return bool(select.select([fd], [], [], wait_s)[0])


def _end(fd, wait_s):
    """SIGTERM, then SIGKILL, to one pinned process; True once it has exited."""
    import signal
    for sig in (signal.SIGTERM, signal.SIGKILL):
        try:
            signal.pidfd_send_signal(fd, sig)
        except ProcessLookupError:
            return True
        except OSError:
            pass
        if _gone(fd, wait_s):
            return True
    return False


def cli_alive(hub, hid):
    """Is the helper's CLI (or its pty holder) still a live process? Not the same as the hub being connected to it."""
    return bool(owned_pids(hub, hid))


def shutdown(hub, hid, wait_s=5):
    """Exit the helper's CLI. With no connection to its terminal (a hub restart, or the holder dropped us), connect
    again first; if that fails, or /exit does not end it, end the helper's own processes: the CLI, then its holder."""
    t = hub.t.term(hid)
    if not t or getattr(t, "reconnecting", False):  # reconnecting: the hub's own retries are still in backoff
        try:
            t = hub.t._connect(hid)
        except (OSError, ValueError):
            t = None
    try:
        if t and hub.t.exit_cli(hid, True) and not cli_alive(hub, hid):
            return
    except (OSError, ConnectionError):
        pass
    # pin the CLI and its holder before signalling either: once the holder is gone the CLI could no longer be
    # proven the helper's, so the CLI is ended first, through its pin, and the holder only after it has exited
    pins = [(who, _pin(who)) for who in owned_pids(hub, hid)]
    try:
        for who, fd in pins:  # owned_pids lists the CLI first
            cli = who[3] is not None
            if fd is None:
                if cli and _is(*who):
                    return  # the CLI lives on but could not be pinned: leave its holder, the next tick retries
                continue
            if not _end(fd, wait_s) and cli:
                return  # the CLI outlived SIGKILL: its holder stays, so the next tick can still prove and retry it
    finally:
        for _, fd in pins:
            if fd is not None:
                os.close(fd)


def retire(hub, h):
    """Exit the helper's CLI; it is hidden and marked finished only once neither the CLI nor its holder is alive.
    One exit attempt at a time, a new one only after the last has returned; after a hub restart the stored state
    carries on."""
    s, hid = hub.s, h["id"]
    exits = hub.__dict__.setdefault("helper_exits", {})
    if exits.get(hid) and exits[hid].is_alive():
        return  # a shutdown is under way; it holds its own pins on the processes, so let it finish
    if cli_alive(hub, hid):
        exits[hid] = threading.Thread(target=shutdown, args=(hub, hid), daemon=True)
        exits[hid].start()
        return
    s.x("UPDATE helpers SET state=? WHERE id=?", ("done" if h.get("outcome") == "done" else "expired", hid))
    s.set_agent(hid, gone=1, paused=1)
    s.event(hid, "system", "helper retired")
