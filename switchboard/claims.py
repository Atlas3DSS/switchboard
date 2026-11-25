"""Are you sure? The hub checks what an agent claims against what is there.

The failure this answers: agents that run placeholder scripts of a few lines and report them 'launched', or present
a mock checker's hard-coded PASS as proof and cite files that do not exist, caught only when someone opens the files
by hand.

A claim is a message that says something is done (launched, done, complete, PASS, verified ...) and names things the
hub can look at:

  a job id         the job exists, is running or ended with exit 0, and the script it runs is not a stub
  a file path      it exists and is not empty, on this machine or on a host the agent has run jobs on

Two places are watched:

  sb send          an agent's message to another agent. For a watched agent the message is still delivered, never
                   held back, with one line under it saying what the hub found; when something did not hold, the
                   sender gets "Are you sure?" with the specific failures.
  the last reply   a watched terminal agent at rest whose final reply makes such a claim (this is what the owner
                   reads). When something did not hold, the same "Are you sure?" is typed to it, at most once per
                   ASK_EVERY_S, and never about its answer to a question the hub just asked.

The attest box. An agent with the box ticked (every watched agent, unless the owner
unticks it: the "attest" box on its pane, `sb trust --require / --release`) must run `sb attest` before it reports
anything as done: it states the claim and names its evidence (jobs, files, lines quoted from those files), the hub
checks each one, and either asks "Are you sure?" with what did not hold or hands back a receipt id. Every request
the hub delivers to such an agent ends with one line saying so, and a completion from it that carries no valid
receipt, in a message or in its final reply, is answered with "Are you sure? Run sb attest".

Watched agents: setting "claims" {"enabled", "providers", "agents", "attest_off", "attest_on"}; by default every
Antigravity agent. Every checked
claim, watched or not, goes in the ledger (table claims), which `sb trust` reads: per agent, how many claims, how many
did not hold, how many times it was asked. The hub never blocks a message and never edits what an agent wrote.
"""
import hashlib
import json
import os
import re
import shlex
import threading
import time

from . import hosts
from .store import epoch, now

DEFAULTS = {"enabled": True, "providers": ["antigravity"], "agents": [], "attest_off": [], "attest_on": [],
            "attest_watched": False}
# "It cannot be done" is a claim too: an agent asked what it ran when it first said so often turns out not to have
# tried.
CANNOT = re.compile(r"\b(impossible|not possible|cannot be (done|read|accessed|shown|surfaced|retrieved)|"
                    r"does not (exist|expose|provide|support|report)|do(es)? not have (access|the ability)|"
                    r"no (api|way|telemetry|mechanism|means) (to|for|is|exists)|outside (of )?my|"
                    r"is not (exposed|available|supported|accessible))\b", re.I)
CANNOT_ASK = (f"[sb: Are you sure? You said something cannot be done, is not available or does not exist. What did you "
              "run to establish that? Run the tool's --help, list its subcommands and slash commands, search its "
              "docs and changelog, and try it. Then either do the thing, or list exactly what you ran and what each "
              "printed.]")
RECEIPT = re.compile(r"\batt-[0-9a-f]{8}\b")
RECEIPT_S = 6 * 3600   # a receipt backs a report made within this long of it
ATTESTS = """CREATE TABLE IF NOT EXISTS attests (
  id TEXT PRIMARY KEY, agent TEXT, project TEXT, at TEXT, claim TEXT, ok INTEGER, checks INTEGER, failed INTEGER,
  detail TEXT)"""
FOOTER = ("[sb: before you report any of this as done, launched, fixed or verified, run `sb attest --claim 'what "
          "you are reporting' --job JOB_ID --file PATH --quote PATH 'exact text from that file'` (as many of each as "
          "apply) and put the receipt id it prints (att-...) in your report. A report with no receipt is sent back.]")
ASK_EVERY_S = 600      # one "are you sure" per agent in this long, from the reply watcher
QUIET_S = 20           # the terminal is at rest this long before its reply is read
FRESH_S = 300          # a reply older than this is never asked about
MAX_CHECKS = 8         # things looked at per message
REMOTE_TIMEOUT_S = 8
SCHEMA = """CREATE TABLE IF NOT EXISTS claims (
  id INTEGER PRIMARY KEY AUTOINCREMENT, agent TEXT, project TEXT, at TEXT, via TEXT, checks INTEGER, failed INTEGER,
  asked INTEGER DEFAULT 0, detail TEXT)"""

