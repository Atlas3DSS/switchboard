"""One agent's context over its current session: per-request usage, an estimated composition, timing, tool costs.

The Context view (web/viz.js agentContext) follows dsh-context's Context tab (bowenliang123/dsh-context,
Apache-2.0): current-context bar, composition donut, a per-request trend with a latency line, timing split.

Token counts per request are exact (the API's usage block). What fills the context is estimated from the text in
the transcript (characters / CHARS_PER_TOKEN) and labelled as an estimate: the fixed part (system prompt, tool
schemas, CLAUDE.md) is the first request's input minus the content before it; what the estimate cannot place
(carried thinking blocks, tokenizer drift) is "other". Compaction starts the composition over.
"""
import json
import os
import re
import threading
from collections import OrderedDict

from .insights import epoch, price_of

CHARS_PER_TOKEN = 3.6
CATS = ("system", "tool_schemas", "injected", "user", "assistant", "tool_calls", "tool_results", "other")
FIXED = ("system", "tool_schemas", "other")
REMINDER = re.compile(r"<system-reminder>.*?</system-reminder>", re.S)
MAX_POINTS = 360


def est(n_chars):
    return int(n_chars / CHARS_PER_TOKEN)


def _text_of(c):
    if isinstance(c, str):
        return c
    if isinstance(c, list):
        out = []
        for x in c:
            if isinstance(x, dict):
                if x.get("type") == "text":
                    out.append(x.get("text") or "")
                elif x.get("type") == "image":
                    out.append("x" * 5400)  # an image is ~1.5k tokens
            elif isinstance(x, str):
                out.append(x)
        return "\n".join(out)
    return ""


