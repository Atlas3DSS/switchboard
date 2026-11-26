"""Find accounts, live interactive sessions, tmux panes and transcript files. Read-only."""
import glob
import json
import os
import re
import shutil
import subprocess
import time

from . import config
from .store import STATE

CLAUDE_PROJECTS = os.path.expanduser("~/.claude/projects")
CODEX_SESSIONS = os.path.expanduser("~/.codex/sessions")
API_SESSIONS = os.path.join(STATE, "api-sessions")  # the built-in agent's transcripts (agent.py)
EXTRA_PROJECTS = []  # tests: more transcript roots (fake CLIs)
DEFAULT_CLAUDE_DIR = os.path.realpath(os.path.expanduser("~/.claude"))
_REAL_PROJECTS = CLAUDE_PROJECTS

# Claude Code on another company's endpoint: a Claude agent whose model is listed here talks to that
# Anthropic-format API (/v1/messages) instead of Anthropic. It also gets a home of its own (CLAUDE_CONFIG_DIR): its
# transcripts, history, settings and memory live there, shared with no Claude account. The key is read from the
# hub's environment, else from ~/.hermes/.env. Entry: {"model-name": {"url": ..., "key": "ENV_VAR", "home": "~/dir"}}.
CLAUDE_ENDPOINTS = {}


def endpoint(agent):
    """The endpoint entry a Claude agent's model is served by; None for a model Anthropic serves."""
    return CLAUDE_ENDPOINTS.get(agent.get("model")) if agent.get("provider") == "claude" else None


def endpoint_projects():
    """Transcript roots of the endpoint homes (none while a test has moved the transcript root elsewhere)."""
    if CLAUDE_PROJECTS != _REAL_PROJECTS:
        return []
    return [os.path.join(e["home"], "projects") for e in CLAUDE_ENDPOINTS.values()]


def endpoint_key(name):
    if os.environ.get(name):
        return os.environ[name]
    try:
        for line in open(os.path.expanduser("~/.hermes/.env"), encoding="utf-8"):
            k, _, v = line.strip().partition("=")
            if k == name:
                return v.strip("'\"")
    except OSError:
        pass
    return ""


def endpoint_home(agent, folder=None):
    """Make the endpoint agent's own home ready and return it: private folder, first-run screens answered, and the
    folder the owner put the agent in trusted (the CLI would otherwise open on those questions and hold its queue)."""
    home = endpoint(agent)["home"]
    os.makedirs(os.path.join(home, "projects"), mode=0o700, exist_ok=True)
    if os.stat(home).st_mode & 0o077:
        os.chmod(home, 0o700)  # the owner's alone
    path = os.path.join(home, ".claude.json")
    try:
        with open(path) as f:
            cfg = json.load(f)
    except (OSError, ValueError):
        cfg = {}
    before = json.dumps(cfg, sort_keys=True)
    cfg.setdefault("hasCompletedOnboarding", True)
    cfg.setdefault("theme", "dark")
    if folder:
        cfg.setdefault("projects", {}).setdefault(folder, {})["hasTrustDialogAccepted"] = True
    if json.dumps(cfg, sort_keys=True) != before:
        tmp = path + ".sb-tmp"
        with open(os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600), "w") as f:
            json.dump(cfg, f, indent=2)
        os.replace(tmp, path)
    return home


def endpoint_env(agent, folder=None):
    """Environment that points a Claude agent's CLI at its model's own endpoint and home; {} for Anthropic's models."""
    e = endpoint(agent)
    if not e:
        return {}
    # the token replaces any login for these calls; the small/background model must be one the endpoint has; nothing
    # besides the model calls leaves the machine (no telemetry, error reports or update checks to Anthropic)
    return {"ANTHROPIC_BASE_URL": e["url"], "ANTHROPIC_AUTH_TOKEN": endpoint_key(e["key"]),
            "ANTHROPIC_DEFAULT_HAIKU_MODEL": agent["model"], "ANTHROPIC_SMALL_FAST_MODEL": agent["model"],
            "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1", "CLAUDE_CONFIG_DIR": endpoint_home(agent, folder)}


AGENT_CLIS = ("claude", "codex", "gemini", "hermes", "antigravity")  # interactive agent CLIs Switchboard runs as terminals


