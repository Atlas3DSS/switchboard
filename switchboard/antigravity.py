"""Google Antigravity CLI (`agy`) as a Switchboard terminal agent: runs on the owner's Google AI subscription
(Pro or Ultra), not the Gemini API.

What agy keeps, under ~/.gemini/antigravity-cli (ANTIGRAVITY_HOME overrides, for a second account):
  history.jsonl            one line per prompt typed: {"display", "timestamp" (ms), "workspace", "conversationId"}
  brain/<cid>/.system_generated/logs/transcript.jsonl
                           one JSON line per step: USER_INPUT (content wraps the text in <USER_REQUEST>),
                           PLANNER_RESPONSE (model reply: content, tool_calls, model, input/output/cache tokens),
                           GENERIC (tool output; not shown)
  settings.json            {"trustedWorkspaces": [...]}: a folder not listed opens a trust question at launch

Same shapes as hermes.py: read_chat / last_reply / has_user_text / new_session / usage_stats / last_model / entries.
"""
import json
import os
import re
import time

from .store import epoch

CONTEXT_WINDOW = 1_000_000
NEON_CONTEXT_WINDOW = 512_000  # docs/models: Gemini 4 Neon context window is up to 512K tokens
TEXT_CAP = 20000
REQ = re.compile(r"<USER_REQUEST>\s*(.*?)\s*</USER_REQUEST>", re.S)


def context_window_for(model):
    """Max context tokens for an Antigravity model (Neon is 512K per docs/models; others 1M)."""
    return NEON_CONTEXT_WINDOW if "neon" in (model or "").lower() else CONTEXT_WINDOW


def session_settings(outside_access=False):
    """Per-session agy --settings JSON: keep panes quiet and aligned with Switchboard's workspace policy."""
    s = {"artifactReviewPolicy": "always-proceed", "showTips": False, "showFeedbackSurvey": False,
         "queuedMessages": "send-immediately"}
    if outside_access:
        s["allowNonWorkspaceAccess"] = True
    return s


def home(d=None):
    return os.path.expanduser(d or os.environ.get("ANTIGRAVITY_HOME") or "~/.gemini/antigravity-cli")


_labels = {}  # agy binary -> (read at, {slug: label})


def model_arg(model, exe="agy", now=None):
    """What to pass as --model. agy lists each model as a slug and a label ("gemini-pro-orchestrator",
    "Orchestrator (Carbon)") and accepts either, but the two are not the same request: given the slug of a model
    with no reasoning level in its name, agy 1.x asks the API for THINKING_LEVEL_XHIGH, which the API refuses
    ("Thinking level is unsupported"), and every turn of that terminal dies on a 400. The label, which
    is what agy's own /model picker and settings.json store, works. So a known slug is passed as its label; anything
    else goes through as given. The list is read from `agy models` and kept for a day."""
    import subprocess
    import time
    now = now or time.time()
    at, known = _labels.get(exe, (0, {}))
    if now - at > 86400:
        try:
            out = subprocess.run([exe, "models"], capture_output=True, text=True, timeout=20).stdout
            found = dict(l.split("\t", 1) for l in out.splitlines() if "\t" in l)
            if found:
                known = {k.strip(): v.strip() for k, v in found.items()}
                _labels[exe] = (now, known)
        except (OSError, subprocess.SubprocessError):
            pass
    if model.endswith("-xhigh") and model[:-len("-xhigh")] in NO_XHIGH:
        model = model[:-len("-xhigh")] + "-high"  # the API refuses XHigh for this model: every turn would die on a 400
    return known.get(model, model)


NO_XHIGH = set()  # model families (slug without its level) the API refused XHigh for; kept in setting "agy_no_xhigh"
XHIGH_REFUSED = "THINKING_LEVEL_XHIGH"


def high_for(model, screen="", exe="agy"):
    """The High variant of the XHigh model a terminal is on, as a slug: from the CLI's own "Model set to NAME
    (XHigh)" line when the owner picked it in /model (the hub never saw that change), else from the agent's model.
    None when neither names an XHigh model."""
    picked = re.findall(r"Model set to (.+?) \(XHigh\)", screen or "")
    if picked:
        model_arg("", exe)  # fills the label cache
        want = picked[-1].strip() + " (High)"
        return next((slug for slug, label in _labels.get(exe, (0, {}))[1].items() if label == want), want)
    if (model or "").endswith("-xhigh"):
        return model[:-len("-xhigh")] + "-high"
    return None