class Trace:
    def __init__(self, provider):
        self.provider = provider
        self.off = 0
        self.cats = dict.fromkeys(CATS, 0)  # estimated tokens currently in context, per category
        self.by_tool = {}  # tool name -> estimated tokens of its results in context
        self.fixed = None  # system prompt + tool schemas, learned from the first request after a (re)start
        self.shape = None  # (system, tool schemas) measured from Claude Code's api-request-shape entries
        self.points = []  # one per API request
        self.turn = 0
        self.compactions = []
        self.prev_t = None
        self.last_id = None
        self.pending_tools = {}  # tool_use id -> (name, started, brief)
        self.tool_time = {}  # name -> [runs, seconds]
        self.big = []  # largest tool results: (tokens, name, brief, t)
        self.model = None
        self.window = None
        self.cost = 0.0
        self.out = self.think = self.cr = self.inp = 0
        self.model_s = 0.0

    # ---------- reading ----------
    def feed(self, path):
        size = os.path.getsize(path)
        if size < self.off:
            self.__init__(self.provider)
        with open(path, "rb") as f:
            f.seek(self.off)
            for line in f:
                if not line.endswith(b"\n"):
                    break
                self.off += len(line)
                try:
                    (self._claude if self.provider == "claude" else self._codex)(json.loads(line))
                except (ValueError, TypeError, AttributeError, KeyError):
                    continue

    def _claude(self, e):
        if e.get("isSidechain"):
            return
        t = epoch(e.get("timestamp"))
        typ = e.get("type")
        if typ == "system" and e.get("subtype") == "compact_boundary":
            self.compactions.append({"t": t, "at": len(self.points)})
            self.cats = dict.fromkeys(CATS, 0)
            self.by_tool = {}
            self.fixed = None
            return
        if typ == "api-request-shape":
            sh = e.get("shape") or {}
            self.shape = (est(len(json.dumps(sh.get("system") or []))), est(len(json.dumps(sh.get("tools") or []))))
            return
        if typ == "attachment":
            att = e.get("attachment") or {}
            text = "".join(r.get("content") or "" for r in e.get("rendered") or [] if isinstance(r, dict))
            if att.get("type") == "queued_command" and att.get("humanTurn"):
                self.cats["user"] += est(len(text))
                self.turn += 1
            else:
                self.cats["injected"] += est(len(text))
            return
        msg = e.get("message") or {}
        if typ == "user":
            c = msg.get("content")
            if isinstance(c, str) or e.get("isCompactSummary"):
                text = _text_of(c)
                inj = len(text) if e.get("isMeta") else sum(len(m) for m in REMINDER.findall(text))
                self.cats["injected"] += est(inj)
                rest = len(text) - inj
                self.cats["user"] += est(rest)
                if rest > 0 and not text.lstrip().startswith(("<command-", "<local-command", "Caveat:")) \
                        and not e.get("isMeta") and not e.get("isCompactSummary"):
                    self.turn += 1
            elif isinstance(c, list):
                for x in c:
                    if not isinstance(x, dict):
                        continue
                    if x.get("type") == "tool_result":
                        body = _text_of(x.get("content"))
                        inj = sum(len(m) for m in REMINDER.findall(body))
                        tok = est(len(body) - inj)
                        self.cats["injected"] += est(inj)
                        name, started, brief = self.pending_tools.pop(x.get("tool_use_id"), ("?", None, ""))
                        self.cats["tool_results"] += tok
                        self.by_tool[name] = self.by_tool.get(name, 0) + tok
                        if started and t:
                            v = self.tool_time.setdefault(name, [0, 0.0])
                            v[0] += 1
                            v[1] += max(0.0, t - started)
                        if tok > 2000:
                            self.big.append((tok, name, brief, t))
                            self.big = sorted(self.big, reverse=True)[:12]
                    elif x.get("type") == "text":
                        text = x.get("text") or ""
                        inj = len(text) if e.get("isMeta") else sum(len(m) for m in REMINDER.findall(text))
                        self.cats["injected"] += est(inj)
                        self.cats["user"] += est(len(text) - inj)
                        if len(text) - inj > 0 and not e.get("isMeta"):
                            self.turn += 1
                    elif x.get("type") == "image":
                        self.cats["user"] += 1500
            if t:
                self.prev_t = t
            return
        if typ != "assistant" or msg.get("model") == "<synthetic>":
            return
        mid, u = msg.get("id"), msg.get("usage") or {}
        tools = []
        for c in msg.get("content") or []:
            if not isinstance(c, dict):
                continue
            if c.get("type") == "text":
                self.cats["assistant"] += est(len(c.get("text") or ""))
            elif c.get("type") == "tool_use":
                inp = c.get("input") or {}
                self.cats["tool_calls"] += est(len(json.dumps(inp)))
                name = c.get("name") or "?"
                if name.startswith("mcp__"):
                    name = "mcp:" + name.split("__")[-1]
                brief = inp.get("description") or inp.get("command") or inp.get("file_path") or inp.get("pattern") \
                    or inp.get("url") or inp.get("query") or inp.get("prompt") or inp.get("skill") or ""
                brief = " ".join(str(brief).split())[:160]
                self.pending_tools[c.get("id")] = (name, t, brief)
                tools.append(f"{name}: {brief}" if brief else name)
        if mid == self.last_id or not u:
            if self.points and tools:
                self.points[-1]["tools"] = (self.points[-1].get("tools") or []) + tools[:4]
            return
        self.last_id = mid
        i, cw, cr, o = (u.get("input_tokens") or 0, u.get("cache_creation_input_tokens") or 0,
                        u.get("cache_read_input_tokens") or 0, u.get("output_tokens") or 0)
        th = (u.get("output_tokens_details") or {}).get("thinking_tokens") or 0
        ctx = i + cw + cr
        self.model = msg.get("model") or self.model
        p = price_of(self.model)
        if p:
            cc = u.get("cache_creation") or {}
            w1h = cc.get("ephemeral_1h_input_tokens") or 0
            self.cost += (i * p[0] + (cw - w1h) * p[0] * 1.25 + w1h * p[0] * 2 + cr * p[2] + o * p[1]) / 1e6
        self.out += o; self.think += th; self.cr += cr; self.inp += ctx
        self._settle(ctx)
        dur = (t - self.prev_t) if (t and self.prev_t and 0 <= t - self.prev_t < 3600) else None
        if dur:
            self.model_s += dur
        snap = self._scaled(ctx)
        self.points.append({"t": t, "turn": self.turn, "ctx": ctx, "in": i, "cw": cw, "cr": cr, "out": o,
                            "think": th, "dur": round(dur, 2) if dur is not None else None,
                            "tools": tools[:4], "mix": [snap[k] for k in CATS]})
        if t:
            self.prev_t = t

    def _settle(self, ctx):
        """Fix the parts the transcript does not spell out, given the real input size of this request."""
        content = sum(v for k, v in self.cats.items() if k not in FIXED)
        if self.shape:
            self.cats["system"], self.cats["tool_schemas"] = self.shape
        else:
            if self.fixed is None:  # system prompt + tool schemas + memory: the first request's unexplained part
                self.fixed = max(0, ctx - content)
            self.cats["system"] = self.fixed
        self.cats["other"] = max(0, ctx - self.cats["system"] - self.cats["tool_schemas"] - content)

    def _scaled(self, ctx):
        """The estimate fitted to the real total of this request. Text-based counts run ~2x low on Claude's
        tokenizer and a little high on Codex, so every estimated part is scaled by one factor (at most 2.5x up);
        a first-request remainder (no request shape) is already exact and is not scaled. Left over is "other"."""
        exact = {"system"} if (self.fixed is not None and not self.shape) else set()
        est_tot = sum(v for k, v in self.cats.items() if k != "other" and k not in exact)
        room = ctx - sum(self.cats[k] for k in exact)
        f = min(2.5, room / est_tot) if est_tot and room > 0 else 1.0
        out = {k: (v if k in exact else int(v * f)) for k, v in self.cats.items() if k != "other"}
        out["other"] = max(0, ctx - sum(out.values()))
        return out

    def _codex(self, e):
        p = e.get("payload") or {}
        t = epoch(e.get("timestamp"))
        typ = p.get("type")
        if e.get("type") == "turn_context":
            self.model = p.get("model") or self.model
            return
        if e.get("type") == "compacted":
            self.compactions.append({"t": t, "at": len(self.points)})
            self.cats = dict.fromkeys(CATS, 0)
            self.by_tool = {}
            self.fixed = None
            self.cats["user"] = est(len(json.dumps(p)))  # the summary that replaces the history
            return
        if e.get("type") == "response_item" and typ in ("message", "additional_tools"):
            role = p.get("role")
            text = json.dumps(p.get("content") or p.get("tools") or "")
            if typ == "additional_tools":
                self.cats["tool_schemas"] += est(len(text))
            elif role == "developer":
                self.cats["injected"] += est(len(text))
            elif role == "assistant":
                self.cats["assistant"] += est(len(text))
            elif role == "user":
                body = _text_of([c | {"type": "text"} if c.get("type") == "input_text" else c
                                 for c in p.get("content") or [] if isinstance(c, dict)])
                if body.lstrip().startswith("<"):
                    self.cats["injected"] += est(len(body))
                else:
                    self.cats["user"] += est(len(body))
                    self.turn += 1
        elif typ in ("function_call", "custom_tool_call"):
            args = p.get("arguments") or p.get("input") or ""
            self.cats["tool_calls"] += est(len(str(args)))
            self.pending_tools[p.get("call_id")] = (p.get("name") or "?", t, str(args)[:160])
        elif typ in ("function_call_output", "custom_tool_call_output"):
            out = p.get("output")
            body = out if isinstance(out, str) else json.dumps(out)
            tok = est(len(body))
            name, started, brief = self.pending_tools.pop(p.get("call_id"), ("?", None, ""))
            self.cats["tool_results"] += tok
            self.by_tool[name] = self.by_tool.get(name, 0) + tok
            if started and t:
                v = self.tool_time.setdefault(name, [0, 0.0])
                v[0] += 1
                v[1] += max(0.0, t - started)
        elif typ == "token_count":
            info = p.get("info") or {}
            last = info.get("last_token_usage") or {}
            if not last:
                return
            self.window = info.get("model_context_window") or self.window
            ctx, cr, o = last.get("input_tokens", 0), last.get("cached_input_tokens", 0), last.get("output_tokens", 0)
            th = last.get("reasoning_output_tokens", 0)
            self._settle(ctx)
            self.out += o; self.think += th; self.cr += cr; self.inp += ctx
            dur = (t - self.prev_t) if (t and self.prev_t and 0 <= t - self.prev_t < 3600) else None
            snap = self._scaled(ctx)
            self.points.append({"t": t, "turn": self.turn, "ctx": ctx, "in": ctx - cr, "cw": 0, "cr": cr, "out": o,
                                "think": th, "dur": round(dur, 2) if dur else None, "tools": [],
                                "mix": [snap[k] for k in CATS]})
        if t:
            self.prev_t = t

    # ---------- the view ----------
    def view(self):
        pts = self.points
        step = max(1, -(-len(pts) // MAX_POINTS))
        series = []
        for k in range(0, len(pts), step):  # keep the last request of each bucket so the end is exact
            chunk = pts[k:k + step]
            last = dict(chunk[-1])
            last["n"] = len(chunk)
            last["index"] = k + len(chunk)
            last["out"] = sum(x["out"] for x in chunk)
            durs = [x["dur"] for x in chunk if x["dur"] is not None]
            last["dur"] = round(sum(durs) / len(durs), 2) if durs else None
            series.append(last)
        cur = pts[-1] if pts else None
        snap = self._scaled(cur["ctx"]) if cur else dict(self.cats)
        tools_ctx = sorted(self.by_tool.items(), key=lambda kv: -kv[1])
        shown = sum(v for _, v in tools_ctx)
        scale = (snap["tool_results"] / shown) if shown else 1  # the same fit as the composition
        return {
            "provider": self.provider, "model": self.model, "window": self.window, "cats": list(CATS),
            "current": {"ctx": cur["ctx"] if cur else 0, "mix": snap, "at": cur["t"] if cur else None,
                        "tool_results": [{"name": k, "tokens": int(v * scale)} for k, v in tools_ctx[:10]]},
            "series": series, "step": step, "requests": len(pts), "turns": self.turn,
            "compactions": self.compactions,
            "totals": {"output": self.out, "reasoning": self.think, "cost": round(self.cost, 4),
                       "cache_hit": round(self.cr / self.inp, 4) if self.inp else None,
                       "model_s": round(self.model_s), "priced": bool(price_of(self.model))},
            "tool_time": sorted(({"name": k, "runs": v[0], "s": round(v[1], 1)} for k, v in self.tool_time.items()),
                                key=lambda r: -r["s"])[:12],
            "biggest": [{"tokens": b[0], "name": b[1], "brief": b[2], "t": b[3]} for b in self.big],
        }


class Traces:
    """Recently viewed agents' traces, kept in memory and read on from where they stopped."""

    def __init__(self, keep=24):
        self.keep = keep
        self.items = OrderedDict()
        self.lock = threading.Lock()

    def view(self, path, provider):
        with self.lock:
            tr = self.items.pop(path, None) or Trace(provider)
            self.items[path] = tr
            while len(self.items) > self.keep:
                self.items.popitem(last=False)
            tr.feed(path)
            return tr.view()