CLAIM = re.compile(r"\b(launched|started|is running|now running|done|completed?|finished|pass(?:ed|es)?|verified|"
                   r"confirmed|succe(?:ss|ssful|ssfully|eded)|fixed|implemented|written|wrote|created|saved|generated|"
                   r"produced)\b", re.I)
JOB = re.compile(r"\bjob-\d{10}-[0-9a-f]{6}\b")
ABS = re.compile(r"(?<![\w.:/~-])(~?/(?:[\w.@+-]+/)+[\w.@+-]+)")
STOPPING = re.compile(r"\b(stopp(?:ed|ing)|cancel(?:l?ed|ling)|kill(?:ed|ing)|halt(?:ed|ing)|abort(?:ed|ing)|removed|"
                      r"withdr(?:ew|aw(?:n|ing))|retract(?:ed|ing)|dropp(?:ed|ing)|pulled|"
                      r"took (?:it )?down|shut (?:it )?down)\b", re.I)
STOPPING_NEG = re.compile(
    r"\b(?:not|never|without|isn['’]?t|wasn['’]?t|aren['’]?t|weren['’]?t|didn['’]?t|hasn['’]?t|haven['’]?t)"
    r"(?:\s+(?:yet|been|being|actually|already|[a-z]+(?:ed|ing)?|or|and|nor)){0,4}\s*$",
    re.I,
)
JOB_JOIN = re.compile(r"^\s*(?:,|and|&|or)\s*$", re.I)
QUEUING = re.compile(r"\b(queu(?:ed|ing)|queueing|submitted|pending|waiting(?:\s+(?:for|on|behind))?|in\s+(?:the\s+)?queue)\b", re.I)
REPORTING_FAIL = re.compile(r"\b(fail(?:ed|ure|ing)?|crash(?:ed|ing)?|oom(?:ed)?|exit(?:ed)?\s+(?:code\s+)?[1-9]\d*|died)\b", re.I)
STUB = re.compile(r"placeholder|\bmock|simulat|\bdummy\b|\bfake\b|hard-?coded", re.I)
# Real host roots only: avoids treating HTTP/web routes (`/api/...`, `/vr/cut`, `/findings/...`) as root disk paths
DISK_ROOTS = ("/home/", "/mnt/", "/tmp/", "/var/", "/opt/", "/etc/", "/usr/", "/srv/", "/root/",
              "/run/", "/media/", "/boot/", "/lib/", "/bin/", "/sbin/", "/Users/", "/data/", "/scratch/", "/workspace/")
DELETED = re.compile(r"\b(remov(?:ed|ing)|delet(?:ed|ing)|unlink(?:ed|ing)?|purg(?:ed|ing)|prun(?:ed|ing)|"
                     r"dropp(?:ed|ing)|trash(?:ed|ing)|clean(?:ed|ing)\s+up|ripp(?:ed|ing)\s+out|rm)\b", re.I)
DELETED_AFTER = re.compile(r"^\s*[\]`)\"'*]*\s*(?:(?:was|were|is|are|has\s+been|have\s+been|now)\s+"
                           r"(?:(?:intentionally|cleanly|permanently|now|already)\s+)*"
                           r"(?:removed|deleted|unlinked|purged|pruned|dropped|trashed|cleaned\s+up|gone)|"
                           r"(?:(?:was|were|is|are|has\s+been|have\s+been)\s+)?"
                           r"(?:\*{0,2})not(?:\*{0,2})\s+(?:yet\s+)?(?:created|written|generated|saved|present|found|produced|touched|modified|exists?))\b", re.I)
NOT_CREATED_BEFORE = re.compile(r"\b(?:not\s+(?:creating|writing|touching|modifying)|"
                                r"without\s+(?:creating|writing|touching|modifying)|no\s+file\s+at)\s*[\`(\"'*]*$", re.I)
