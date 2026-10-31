"""Your AI accounts: which subscriptions and API keys Switchboard may use, written by the setup page or `sb setup`.

The file is ~/.config/switchboard/accounts.json (SB_CONFIG overrides), mode 0600 because it can hold API keys.
Keys never leave the hub: the API returns them masked.

Account kinds (an agent's `provider` is its account's kind):
  claude  Claude Code CLI      a Claude subscription login, or an Anthropic API key
  codex   Codex CLI            a ChatGPT subscription login, or an OpenAI API key
  gemini  Gemini CLI           a Google account login, or a Gemini API key
  api     built-in agent       any OpenAI-compatible endpoint: Anthropic, OpenAI, Google, OpenRouter, Ollama, vLLM, ...
"""
import json
import os
import re
import shutil
import subprocess
import urllib.error
import urllib.request

PATH = os.path.expanduser(os.environ.get("SB_CONFIG", "~/.config/switchboard/accounts.json"))
CONFIG_DIR = os.path.dirname(PATH)
KINDS = ("claude", "codex", "gemini", "api")
CLI = {"claude": "claude", "codex": "codex", "gemini": "gemini"}
KEY_ENV = {"claude": "ANTHROPIC_API_KEY", "codex": "OPENAI_API_KEY", "gemini": "GEMINI_API_KEY"}
INSTALL = {"claude": "curl -fsSL https://claude.ai/install.sh | bash",
           "codex": "npm install -g @openai/codex",
           "gemini": "npm install -g @google/gemini-cli"}
DEFAULT_WORKSPACE = "~/switchboard-projects"

# OpenAI-compatible endpoints the built-in agent knows out of the box. `models` are suggestions only: the setup page
# lists what the endpoint itself reports (GET /models) once a key is entered.
PRESETS = {
    "anthropic": {"label": "Anthropic API", "base_url": "https://api.anthropic.com/v1/", "env": "ANTHROPIC_API_KEY",
                  "models": ["claude-sonnet-5-5", "claude-opus-5-5", "claude-haiku-5-5"],
                  "keys_at": "https://console.anthropic.com/settings/keys"},
    "openai": {"label": "OpenAI API", "base_url": "https://api.openai.com/v1", "env": "OPENAI_API_KEY", "models": [],
               "keys_at": "https://platform.openai.com/api-keys"},
    "google": {"label": "Google Gemini API", "base_url": "https://generativelanguage.googleapis.com/v1beta/openai/",
               "env": "GEMINI_API_KEY", "models": [], "keys_at": "https://aistudio.google.com/apikey"},
    "openrouter": {"label": "OpenRouter", "base_url": "https://openrouter.ai/api/v1", "env": "OPENROUTER_API_KEY",
                   "models": [], "keys_at": "https://openrouter.ai/keys"},
    "custom": {"label": "OpenAI-compatible endpoint", "base_url": "", "env": "", "models": [], "keys_at": ""},
}


def slug(s):
    return re.sub(r"[^a-z0-9]+", "-", (s or "").lower()).strip("-")[:32]


def load():
    try:
        with open(PATH) as f:
            cfg = json.load(f)
    except (OSError, ValueError):
        cfg = {}
    cfg.setdefault("accounts", [])
    cfg.setdefault("workspace", DEFAULT_WORKSPACE)
    return cfg


def save(cfg):
    os.makedirs(CONFIG_DIR, mode=0o700, exist_ok=True)
    tmp = PATH + ".tmp"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        json.dump(cfg, f, indent=1)
    os.replace(tmp, PATH)


def configured():
    return bool(load()["accounts"])


def workspace():
    return os.path.expanduser(os.environ.get("SB_WORKSPACE") or load().get("workspace") or DEFAULT_WORKSPACE)


def accounts():
    return load()["accounts"]


def account(name):
    return next((a for a in accounts() if a.get("name") == name), None)


def mask(key):
    if not key:
        return ""
    return key[:4] + "…" + key[-4:] if len(key) > 12 else "…"


def public(a):
    """An account as the API shows it: the key masked."""
    out = {k: v for k, v in a.items() if k != "api_key"}
    out["has_key"] = bool(a.get("api_key"))
    out["key_hint"] = mask(a.get("api_key"))
    return out