QUOTA_RE = re.compile(rb"^[IWE](\d\d)(\d\d) (\d\d):(\d\d):(\d\d)\.\d+ .*RESOURCE_EXHAUSTED \(code 429\): ([^\n]*?Resets in ((?:\d+h)?(?:\d+m)?(?:\d+s)?))\.", re.M)
_quota = {}  # home -> (read at, result)


def quota_hit(d=None, days=5, now=None):
    """The newest "quota reached" the CLI logged: {at, resets (epoch seconds), text}, or None. agy offers no way to
    read how much quota is left (its quota endpoint answers only the CLI itself), so the 429 in its log, which names
    the time to the reset, is the one reading there is. Cached for a minute."""
    import glob
    import time
    now = now or time.time()
    h = home(d)
    if now - _quota.get(h, (0, None))[0] < 60:
        return _quota[h][1]
    best = None
    for p in glob.glob(os.path.join(h, "log", "cli-*.log")):
        try:
            if now - os.path.getmtime(p) > days * 86400:
                continue
            data = open(p, "rb").read()
        except OSError:
            continue
        if b"RESOURCE_EXHAUSTED" not in data:
            continue
        for m in QUOTA_RE.finditer(data):
            mo, dd, hh, mi, ss = (int(x) for x in m.groups()[:5])
            lt = time.localtime(now)
            at = time.mktime((lt.tm_year, mo, dd, hh, mi, ss, 0, 0, -1))
            if at > now + 86400:  # a December line read in January
                at = time.mktime((lt.tm_year - 1, mo, dd, hh, mi, ss, 0, 0, -1))
            if best and at <= best["at"]:
                continue
            left = sum(int(n) * {"h": 3600, "m": 60, "s": 1}[u] for n, u in re.findall(r"(\d+)([hms])", m.group(7).decode()))
            best = {"at": at, "resets": at + left, "text": m.group(6).decode("utf-8", "replace")}
    _quota[h] = (now, best)
    return best


def path_for(cid, d=None):
    if not cid:
        return None
    return os.path.join(home(d), "brain", cid, ".system_generated", "logs", "transcript.jsonl")


def trust(cwd, d=None):
    """List cwd in trustedWorkspaces so the CLI opens straight to its prompt (Switchboard chose the folder)."""
    p = os.path.join(home(d), "settings.json")
    try:
        s = json.load(open(p))
    except (OSError, ValueError):
        s = {}
    real = os.path.realpath(cwd)
    have = s.get("trustedWorkspaces") or []
    if real in have or cwd in have:
        return False
    s["trustedWorkspaces"] = have + [real]
    os.makedirs(os.path.dirname(p), exist_ok=True)
    tmp = p + ".sb-tmp"
    with open(tmp, "w") as f:
        json.dump(s, f, indent=2)
    os.replace(tmp, p)
    return True


_hist_cache = {}  # path -> (mtime_ns, size, parsed_rows_newest_first)
_HIST_TAIL_BYTES = 512 << 10  # last 512 KB of history.jsonl easily holds >500 recent prompts


def _history_rows(d=None, limit=500):
    """Newest-first parsed JSON rows from the tail of history.jsonl, cached by (mtime_ns, size)."""
    path = os.path.join(home(d), "history.jsonl")
    try:
        st = os.stat(path)
    except OSError:
        return []
    key = (st.st_mtime_ns, st.st_size)
    cached = _hist_cache.get(path)
    if cached and cached[0] == key:
        return cached[1][:limit]
    try:
        with open(path, "rb") as f:
            if st.st_size > _HIST_TAIL_BYTES:
                f.seek(st.st_size - _HIST_TAIL_BYTES)
                f.readline()  # discard partial first line
            raw = f.read().decode("utf-8", "replace")
    except OSError:
        return []
    rows = []
    for line in reversed(raw.splitlines()):
        if not line:
            continue
        try:
            r = json.loads(line)
        except ValueError:
            continue
        if isinstance(r, dict):
            rows.append(r)
        if len(rows) >= 600:
            break
    _hist_cache[path] = (key, rows)
    return rows[:limit]


