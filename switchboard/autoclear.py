"""Auto-clear: a Claude or Codex terminal agent whose context passes its model's limit is cleared the Switchboard way, not
compacted the CLI's way.

The limit is a mark, not a blade. Past it nothing happens mid-turn: the hub waits for a natural pause (the turn
finished and confirmed, nothing in the composer, no dialog, no background shells or subagents the clear would end,
the screen still for QUIET_S) and only then types /clear. The hub follows the new session as for any /clear
(hub.follow_cleared), which queues the old conversation's outline for the project memory. The next message to reach
the agent, from the owner, another agent, an automation or sb, starts with a short reminder naming that outline.

Limits are per model family; setting 'autoclear' overrides them, 0 or null turns one off.

Codex (OpenAI models): their window is about 258k, so the Claude marks were never reached. The
'codex' limit applies to every Codex terminal whatever its model. Codex has no session to follow at once: /new starts
a chat whose rollout file is only written with its first turn. So the clear is taken as done when typed: the old
conversation is queued for the memory, the agent's thread is forgotten and the reminder armed. terminals.
_find_codex_thread then claims the new rollout when it appears (codex_claim), or the old one again if the CLI went on
writing to it, which means the /new did not take.
"""
import re
import time

from . import memory
from .store import epoch

DEFAULTS = {"fable": 382_000, "opus": 482_000, "haiku": 650_000, "codex": 125_000,
            "neon": 470_000, "antigravity": 750_000}
QUIET_S = 20         # the screen holds still this long after the turn before the clear is typed
FOLLOW_S = 90        # the new session should show up by then; if not, the clear is taken as not done
RETRY_S = 1800       # a clear that did not take is tried again no sooner than this
NEW_WHERE = re.compile(r"Where should the new conversation run", re.I)
CODEX_STILL_S = 30   # a Codex rollout written this long after its /new is still the live one: the clear did not take


def limits(store):
    out = dict(DEFAULTS)
    out.update({k: v for k, v in (store.setting("autoclear", {}) or {}).items()})
    return out


def limit_for(store, model, provider=None):
    """The mark for a model family, or for the provider as a whole (codex: any OpenAI model)."""
    m = (model or "").lower()
    for fam, n in limits(store).items():
        if fam in m or fam == provider:
            return int(n or 0) or None
    return None


def _key(aid):
    return f"autoclear:{aid}"


def state(store, aid):
    return store.setting(_key(aid)) or None


def eligible(agent):
    return (agent["mode"] == "terminal" and agent["provider"] in ("claude", "codex", "antigravity")
            and not agent.get("gone") and agent.get("role") not in ("helper", "desk") and agent.get("session_id"))


def check(terms, agent, t):
    """Called from the terminal tick for each live terminal, on the delivery thread (so no message is typed between
    the check and the clear). Types /clear when the agent is past its limit and at rest."""
    s, aid = terms.s, agent["id"]
    st = state(s, aid)
    if st and st.get("phase") == "clearing":
        if time.time() - st["ts"] > FOLLOW_S:  # the CLI never moved to a new session
            s.set_setting(_key(aid), {"phase": "failed", "ts": time.time()})
            s.event(aid, "error", "auto-clear: /clear was typed but no new session appeared; will try again later")
        return
    if st and st.get("phase") == "failed" and time.time() - st["ts"] < RETRY_S:
        return
    if not eligible(agent) or agent.get("paused"):
        return
    lim = limit_for(s, agent.get("model"), agent["provider"])
    tokens = agent.get("context_tokens") or 0
    if not lim or tokens < lim:
        return
    if aid in terms.switching or not t.alive:
        return
    ready, busy, comp = t.view()
    if not ready or busy or comp or t.dialog() or t.background():
        return
    if t.steady_for() < QUIET_S or time.time() - (t.last_output or 0) < QUIET_S:
        return
    if s.one("SELECT 1 n FROM messages WHERE to_agent=? AND state IN ('delivering','delivered')", (aid,)):
        return  # a turn the hub is still waiting to see finish
    codex = agent["provider"] == "codex"
    agy = agent["provider"] == "antigravity"
    deferred = codex or agy  # new conversation id appears in history/rollout only on the next prompt
    mark = {"phase": "clearing", "ts": time.time(), "from_session": agent["session_id"], "tokens": tokens,
            "limit": lim}
    if not deferred:
        s.set_setting(_key(aid), mark)
    s.event(aid, "system", f"auto-clear: context at {tokens // 1000}k, past the {lim // 1000}k limit for its model, "
                           "and the agent is at rest; clearing (the conversation goes to the project memory)")
    t.write("/new" if codex else "/clear")
    time.sleep(0.6)
    t.write("\r")
    if deferred:  # no new session to wait for: done when typed
        if codex:
            for _ in range(8):  # in a Git checkout Codex asks where the new chat runs; the first choice is "here"
                time.sleep(0.5)
                if NEW_WHERE.search("\n".join(t.text().splitlines()[-14:])):
                    t.write("\r")
                    break
        row = memory.session_ended(terms, agent, agent["session_id"], "auto-cleared at its context limit")
        if not row:  # nothing new since its last outline: name that one, and write it again if it failed
            last = s.one("SELECT id, state FROM memory_sessions WHERE session_id=? AND agent=? ORDER BY id DESC LIMIT 1",
                         (agent["session_id"], aid))
            if last and last["state"] == "failed":
                s.x("UPDATE memory_sessions SET state='pending', note=NULL WHERE id=?", (last["id"],))
            row = last["id"] if last and last["state"] != "skipped" else None
        memory.mark_autoclear(s, agent, agent["session_id"], row)
        extra = {"codex": 1} if codex else {"antigravity": 1}
        s.set_setting(_key(aid), {**mark, "phase": "remind", "outline": row, **extra, "ts": time.time()})
        s.set_agent(aid, session_id=None, context_tokens=0)