def slug(s):
    return re.sub(r"[^a-z0-9]+", "-", (s or "").lower()).strip("-")[:48] or "agent"


def accounts():
    """Your accounts from the setup page (config.py), as {name, kind, label, model, config_dir}; keys stay out."""
    out = []
    for a in config.accounts():
        out.append({"name": a["name"], "kind": a["kind"], "label": a.get("label") or "", "model": a.get("model"),
                    "auth": "key" if a.get("api_key") else ("none" if a["kind"] == "api" else "login"), "base_url": a.get("base_url"),
                    "config_dir": os.path.expanduser(a.get("config_dir") or "")})
    return out


def account_env(account_name, kind="claude"):
    """Environment for running a CLI (or the built-in agent) under an account."""
    env = dict(os.environ)
    env.pop("CLAUDECODE", None)
    env.pop("CLAUDE_CODE_ENTRYPOINT", None)
    a = config.account(account_name)
    if a:
        config.env_for(a, env)
    return env


def claude_live_sessions():
    """Union of `claude agents --json` over every Claude account dir, keyed by pid or session id."""
    seen = {}
    if not shutil.which("claude"):
        return []
    for a in accounts():
        if a["kind"] != "claude":
            continue
        try:
            out = subprocess.run(["claude", "agents", "--json"], env=account_env(a["name"]), capture_output=True,
                                 text=True, timeout=15).stdout
            rows = json.loads(out or "[]")
        except (subprocess.SubprocessError, ValueError, OSError):
            continue
        for r in rows:
            key = r.get("pid") or r.get("sessionId")
            if key in seen:
                continue
            r["_account_dir"] = a["name"]
            seen[key] = r
    return list(seen.values())


def tmux_panes():
    fmt = "\t".join(["#{session_name}", "#{window_index}.#{pane_index}", "#{pane_pid}", "#{pane_current_path}",
                     "#{@claude_session}", "#{@sb_account}"])
    try:
        out = subprocess.run(["tmux", "list-panes", "-a", "-F", fmt], capture_output=True, text=True,
                             timeout=5).stdout
    except (subprocess.SubprocessError, OSError):
        return []
    panes = []
    for line in out.splitlines():
        f = line.split("\t")
        if len(f) < 6:
            continue
        sess, idx, pid, cwd, csess, account = f
        kind = None
        descendants = _descendants(int(pid))
        for p in [int(pid)] + descendants:
            cmd = _cmdline(p)
            if re.search(r"(^|/)codex(\s|$)", cmd) or "/@openai/codex" in cmd:
                kind = "codex"
                break
            if re.search(r"(^|/)claude(\s|$)", cmd):
                kind = "claude"
                break
        panes.append({"session": sess, "target": f"{sess}:{idx}", "pid": int(pid), "cwd": cwd, "kind": kind,
                      "claude_session": csess or None, "account": account or None,
                      "pids": set([int(pid)] + descendants)})
    return panes


def _cmdline(pid):
    try:
        return open(f"/proc/{pid}/cmdline", "rb").read().replace(b"\0", b" ").decode(errors="replace").strip()
    except OSError:
        return ""


def _descendants(pid, depth=3):
    out, frontier = [], [pid]
    for _ in range(depth):
        nxt = []
        for p in frontier:
            try:
                kids = open(f"/proc/{p}/task/{p}/children").read().split()
            except OSError:
                kids = []
            nxt += [int(k) for k in kids]
        out += nxt
        frontier = nxt
    return out


def pid_alive(pid):
    if not pid:
        return False
    try:
        os.kill(int(pid), 0)
    except (OSError, ValueError):
        return False
    try:
        return open(f"/proc/{pid}/stat").read().split()[2] != "Z"
    except OSError:
        return False


_path_cache = {}


def transcript_format(provider):
    """Which transcript parser reads this provider: the built-in agent writes Claude-format lines; Hermes sessions
    are rows in its state.db (switchboard/hermes.py)."""
    return {"claude": "claude", "api": "claude", "codex": "codex", "hermes": "hermes", "antigravity": "antigravity"}.get(provider)