def new_session(cwd, since, taken=(), d=None):
    """The newest unclaimed conversation whose prompt was typed in cwd since launch (history.jsonl)."""
    real, taken = os.path.realpath(cwd), set(taken or ())
    for r in _history_rows(d, 500):
        cid, ws = r.get("conversationId"), r.get("workspace") or ""
        if not cid or cid in taken or (r.get("timestamp") or 0) / 1000 < (since or 0) - 5:
            continue
        if ws in (cwd, real) or os.path.realpath(ws) == real:
            return cid
    return None


def follow_session(cwd, current_sid, screen_text="", since=0, taken=(), d=None):
    """After /clear inside a running agy terminal: the newest unclaimed conversation in cwd whose prompt text
    appears on this terminal's screen, so two agents in the same folder never steal each other's session."""
    if not cwd or not screen_text:
        return None
    real, taken = os.path.realpath(cwd), set(taken or ())
    norm_screen = " ".join(screen_text.split())
    if not norm_screen:
        return None
    cur_mtime = 0.0
    if current_sid:
        cur_p = path_for(current_sid, d)
        if cur_p:
            try:
                cur_mtime = os.path.getmtime(cur_p)
            except OSError:
                pass
    for r in _history_rows(d, 200):
        cid, ws = r.get("conversationId"), r.get("workspace") or ""
        ts_s = (r.get("timestamp") or 0) / 1000
        if not cid or ts_s < (since or 0) - 5:
            continue
        if ws not in (cwd, real) and os.path.realpath(ws) != real:
            continue
        disp = " ".join((r.get("display") or "").split())
        if len(disp) < 8 or disp[:40] not in norm_screen:
            continue
        if cid == current_sid:
            return None  # the newest matching conversation on screen is already the current one (never bounce back)
        if cid in taken:
            continue
        if cur_mtime and ts_s < cur_mtime - 5:
            cand_p = path_for(cid, d)
            try:
                cand_mtime = os.path.getmtime(cand_p) if cand_p else 0.0
            except OSError:
                cand_mtime = 0.0
            if cand_mtime <= cur_mtime:
                continue  # never switch backward from an actively written conversation to an older one
        return cid
    return None


def _user_text(content):
    m = REQ.search(content or "")
    return (m.group(1) if m else (content or "")).strip()


def _tools(raw):
    """'run_command: ls -la' lines from a step's tool_calls (a list, or its Python repr)."""
    calls = raw
    if isinstance(raw, str):
        try:
            calls = json.loads(raw)
        except ValueError:
            return [f"tool: {raw[:200]}"]
    out = []
    for c in calls or []:
        if not isinstance(c, dict):
            continue
        args = c.get("args") or {}

        def arg(k):
            v = args.get(k)
            try:
                return json.loads(v) if isinstance(v, str) else v
            except ValueError:
                return v
        brief = arg("CommandLine") or arg("toolSummary") or arg("AbsolutePath") or arg("TargetFile") or ""
        out.append(" ".join(f"{c.get('name') or 'tool'}: {brief}".split())[:240])
    return out


def entries(line):
    """[(role, text, ts, n_images)] for one transcript line."""
    try:
        r = json.loads(line)
    except ValueError:
        return []
    if not isinstance(r, dict):
        return []
    ts, kind = r.get("created_at"), r.get("type")
    if kind == "USER_INPUT":
        text = _user_text(r.get("content"))
        return [("user", text, ts, 0)] if text else []
    if kind == "PLANNER_RESPONSE":
        out = [("assistant", r["content"].strip(), ts, 0)] if (r.get("content") or "").strip() else []
        return out + [("tool", t, ts, 0) for t in _tools(r.get("tool_calls"))]
    return []


_TAIL_BLOCK = 256 << 10


def _reverse_lines(path, max_bytes=32 << 20):
    """Yield non-empty transcript lines newest-first in bounded chunks from EOF (never loads whole 30+ MB files)."""
    if not path:
        return
    try:
        with open(path, "rb") as f:
            f.seek(0, 2)
            pos = f.tell()
            stop = max(0, pos - max_bytes)
            tail = b""
            while pos > stop:
                step = min(_TAIL_BLOCK, pos - stop)
                pos -= step
                f.seek(pos)
                chunk = f.read(step) + tail
                lines = chunk.split(b"\n")
                tail = lines.pop(0) if pos > 0 else b""
                for raw in reversed(lines):
                    s = raw.strip()
                    if s:
                        yield s.decode("utf-8", "replace")
                if pos == 0 and tail:
                    s = tail.strip()
                    if s:
                        yield s.decode("utf-8", "replace")
    except (OSError, TypeError):
        return


