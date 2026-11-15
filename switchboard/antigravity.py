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

from .store import epoch

CONTEXT_WINDOW = 1_000_000
TEXT_CAP = 20000
REQ = re.compile(r"<USER_REQUEST>\s*(.*?)\s*</USER_REQUEST>", re.S)


def home(d=None):
    return os.path.expanduser(d or os.environ.get("ANTIGRAVITY_HOME") or "~/.gemini/antigravity-cli")


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


def new_session(cwd, since, taken=(), d=None):
    """The newest unclaimed conversation whose prompt was typed in cwd since launch (history.jsonl)."""
    real, taken = os.path.realpath(cwd), set(taken or ())
    try:
        lines = open(os.path.join(home(d), "history.jsonl"), errors="replace").read().splitlines()[-500:]
    except OSError:
        return None
    for line in reversed(lines):
        try:
            r = json.loads(line)
        except ValueError:
            continue
        cid, ws = r.get("conversationId"), r.get("workspace") or ""
        if not cid or cid in taken or (r.get("timestamp") or 0) / 1000 < (since or 0) - 5:
            continue
        if ws in (cwd, real) or os.path.realpath(ws) == real:
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


def _lines(path):
    try:
        with open(path, errors="replace") as f:
            return [l for l in (x.strip() for x in f) if l]
    except (OSError, TypeError):
        return []


def lines_newest_first(path, limit=5000):
    return list(reversed(_lines(path)))[:limit]


def read_chat(path, limit=40, before=None, after=None):
    """Same shape and paging as hermes.read_chat: ids are line_no * 8 + part."""
    limit = max(1, min(int(limit), 400))
    before, after = (int(before) if before is not None else None), (int(after) if after is not None else None)
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
    for e in reversed(read_chat(path, 40)["entries"]):
        if e["role"] == "assistant":
            return e["text"], e["ts"]
    return "", None


def last_model(path):
    for line in lines_newest_first(path):
        try:
            m = json.loads(line).get("model")
        except (ValueError, AttributeError):
            continue
        if m:
            return m
    return None


def has_user_text(path, needle, since_epoch=0.0):
    if not needle:
        return False
    norm = " ".join(needle.split())[:80]
    for line in lines_newest_first(path, 200):
        try:
            r = json.loads(line)
        except ValueError:
            continue
        if r.get("type") != "USER_INPUT":
            continue
        if since_epoch and r.get("created_at") and epoch(r["created_at"]) < since_epoch - 5:
            continue
        text = _user_text(r.get("content"))
        if needle in text or (norm and norm in " ".join(text.split())):
            return True
    return False


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
            out.update(context_tokens=i + c + o, context_window=CONTEXT_WINDOW,
                       last_turn={"input_tokens": i, "output_tokens": o, "cache_read": c, "cost_usd": None,
                                  "at": r.get("created_at")})
        if since_epoch and r.get("created_at") and epoch(r["created_at"]) < since_epoch:
            if out["context_tokens"] is not None:
                break
            continue
        out["tokens_24h"] += i + o + c
    return out