FUTURE_BEFORE = re.compile(r"\b(?:(?:will|to|going\s+to)\s+(?:write|save|output|create|generate|produce|land|go)(?:\s+\w+){0,3}\s*(?:to|into|at)?|"
                           r"(?:planned|target|future|upcoming|pending)\s+(?:output|file|path|dir|artifact))\s*[\`(\"'*]*$", re.I)
IN_PROGRESS = re.compile(r"\b(launch(?:ing|ed)|execut(?:ing|e)|starting|running|queu(?:ed|ing)|queueing|staging|"
                         r"converting|baking|pass\s+over)\b", re.I)
DONE_OUTPUT = re.compile(r"\b(wrote|written|saved|created|generated|produced|finished|completed|done|synced|copied|verified)\b", re.I)
ARROW_INTO = re.compile(r"(?:->|→|\binto)\s*[\`(\"'*]*$", re.I)
KEPT_VERB = re.compile(r"\b(creat(?:ed|ing)|wr(?:ote|itten|iting)|sav(?:ed|ing)|fix(?:ed|ing)|updat(?:ed|ing)|"
                       r"edit(?:ed|ing)|add(?:ed|ing)|generat(?:ed|ing)|produc(?:ed|ing)|install(?:ed|ing)|"
                       r"rewr(?:ote|itten)|kept|keep(?:ing)?|restor(?:ed|ing)|verified|confirmed)\b", re.I)
FROM_CONTAINER = re.compile(r"\b(?:from|in|inside|out\s+of)\s*[\`(\"'*]*$", re.I)
CLAUSE_END = re.compile(r"(?:\.(?:\s|$)|[;\n!?|])")
ASKED = "[sb: Are you sure?"


def settings(store):
    cfg = dict(DEFAULTS)
    cfg.update(store.setting("claims", {}) or {})
    return cfg


def watched(store, agent):
    cfg = settings(store)
    return bool(cfg["enabled"]) and bool(agent) and not agent.get("ghost") and (
        agent.get("provider") in cfg["providers"] or agent.get("id") in cfg["agents"])


def required(store, agent):
    """Does this agent have the attest box ticked? Opt-in per agent (attest_on), or all watched agents when
    attest_watched is enabled (minus attest_off)."""
    cfg = settings(store)
    if not cfg["enabled"] or not agent or agent.get("ghost") or agent.get("provider") == "shell":
        return False
    aid = agent.get("id")
    return aid in cfg["attest_on"] or (bool(cfg.get("attest_watched")) and watched(store, agent) and aid not in cfg["attest_off"])


def set_required(store, agent, on):
    cfg = settings(store)
    aid = agent["id"]
    cfg["attest_on"] = [a for a in cfg["attest_on"] if a != aid]
    cfg["attest_off"] = [a for a in cfg["attest_off"] if a != aid]
    default_on = bool(cfg.get("attest_watched")) and watched(store, agent)
    if on and not default_on:
        cfg["attest_on"].append(aid)
    if not on and default_on:
        cfg["attest_off"].append(aid)
    store.set_setting("claims", cfg)
    return {"agent": aid, "attest": required(store, agent)}


def receipt(store, agent, text):
    """The valid receipt a message carries: one of this agent's own passed attests, recent enough. Or None."""
    store.x(ATTESTS)
    since = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() - RECEIPT_S))
    for rid in RECEIPT.findall(text or ""):
        if store.one("SELECT 1 n FROM attests WHERE id=? AND agent=? AND ok=1 AND at>=?", (rid, agent["id"], since)):
            return rid
    return None


def _has(host, path, text):
    """Is this exact text in that file on that host? None when the host did not answer."""
    script = f"if grep -qF -- {shlex.quote(text)} {shlex.quote(path)} 2>/dev/null; then echo yes; else echo no; fi\n"
    rc, out, err = hosts.run_on(host, script, timeout=REMOTE_TIMEOUT_S)
    return True if "yes" in out.split() else False if "no" in out.split() else None


