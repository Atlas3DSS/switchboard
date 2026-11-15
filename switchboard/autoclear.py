"""Auto-clear: a Claude terminal agent whose context passes its model's limit is cleared the Switchboard way, not
compacted the CLI's way.

The limit is a mark, not a blade. Past it nothing happens mid-turn: the hub waits for a natural pause (the turn
finished and confirmed, nothing in the composer, no dialog, no background shells or subagents the clear would end,
the screen still for QUIET_S) and only then types /clear. The hub follows the new session as for any /clear
(hub.follow_cleared), which queues the old conversation's outline for the project memory. The next message to reach
the agent, from the owner, another agent, an automation or sb, starts with a short reminder naming that outline.

Limits are per model family; setting 'autoclear' overrides them, 0 or null turns one off.
"""
import time

DEFAULTS = {"fable": 382_000, "opus": 482_000, "haiku": 650_000}
QUIET_S = 20         # the screen holds still this long after the turn before the clear is typed
FOLLOW_S = 90        # the new session should show up by then; if not, the clear is taken as not done
RETRY_S = 1800       # a clear that did not take is tried again no sooner than this


def limits(store):
    out = dict(DEFAULTS)
    out.update({k: v for k, v in (store.setting("autoclear", {}) or {}).items()})
    return out


def limit_for(store, model):
    m = (model or "").lower()
    for fam, n in limits(store).items():
        if fam in m:
            return int(n or 0) or None
    return None


def _key(aid):
    return f"autoclear:{aid}"


def state(store, aid):
    return store.setting(_key(aid)) or None


def eligible(agent):
    return (agent["mode"] == "terminal" and agent["provider"] == "claude" and not agent.get("gone")
            and agent.get("role") not in ("helper", "desk") and agent.get("session_id"))


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
    lim = limit_for(s, agent.get("model"))
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
    s.set_setting(_key(aid), {"phase": "clearing", "ts": time.time(), "from_session": agent["session_id"],
                              "tokens": tokens, "limit": lim})
    s.event(aid, "system", f"auto-clear: context at {tokens // 1000}k, past the {lim // 1000}k limit for its model, "
                           "and the agent is at rest; clearing (the conversation goes to the project memory)")
    t.write("/clear")
    time.sleep(0.6)
    t.write("\r")


def followed(store, agent, old_sid, memory_row):
    """hub.follow_cleared saw the agent move to a new session: arm the reminder if this was our clear."""
    st = state(store, agent["id"])
    if not st or st.get("phase") != "clearing" or st.get("from_session") != old_sid:
        return
    if not memory_row:
        r = store.one("SELECT id FROM memory_sessions WHERE session_id=? ORDER BY id DESC LIMIT 1", (old_sid,))
        memory_row = r["id"] if r else None
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
    store.set_setting(_key(agent["id"]), None)
    n = st.get("outline")
    where = (f"outlined as conversation #{n}: `sb memory {n}` (it may take a minute to be written; "
             f"`sb memory {n} --at <m>` shows its messages in full)") if n else \
        f"in the project memory: `sb memory`, and `sb context {agent['id']}` for its recent messages"
    return (f"[sb: a gentle heads-up. Your context reached {st['tokens'] // 1000}k tokens, past the "
            f"{st['limit'] // 1000}k mark for your model, so Switchboard cleared it at a quiet moment. Your previous "
            f"conversation is {where}. If this message continues earlier work, look there first.]\n\n")