def api_transcript(session_id):
    hits = glob.glob(os.path.join(API_SESSIONS, "*", f"{session_id}.jsonl"))
    return max(hits, key=os.path.getmtime) if hits else None


def claude_transcript(session_id):
    p = _path_cache.get(("c", session_id))
    if p and os.path.exists(p):
        return p
    hits = [h for root in [CLAUDE_PROJECTS] + endpoint_projects() + EXTRA_PROJECTS
            for h in glob.glob(os.path.join(root, "*", f"{session_id}.jsonl"))]
    if hits:
        hits.sort(key=os.path.getmtime)
        _path_cache[("c", session_id)] = hits[-1]
        return hits[-1]
    return None


def codex_rollout(thread_id):
    p = _path_cache.get(("x", thread_id))
    if p and os.path.exists(p):
        return p
    hits = glob.glob(os.path.join(CODEX_SESSIONS, "*", "*", "*", f"rollout-*{thread_id}.jsonl"))
    if hits:
        _path_cache[("x", thread_id)] = hits[-1]
        return hits[-1]
    return None


def claude_project_roots():
    """Every Claude transcript root (the default one, each account's, test roots), each real directory once."""
    roots = [CLAUDE_PROJECTS] + [os.path.join(a["config_dir"], "projects") for a in accounts()
                                 if a["kind"] == "claude" and a["config_dir"]] + endpoint_projects() + EXTRA_PROJECTS
    seen, out = set(), []
    for r in roots:
        real = os.path.realpath(r)
        if real not in seen and os.path.isdir(real):
            seen.add(real)
            out.append(r)
    return out


def subagents(hours=72, model=None):
    """Subagents Claude sessions started with the Agent tool (<session>/subagents/agent-*.jsonl) and touched in
    the last `hours`, newest first. `model` keeps those whose model alias or actual model contains it."""
    from .store import iso_from_epoch
    cutoff = time.time() - hours * 3600
    out = []
    for root in claude_project_roots():
        for path in glob.glob(os.path.join(root, "*", "*", "subagents", "agent-*.jsonl")):
            try:
                mtime = os.path.getmtime(path)
            except OSError:
                continue
            if mtime < cutoff:
                continue
            try:
                meta = json.load(open(path[:-len(".jsonl")] + ".meta.json"))
            except (OSError, ValueError):
                meta = {}
            first, used = {}, None
            try:
                with open(path, errors="replace") as f:
                    for i, line in enumerate(f):
                        if i == 0 or '"type":"assistant"' in line:
                            e = json.loads(line)
                            if i == 0:
                                first = e
                            else:
                                used = (e.get("message") or {}).get("model")
                                if used and not used.startswith("<"):
                                    break
                                used = None
                        if i > 400:
                            break
            except (OSError, ValueError):
                pass
            alias = meta.get("model") or ""
            if model and model.lower() not in f"{alias} {used or ''}".lower():
                continue
            out.append({"id": os.path.basename(path)[len("agent-"):-len(".jsonl")],
                        "model": used or alias or None, "alias": alias or None,
                        "type": meta.get("agentType"), "description": meta.get("description") or "",
                        "finished": meta.get("finished"), "background": meta.get("requestShape") == "background",
                        "parent_session": first.get("sessionId") or os.path.basename(os.path.dirname(os.path.dirname(path))),
                        "cwd": first.get("cwd"), "started_at": first.get("timestamp"),
                        "updated_at": iso_from_epoch(mtime), "_m": mtime})
    out.sort(key=lambda x: -x.pop("_m"))
    return out


def transcript_path(provider, sid, account=None):
    """Where an agent's conversation is: a Claude jsonl, a Codex rollout, or a Hermes session (hermes:<db>#<id>)."""
    if not sid:
        return None
    if provider == "claude":
        return claude_transcript(sid)
    if provider == "codex":
        return codex_rollout(sid)
    if provider == "api":
        return api_transcript(sid)
    if provider == "hermes":
        from . import hermes
        d = next((a["config_dir"] for a in accounts() if a["name"] == account and a["kind"] == "hermes"), None)
        if d or account:
            return hermes.path_for(sid, d)
        for d in [a["config_dir"] for a in accounts() if a["kind"] == "hermes" and a["config_dir"]] + [None]:
            p = hermes.path_for(sid, d)
            if p:
                return p
        return None
    if provider == "antigravity":
        from . import antigravity
        d = next((a["config_dir"] for a in accounts() if a["name"] == account and a["kind"] == "antigravity"), None)
        p = antigravity.path_for(sid, d)
        return p if p and os.path.exists(p) else None
    return None


