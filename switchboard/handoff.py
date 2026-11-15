"""Account handoff: move a Claude agent to another account before its account runs out, and carry on without losing
anything when a limit hits mid-step anyway.

- Ahead of the limit: once any limit that applies to the agent's model (session, weekly, and Fable weekly for Fable)
  is at `at`% (default 95) on its account, the agent moves at its next turn boundary: as soon as it is idle, before
  Switchboard types the next message into it, and before a stopped agent is launched. The move is the account
  toggle: /exit, then `--resume` of the same session under the other account (both accounts share one transcript).
- At the limit: when the CLI stops on its usage-limit notice, the agent moves at once and is told to continue, so
  the interrupted step carries on. A manual toggle away from an agent stopped on that notice continues it too.
Accounts in `reserve` (none by default; your last-resort account) come last in the cascade: one is picked only
when no other account has room. A reading older than accounts.STALE_S is not trusted, except for a reserve
account: nobody spends on it while it idles (and its token lapses, so `plan` cannot re-read it), so its last reading
with the windows' resets applied is the truth; the move itself wakes the token and the readings.
"""
import os
import time

from . import accounts, discovery, transcript
from .store import epoch
from .terminals import is_fable

DEFAULTS = {"enabled": True, "at": 95, "reserve": []}
COOLDOWN_S = 120  # an agent that just moved is left alone for this long
CONTINUE = ("[Switchboard: account {old} ran out ({why}) in the middle of your last step, so this same conversation "
            "now runs on account {new}. Nothing was lost. Pick up exactly where you stopped and carry on with the "
            "task; do not redo finished steps.]")


def settings(store):
    cfg = dict(DEFAULTS)
    cfg.update(store.setting("handoff", {}) or {})
    return cfg


def used(acct, model):
    """Highest % used among the limits that apply to this model on one account (from accounts.usage())."""
    return max((l["used"] for l in (acct or {}).get("limits") or [] if l["key"] != "fable" or is_fable(model)),
               default=0)


def trusted(acct, reserve=False):
    return bool(acct) and bool(acct.get("read_at")) and (reserve or not acct.get("stale")) and not acct.get("error")


def best_account(store, agent, use, cfg):
    """The same-provider account with the most room for this agent's model, or None. Reserve accounts are tried
    only when no other account has room, and then the one with the most room among them."""
    room, last = [], []
    for a in discovery.accounts():
        n = a["name"]
        if a["kind"] != agent["provider"] or n == agent["account"]:
            continue
        u, res = use.get(n), n in cfg["reserve"]
        if trusted(u, res) and used(u, agent.get("model")) < cfg["at"]:
            (last if res else room).append((used(u, agent.get("model")), n))
    pick = room or last
    return min(pick)[1] if pick else None


def why_move(store, agent, use, cfg):
    """(reason, target account) when this agent should leave its account now, else None."""
    if not cfg["enabled"] or agent["provider"] != "claude":
        return None
    u = use.get(agent["account"])
    if not trusted(u):
        return None
    pct = used(u, agent.get("model"))
    if pct < cfg["at"]:
        return None
    to = best_account(store, agent, use, cfg)
    return (f"{agent['account']} at {pct}%", to) if to else None