def attest(hub, agent, b, facts=None, has=None):
    """sb attest: an agent states a claim and its evidence, the hub checks each piece. -> {"ok", "receipt",
    "checks", "say"}; `say` is what the agent is shown: the receipt, or "Are you sure?" with what did not hold."""
    s, facts, has = hub.s, facts or _facts, has or _has
    s.x(ATTESTS)
    claim = " ".join((b.get("claim") or "").split())[:600]
    if not claim:
        raise ValueError("--claim is required: one sentence saying what you are reporting as done")
    jobs = [j for j in b.get("jobs") or [] if j]
    files = [os.path.expanduser(f) if f.startswith(("/", "~")) else os.path.normpath(os.path.join(agent.get("cwd") or "", f))
             for f in b.get("files") or [] if f]
    quotes = [(os.path.expanduser(p) if p.startswith(("/", "~")) else os.path.normpath(os.path.join(agent.get("cwd") or "", p)),
               t) for p, t in b.get("quotes") or [] if p and t]
    if not (jobs or files or quotes):
        say = (f"{ASKED} You named nothing that can be checked. Name the job (--job), the file that holds the "
               "result (--file), or quote the exact line that proves it (--quote PATH 'text'). If there is nothing "
               "to name, the work is not done: say so instead of reporting it.]")
        s.x("INSERT INTO attests(id,agent,project,at,claim,ok,checks,failed,detail) VALUES(?,?,?,?,?,0,0,0,?)",
            ("att-" + os.urandom(4).hex(), agent["id"], agent.get("project"), now(), claim, "nothing named"))
        return {"ok": False, "receipt": None, "checks": [], "say": say}
    text = " ".join(jobs + files + [p for p, _ in quotes])
    checks = verify(hub, {**agent, "cwd": None}, text, facts, intent=claim)
    seen = {c["what"] for c in checks}
    checks += [{"what": j, "ok": False, "why": "not a job id (they look like job-1118193748-e31456)"}
               for j in jobs if j not in seen]
    cfg = hosts.load_config()
    local = next(h for h in cfg["hosts"] if h["ssh"] is None)
    byname = {h["name"]: h for h in cfg["hosts"]}
    where = {c["what"]: byname.get(c["why"].rsplit(" on ", 1)[-1], local) for c in checks if c["ok"] and " on " in c["why"]}
    for p, t in quotes[:MAX_CHECKS]:
        found = has(where.get(p, local), p, t)
        short = t if len(t) <= 60 else t[:57] + "..."
        if found:
            checks.append({"what": f"{p} contains '{short}'", "ok": True, "why": "yes"})
        elif found is False:
            checks.append({"what": f"{p} contains '{short}'", "ok": False,
                           "why": "that text is not in the file" if any(c["what"] == p and c["ok"] for c in checks)
                           else "that text is not there (or the file is not)"})
    failed = [c for c in checks if not c["ok"]]
    rid = "att-" + os.urandom(4).hex()
    s.x("INSERT INTO attests(id,agent,project,at,claim,ok,checks,failed,detail) VALUES(?,?,?,?,?,?,?,?,?)",
        (rid, agent["id"], agent.get("project"), now(), claim, int(not failed), len(checks), len(failed),
         json.dumps(failed)[:4000]))
    if failed:
        bad = "\n".join(f"- {c['what']}: {c['why']}" for c in failed)
        say = (f"{ASKED} You are about to report: {claim}\nThis did not hold:\n{bad}\nNo receipt. Do not report "
               "it. Look at each item, fix what is wrong or make what is missing, then run sb attest again. (If what "
               "you are reporting is that you stopped a job, say 'stopped' in --claim: a stopped job then counts.)]")
        return {"ok": False, "receipt": None, "checks": checks, "say": say}
    good = "\n".join(f"- {c['what']}: {c['why']}" for c in checks)
    return {"ok": True, "receipt": rid, "checks": checks,
            "say": f"Checked:\n{good}\nAre you sure this is everything the request asked for? If yes, put this "
                   f"receipt in your report: {rid}"}