_rollout_meta_cache = {}  # path -> (ino, mtime_ns, meta)


def recent_codex_rollouts(days=7):
    """[(mtime, path, meta)] for rollouts touched recently, newest first."""
    cutoff = time.time() - days * 86400
    out = []
    for path in glob.glob(os.path.join(CODEX_SESSIONS, "*", "*", "*", "rollout-*.jsonl")):
        try:
            st = os.stat(path)
        except OSError:
            continue
        m = st.st_mtime
        if m < cutoff:
            continue
        cached = _rollout_meta_cache.get(path)
        if cached and cached[0] == st.st_ino and (cached[2] or cached[1] == st.st_mtime_ns):
            meta = cached[2]
        else:
            meta = {}
            try:
                with open(path, errors="replace") as f:
                    first = json.loads(f.readline())
                if first.get("type") == "session_meta":
                    meta = first.get("payload") or {}
            except (OSError, ValueError):
                pass
            _rollout_meta_cache[path] = (st.st_ino, st.st_mtime_ns, meta)
        out.append((m, path, meta))
    out.sort(key=lambda t: -t[0])
    return out


def _tail_lines(path, nbytes=400_000):
    try:
        with open(path, "rb") as f:
            f.seek(0, 2)
            size = f.tell()
            f.seek(max(0, size - nbytes))
            data = f.read().decode(errors="replace")
    except OSError:
        return []
    lines = data.splitlines()
    return lines[1:] if size > nbytes else lines


def _text(content):
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "\n".join(c.get("text", "") for c in content if isinstance(c, dict) and c.get("type") in
                         ("text", "output_text", "input_text"))
    return ""


def last_assistant_text(path, provider):
    """Last assistant text in a transcript (Claude jsonl, Codex rollout, Hermes or Antigravity session)."""
    if provider in ("hermes", "antigravity"):
        from . import transcript
        return transcript.last_reply(path, provider)[0]
    for line in reversed(_tail_lines(path)):
        try:
            e = json.loads(line)
        except ValueError:
            continue
        if provider != "codex":  # Claude and built-in agent transcripts share a format
            if e.get("type") == "assistant" and not e.get("isSidechain"):
                t = _text((e.get("message") or {}).get("content")).strip()
                if t:
                    return t
        else:
            p = e.get("payload") or {}
            if p.get("type") in ("agent_message",) and p.get("message"):
                return p["message"].strip()
            if p.get("type") == "message" and p.get("role") == "assistant":
                t = _text(p.get("content")).strip()
                if t:
                    return t
    return ""


_SET_MODEL = re.compile(r"Set model to\s+[`*]*(?:\x1b\[[0-9;]*m)*([^`*\x1b\n<]+)")


def _model_cmd(line):
    """Model id from the TUI's `/model` echo ("Set model to `Fable 5.2` ..."): display name -> claude-fable-5-2."""
    try:
        e = json.loads(line)
    except ValueError:
        return None
    c = (e.get("message") or {}).get("content")
    if isinstance(c, list):
        c = " ".join(x.get("text", "") for x in c if isinstance(x, dict))
    m = _SET_MODEL.search(c or "")
    if not m:
        return None
    name = m.group(1).strip()
    d = re.match(r"(?i)default\s*\((.+)\)", name)  # "Default (Opus 5.5)"
    name = re.sub(r"\s*\(.*\)\s*$", "", d.group(1) if d else name).strip()  # drop "(1M context)"
    if not name:
        return None
    if re.fullmatch(r"[\w.\[\]-]+", name) and "-" in name:  # already an id (claude-opus-5-5)
        return name
    slug = re.sub(r"[\s.]+", "-", name.lower())
    return slug if slug.startswith("claude-") else "claude-" + slug