def _lines(path):
    try:
        with open(path, errors="replace") as f:
            return [l for l in (x.strip() for x in f) if l]
    except (OSError, TypeError):
        return []


def lines_newest_first(path, limit=5000):
    out = []
    for line in _reverse_lines(path):
        out.append(line)
        if len(out) >= limit:
            break
    return out


def read_chat(path, limit=40, before=None, after=None):
    """Same shape and paging as hermes.read_chat: ids are byte-or-line offsets."""
    limit = max(1, min(int(limit), 400))
    before, after = (int(before) if before is not None else None), (int(after) if after is not None else None)
    if before is None and after is None:
        try:
            size = os.path.getsize(path) if path else 0
        except OSError:
            size = 0
        if size > (1 << 20):
            collected = []
            for idx, line in enumerate(_reverse_lines(path), start=1):
                ents = entries(line)
                for k in range(len(ents) - 1, -1, -1):
                    role, text, ts, imgs = ents[k]
                    collected.append({"id": (size - idx) * 8 + k, "role": role, "text": text[:TEXT_CAP],
                                      "ts": ts, "n_images": imgs})
                    if len(collected) >= limit + 1:
                        break
                if len(collected) >= limit + 1:
                    break
            collected.reverse()
            has_more = len(collected) > limit
            out = collected[-limit:]
            return {"entries": out, "before": None if not has_more else out[0]["id"],
                    "latest": out[-1]["id"] if out else None}
    rows = []
    for n, line in enumerate(_lines(path), start=1):
        for k, (role, text, ts, imgs) in enumerate(entries(line)):
            i = n * 8 + k
            if (after is not None and i <= after) or (after is None and before is not None and i >= before):
                continue
            rows.append({"id": i, "role": role, "text": text[:TEXT_CAP], "ts": ts, "n_images": imgs})
    if after is not None:
        rows = rows[:2000]
        return {"entries": rows, "before": None, "latest": rows[-1]["id"] if rows else after}
    out = rows[-limit:]
    return {"entries": out, "before": out[0]["id"] if len(rows) > limit and out else None,
            "latest": out[-1]["id"] if out else None}


def last_reply(path):
    """Newest user-facing assistant reply from the tail of transcript.jsonl (reads backwards from EOF)."""
    parts, ts = [], None
    for line in _reverse_lines(path):
        for role, text, et, _ in reversed(entries(line)):
            if role == "assistant":
                if ts is None:
                    ts = et
                if text:
                    parts.append(text[:TEXT_CAP])
            elif parts:
                return "\n\n".join(reversed(parts))[:TEXT_CAP], ts
    if not parts:
        return "", None
    return "\n\n".join(reversed(parts))[:TEXT_CAP], ts


def last_model(path):
    for line in lines_newest_first(path):
        try:
            m = json.loads(line).get("model")
        except (ValueError, AttributeError):
            continue
        if m:
            return m
    return None


def read_at(path, needle, since_epoch=0.0):
    """Timestamp of the USER_INPUT record containing `needle` after since_epoch, or None."""
    if not needle:
        return None
    norm = " ".join(needle.split())[:80]
    for line in lines_newest_first(path, 200):
        try:
            r = json.loads(line)
        except ValueError:
            continue
        if r.get("type") != "USER_INPUT":
            continue
        ts = r.get("created_at") or ""
        if since_epoch and ts and epoch(ts) < since_epoch - 5:
            continue
        text = _user_text(r.get("content"))
        if needle in text or (norm and norm in " ".join(text.split())):
            return ts or True
    return None


def has_user_text(path, needle, since_epoch=0.0):
    return bool(read_at(path, needle, since_epoch))