def normalize(a, old=None):
    """Validate one account from the setup page. A blank api_key keeps the stored one."""
    kind = a.get("kind")
    if kind not in KINDS:
        raise ValueError(f"kind must be one of {', '.join(KINDS)}")
    name = slug(a.get("name") or kind)
    if not name:
        raise ValueError("every account needs a name")
    out = {"name": name, "kind": kind, "label": (a.get("label") or "").strip()[:60]}
    key = (a.get("api_key") or "").strip() or (old or {}).get("api_key") or ""
    if key:
        out["api_key"] = key
    if a.get("config_dir"):
        out["config_dir"] = a["config_dir"].strip()
    if kind == "api":
        preset = a.get("preset") if a.get("preset") in PRESETS else "custom"
        base = (a.get("base_url") or PRESETS[preset]["base_url"]).strip()
        if not re.match(r"^https?://", base):
            raise ValueError(f"{name}: base URL must start with http:// or https://")
        if not (a.get("model") or "").strip():
            raise ValueError(f"{name}: pick a model")
        out.update(preset=preset, base_url=base, model=a["model"].strip(),
                   models=[m for m in (a.get("models") or []) if isinstance(m, str)][:400])
    elif a.get("model"):
        out["model"] = a["model"].strip()
    return out


def set_accounts(items, workspace_dir=None):
    cfg = load()
    old = {a["name"]: a for a in cfg["accounts"]}
    out, names = [], set()
    for a in items:
        n = normalize(a, old.get(slug(a.get("name") or a.get("kind") or "")))
        if n["name"] in names:
            raise ValueError(f"two accounts are named {n['name']}")
        names.add(n["name"])
        out.append(n)
    cfg["accounts"] = out
    if workspace_dir:
        cfg["workspace"] = workspace_dir.strip()
    save(cfg)
    for a in out:
        if a["kind"] == "codex" and a.get("api_key"):
            codex_api_login(a)
    return cfg


def codex_home(a):
    """Codex keeps its login in CODEX_HOME: an API-key account gets its own, so it never touches your ChatGPT login."""
    if a.get("config_dir"):
        return os.path.expanduser(a["config_dir"])
    if a.get("api_key"):
        return os.path.join(CONFIG_DIR, "codex-" + a["name"])
    return None


def codex_api_login(a):
    home = codex_home(a)
    os.makedirs(home, mode=0o700, exist_ok=True)
    try:
        subprocess.run([shutil.which("codex") or "codex", "login", "--with-api-key"], input=a["api_key"], text=True,
                       env={**os.environ, "CODEX_HOME": home}, capture_output=True, timeout=30)
    except (OSError, subprocess.SubprocessError):
        pass


def env_for(a, env):
    """Point a CLI at this account: its config dir and/or API key."""
    kind = a["kind"]
    if kind == "claude":
        if a.get("config_dir"):
            env["CLAUDE_CONFIG_DIR"] = os.path.expanduser(a["config_dir"])
        if a.get("api_key"):
            env["ANTHROPIC_API_KEY"] = a["api_key"]
    elif kind == "codex":
        home = codex_home(a)
        if home:
            env["CODEX_HOME"] = home
    elif kind == "gemini":
        if a.get("api_key"):
            env["GEMINI_API_KEY"] = a["api_key"]
    elif kind == "api":
        env["SB_API_BASE"] = a.get("base_url") or ""
        env["SB_API_KEY"] = a.get("api_key") or ""
        env["SB_API_MODEL"] = a.get("model") or ""
        env["SB_API_PRESET"] = a.get("preset") or "custom"
    return env


# ---------- what is on this machine (for the setup page) ----------
def _exists(*paths):
    return any(os.path.exists(os.path.expanduser(p)) for p in paths)


