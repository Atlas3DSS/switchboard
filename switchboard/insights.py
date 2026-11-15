"""Usage insights across every agent transcript: a per-day ledger per transcript, built incrementally.

Layout and ideas follow dsh-context's Context Insights page (bowenliang123/dsh-context, Apache-2.0): KPI tiles,
7-day usage, an 8-week activity heatmap, tool / command / skill tallies, and per-agent cards.

The ledger is keyed by transcript path and keeps the byte offset it has read to, so each pass parses only new lines.
It is saved to $STATE/insights.json so a hub restart does not re-read ~1.5 GB of transcripts.
Costs are API-list-price estimates (subscription accounts pay nothing per token); Codex models are not priced.
"""
import json
import os
import re
import threading
import time
from datetime import datetime

from . import discovery
from .store import STATE

CACHE = os.path.join(STATE, "insights.json")
VERSION = 5
GAP_S = 300  # a pause longer than this between transcript lines is not active time

# $ per million tokens: input, output, cache read. Cache writes cost 1.25x input (5 min) or 2x input (1 h).
# Source: the claude-api skill's model table, cached 2026-10-06.
PRICES = (
    ("fable", 10.0, 50.0, 0.25), ("mythos", 10.0, 50.0, 0.25),
    ("opus-5-5", 4.0, 20.0, 0.20), ("opus", 5.0, 25.0, 0.50),
    ("sonnet-5-5", 2.0, 10.0, 0.20), ("sonnet-5", 2.0, 10.0, 0.20), ("sonnet", 3.0, 15.0, 0.30),
    ("haiku-5-5", 0.10, 0.50, 0.01), ("haiku", 1.0, 5.0, 0.10),
)


def price_of(model):
    m = (model or "").lower()
    if not m.startswith("claude"):
        return None
    for key, i, o, r in PRICES:
        if key in m:
            return i, o, r
    return None


def epoch(ts):
    try:
        return datetime.fromisoformat(str(ts).replace("Z", "+00:00")).timestamp()
    except (TypeError, ValueError):
        return None


def day_of(t):
    return datetime.fromtimestamp(t).strftime("%Y-%m-%d")


SPLIT = re.compile(r"\s*(?:&&|\|\||;|\||\n)\s*")
HEREDOC = re.compile(r"<<-?\s*(['\"]?)(\w+)\1[^\n]*\n.*?(?:\n\2[ \t]*(?=\n|$)|$)", re.S)
SKIP_PROG = {"cd", "export", "set", "true", "false", "then", "do", "done", "fi", "else", "elif", "echo", "sleep",
             "local", "for", "while", "until", "if", "case", "esac", "in", "function", "return", "break", "continue",
             "wait", "read", "shift", "unset", "source", "trap", "printf", "test", "eval", "declare"}
WRAPPERS = {"sudo", "timeout", "env", "nice", "nohup", "time", "exec", "command", "xargs", "do", "then", "else", "!"}


def programs(cmd):
    """The programs a shell command runs: 'cd x && git status | head' -> ['git', 'head']. Heredoc bodies are data."""
    out = []
    for seg in SPLIT.split(HEREDOC.sub(" ", str(cmd or "")))[:12]:
        words = seg.strip().lstrip("(!").split()
        while words and ("=" in words[0] and not words[0].startswith("=") or words[0] in WRAPPERS
                         or words[0].startswith("-") or words[0].isdigit()):
            words = words[1:]
        if not words:
            continue
        p = os.path.basename(words[0].strip("'\"`$("))
        if p and p not in SKIP_PROG and re.match(r"^[\w.+-]{1,40}$", p):
            out.append(p)
    return out


def _bucket():
    return {"in": 0, "cw": 0, "cr": 0, "out": 0, "think": 0, "cost": 0.0, "unpriced": 0, "req": 0, "turns": 0,
            "active": 0.0, "tools": {}, "cmds": {}, "skills": {}, "models": {}, "last": 0}


def _tally(d, name, t):
    v = d.get(name)
    if v:
        v[0] += 1
        v[1] = max(v[1], t)
    else:
        d[name] = [1, t]