def _skip_path_in_clause(text, start, end):
    """True when the path at text[start:end] should not be required to exist yet: deleted/removed, renamed away,
    explicitly reported as not created, or a planned future output of an in-progress launch."""
    chunk = text[max(0, start - 160):start]
    cuts = [m.end() for m in CLAUSE_END.finditer(chunk)]
    before = chunk[cuts[-1]:] if cuts else chunk
    after = text[end:min(len(text), end + 80)]
    if DELETED_AFTER.search(after) or NOT_CREATED_BEFORE.search(before) or FUTURE_BEFORE.search(before):
        return True
    if ARROW_INTO.search(before) and IN_PROGRESS.search(before) and not DONE_OUTPUT.search(before):
        return True
    if re.search(r"\b(?:renamed|moved|mv)\b", before, re.I) and re.match(r"^\s*[\]`)\"']*\s*(?:->|→|to\b)", after, re.I):
        return True
    dels = list(DELETED.finditer(before))
    if dels:
        tail = before[dels[-1].end():]
        if not KEPT_VERB.search(tail) and not FROM_CONTAINER.search(tail):
            return True
    return False


def named(text, cwd=None):
    """(job ids, file paths) a message names, in order, without repeats. Only explicit absolute (under a real host
    root) or ~/... paths are extracted from prose — relative backticked names are left to explicit `sb attest`, so
    cross-repo/subfolder quotes never false-positive against cwd. Deleted/uncreated/planned outputs are skipped."""
    text = text or ""
    jobs = list(dict.fromkeys(JOB.findall(text)))
    paths = []
    for m in ABS.finditer(text):
        raw = m.group(1).rstrip(".,;:)")
        p = os.path.expanduser(raw)
        end = m.start(1) + len(raw)
        if p.startswith(DISK_ROOTS) and "/job-" not in p and ".." not in p and not _skip_path_in_clause(text, m.start(1), end):
            paths.append(p)
    return jobs, list(dict.fromkeys(paths))


def _facts(host, path):
    """{'exists', 'size', 'lines', 'stub'} for a file on a host, or None when the host did not answer."""
    q = shlex.quote(path)
    script = (f"if [ -e {q} ]; then echo exists; if [ -d {q} ]; then echo dir; else stat -c 'size %s' {q}; "
              f"echo lines $(head -c 200000 {q} | wc -l); "
              f"echo stub $(head -c 200000 {q} | grep -ciE 'placeholder|mock|simulat|dummy|hard-?coded'); fi; "
              "else echo missing; fi\n")
    rc, out, err = hosts.run_on(host, script, timeout=REMOTE_TIMEOUT_S)
    if "missing" in out.split():
        return {"exists": False}
    if "exists" not in out.split():
        return None
    f = {"exists": True, "dir": "dir" in out.split(), "size": 0, "lines": 0, "stub": 0}
    for k in ("size", "lines", "stub"):
        m = re.search(rf"^{k} (\d+)", out, re.M)
        if m:
            f[k] = int(m.group(1))
    return f


def _size(n):
    return f"{n} B" if n < 2048 else f"{n / 1024:.0f} KB" if n < 2 << 20 else f"{n / 1048576:.1f} MB"


def _script(argv):
    """The script a job runs: the first argument that is a .py or .sh file path."""
    return next((a for a in argv if re.search(r"\.(py|sh)$", a) and "/" in a), None)


def _job_clause(text, jid):
    """Clause around jid: stops at sentence/clause breaks and unrelated job ids (preserving 'stopped job-A and job-B')."""
    text = text or ""
    at = text.find(jid)
    if at < 0:
        return ""
    end = at + len(jid)
    before = text[max(0, at - 120):at]
    cuts = [m.end() for m in CLAUSE_END.finditer(before)]
    if cuts:
        before = before[cuts[-1]:]
    for m in reversed(list(JOB.finditer(before))):
        if m.group(0) != jid and not JOB_JOIN.match(before[m.end():]):
            before = before[m.end():]
            break
    after = text[end:min(len(text), end + 160)]
    c = CLAUSE_END.search(after)
    if c:
        after = after[:c.start()]
    for m in JOB.finditer(after):
        if m.group(0) != jid and not JOB_JOIN.match(after[:m.start()]):
            after = after[:m.start()]
            break
    return before + jid + after


def _claims_stopped(ctx):
    """True when ctx asserts a job was stopped/withdrawn (ignoring negated phrases like 'not stopped' / 'not ended or withdrawn')."""
    for m in STOPPING.finditer(ctx or ""):
        if not STOPPING_NEG.search((ctx or "")[max(0, m.start() - 60):m.start()]):
            return True
    return False