def detect():
    """Installed CLIs, whether each looks logged in, and API keys already in the environment (never their values)."""
    clis = {}
    for kind, exe in CLI.items():
        path = shutil.which(exe)
        version = None
        if path:
            try:
                version = subprocess.run([path, "--version"], capture_output=True, text=True,
                                         timeout=10).stdout.strip().splitlines()[0][:60]
            except (OSError, subprocess.SubprocessError, IndexError):
                version = None
        login = {"claude": _exists("~/.claude/.credentials.json") or _claude_oauth(),
                 "codex": _exists("~/.codex/auth.json"),
                 "gemini": _exists("~/.gemini/oauth_creds.json", "~/.gemini/google_accounts.json")}[kind]
        clis[kind] = {"installed": bool(path), "version": version, "logged_in": bool(login), "install": INSTALL[kind]}
    env = {k: bool(os.environ.get(k)) for k in
           ("ANTHROPIC_API_KEY", "OPENAI_API_KEY", "GEMINI_API_KEY", "GOOGLE_API_KEY", "OPENROUTER_API_KEY")}
    return {"clis": clis, "env": env}


def _claude_oauth():
    try:
        with open(os.path.expanduser("~/.claude.json")) as f:
            return bool(json.load(f).get("oauthAccount"))
    except (OSError, ValueError):
        return False


def env_key(name):
    """An API key from the hub's own environment (the setup page offers 'use the key in $NAME')."""
    if name in ("ANTHROPIC_API_KEY", "OPENAI_API_KEY", "GEMINI_API_KEY", "GOOGLE_API_KEY", "OPENROUTER_API_KEY"):
        return os.environ.get(name) or ""
    return ""


# ---------- talking to an OpenAI-compatible endpoint ----------
def headers(base_url, key, preset=None):
    h = {"Content-Type": "application/json", "User-Agent": "switchboard"}
    if key:
        h["Authorization"] = "Bearer " + key
        if preset == "anthropic" or "anthropic.com" in base_url:
            h["x-api-key"] = key
            h["anthropic-version"] = "2023-06-01"
    if preset == "openrouter" or "openrouter.ai" in base_url:
        h["X-Title"] = "Switchboard"
    return h


def url(base_url, path):
    return base_url.rstrip("/") + "/" + path.lstrip("/")


def list_models(base_url, key, preset=None, timeout=20):
    req = urllib.request.Request(url(base_url, "models"), headers=headers(base_url, key, preset))
    with urllib.request.urlopen(req, timeout=timeout) as r:
        data = json.loads(r.read())
    rows = data.get("data") if isinstance(data, dict) else data
    ids = []
    for m in rows or []:
        mid = m.get("id") if isinstance(m, dict) else m
        if isinstance(mid, str):
            ids.append(mid[len("models/"):] if mid.startswith("models/") else mid)
    return sorted(set(ids))


def test_api(a):
    """Check an API account: list its models, then (when a model is picked) one tiny chat completion."""
    preset = a.get("preset") if a.get("preset") in PRESETS else "custom"
    base = (a.get("base_url") or PRESETS[preset]["base_url"]).strip()
    key = (a.get("api_key") or "").strip()
    if not key and a.get("name"):
        key = (account(slug(a["name"])) or {}).get("api_key") or ""
    if not key and a.get("key_env"):
        key = env_key(a["key_env"])
    if not base:
        return {"ok": False, "error": "base URL is required"}
    out = {"ok": False, "models": [], "base_url": base}
    try:
        out["models"] = list_models(base, key, preset)
    except Exception as e:
        out["models_error"] = _err(e)
    model = (a.get("model") or "").strip()
    if not model:
        out["ok"] = bool(out["models"])
        if not out["ok"]:
            out["error"] = out.get("models_error") or "the endpoint listed no models; type the model name"
        return out
    body = {"model": model, "messages": [{"role": "user", "content": "Reply with the word ok."}], "max_tokens": 16}
    req = urllib.request.Request(url(base, "chat/completions"), data=json.dumps(body).encode(),
                                 headers=headers(base, key, preset))
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            reply = json.loads(r.read())
        msg = ((reply.get("choices") or [{}])[0].get("message") or {}).get("content")
        out.update(ok=True, reply=(msg or "")[:200])
    except Exception as e:
        out["error"] = _err(e)
    return out


def _err(e):
    if isinstance(e, urllib.error.HTTPError):
        try:
            body = e.read().decode(errors="replace")[:400]
        except Exception:
            body = ""
        return f"HTTP {e.code}: {body or e.reason}"
    if isinstance(e, urllib.error.URLError):
        return f"cannot reach the endpoint ({e.reason})"
    return str(e)[:400]