class Handoff:
    def __init__(self, hub):
        self.hub = hub
        self.moved_at = {}  # aid -> time of the last automatic move
        self.seen = {}  # aid -> (transcript mtime, limit hit or None)
        self._use = (0.0, {})

    def usage(self):
        t, u = self._use
        if time.time() - t > 10:
            u = accounts.usage()
            self._use = (time.time(), u)
        return u

    def cooling(self, aid):
        return time.time() - self.moved_at.get(aid, 0) < COOLDOWN_S

    def move(self, agent, to, why, hit=None):
        aid = agent["id"]
        self.moved_at[aid] = time.time()
        old = agent["account"]
        self.hub.s.event(aid, "system", f"handoff: {why}; moving to {to}" + (" and continuing" if hit else ""))
        self.hub.t.switch_account(aid, to, confirm=True)
        self.hub.notify(aid, f"moved {old} → {to} ({why})" + ("; told to continue its step" if hit else ""),
                        kind="handoff", level="info", idem=f"handoff:{aid}:{int(time.time())}")

    # ---------- hooks called by Terminals ----------
    def before_launch(self, agent):
        """The account a stopped agent should start on: its own unless that one is out."""
        cfg = settings(self.hub.s)
        r = why_move(self.hub.s, agent, self.usage(), cfg)
        if not r:
            return agent["account"]
        self.hub.s.event(agent["id"], "system", f"handoff: {r[0]}; starting on {r[1]} instead")
        return r[1]

    def before_deliver(self, agent):
        """True when the agent is moving to another account first (the message waits for the relaunch)."""
        if self.cooling(agent["id"]):
            return False
        r = why_move(self.hub.s, agent, self.usage(), settings(self.hub.s))
        if not r:
            return False
        self.move(agent, r[1], r[0])
        return True

    def after_switch(self, aid, old, new, hit):
        """Every relaunch on another account (automatic or the owner's toggle): if the agent had stopped on a limit
        notice, tell it to carry on, exactly once per notice."""
        if not hit:
            return
        why = {"seven_day": "weekly limit", "five_hour": "session limit",
               "model_requires_usage_credits": "model usage credits"}.get(hit["kind"], hit["kind"] or "usage limit")
        self.hub.s.enqueue(aid, CONTINUE.format(old=old, new=new, why=why), frm="owner",
                           idem=f"continue:{aid}:{hit['uuid']}")

    # ---------- loop ----------
    def tick(self):
        cfg = settings(self.hub.s)
        if not cfg["enabled"]:
            return
        use = None
        for a in self.hub.s.agents():
            aid = a["id"]
            if a["provider"] != "claude" or a["mode"] != "terminal" or not self.hub.t.running(aid) \
                    or aid in self.hub.t.switching or self.cooling(aid):
                continue
            use = use or self.usage()
            t = self.hub.t.term(aid)
            hit = self.limit_hit(a, t)
            if hit:
                to = best_account(self.hub.s, a, use, cfg)
                if to:
                    self.move(a, to, f"{a['account']} hit its limit: {hit['text'][:80]}", hit)
                else:
                    self.hub.notify(aid, f"stopped on {a['account']}'s limit and no other account has room "
                                    f"(reserve {', '.join(cfg['reserve']) or 'none'} included): switch by hand",
                                    kind="handoff", idem=f"handoff-none:{aid}:{hit['uuid']}")
                continue
            if not self.hub.t.busy(aid) and self.hub.t.status(a)["state"] == "idle":
                r = why_move(self.hub.s, a, use, cfg)
                if r:
                    self.move(a, r[1], r[0])

    def limit_hit(self, agent, term):
        """A limit notice written by this CLI process (not one left over from before it started)."""
        path = discovery.claude_transcript(agent["session_id"]) if agent.get("session_id") else None
        if not path:
            return None
        try:
            mt = os.path.getmtime(path)
        except OSError:
            return None
        seen = self.seen.get(agent["id"])
        if seen and seen[0] == mt:
            hit = seen[1]
        else:
            hit = transcript.limit_hit(path)
            self.seen[agent["id"]] = (mt, hit)
        started = (term.state.get("started_at") if term else 0) or 0
        return hit if hit and epoch(hit["ts"]) >= started - 2 else None


def turn_account(hub, agent):
    """Account for a headless turn (the Switchboard chat): its own unless that one is out."""
    r = why_move(hub.s, agent, accounts.usage(), settings(hub.s))
    if r:
        hub.s.set_agent(agent["id"], account=r[1])
        hub.s.event(agent["id"], "system", f"handoff: {r[0]}; this turn runs on {r[1]}")
        return r[1]
    return agent["account"]