def codex_claim(store, aid, rollouts, cwds, started):
    """The thread id a Codex terminal with none should take, from [(mtime, path, meta)] newest first, or None.
    After an auto-clear: a rollout started since the /new; failing that the old one, if it is still being written."""
    st = state(store, aid) or {}
    old, since = (st.get("from_session"), st["ts"]) if st.get("codex") else (None, 0)
    for mtime, _path, meta in rollouts:
        sid = meta.get("id")
        if not sid or meta.get("cwd") not in cwds or sid == old:
            continue
        if epoch(meta.get("timestamp")) >= max(started, since) - 5 and \
                not store.one("SELECT 1 n FROM agents WHERE session_id=?", (sid,)):
            _settle(store, aid, st)
            return sid
    for mtime, _path, meta in rollouts:
        if old and meta.get("id") == old and mtime > since + CODEX_STILL_S:
            store.set_setting(_key(aid), {"phase": "failed", "ts": time.time()})
            store.event(aid, "error", "auto-clear: /new was typed but Codex kept its old conversation; will try "
                                      "again later")
            return old
    return None


def _settle(store, aid, st):
    if st.get("codex") and st.get("phase") == "moved":
        store.set_setting(_key(aid), None)


def followed(store, agent, old_sid, memory_row):
    """hub.follow_cleared saw the agent move to a new session: arm the reminder if this was our clear."""
    st = state(store, agent["id"])
    if not st or st.get("phase") != "clearing" or st.get("from_session") != old_sid:
        return
    if not memory_row:
        r = store.one("SELECT id FROM memory_sessions WHERE session_id=? AND agent=? ORDER BY id DESC LIMIT 1",
                      (old_sid, agent["id"]))
        memory_row = r["id"] if r else None
    memory.mark_autoclear(store, agent, old_sid, memory_row)
    store.set_setting(_key(agent["id"]), {**st, "phase": "remind", "outline": memory_row, "ts": time.time()})


def holding(store, aid):
    """True while our /clear is typed and the new session not yet seen: deliveries wait, so the reminder can name
    the outline."""
    st = state(store, aid)
    return bool(st and st.get("phase") == "clearing")


def reminder(store, agent):
    """The one-time note for the start of the next message after an auto-clear, or ''. Consumed when read."""
    st = state(store, agent["id"])
    if not st or st.get("phase") != "remind":
        return ""
    # Codex / Antigravity: the mark stays (without the note) until the new session is claimed, so the old one is not taken again
    store.set_setting(_key(agent["id"]), {**st, "phase": "moved"} if (st.get("codex") or st.get("antigravity"))
                      and not agent.get("session_id") else None)
    n = st.get("outline")
    where = (f"outlined as conversation #{n}: `sb memory {n}` (it may take a minute to be written; "
             f"`sb memory {n} --at <m>` shows its messages in full)") if n else \
        f"in the project memory: `sb memory`, and `sb context {agent['id']}` for its recent messages"
    return (f"[sb: a gentle heads-up. Your context reached {st['tokens'] // 1000}k tokens, past the "
            f"{st['limit'] // 1000}k mark for your model, so Switchboard cleared it at a quiet moment. Your previous "
            f"conversation is {where}. If this message continues earlier work, look there first.]\n\n")