class Ledger:
    """One transcript: where reading stopped, and its per-day buckets."""

    def __init__(self, provider, doc=None):
        doc = doc or {}
        self.provider = provider
        self.off = doc.get("off", 0)
        self.size = doc.get("size", 0)
        self.days = doc.get("days", {})
        self.prev_t = doc.get("prev_t")  # last timestamp seen, for active time
        self.last_id = doc.get("last_id")  # Claude splits one message over lines that repeat its usage
        self.model = doc.get("model")  # Codex names its model once per turn

    def doc(self):
        return {"off": self.off, "size": self.size, "days": self.days, "prev_t": self.prev_t,
                "last_id": self.last_id, "model": self.model, "provider": self.provider}

    def _b(self, t):
        k = day_of(t)
        b = self.days.get(k)
        if b is None:
            b = self.days[k] = _bucket()
        b["last"] = max(b["last"], t)
        return b

    def _tick(self, t):
        if self.prev_t is not None and 0 < t - self.prev_t <= GAP_S:
            self._b(t)["active"] += t - self.prev_t
        self.prev_t = t if self.prev_t is None else max(self.prev_t, t)

    def feed(self, path, budget):
        """Read new lines (at most `budget` bytes). Returns bytes read."""
        size = os.path.getsize(path)
        if size < self.off:  # rewritten: start over
            self.__init__(self.provider)
        if size == self.off:
            return 0
        n = 0
        with open(path, "rb") as f:
            f.seek(self.off)
            while n < budget:
                line = f.readline()
                if not line or not line.endswith(b"\n"):
                    break  # a line still being written is read next time
                n += len(line)
                try:
                    (self._codex if self.provider == "codex" else self._claude)(line)  # the built-in agent writes Claude-format lines
                except (ValueError, TypeError, AttributeError, KeyError):
                    pass
        self.off += n
        self.size = size
        return n

    def _claude(self, raw):
        if b'"assistant"' not in raw and b'"user"' not in raw:
            return
        e = json.loads(raw)
        if e.get("isSidechain"):
            return
        t = epoch(e.get("timestamp"))
        if t is None:
            return
        typ, msg = e.get("type"), e.get("message") or {}
        if typ == "user":
            c = msg.get("content")
            if isinstance(c, str):
                if not c.lstrip().startswith(("<", "Caveat:", "[Request interrupted")):
                    self._tick(t)
                    self._b(t)["turns"] += 1
            else:
                self._tick(t)
            return
        if typ != "assistant" or msg.get("model") == "<synthetic>":
            return
        self._tick(t)
        b = self._b(t)
        mid = msg.get("id")
        u = msg.get("usage") or {}
        if mid != self.last_id and u:
            self.last_id = mid
            i, cw, cr, o = (u.get("input_tokens") or 0, u.get("cache_creation_input_tokens") or 0,
                            u.get("cache_read_input_tokens") or 0, u.get("output_tokens") or 0)
            th = (u.get("output_tokens_details") or {}).get("thinking_tokens") or 0
            b["in"] += i; b["cw"] += cw; b["cr"] += cr; b["out"] += o; b["think"] += th; b["req"] += 1
            model = msg.get("model") or "?"
            b["models"][model] = b["models"].get(model, 0) + i + cw + o
            p = price_of(model)
            if p:
                cc = u.get("cache_creation") or {}
                w1h = cc.get("ephemeral_1h_input_tokens") or 0
                w5m = cw - w1h if cc else cw
                b["cost"] += (i * p[0] + w5m * p[0] * 1.25 + w1h * p[0] * 2 + cr * p[2] + o * p[1]) / 1e6
            else:
                b["unpriced"] += 1
        for c in msg.get("content") or []:
            if not isinstance(c, dict) or c.get("type") != "tool_use":
                continue
            name = c.get("name") or "?"
            if name.startswith("mcp__"):
                name = "mcp:" + name.split("__")[-1]
            _tally(b["tools"], name, t)
            inp = c.get("input") or {}
            if name == "Bash":
                for prog in programs(inp.get("command")):
                    _tally(b["cmds"], prog, t)
            elif name == "Skill" and inp.get("skill"):
                _tally(b["skills"], str(inp["skill"])[:60], t)

    def _codex(self, raw):
        if b'"token_count"' not in raw and b'"function_call"' not in raw and b'"user_message"' not in raw \
                and b'"turn_context"' not in raw and b'"custom_tool_call"' not in raw:
            return
        e = json.loads(raw)
        t = epoch(e.get("timestamp"))
        if t is None:
            return
        p = e.get("payload") or {}
        typ = p.get("type")
        if e.get("type") == "turn_context":
            self.model = p.get("model") or self.model
            return
        self._tick(t)
        b = self._b(t)
        if typ == "user_message":
            b["turns"] += 1
        elif typ == "token_count":
            last = (p.get("info") or {}).get("last_token_usage") or {}
            if not last:
                return
            cr = last.get("cached_input_tokens", 0)
            i = max(0, last.get("input_tokens", 0) - cr)
            o = last.get("output_tokens", 0)
            b["in"] += i; b["cr"] += cr; b["out"] += o; b["think"] += last.get("reasoning_output_tokens", 0)
            b["req"] += 1; b["unpriced"] += 1
            m = self.model or "codex"
            b["models"][m] = b["models"].get(m, 0) + i + o
        elif typ in ("function_call", "custom_tool_call"):
            name = p.get("name") or "?"
            _tally(b["tools"], name, t)
            args = p.get("arguments") or p.get("input") or ""
            try:
                a = json.loads(args) if isinstance(args, str) and args.startswith("{") else {}
            except ValueError:
                a = {}
            cmd = a.get("cmd") or a.get("command")
            if isinstance(cmd, list):
                cmd = " ".join(cmd[2:] if cmd[:2] == ["bash", "-lc"] else cmd)
            for prog in programs(cmd):
                _tally(b["cmds"], prog, t)