def usage_stats(path, since_epoch=0.0):
    out = {"context_tokens": None, "context_window": None, "tokens_24h": 0, "last_turn": None}
    for line in lines_newest_first(path):
        try:
            r = json.loads(line)
        except ValueError:
            continue
        if r.get("type") != "PLANNER_RESPONSE":
            continue
        i, o, c = (int(r.get(k) or 0) for k in ("input_tokens", "output_tokens", "cache_read_tokens"))
        if out["context_tokens"] is None and i + c:
            out.update(context_tokens=i + c + o, context_window=context_window_for(r.get("model")),
                       last_turn={"input_tokens": i, "output_tokens": o, "cache_read": c, "cost_usd": None,
                                  "at": r.get("created_at")})
        if since_epoch and r.get("created_at") and epoch(r["created_at"]) < since_epoch:
            if out["context_tokens"] is not None:
                break
            continue
        out["tokens_24h"] += i + o + c
    return out


def parse_usage(text):
    """The lines of `agy -p "/usage"` (group TAB window TAB NN% remaining TAB reset time) as limits:
    [{"group", "window": "session" | "weekly", "left": % remaining, "resets": iso}]. Other lines are skipped."""
    out = []
    for line in (text or "").splitlines():
        parts = [x.strip() for x in line.split("\t")]
        if len(parts) < 4 or not re.fullmatch(r"\d+(\.\d+)?%", parts[2]):
            continue
        low = parts[1].lower()
        window = "weekly" if "week" in low else "session" if "hour" in low else low.replace(" ", "_")
        out.append({"group": parts[0], "window": window, "left": max(0, min(100, int(round(float(parts[2][:-1]))))),
                    "resets": parts[3]})
    return out


_usage = {}  # agy home -> (read at, limits or None)
USAGE_TTL_S = 300


def subscription_usage(d=None, now=None, run=None):
    """What is left on the owner's Google AI subscription, read from agy itself: `agy -p "/usage"` answers without a
    model turn. -> (limits as parse_usage gives them, epoch of the reading), or (None, None) when agy gave nothing.
    One reading is kept USAGE_TTL_S, a failed one too, so the account cards never wait on agy more than once."""
    now = time.time() if now is None else now
    h = home(d)
    at, got = _usage.get(h, (0, None))
    if h in _usage and now - at < USAGE_TTL_S:
        return (got, at) if got else (None, None)
    import subprocess
    import tempfile
    try:
        if run:
            text = run()
        else:
            env = dict(os.environ, ANTIGRAVITY_HOME=h) if d else None
            text = subprocess.run(["agy", "-p", "/usage"], capture_output=True, text=True, timeout=30,
                                  cwd=tempfile.gettempdir(), env=env).stdout
        got = parse_usage(text) or None
    except (OSError, ValueError, subprocess.SubprocessError):
        got = None
    _usage[h] = (now, got)
    return (got, now) if got else (None, None)


# ---------- rewind and edit (switchboard/rewind.py) ----------
# The conversation agy resumes from is not the transcript log but conversations/<cid>.db, one row per step:
#   steps(idx, step_type, step_payload ...)   protobuf; 14 = what the user typed, 15 = the model's reply, 101 = system
#   gen_metadata(idx, data)                   one per model call; field 2 = the step indices it produced (packed)
#   executor_metadata(idx, data)              one per run of the CLI; field 3 = the last step it reached
# The logs under brain/<cid>/.system_generated (transcript*.jsonl, their chunks/, steps/<idx>/, tasks/task-<idx>.log)
# are written from it. Only with the CLI down: it holds the conversation in memory and would write it back.
def db_for(cid, d=None):
    return os.path.join(home(d), "conversations", f"{cid}.db")


def _varint(b, i):
    n = s = 0
    while True:
        c = b[i]
        i += 1
        n |= (c & 0x7F) << s
        s += 7
        if not c & 0x80:
            return n, i


def _enc(n):
    out = bytearray()
    while True:
        c, n = n & 0x7F, n >> 7
        out.append(c | (0x80 if n else 0))
        if not n:
            return bytes(out)


def _fields(b):
    """[(field, wire type, value)] of one protobuf message; ValueError when b is not one."""
    i, out = 0, []
    try:
        while i < len(b):
            k, i = _varint(b, i)
            f, w = k >> 3, k & 7
            if not f:
                raise ValueError
            if w == 0:
                v, i = _varint(b, i)
            elif w == 2:
                n, i = _varint(b, i)
                v, i = b[i:i + n], i + n
            elif w in (1, 5):
                n = 8 if w == 1 else 4
                v, i = b[i:i + n], i + n
            else:
                raise ValueError
            if i > len(b):
                raise ValueError
            out.append((f, w, v))
    except IndexError:
        raise ValueError
    return out


