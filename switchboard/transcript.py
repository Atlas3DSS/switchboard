"""Read an agent's conversation straight from its session transcript (Claude jsonl or Codex rollout).

Cursors are byte offsets of transcript lines, so paging is stable while the file grows.
"""
import base64
import json
import os

BLOCK = 1 << 20
SKIP_USER_PREFIXES = ("<system-reminder", "<command-", "<local-command", "Caveat:", "# AGENTS.md",
                      "<environment_context", "<user_instructions", "<turn_aborted", "[Request interrupted")
TEXT_CAP = 30000


def _claude_entries(e):
    if e.get("isSidechain") or e.get("isMeta"):
        return []
    t, ts = e.get("type"), e.get("timestamp")
    msg = e.get("message") or {}
    content = msg.get("content")
    out = []
    if t == "user":
        if isinstance(content, str):
            text = content.strip()
            if text.startswith("<task-notification"):
                out.append(("tool", "background task finished"))
            elif text and not text.startswith(SKIP_USER_PREFIXES):
                out.append(("user", text))
        elif isinstance(content, list):
            texts = [c.get("text", "") for c in content if isinstance(c, dict) and c.get("type") == "text"]
            text = "\n".join(x for x in texts if x and not x.lstrip().startswith(SKIP_USER_PREFIXES)).strip()
            n_img = sum(1 for c in content if isinstance(c, dict) and c.get("type") == "image")
            if text or n_img:
                out.append(("user", text, n_img))
    elif t == "assistant":
        if msg.get("model") == "<synthetic>":  # "No response requested." after a stop, rate-limit notices
            return []
        for c in content or []:
            if not isinstance(c, dict):
                continue
            if c.get("type") == "text" and c.get("text", "").strip():
                out.append(("assistant", c["text"].strip()))
            elif c.get("type") == "tool_use":
                inp = c.get("input") or {}
                brief = inp.get("description") or inp.get("command") or inp.get("file_path") or \
                    inp.get("prompt") or inp.get("pattern") or ""
                out.append(("tool", f"{c.get('name')}: {' '.join(str(brief).split())[:240]}"))
    return [(o[0], o[1], ts, o[2] if len(o) > 2 else 0) for o in out]


def _codex_entries(e):
    p = e.get("payload") or {}
    ts = e.get("timestamp")
    typ = p.get("type")
    if e.get("type") == "event_msg":
        if typ == "user_message":
            text = (p.get("message") or "").strip()
            return [] if not text or text.startswith(SKIP_USER_PREFIXES) else [("user", text, ts)]
        if typ == "agent_message":
            return [("assistant", (p.get("message") or "").strip(), ts)] if p.get("message") else []
        return []
    if e.get("type") != "response_item":
        return []
    if typ == "message" and p.get("role") in ("user", "assistant"):
        parts = [c for c in p.get("content") or [] if isinstance(c, dict)]
        text = "\n".join(c.get("text", "") for c in parts if c.get("text")).strip()
        n_img = sum(1 for c in parts if c.get("type") == "input_image")
        if (not text and not n_img) or (p["role"] == "user" and text.startswith(SKIP_USER_PREFIXES)):
            return []
        return [(p["role"], text, ts, n_img)]
    if typ in ("function_call", "custom_tool_call"):
        args = p.get("arguments") or p.get("input") or ""
        try:
            a = json.loads(args) if isinstance(args, str) and args.startswith("{") else {}
        except ValueError:
            a = {}
        brief = a.get("cmd") or a.get("command") or (args if isinstance(args, str) else "")
        if isinstance(brief, str) and not a:
            import re
            hit = re.search(r'cmd:\s*"((?:[^"\\]|\\.){0,240})', brief)
            brief = hit.group(1).replace("\\n", " ") if hit else brief
        if isinstance(brief, list):
            brief = " ".join(brief)
        return [("tool", f"{p.get('name')}: {' '.join(str(brief).split())[:240]}", ts)]
    return []


