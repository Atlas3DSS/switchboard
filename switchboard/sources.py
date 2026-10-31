"""Live project sources: git repos, status files, commands. Status comes from these, not from agent claims."""
import glob
import json
import os
import subprocess
import time

from .store import iso_from_epoch

MAX = 1200


def _git(path):
    path = os.path.expanduser(path)
    try:
        out = subprocess.run(["git", "-C", path, "log", "-6", "--format=%ct\t%h %s"], capture_output=True,
                             text=True, timeout=10)
    except (subprocess.SubprocessError, OSError) as e:
        return {"ok": False, "summary": str(e)}
    if out.returncode != 0:
        return {"ok": False, "summary": out.stderr.strip()[:MAX]}
    rows = [l.split("\t", 1) for l in out.stdout.splitlines() if "\t" in l]
    if not rows:
        return {"ok": True, "summary": "no commits", "lines": []}
    lines = [f"{_age(int(t))} · {s}" for t, s in rows]
    return {"ok": True, "mtime": iso_from_epoch(int(rows[0][0])), "summary": "\n".join(lines)[:MAX], "lines": lines}


def _file(path):
    pats = sorted(glob.glob(os.path.expanduser(path)), key=os.path.getmtime)
    if not pats:
        return {"ok": False, "summary": "missing"}
    p = pats[-1]
    m = os.path.getmtime(p)
    try:
        if p.endswith(".json") and os.path.getsize(p) < 5_000_000:
            d = json.load(open(p))
            summary = _json_summary(d)
        else:
            with open(p, errors="replace") as f:
                f.seek(0, 2)
                size = f.tell()
                f.seek(max(0, size - 4000))
                text = f.read()
            summary = text.strip()[-MAX:] if not p.endswith(".md") else _md_head(p)
    except (OSError, ValueError) as e:
        return {"ok": False, "mtime": iso_from_epoch(m), "summary": str(e)}
    label = os.path.basename(p) if len(pats) > 1 else None
    return {"ok": True, "mtime": iso_from_epoch(m), "summary": summary[:MAX], "resolved": label}


def _md_head(p):
    with open(p, errors="replace") as f:
        return f.read(MAX)


def _json_summary(d):
    if isinstance(d, dict):
        for key in ("status", "state", "summary", "verdict", "outcome"):
            if key in d and isinstance(d[key], (str, int, float)):
                head = f"{key}: {d[key]}"
                break
        else:
            head = ""
        keys = ", ".join(list(d)[:30])
        return (head + "\n" if head else "") + f"keys: {keys}"
    if isinstance(d, list):
        return f"{len(d)} items"
    return str(d)[:MAX]


def _recent(path, max_entries=20000, depth=3):
    """Newest files under path (bounded walk; never follows symlinks)."""
    root = os.path.expanduser(path)
    if not os.path.isdir(root):
        return {"ok": False, "summary": "missing"}
    files, n = [], 0
    base_depth = root.rstrip("/").count("/")
    for d, dirs, names in os.walk(root, followlinks=False):
        dirs[:] = [x for x in dirs if not x.startswith(".") and x not in ("node_modules", "__pycache__", "venv",
                                                                           ".venv", "Library", "tmp", "Temp")]
        if d.count("/") - base_depth >= depth:
            dirs[:] = []
        for nm in names:
            n += 1
            if n > max_entries:
                break
            p = os.path.join(d, nm)
            try:
                if not os.path.islink(p):
                    files.append((os.path.getmtime(p), os.path.relpath(p, root)))
            except OSError:
                pass
        if n > max_entries:
            break
    files.sort(reverse=True)
    lines = [f"{_age(t)} · {rel}" for t, rel in files[:8]]
    return {"ok": True, "mtime": iso_from_epoch(files[0][0]) if files else None, "summary": "\n".join(lines),
            "lines": lines}


def _cmd(cmd, cwd=None):
    try:
        out = subprocess.run(cmd, shell=True, capture_output=True, text=True, timeout=15,
                             cwd=os.path.expanduser(cwd) if cwd else None)
    except subprocess.TimeoutExpired:
        return {"ok": False, "summary": "timed out after 15 s"}
    text = (out.stdout + (("\n" + out.stderr) if out.returncode else "")).strip()
    return {"ok": out.returncode == 0, "mtime": iso_from_epoch(time.time()), "summary": text[-MAX:],
            "lines": text.splitlines()[-12:]}


def _age(t):
    s = time.time() - t
    if s < 3600:
        return f"{int(s // 60)} m ago"
    if s < 86400:
        return f"{int(s // 3600)} h ago"
    return f"{int(s // 86400)} d ago"


def probe(src):
    kind, path = src.get("kind"), src.get("path", "")
    if kind == "git":
        r = _git(path)
    elif kind == "file":
        r = _file(path)
    elif kind == "recent":
        r = _recent(path)
    elif kind == "cmd":
        r = _cmd(path, src.get("cwd"))
    elif kind == "url":
        r = {"ok": True, "summary": path}
    else:
        r = {"ok": False, "summary": f"unknown source kind {kind}"}
    r.setdefault("lines", [])
    r.setdefault("mtime", None)
    return {"kind": kind, "label": src.get("label") or path, "path": path, **r}