def _build(fields):
    out = bytearray()
    for f, w, v in fields:
        out += _enc(f << 3 | w)
        out += _enc(v) if w == 0 else (_enc(len(v)) + v if w == 2 else v)
    return bytes(out)


def _swap(b, old, new, depth=0):
    """(bytes, n): every length-delimited field of message b, at any depth, that is exactly `old` becomes `new`."""
    if old not in b or depth > 40:
        return b, 0
    try:
        fs = _fields(b)
    except ValueError:
        return b, 0
    n, out = 0, []
    for f, w, v in fs:
        if w == 2 and v == old:
            v, n = new, n + 1
        elif w == 2 and old in v:
            v, k = _swap(v, old, new, depth + 1)
            n += k
        out.append((f, w, v))
    return (_build(out), n) if n else (b, 0)


def _strings(b, depth=0, out=None):
    """The text leaves of a message, in order: valid UTF-8 that does not itself read as a nested message."""
    out = [] if out is None else out
    try:
        fs = _fields(b)
    except ValueError:
        return out
    for f, w, v in fs:
        if w != 2 or not v:
            continue
        try:
            _fields(v)
            nested = depth < 40
        except ValueError:
            nested = False
        if nested:
            _strings(v, depth + 1, out)
            continue
        try:
            out.append(v.decode())
        except UnicodeDecodeError:
            pass
    return out


def _open(cid, d=None):
    import sqlite3
    p = db_for(cid, d)
    if not os.path.exists(p):
        raise LookupError(f"Antigravity's conversation database {p}")
    db = sqlite3.connect(p, timeout=10)
    db.row_factory = sqlite3.Row
    return db


def _log_rows(path):
    """(byte offset, record) of each complete line of a transcript log."""
    off = 0
    try:
        with open(path, "rb") as f:
            for line in f:
                if not line.endswith(b"\n"):
                    break
                try:
                    r = json.loads(line)
                except ValueError:
                    r = None
                yield off, r if isinstance(r, dict) else {}
                off += len(line)
    except OSError:
        return


def moments(path):
    """[{"offset": step index, "ts", "text"}] of what the user typed, oldest first (path: the transcript log)."""
    out = []
    for off, r in _log_rows(path):
        if r.get("type") == "USER_INPUT" and _user_text(r.get("content")):
            out.append({"offset": int(r.get("step_index") or 0), "ts": r.get("created_at"),
                        "text": _user_text(r.get("content")), "byte": off})
    return out


def records(path, limit=60, before=None):
    """Every shown entry of a conversation with the step it belongs to, oldest first; `before` (a step index) pages
    back. {"entries": [{"at": step, "role", "text", "ts"}], "before": step or None}."""
    rows = []
    for _, r in _log_rows(path):
        step = int(r.get("step_index", -1))
        if before is not None and step >= int(before):
            break
        for role, text, ts, _ in entries(json.dumps(r)):
            rows.append({"at": step, "role": role, "text": text[:TEXT_CAP], "ts": ts})
    cut_at = max(0, len(rows) - max(1, min(int(limit), 400)))
    while cut_at and rows[cut_at - 1]["at"] == rows[cut_at]["at"]:  # a page starts on a whole step
        cut_at -= 1
    out = rows[cut_at:]
    return {"entries": out, "before": out[0]["at"] if cut_at and out else None}


def _logs(cid, d=None):
    base = os.path.join(home(d), "brain", cid, ".system_generated")
    logs = os.path.join(base, "logs")
    out = [os.path.join(logs, n) for n in ("transcript.jsonl", "transcript_full.jsonl")]
    for sub in ("transcript", "transcript_full"):
        c = os.path.join(logs, "chunks", sub)
        if os.path.isdir(c):
            out += [os.path.join(c, n) for n in sorted(os.listdir(c)) if n.endswith(".jsonl")]
    return base, [p for p in out if os.path.isfile(p)]