def _parse(line, provider):
    if '"type"' not in line:
        return []
    try:
        e = json.loads(line)
    except ValueError:
        return []
    rows = _codex_entries(e) if provider == "codex" else _claude_entries(e)
    return [tuple(r) + (0,) * (4 - len(r)) for r in rows]


def _dedupe(entries):
    """Codex logs carry some messages twice (event + response item); drop adjacent repeats."""
    out = []
    for ent in entries:
        if out and out[-1]["role"] == ent["role"] and out[-1]["text"] == ent["text"]:
            continue
        out.append(ent)
    return out


def _mk(off, k, role, text, ts, n_img=0):
    return {"id": off * 8 + k, "role": role, "text": text[:TEXT_CAP], "ts": ts, "n_images": n_img}


def read_chat(path, provider, limit=40, before=None, after=None):
    """Entries oldest→newest. `before`/`after` are entry ids from an earlier call."""
    if not path or not os.path.exists(path):
        return {"entries": [], "before": None, "latest": None}
    size = os.path.getsize(path)
    limit = max(1, min(int(limit), 400))
    if after is not None:
        start = int(after) // 8
        out = []
        with open(path, "rb") as f:
            f.seek(start)
            data = f.read(min(size - start, 32 * BLOCK))
        pos = start
        for raw in data.split(b"\n"):
            line_off, pos = pos, pos + len(raw) + 1
            if pos > start + len(data):  # incomplete trailing line
                break
            for k, (role, text, ts, n_img) in enumerate(_parse(raw.decode(errors="replace"), provider)):
                ent = _mk(line_off, k, role, text, ts, n_img)
                if ent["id"] > int(after):
                    out.append(ent)
        out = _dedupe(out)
        return {"entries": out, "before": None, "latest": out[-1]["id"] if out else int(after)}
    end = size if before is None else int(before) // 8
    collected = []  # oldest -> newest
    buf, buf_start = b"", end
    with open(path, "rb") as f:
        while buf_start > 0 and len(collected) < limit + 1:
            read_from = max(0, buf_start - BLOCK)
            f.seek(read_from)
            buf = f.read(buf_start - read_from) + buf
            buf_start = read_from
            lines = buf.split(b"\n")
            # the first piece may be a partial line unless we reached the file start
            head = lines.pop(0) if buf_start > 0 else None
            off = buf_start + (len(head) + 1 if head is not None else 0)
            found = []
            for raw in lines:
                if raw:
                    for k, (role, text, ts, n_img) in enumerate(_parse(raw.decode(errors="replace"), provider)):
                        ent = _mk(off, k, role, text, ts, n_img)
                        if before is None or ent["id"] < int(before):
                            found.append(ent)
                off += len(raw) + 1
            collected = found + collected
            buf = head if head is not None else b""
            if buf_start == 0:
                break
    entries = _dedupe(collected[-limit - 1:])
    has_more = len(entries) > limit
    entries = entries[-limit:]
    return {"entries": entries, "before": entries[0]["id"] if has_more and entries else None,
            "latest": entries[-1]["id"] if entries else None}


def last_reply(path, provider):
    """(text, ts) of the newest assistant message, from the transcript tail."""
    r = read_chat(path, provider, limit=30)
    for ent in reversed(r["entries"]):
        if ent["role"] == "assistant":
            return ent["text"], ent["ts"]
    return "", None


def _reverse_lines(path, max_bytes=256 << 20):
    """Complete lines newest first (bounded)."""
    with open(path, "rb") as f:
        f.seek(0, 2)
        pos = f.tell()
        stop = max(0, pos - max_bytes)
        tail = b""
        while pos > stop:
            step = min(BLOCK, pos - stop)
            pos -= step
            f.seek(pos)
            chunk = f.read(step) + tail
            lines = chunk.split(b"\n")
            tail = lines.pop(0) if pos > 0 else b""
            for raw in reversed(lines):
                if raw:
                    yield raw.decode(errors="replace")
            if pos == 0 and tail:
                yield tail.decode(errors="replace")