def verify(hub, agent, text, facts=_facts, intent=None):
    """[{'what', 'ok', 'why'}] for the jobs and files a message names. Nothing named: an empty list. `intent`: the
    claim the named things are evidence for (sb attest), read for whether a job is reported as stopped."""
    cfg = hosts.load_config()
    local = next(h for h in cfg["hosts"] if h["ssh"] is None)
    byname = {h["name"]: h for h in cfg["hosts"]}
    jobs, paths = named(text, agent.get("cwd"))
    out = []
    for jid in jobs[:MAX_CHECKS]:
        j = hub.s.one("SELECT * FROM jobs WHERE id=?", (jid,))
        if not j:
            out.append({"what": jid, "ok": False, "why": "no such job"})
            continue
        state, code = j["state"], j.get("exit_code")
        at = (text or "").find(jid)
        ctx = intent if intent is not None else _job_clause(text, jid)
        # Local clause around this job id (for queued/failed notices so an honest status update is not flagged)
        local_ctx = intent if intent is not None else (text or "")[max(0, at - 60):at + 80]
        # "I stopped/withdrew job X" is a true report of a stopped job: the words around the id say which claim it is
        stopping = _claims_stopped(ctx)
        if state == "stopped" and stopping:
            out.append({"what": jid, "ok": True, "why": "stopped" + (f" ({j['reason']})" if j.get("reason") else "")})
            continue
        if stopping and state in ("running", "queued", "stopping"):
            out.append({"what": jid, "ok": False, "why": f"is still {state}, not stopped"})
            continue
        if state in ("failed", "stopped") or (state == "finished" and code not in (0, None)):
            if state == "failed" and REPORTING_FAIL.search(local_ctx):
                out.append({"what": jid, "ok": True, "why": f"failed (reported)"})
                continue
            out.append({"what": jid, "ok": False, "why": f"{state}" + (f", exit {code}" if code is not None else "")
                        + (f": {j['reason']}" if j.get("reason") else "")})
            continue
        if state == "queued":
            if QUEUING.search(local_ctx):
                out.append({"what": jid, "ok": True, "why": "queued" + (f" ({j['reason']})" if j.get("reason") else "")})
                continue
            out.append({"what": jid, "ok": False, "why": "still queued, not started"
                        + (f": {j['reason']}" if j.get("reason") else "")})
            continue
        why = f"{state} on {j.get('host') or '?'}" + (", exit 0" if state == "finished" else "")
        script, h = _script(json.loads(j.get("argv") or "[]")), byname.get(j.get("host"))
        if script and h:
            if not script.startswith("/"):
                script = os.path.join(j.get("cwd") or "", script)
            f = facts(h, script)
            if f and f.get("exists") and (f["stub"] or f["lines"] <= 5):
                out.append({"what": jid, "ok": False, "why": f"{why}, but its script {script} is {f['lines']} lines"
                            + (" and reads like a stub (placeholder/mock/simulated)" if f["stub"] else "")})
                continue
        out.append({"what": jid, "ok": True, "why": why})
    others = [byname[r["host"]] for r in hub.s.q(
        "SELECT DISTINCT host FROM jobs WHERE agent=? AND host IS NOT NULL ORDER BY created_at DESC LIMIT 3",
        (agent["id"],)) if r["host"] in byname and byname[r["host"]] is not local]
    seen_hosts = {local["name"]} | {h["name"] for h in others}
    for name, h in byname.items():  # also check any fleet host explicitly named in the message (e.g. "synced to gpu-box")
        if name not in seen_hosts and re.search(rf"\b{re.escape(name)}\b", text or "", re.I):
            others.append(h)
            seen_hosts.add(name)
    for p in paths[:max(0, MAX_CHECKS - len(out))]:
        tried, found = [], None
        for h in [local] + others:
            f = facts(h, p)
            if f is None:
                continue
            tried.append(h["name"])
            if f.get("exists"):
                found = (h, f)
                break
        if not tried:
            continue  # no host answered: nothing is known about it
        if not found:
            out.append({"what": p, "ok": False, "why": "not found on " + ", ".join(tried)})
        elif not found[1].get("dir") and not found[1]["size"]:
            out.append({"what": p, "ok": False, "why": f"is empty (0 bytes) on {found[0]['name']}"})
        else:
            f = found[1]
            out.append({"what": p, "ok": True, "why": ("a folder" if f.get("dir") else _size(f["size"]))
                        + f" on {found[0]['name']}"})
    return out