def cut(cid, step, d=None):
    """Take step `step` and everything after it out of a conversation: the database agy resumes from, its logs and
    the per-step files. Returns the number of steps removed."""
    import shutil
    step = int(step)
    db = _open(cid, d)
    try:
        with db:
            n = db.execute("DELETE FROM steps WHERE idx>=?", (step,)).rowcount
            for r in db.execute("SELECT idx, data FROM gen_metadata").fetchall():
                try:
                    packed = next((v for f, w, v in _fields(r["data"] or b"") if f == 2 and w == 2), b"")
                    i, top = 0, -1
                    while i < len(packed):
                        v, i = _varint(packed, i)
                        top = max(top, v)
                except (ValueError, IndexError):
                    continue
                if top >= step:
                    db.execute("DELETE FROM gen_metadata WHERE idx=?", (r["idx"],))
            for r in db.execute("SELECT idx, data FROM executor_metadata").fetchall():
                try:
                    fs = _fields(r["data"] or b"")
                except ValueError:
                    continue
                last = next((v for f, w, v in fs if f == 3 and w == 0), None)
                if last is None or last < step:
                    continue
                first = db.execute("SELECT MAX(idx) m FROM steps").fetchone()["m"]
                before = db.execute("SELECT data FROM executor_metadata WHERE idx<? ORDER BY idx DESC LIMIT 1",
                                    (r["idx"],)).fetchone()
                try:
                    prev = next((v for f, w, v in _fields(before["data"]) if f == 3 and w == 0), -1) if before else -1
                except ValueError:
                    prev = -1
                if first is None or prev >= first:  # this run of the CLI began at or after the cut: it never happened
                    db.execute("DELETE FROM executor_metadata WHERE idx=?", (r["idx"],))
                else:  # it began before the cut: it now ends at the last step kept
                    db.execute("UPDATE executor_metadata SET data=? WHERE idx=?",
                               (_build([(f, w, first if f == 3 and w == 0 else v) for f, w, v in fs]), r["idx"]))
            for t in ("parent_references", "battle_mode_infos"):
                db.execute(f"DELETE FROM {t} WHERE idx>=?", (step,))
        db.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    finally:
        db.close()
    base, logs = _logs(cid, d)
    for p in logs:
        at = next((off for off, r in _log_rows(p) if int(r.get("step_index", -1)) >= step), None)
        if at is not None:
            os.truncate(p, at)
            if not at and os.sep + "chunks" + os.sep in p:
                os.remove(p)
    for sub, rx in (("steps", r"(\d+)$"), ("tasks", r"task-(\d+)\.log$")):
        folder = os.path.join(base, sub)
        for name in (os.listdir(folder) if os.path.isdir(folder) else []):
            m = re.match(rx, name)
            if m and int(m.group(1)) >= step:
                p = os.path.join(folder, name)
                shutil.rmtree(p, ignore_errors=True) if os.path.isdir(p) else os.remove(p)
    _summary(cid, d)
    return n


def _summary(cid, d=None):
    """Bring agy's own list of conversations (conversation_summaries.db) in line with a cut one. Best effort."""
    import sqlite3
    p = os.path.join(home(d), "conversation_summaries.db")
    if not os.path.exists(p):
        return
    try:
        db = _open(cid, d)
        try:
            n = db.execute("SELECT COUNT(*) FROM steps").fetchone()[0]
            u = db.execute("SELECT MAX(idx) FROM steps WHERE step_type=14").fetchone()[0]
        finally:
            db.close()
        s = sqlite3.connect(p, timeout=5)
        with s:
            s.execute("UPDATE conversation_summaries SET step_count=?, last_user_input_step_index=? "
                      "WHERE conversation_id=?", (n, -1 if u is None else u, cid))
        s.close()
    except (sqlite3.Error, LookupError):
        pass


def step_texts(cid, step, d=None):
    """The texts of one step as agy stores them (what the user typed, what the model said, a tool's output ...)."""
    db = _open(cid, d)
    try:
        r = db.execute("SELECT step_type, step_payload FROM steps WHERE idx=?", (int(step),)).fetchone()
    finally:
        db.close()
    if not r:
        raise LookupError(f"step {step}")
    logged = []  # what the logs show of this step: of all the strings a step holds, those are its conversation
    for p in _logs(cid, d)[1][:2]:
        for _, rec in _log_rows(p):
            if rec.get("step_index") == int(step):
                logged += [v if isinstance(v, str) else json.dumps(v) for v in rec.values()]
    every = [x for x in dict.fromkeys(_strings(r["step_payload"] or b"")) if len(x) >= 2]
    out = [x for x in every if any(x in v or json.dumps(x)[1:-1] in v for v in logged)]
    if not out:
        out = [x for x in every if len(x) >= 12 and " " in x]
    return {"step": int(step), "type": r["step_type"], "texts": out}