CLAUDE_WINDOW = 1_000_000  # recent Claude models in Claude Code; an approximation for other models


def usage_stats(path, provider, since_epoch):
    """{'context_tokens', 'context_window', 'tokens_24h', 'last_turn'} from a transcript.

    tokens_24h counts input + cache writes + output (cache reads excluded: cheap and re-counted every call)."""
    from .store import epoch
    out = {"context_tokens": None, "context_window": None, "tokens_24h": 0, "last_turn": None}
    if not path or not os.path.exists(path):
        return out
    seen = set()
    for line in _reverse_lines(path):
        if provider != "codex":
            if '"usage"' not in line or '"assistant"' not in line:
                if '"timestamp"' in line and '"type":"user"' in line:
                    try:
                        if epoch(json.loads(line).get("timestamp")) < since_epoch and out["context_tokens"] is not None:
                            break
                    except ValueError:
                        pass
                continue
            try:
                e = json.loads(line)
            except ValueError:
                continue
            if e.get("type") != "assistant" or e.get("isSidechain"):
                continue
            msg = e.get("message") or {}
            u = msg.get("usage") or {}
            mid = msg.get("id") or e.get("uuid")
            if mid in seen:
                continue
            seen.add(mid)
            ctx = (u.get("input_tokens") or 0) + (u.get("cache_creation_input_tokens") or 0) + \
                (u.get("cache_read_input_tokens") or 0)
            if not ctx:  # synthetic messages (rate-limit notices) carry no usage
                continue
            if out["context_tokens"] is None:
                out["context_tokens"] = ctx + (u.get("output_tokens") or 0)
                out["context_window"] = CLAUDE_WINDOW
                out["last_turn"] = {"input_tokens": ctx, "output_tokens": u.get("output_tokens") or 0,
                                    "cache_read": u.get("cache_read_input_tokens") or 0, "cost_usd": None,
                                    "at": e.get("timestamp")}
            if epoch(e.get("timestamp")) < since_epoch:
                break
            out["tokens_24h"] += (u.get("input_tokens") or 0) + (u.get("cache_creation_input_tokens") or 0) + \
                (u.get("output_tokens") or 0)
        else:
            if '"token_count"' not in line:
                continue
            try:
                e = json.loads(line)
            except ValueError:
                continue
            info = (e.get("payload") or {}).get("info") or {}
            last = info.get("last_token_usage") or {}
            if out["context_tokens"] is None and last:
                out["context_tokens"] = last.get("input_tokens", 0) + last.get("output_tokens", 0)
                out["context_window"] = info.get("model_context_window")
                out["last_turn"] = {"input_tokens": last.get("input_tokens", 0),
                                    "output_tokens": last.get("output_tokens", 0),
                                    "cache_read": last.get("cached_input_tokens", 0), "cost_usd": None,
                                    "at": e.get("timestamp")}
            if epoch(e.get("timestamp")) < since_epoch:
                break
            if last:
                out["tokens_24h"] += max(0, last.get("input_tokens", 0) - last.get("cached_input_tokens", 0)) + \
                    last.get("output_tokens", 0)
    return out


def image_at(path, provider, entry_id, n):
    """(bytes, media_type) of the n-th image in the transcript line behind a chat entry id."""
    off = int(entry_id) // 8
    with open(path, "rb") as f:
        f.seek(off)
        line = f.readline(64 << 20)
    e = json.loads(line)
    if provider != "codex":
        blocks = [c for c in ((e.get("message") or {}).get("content") or []) if isinstance(c, dict)
                  and c.get("type") == "image"]
        src = blocks[int(n)].get("source") or {}
        return base64.b64decode(src.get("data", "")), src.get("media_type", "image/png")
    blocks = [c for c in ((e.get("payload") or {}).get("content") or []) if isinstance(c, dict)
              and c.get("type") == "input_image"]
    url = blocks[int(n)].get("image_url", "")
    head, _, data = url.partition(",")
    media = head[5:].split(";")[0] if head.startswith("data:") else "image/png"
    return base64.b64decode(data), media