def record(store, agent, via, checks, asked=False):
    store.x(SCHEMA)
    store.x("INSERT INTO claims(agent,project,at,via,checks,failed,asked,detail) VALUES(?,?,?,?,?,?,?,?)",
            (agent["id"], agent.get("project"), now(), via, len(checks), sum(not c["ok"] for c in checks),
             int(asked), json.dumps([c for c in checks if not c["ok"]])[:4000]))


def question(checks):
    bad = "\n".join(f"- {c['what']}: {c['why']}" for c in checks if not c["ok"])
    return (f"{ASKED} Switchboard looked at what your last message claims, and this did not hold:\n{bad}\n"
            "Do not restate the claim. Look at each item yourself now (open the file; `sb jobs show <id> --log 40`), "
            "then say plainly which claims were wrong and what is actually true. If the thing does not exist yet, "
            "say so, then make it and check it before you report again.]")


NO_RECEIPT = (f"{ASKED} Your last message reports work as done, launched, fixed or verified, and carries no attest "
              "receipt. Run `sb attest --claim 'what you are reporting' --job JOB_ID --file PATH --quote PATH 'exact "
              "text from that file'` with the evidence for it. If it passes, report again with the receipt id. If "
              "it does not, say what is actually true. If you were not reporting anything as done, say so in one "
              "line and carry on.]")


def stamp(checks):
    ok = "; ".join(f"{c['what']} {c['why']}" for c in checks if c["ok"])
    bad = "; ".join(f"{c['what']} {c['why']}" for c in checks if not c["ok"])
    if bad:
        return ("[hub-checked, NOT confirmed: " + bad + (". Confirmed: " + ok if ok else "")
                + ". The sender has been asked 'are you sure?'.]")
    return "[hub-checked: " + ok + "]"


def on_send(hub, sender, text):
    """Called by hub.send for an agent's message. Returns the text to deliver (the same text, with one line under
    it for a watched sender whose message names something checkable)."""
    try:
        if not sender or not text or not CLAIM.search(text) or text.startswith(ASKED):
            return text
        checks = verify(hub, sender, text)
        mine, must = watched(hub.s, sender), required(hub.s, sender)
        rid = receipt(hub.s, sender, text) if must else None
        if not checks and not (must and not rid):
            return text
        failed = [c for c in checks if not c["ok"]]
        if checks or must:
            record(hub.s, sender, "send", checks, asked=bool((mine and failed) or (must and not rid)))
        if not (mine or must):
            return text
        h = hashlib.sha1(text.encode()).hexdigest()[:12]
        if failed:
            hub.s.enqueue(sender["id"], question(checks), frm="system", idem=f"sure:{sender['id']}:{h}")
        elif must and not rid:
            hub.s.enqueue(sender["id"], NO_RECEIPT, frm="system", idem=f"sure:{sender['id']}:{h}")
        lines = [stamp(checks)] if checks else []
        if must:
            lines.append(f"[hub: attested, receipt {rid}]" if rid else
                         "[hub: this report carries no attest receipt; the sender has been asked 'are you sure?']")
        return text + "\n" + "\n".join(lines)
    except Exception as e:  # a check that breaks never costs a message
        hub.s.event(sender["id"] if sender else None, "error", f"claims: {type(e).__name__}: {e}")
        return text