def edit_step(cid, step, changes, d=None):
    """changes: [(old text, new text)]. Each old text is replaced wherever the step holds exactly it: in the
    database, and in the logs written from it. Returns how many places changed."""
    step = int(step)
    changes = [(o, n) for o, n in changes if o != n and o]
    db = _open(cid, d)
    done = 0
    try:
        with db:
            r = db.execute("SELECT step_payload FROM steps WHERE idx=?", (step,)).fetchone()
            if not r:
                raise LookupError(f"step {step}")
            blob = r["step_payload"] or b""
            for old, new in changes:
                blob, k = _swap(blob, old.encode(), new.encode())
                if not k:
                    raise ValueError("that text is no longer in the step (the conversation changed?)")
                done += k
            db.execute("UPDATE steps SET step_payload=? WHERE idx=?", (blob, step))
        db.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    finally:
        db.close()
    for p in _logs(cid, d)[1]:
        rows, hit = [], False
        with open(p, "rb") as f:
            for line in f:
                try:
                    rec = json.loads(line)
                except ValueError:
                    rec = None
                if isinstance(rec, dict) and rec.get("step_index") == step:
                    for k, v in list(rec.items()):
                        for old, new in changes:
                            if isinstance(v, str) and old in v:
                                rec[k] = v = v.replace(old, new)
                                hit = True
                    line = (json.dumps(rec, ensure_ascii=False, separators=(",", ":")) + "\n").encode()
                rows.append(line)
        if hit:
            tmp = p + ".sb-tmp"
            with open(tmp, "wb") as f:
                f.writelines(rows)
            os.replace(tmp, p)
    return done



API_KEYS = ("GEMINI_API_KEY", "GOOGLE_API_KEY", "GOOGLE_GENAI_USE_VERTEXAI", "GOOGLE_APPLICATION_CREDENTIALS")
TEXT_ONLY = ("This is a text-only consultation. Everything you need is in this conversation. Do not call any tool "
             "(no shell, no file reads, no browser, no plans or artifacts): write your whole answer as your reply.")


def consult(model, parts, timeout):
    """One agy print-mode conversation in an empty folder, on the subscription only (API keys are taken out of the
    environment): (answer, usage), or raises with what went wrong. Print mode denies every tool request."""
    import shutil
    import subprocess
    import tempfile
    env = {k: v for k, v in os.environ.items() if k not in API_KEYS}
    stdin = "".join(json.dumps({"event": "user", "message": {"role": "user", "content": p}}) + "\n" for p in parts)
    d = tempfile.mkdtemp(prefix="sb-agy-")
    try:
        r = subprocess.run([os.environ.get("SB_ANTIGRAVITY_BIN") or "agy", "--model", model, "--sandbox",
                            "--input-format", "stream-json", "--output-format", "stream-json", "-p="], input=stdin,
                           capture_output=True, text=True, timeout=timeout, cwd=d, env=env)
    finally:
        shutil.rmtree(d, ignore_errors=True)
    results = []
    for l in (r.stdout or "").splitlines():
        try:
            e = json.loads(l)
        except ValueError:
            continue
        if isinstance(e, dict) and e.get("event") == "result":
            results.append(e.get("result") or {})
    if not results:
        raise ConnectionError(((r.stderr or "") + (r.stdout or "")).strip()[-500:] or f"agy exited {r.returncode}")
    last = results[-1]
    if last.get("status") != "SUCCESS" or len(results) < len(parts):
        raise RuntimeError(str(last.get("error") or f"agy stopped after {len(results)} of {len(parts)} turns")[:2000])
    answer = last.get("response") or ""
    if not answer.strip():
        raise RuntimeError("no answer")
    u = last.get("usage") or {}
    return answer, {"model": model, "in_tokens": str(u.get("input_tokens", "?")),
                    "out_tokens": str(u.get("output_tokens", "?"))}