class Insights:
    def __init__(self, hub, path=CACHE):
        self.hub, self.path = hub, path
        self.lock = threading.Lock()
        self.ledgers = {}  # transcript path -> Ledger
        self.owner = {}  # transcript path -> (agent id, project)
        self.scanned_at = None
        self.pending = 0  # bytes still to read
        try:
            with open(path) as f:
                doc = json.load(f)
            if doc.get("version") == VERSION:
                self.ledgers = {p: Ledger(d.get("provider", "claude"), d) for p, d in doc["ledgers"].items()}
                self.owner = {p: tuple(v) for p, v in doc.get("owner", {}).items()}
        except (OSError, ValueError, KeyError):
            pass

    def sources(self):
        """Every transcript we know an owner for: agents' current sessions, then their ended ones."""
        s = self.hub.s
        out = {}
        for r in s.q("SELECT agent, project, path, provider FROM memory_sessions WHERE path IS NOT NULL"):
            if r["provider"] in ("claude", "codex", "api") and r["path"]:
                out[r["path"]] = (r["agent"], r["project"], r["provider"])
        for a in s.q("SELECT id, project, provider, account, session_id FROM agents WHERE session_id IS NOT NULL"):
            if a["provider"] not in ("claude", "codex", "api"):
                continue
            p = discovery.transcript_path(a["provider"], a["session_id"], a["account"])
            if p:
                out[p] = (a["id"], a["project"], a["provider"])
        return out

    def tick(self, budget=384 << 20):
        """Read up to `budget` new bytes across all transcripts, then save. Called from the hub loop."""
        srcs = self.sources()
        read = pending = 0
        for path, (aid, pid, prov) in srcs.items():
            try:
                size = os.path.getsize(path)
            except OSError:
                continue
            with self.lock:
                self.owner[path] = (aid, pid)
                led = self.ledgers.get(path)
                if led is None:
                    led = self.ledgers[path] = Ledger(prov)
            if size == led.off:
                continue
            if read >= budget:
                pending += size - led.off
                continue
            try:
                with self.lock:
                    read += led.feed(path, budget - read)
            except OSError:
                continue
            pending += max(0, size - led.off)
        self.pending = pending
        self.scanned_at = time.time()
        if read:
            self.save()

    def save(self):
        with self.lock:
            doc = {"version": VERSION, "ledgers": {p: l.doc() for p, l in self.ledgers.items()},
                   "owner": {p: list(v) for p, v in self.owner.items()}}
        tmp = self.path + ".tmp"
        with open(tmp, "w") as f:
            json.dump(doc, f, separators=(",", ":"))
        os.replace(tmp, self.path)

    def view(self, rng="7d", project=None, top=14):
        """What the Insights page shows: KPIs for the range, 8 weeks of days, tallies and per-agent rows."""
        now = time.time()
        today = day_of(now)
        span = {"1d": 1, "7d": 7, "30d": 30}.get(rng)
        since = day_of(now - 86400 * (span - 1)) if span else ""
        heat_from = day_of(now - 86400 * 7 * 9)
        kpi = _bucket()
        days, per_agent, per_project = {}, {}, {}
        tools, cmds, skills, models = {}, {}, {}, {}
        with self.lock:
            items = [(p, l, self.owner.get(p, (None, None))) for p, l in self.ledgers.items()]
            for path, led, (aid, pid) in items:
                if project and pid != project:
                    continue
                for day, b in led.days.items():
                    if day >= heat_from:
                        d = days.setdefault(day, {"tokens": 0, "cost": 0.0, "agents": set()})
                        tok = b["in"] + b["cw"] + b["out"]
                        d["tokens"] += tok
                        d["cost"] += b["cost"]
                        if tok:
                            d["agents"].add(aid)
                    if day < since:
                        continue
                    for k in ("in", "cw", "cr", "out", "think", "cost", "unpriced", "req", "turns", "active"):
                        kpi[k] += b[k]
                    kpi["last"] = max(kpi["last"], b["last"])
                    a = per_agent.setdefault(aid, {"tokens": 0, "billed": 0, "cost": 0.0, "turns": 0, "req": 0,
                                                   "cr": 0, "inp": 0, "active": 0.0, "last": 0, "project": pid,
                                                   "tools": 0})
                    a["tokens"] += b["in"] + b["cw"] + b["cr"] + b["out"]
                    a["billed"] += b["in"] + b["cw"] + b["out"]
                    a["cost"] += b["cost"]; a["turns"] += b["turns"]; a["req"] += b["req"]
                    a["cr"] += b["cr"]; a["inp"] += b["in"] + b["cw"] + b["cr"]; a["active"] += b["active"]
                    a["last"] = max(a["last"], b["last"]); a["tools"] += sum(v[0] for v in b["tools"].values())
                    pp = per_project.setdefault(pid or "", {"billed": 0, "cost": 0.0, "agents": set()})
                    pp["billed"] += b["in"] + b["cw"] + b["out"]; pp["cost"] += b["cost"]; pp["agents"].add(aid)
                    for src, dst in ((b["tools"], tools), (b["cmds"], cmds), (b["skills"], skills)):
                        for name, (n, t) in src.items():
                            v = dst.setdefault(name, [0, 0])
                            v[0] += n
                            v[1] = max(v[1], t)
                    for m, n in b["models"].items():
                        models[m] = models.get(m, 0) + n

        def ranked(d):
            rows = sorted(d.items(), key=lambda kv: -kv[1][0])
            return {"total": sum(v[0] for v in d.values()), "kinds": len(d),
                    "rows": [{"name": k, "n": v[0], "last": v[1]} for k, v in rows[:top]]}

        inp = kpi["in"] + kpi["cw"] + kpi["cr"]
        agents = []
        for aid, a in per_agent.items():
            if not a["tokens"] and not a["turns"]:
                continue
            row = self.hub.s.agent(aid) if aid else None
            agents.append({"id": aid, "project": a["project"], "title": (row or {}).get("title") or aid,
                           "gone": bool((row or {}).get("gone")), "model": (row or {}).get("model"),
                           "provider": (row or {}).get("provider"),
                           "context_tokens": (row or {}).get("context_tokens"),
                           "context_window": (row or {}).get("context_window"),
                           "tokens": a["tokens"], "billed": a["billed"], "cost": round(a["cost"], 4),
                           "turns": a["turns"], "requests": a["req"], "tools": a["tools"],
                           "cache_hit": round(a["cr"] / a["inp"], 4) if a["inp"] else None,
                           "active_s": round(a["active"]), "last": a["last"]})
        agents.sort(key=lambda r: -r["billed"])
        return {
            "range": rng, "project": project, "today": today, "scanned_at": self.scanned_at, "pending": self.pending,
            "kpi": {"agents": len(agents), "tokens": kpi["in"] + kpi["cw"] + kpi["cr"] + kpi["out"],
                    "billed": kpi["in"] + kpi["cw"] + kpi["out"], "cost": round(kpi["cost"], 2),
                    "unpriced": kpi["unpriced"], "requests": kpi["req"], "turns": kpi["turns"],
                    "cache_hit": round(kpi["cr"] / inp, 4) if inp else None, "active_s": round(kpi["active"]),
                    "tool_calls": sum(v[0] for v in tools.values())},
            "composition": {"cache_read": kpi["cr"], "cache_write": kpi["cw"], "input": kpi["in"],
                            "reasoning": kpi["think"], "output": max(0, kpi["out"] - kpi["think"])},
            "days": {k: {"tokens": v["tokens"], "cost": round(v["cost"], 2), "agents": len(v["agents"])}
                     for k, v in sorted(days.items())},
            "projects": sorted(({"id": k, "billed": v["billed"], "cost": round(v["cost"], 2), "agents": len(v["agents"])}
                                for k, v in per_project.items() if v["billed"]), key=lambda r: -r["billed"]),
            "models": sorted(({"model": k, "tokens": v} for k, v in models.items()), key=lambda r: -r["tokens"])[:8],
            "tools": ranked(tools), "commands": ranked(cmds), "skills": ranked(skills),
            "agents": agents,
        }