def check_reply(terms, agent, t, hub=None, reply=None):
    """Called from the terminal tick for each live terminal: a watched agent at rest whose last reply makes a
    checkable claim that does not hold is asked 'are you sure?'. The looking is done off the delivery thread."""
    s, aid = terms.s, agent["id"]
    if agent.get("paused") or agent.get("gone") or aid in terms.switching or not watched(s, agent):
        return None
    seen = terms.stuck.setdefault("claims:" + aid, {})
    if seen.get("out") == t.last_output or seen.get("busy"):
        return None
    ready, busy, comp = t.view()
    if not ready or busy or comp or t.dialog():
        return None
    if t.steady_for() < QUIET_S or time.time() - (t.last_output or 0) < QUIET_S:
        return None
    if s.one("SELECT 1 n FROM messages WHERE to_agent=? AND state IN ('queued','delivering')", (aid,)):
        return None
    seen["out"] = t.last_output
    if time.time() - seen.get("asked", 0) < ASK_EVERY_S:
        return None

    def look():
        try:
            from . import discovery, transcript
            text = reply
            if text is None:
                path = discovery.transcript_path(agent["provider"], agent.get("session_id"), agent.get("account"))
                text, ts = transcript.last_reply(path, agent["provider"]) if path else ("", None)
                # only a reply just given: after a hub restart every idle terminal is looked at once, and an agent
                # must not be woken over something it said hours ago
                if not ts or time.time() - epoch(ts) > FRESH_S:
                    return
            key = hashlib.sha1((text or "").encode()).hexdigest()[:12]
            if not text or seen.get("reply") == key:
                return
            if CANNOT.search(text) and "--help" not in text:
                seen["reply"], seen["asked"] = key, time.time()
                s.event(aid, "system", "its reply says something cannot be done: asked what it ran to establish that")
                s.enqueue(aid, CANNOT_ASK, frm="system", idem=f"sure:{aid}:{key}")
                return
            if not CLAIM.search(text):
                return
            seen["reply"] = key
            checks = verify(hub or terms.hub, agent, text)
            failed = [c for c in checks if not c["ok"]]
            bare = required(s, agent) and not receipt(s, agent, text)
            if checks or bare:
                record(s, agent, "reply", checks, asked=bool(failed or bare))
            if bare and not failed:
                seen["asked"] = time.time()
                s.event(aid, "system", "its reply reports work as done with no attest receipt: asked 'are you sure?'")
                s.enqueue(aid, NO_RECEIPT, frm="system", idem=f"sure:{aid}:{key}")
            if failed:
                seen["asked"] = time.time()
                s.event(aid, "system", "its reply claims things that did not hold: asked 'are you sure?' ("
                        + "; ".join(f"{c['what']}: {c['why']}" for c in failed)[:300] + ")")
                s.enqueue(aid, question(checks), frm="system", idem=f"sure:{aid}:{key}")
        except Exception as e:
            s.event(aid, "error", f"claims: {type(e).__name__}: {e}")
        finally:
            seen["busy"] = False

    seen["busy"] = True
    th = threading.Thread(target=look, daemon=True)
    th.start()
    return th


def ledger(store, hours=168):
    """Per agent over the window: messages that named something checkable, how many had something that did not
    hold, how many times the agent was asked, and the newest failures."""
    store.x(SCHEMA)
    since = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() - float(hours) * 3600))
    rows = {}
    for r in store.q("SELECT * FROM claims WHERE at>=? ORDER BY at", (since,)):
        a = rows.setdefault(r["agent"], {"agent": r["agent"], "project": r["project"], "claims": 0, "failed": 0,
                                         "asked": 0, "things": 0, "things_failed": 0, "last_failed": None})
        a["claims"] += 1
        a["failed"] += 1 if r["failed"] else 0
        a["asked"] += r["asked"] or 0
        a["things"] += r["checks"] or 0
        a["things_failed"] += r["failed"] or 0
        if r["failed"]:
            a["last_failed"] = {"at": r["at"], "via": r["via"], "what": json.loads(r["detail"] or "[]")[:3]}
    store.x(ATTESTS)
    for r in store.q("SELECT agent, project, SUM(ok) passed, COUNT(*) n FROM attests WHERE at>=? GROUP BY agent", (since,)):
        a = rows.setdefault(r["agent"], {"agent": r["agent"], "project": r["project"], "claims": 0, "failed": 0,
                                         "asked": 0, "things": 0, "things_failed": 0, "last_failed": None})
        a["attests"], a["attests_failed"] = r["n"], r["n"] - (r["passed"] or 0)
    return {"hours": hours, "agents": sorted(rows.values(), key=lambda a: (-a["failed"], -a["claims"]))}