def last_model(path, provider):
    """Model of the newest main-thread reply (Claude) or turn (Codex): follows /model switches made in the TUI."""
    if provider == "hermes":
        from . import hermes
        return hermes.last_model(path)
    if provider == "antigravity":
        from . import antigravity
        return antigravity.last_model(path)
    from .transcript import _reverse_lines
    for line in _reverse_lines(path, 16 << 20):
        if provider != "codex":  # Claude and built-in agent transcripts share a format
            if "Set model to" in line and "local-command-stdout" in line:  # /model typed in the TUI, before any reply on it
                m = _model_cmd(line)
                if m:
                    return m
                continue
            if '"type":"assistant"' not in line or '"isSidechain":true' in line:
                continue
            try:
                m = (json.loads(line).get("message") or {}).get("model")
            except ValueError:
                continue
            if m and not m.startswith("<"):  # "<synthetic>" = a local error message, not a model
                return m
        elif '"turn_context"' in line:
            try:
                m = (json.loads(line).get("payload") or {}).get("model")
            except ValueError:
                continue
            if m:
                return m
    return None


def _scan_claude_or_codex(path, provider, needle, since_epoch, match_kind="read"):
    """Scan a Claude or Codex transcript newest-first for a matching record after since_epoch.
    match_kind='read': user entry or Claude queued_command attachment.
    match_kind='accepted': Claude queue-operation/enqueue record or any 'read' record.
    Returns the record timestamp (or True if the record had no timestamp), or None."""
    from .store import epoch
    from .transcript import _reverse_lines
    needle = " ".join(needle.split())[:80]
    if not needle:
        return None
    probe = needle[:40]
    escaped = json.dumps(probe)[1:-1]  # as the jsonl line spells it: a quote or backslash in the text is escaped there
    for line in _reverse_lines(path, 64 << 20):  # newest first; entries can be huge, so no fixed tail window
        if escaped not in line and probe not in " ".join(line.split()) and probe not in line:
            m = re.search(r'"timestamp":\s*"([^"]+)"', line[:4000])
            if m and epoch(m.group(1)) < since_epoch - 300:
                break
            continue
        try:
            e = json.loads(line)
        except ValueError:
            continue
        ts = e.get("timestamp") or ""
        if provider != "codex":  # Claude and built-in agent transcripts share a format
            typ = e.get("type")
            if typ == "user":
                t = _text((e.get("message") or {}).get("content"))
            elif typ == "attachment":
                att = e.get("attachment") or {}
                if att.get("type") != "queued_command":
                    continue
                t = _text(att.get("prompt"))
                ts = ts or att.get("timestamp") or ""
            elif match_kind == "accepted" and typ == "queue-operation" and e.get("operation") == "enqueue":
                t = _text(e.get("content"))
            else:
                continue
        else:
            p = e.get("payload") or {}
            t = p.get("message") if p.get("type") == "user_message" else _text(p.get("content"))
        if needle in " ".join((t or "").split()):
            if not ts or epoch(ts) >= since_epoch - 5:
                # Claude stamps queued_command with the enqueue time, not when it was read: return True so caller records now()
                if provider == "claude" and e.get("type") == "attachment":
                    return True
                return ts or True
    return None


def transcript_read_at(path, provider, needle, since_epoch):
    """Timestamp string (or True if unstamped) when `needle` was read into the transcript after since_epoch, or None."""
    if not path:
        return None
    if provider == "hermes":
        from . import hermes
        return True if hermes.has_user_text(path, needle, since_epoch) else None
    if provider == "antigravity":
        from . import antigravity
        return antigravity.read_at(path, needle, since_epoch)
    return _scan_claude_or_codex(path, provider, needle, since_epoch, "read")


def transcript_accepted(path, provider, needle, since_epoch):
    """True if the CLI has accepted `needle` into its input queue or transcript after since_epoch."""
    if not path:
        return False
    if provider in ("hermes", "antigravity"):
        return bool(transcript_read_at(path, provider, needle, since_epoch))
    return bool(_scan_claude_or_codex(path, provider, needle, since_epoch, "accepted"))


def transcript_has_user_text(path, provider, needle, since_epoch):
    """True if a user turn (or Claude mid-turn queued_command) containing `needle` was read after since_epoch."""
    return bool(transcript_read_at(path, provider, needle, since_epoch))
