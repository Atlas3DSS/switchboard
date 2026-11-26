"""Every project has the same nodes, so picking a project back up costs no relearning:

  lead       Lead Opus    Opus             the one the owner and every outside agent talk to; routes, splits, reports
  architect  Architect    Fable (or Astra) the smartest seat: design, the hard problems, review
  tasker     Tasker       Haiku (or Terra) carries out well-specified work
  janitor    Janitor      Haiku (or Terra) keeps the queue, notes and compute tidy
  wiki       Live Wiki    no model         a page kept by the hub: last deliverable, goals, sessions

Sonnet is allowed in any seat; the rule of thumb is Opus when Haiku cannot handle it.

A node is a seat, not a process: a project that is idle holds its seats and runs nothing. Each model seat names an
account type and model the owner can swap (claude, codex, deepseek, hermes). A seat is filled by an agent of the
project: the one the owner put there, else the best fit among the project's agents. An agent active in the last
INHERIT_HOURS keeps its session when the seat is picked up; an older or empty seat starts clean, from brief(): the
role, and what the wiki says about the project.

The wiki is a page, agent-first in flow, form and structure: dense, facts first, the same shape for every project,
so a program can route a question through it and a person can still read it.

Seats are read and recorded here; an agent is only started, cleared or retired by pickup() and fold(), which the
owner triggers.
"""
import json
import time

from . import accounts, discovery, fleet, handoff, helpers, memory
from .store import epoch

INHERIT_HOURS = 38
SETTING = "nodes"
ROLES = {
    "lead": {"title": "Lead Opus", "provider": "claude", "model": "claude-opus-5-5",
             "job": "You are the one the owner and every agent outside this project talk to. Keep the project's goal "
                    "and next step true, take hard design questions to the Architect, hand well-specified work to "
                    "the Tasker and chores to the Janitor, and report in plain words."},
    "architect": {"title": "Architect", "provider": "claude", "model": "claude-fable-5-2",
                  "job": "You are the project's strongest model. You design, solve the problems the others cannot, "
                         "and review what matters. The Lead brings you questions; give decisions and specifications "
                         "the Tasker can carry out, not long-running chores."},
    "tasker": {"title": "Tasker", "provider": "claude", "model": "claude-haiku-5-5",
               "job": "You carry out the well-specified work the Lead hands you, end to end, and report back to the "
                      "Lead with what was done and what was verified. If a task needs more than you can give it, "
                      "say so to the Lead at once instead of guessing."},
    "janitor": {"title": "Janitor", "provider": "claude", "model": "claude-haiku-5-5",
                "job": "You keep the project tidy: close or note stale work items, give back compute nobody is "
                       "using, keep notes and the wiki's facts current. You change no code and start no heavy job."},
}
SEATS = tuple(ROLES)
# A seat's provider is an account type. Each maps to the CLI that runs it and the model a seat gets when it lands
# there because its own type had no room (None: that CLI's own default). DeepSeek has no CLI of its own here: it
# runs in Hermes with a DeepSeek model.
PROVIDERS = {
    "claude": {"cli": "claude", "models": {r: ROLES[r]["model"] for r in ROLES}},
    "codex": {"cli": "codex", "models": {"lead": "gpt-6-astra", "architect": "gpt-6-astra", "tasker": "gpt-6.1-terra",
                                         "janitor": "gpt-6.1-terra"}},
    "antigravity": {"cli": "antigravity", "models": {"lead": "gemini-pro-orchestrator-neon",
                                                     "architect": "gemini-4-neon-high",
                                                     "tasker": "gemini-4-hydrogen-high",
                                                     "janitor": "gemini-3.8-flash-medium"}},
    "deepseek": {"cli": "hermes", "models": {r: "deepseek/deepseek-v4-pro" for r in ROLES}},
    "hermes": {"cli": "hermes", "models": {}},
}
FALLBACK = ("claude", "codex", "antigravity", "deepseek", "hermes")  # setting "node_fallback" replaces this order
BRIEF_CAP = {"claude": 60000, "codex": 30000, "antigravity": 30000, "deepseek": 12000, "hermes": 8000}
RESERVE = "_reserve"  # in a project's stored seats: the agents kept outside them
ILLUSTRATOR = "_illustrator"  # in a project's stored seats: the agent the owner marked as its illustrator
MARKS = ("lead", "illustrator", "architect")  # what the owner can pin on an agent from its pane
DEFAULT_SETTING = "node_provider"  # the account type every seat starts on unless the owner set that seat's own


def default_provider(hub):
    """The account type a seat with no choice of its own starts on: the owner's setting (sb nodes --default-provider),
    else each seat's built-in one (Claude). One switch moves every project's new seats, dispatch included, to another
    provider; a seat the owner set by hand keeps its own."""
    v = hub.s.setting(DEFAULT_SETTING)
    return v if v in PROVIDERS else None


def set_default_provider(hub, provider):
    provider = (provider or "").strip()
    if provider and provider not in PROVIDERS:
        raise ValueError(f"{provider} is not an account type ({', '.join(PROVIDERS)})")
    hub.s.set_setting(DEFAULT_SETTING, provider or None)
    return {"default_provider": default_provider(hub)}


def seat_title(role, model):
    """A seat's name. The lead is named after the model it runs on (Lead Opus, Lead Carbon, Lead Astra): the seat is
    the same on any provider."""
    if role != "lead":
        return ROLES[role]["title"]
    word = _model_word(model)
    return f"Lead {word}" if word else "Lead"


def seat_default(hub, role):
    """(provider, model) a seat starts on when the owner chose nothing for it."""
    d, base = ROLES[role], default_provider(hub)
    if base and base != d["provider"] and PROVIDERS[base]["models"].get(role):
        return base, PROVIDERS[base]["models"][role]
    return d["provider"], d["model"]


def deepseek_key():
    """DeepSeek comes through Hermes, from OpenRouter or DeepSeek's own API: is a key there?"""
    import os
    names = ("OPENROUTER_API_KEY", "DEEPSEEK_API_KEY")
    if any(os.environ.get(n) for n in names):
        return True
    try:
        from . import hermes
        text = open(os.path.join(hermes.home(), ".env")).read()
    except OSError:
        return False
    return any(line.split("=", 1)[0].strip() in names and line.split("=", 1)[1].strip()
               for line in text.splitlines() if "=" in line and not line.lstrip().startswith("#"))


def pick_account(hub, provider, model, use=None):
    """The account of this type with the most room for the model, or None when every one is at its limit. Reserve
    accounts come last, as in handoff. An account type with no limits to read (Hermes) always has room."""
    cli = PROVIDERS[provider]["cli"]
    if provider == "deepseek" and not deepseek_key():
        return None
    use = use if use is not None else accounts.usage()
    cfg = handoff.settings(hub.s)
    room, last = [], []
    for a in discovery.accounts():
        if a["kind"] != cli or a["name"] in cfg.get("parked", ()):
            continue
        u, res = use.get(a["name"]), a["name"] in cfg["reserve"]
        if (u or {}).get("error"):
            continue  # its reading failed (a login that no longer works, a lapsed subscription): never "has room"
        if not (u or {}).get("limits"):
            room.append((0, a["name"]))
        elif handoff.trusted(u, res) and handoff.used(u, model) < cfg["at"]:
            (last if res else room).append((handoff.used(u, model), a["name"]))
    pick = room or last
    return min(pick)[1] if pick else None


def place(hub, role, provider, model, use=None):
    """(provider, model, account) for a seat that starts: its own type if an account there has room, else the next
    type in the fallback order, with that type's model for the role."""
    order = [provider] + [p for p in (hub.s.setting("node_fallback") or FALLBACK) if p != provider and p in PROVIDERS]
    for i, prov in enumerate(order):
        m = model if i == 0 else PROVIDERS[prov]["models"].get(role)
        acct = pick_account(hub, prov, m, use)
        if acct:
            return prov, m, acct
    return None, None, None


def _stored(hub, pid):
    return ((hub.s.setting(SETTING, {}) or {}).get(pid)) or {}


def reserved(hub, pid):
    return set(_stored(hub, pid).get(RESERVE) or ())


def _usable(a):
    return not a.get("gone") and a["provider"] != "shell" and a.get("role") not in ("desk", "helper") \
        and a.get("mode") != "observed" and not a.get("ghost")


def seats(hub, pid, now=None, agents=None, stored_all=None, proj=None, def_prov=...):
    """The three model seats of a project, each with who fills it and whether that session would be inherited."""
    now = now or time.time()
    stored = (stored_all.get(pid) or {}) if stored_all is not None else _stored(hub, pid)
    pool = agents if agents is not None else hub.s.agents()
    mine = {a["id"]: a for a in pool if a["project"] == pid and _usable(a)}
    recent = lambda a: (a.get("last_activity") or "", a.get("created_at") or "")
    taken, picked = set(), {}

    def kind(a):
        """Which seats an agent's model suits: architect (Fable, Astra), lead (Opus), small (Haiku, Sonnet, Terra)."""
        m = (a.get("model") or "").lower()
        if "fable" in m or "mythos" in m or "astra" in m:
            return "architect"
        if "opus" in m:
            return "lead"
        if helpers.rank(m) or "terra" in m:
            return "small"
        return None  # model not recorded: no seat is guessed for it

    held_back = set(stored.get(RESERVE) or ())  # agents the owner keeps outside the seats (sb nodes --reserve)
    illus = stored.get(ILLUSTRATOR) if stored.get(ILLUSTRATOR) in mine else None  # no seat fills itself with it

    def give(role, aid, share=False):  # share: the owner's lead may also be the owner's architect
        if aid in mine and (share or aid not in taken) and aid not in held_back and role not in picked:
            picked[role] = aid
            taken.add(aid)

    for role in SEATS:  # the owner's own choices first
        give(role, (stored.get(role) or {}).get("agent"), share=role in ("lead", "architect"))
    p = proj if proj is not None else hub.s.project(pid)
    give("lead", (p or {}).get("lead"), share=True)  # the lead set on the project card
    for role, want in (("architect", "architect"), ("lead", "lead"), ("tasker", "small"), ("janitor", "small")):
        fit = [a for a in mine.values() if a["id"] not in taken and a["id"] not in held_back and a["id"] != illus
               and kind(a) == want]
        if fit:
            give(role, max(fit, key=recent)["id"])
    base_prov = default_provider(hub) if def_prov is ... else def_prov
    out = []
    for role in SEATS:
        d, cfg, a = ROLES[role], stored.get(role) or {}, mine.get(picked.get(role))
        idle_h = (now - epoch(a["last_activity"])) / 3600 if a and a.get("last_activity") else None
        state = "empty" if not a else "live" if idle_h is not None and idle_h <= INHERIT_HOURS else "stale"
        if base_prov and base_prov != d["provider"] and PROVIDERS[base_prov]["models"].get(role):
            dprov, dmodel = base_prov, PROVIDERS[base_prov]["models"][role]
        else:
            dprov, dmodel = d["provider"], d["model"]
        out.append({"role": role, "title": seat_title(role, cfg.get("model") or dmodel),
                    "provider": cfg.get("provider") or dprov,
                    "model": cfg.get("model") or dmodel, "default_model": dmodel,
                    "agent": a["id"] if a else None, "chosen": bool(a and cfg.get("agent") == a["id"]),
                    "running_model": a["model"] if a else None, "running_provider": a["provider"] if a else None,
                    "account": a["account"] if a else None, "session_id": a["session_id"] if a else None,
                    "last_activity": a["last_activity"] if a else None,
                    "idle_hours": round(idle_h, 1) if idle_h is not None else None, "state": state,
                    # live: the seat's session is picked up as it is; stale or empty: a clean start from brief()
                    "pickup": "inherit session" if state == "live" else "clean start"})
    extras = [{"agent": a["id"], "provider": a["provider"], "model": a["model"], "last_activity": a["last_activity"],
               "reserve": a["id"] in held_back, "illustrator": a["id"] == illus}
              for a in sorted(mine.values(), key=recent, reverse=True) if a["id"] not in taken]
    return {"project": pid, "seats": out, "extras": extras, "illustrator": illus, "inherit_hours": INHERIT_HOURS,
            "default_provider": base_prov}


def marks(hub, pid, st=None, lead=..., proj=None, stored_all=None):
    """Per agent, the marks the owner sees on its pane: lead (where a message to the project lands), architect and
    illustrator. 'on' is the owner's own choice; 'auto' is one the hub made because nobody was chosen."""
    p = proj if proj is not None else hub.s.project(pid)
    if not p:
        return {}
    st = st or seats(hub, pid, proj=p, stored_all=stored_all)
    stored = (stored_all.get(pid) or {}) if stored_all is not None else _stored(hub, pid)
    out = {}
    lead = hub.lead_of(pid) if lead is ... else lead
    if lead:
        pinned = p.get("lead") == lead or (stored.get("lead") or {}).get("agent") == lead
        out.setdefault(lead, {})["lead"] = "on" if pinned else "auto"
    arch = next((x for x in st["seats"] if x["role"] == "architect" and x["agent"]), None)
    if arch:
        out.setdefault(arch["agent"], {})["architect"] = "on" if arch["chosen"] else "auto"
    if st.get("illustrator"):
        out.setdefault(st["illustrator"], {})["illustrator"] = "on"
    return out


def set_mark(hub, pid, role, agent, on=True):
    """The owner pins or unpins a mark on an agent. Lead: the agent messages to the project go to (the project's
    lead and its lead seat together, so routing and the seats agree). Architect: the architect seat. Illustrator:
    a mark only; the agent keeps out of the seats that fill themselves and is not retired by a fold. One agent may
    be both lead and architect; a pin takes it out of the tasker and janitor seats. Unpinning leaves the hub's own
    choice in place, shown as 'auto'."""
    if role not in MARKS:
        raise ValueError(f"{role} is not a mark ({', '.join(MARKS)})")
    p = hub.s.project(pid)
    if not p:
        raise LookupError(pid)
    a = hub.s.agent(agent or "")
    if not a or a["project"] != pid or not _usable(a):
        raise ValueError(f"{agent} is not an agent on {pid} that can be marked")
    every = hub.s.setting(SETTING, {}) or {}
    cur = dict(every.get(pid) or {})

    def unseat(r):
        if (cur.get(r) or {}).get("agent") == agent:
            cur[r] = {k: v for k, v in cur[r].items() if k != "agent"}
            if not cur[r]:
                cur.pop(r)

    if role == "illustrator":
        if on:
            cur[ILLUSTRATOR] = agent
        elif cur.get(ILLUSTRATOR) == agent:
            cur.pop(ILLUSTRATOR)
    elif on:
        for r in ("tasker", "janitor"):
            unseat(r)
        cur[role] = {**(cur.get(role) or {}), "agent": agent}
        if agent in (cur.get(RESERVE) or ()):  # a held-back agent cannot hold a seat
            cur[RESERVE] = [x for x in cur[RESERVE] if x != agent]
            if not cur[RESERVE]:
                cur.pop(RESERVE)
    else:
        unseat(role)
    if role == "lead":
        if on:
            hub.s.upsert_project({"id": pid, "lead": agent})
        elif p.get("lead") == agent:
            hub.s.upsert_project({"id": pid, "lead": ""})
    every[pid] = cur
    hub.s.set_setting(SETTING, {k: v for k, v in every.items() if v})
    return {"project": pid, "lead": hub.lead_of(pid), "marks": marks(hub, pid)}


def set_reserve(hub, pid, agent, on=True):
    """Keep an agent outside the seats (or let it back in): it stays on the project and can be written to by name,
    but no seat fills itself with it, so a message to the project never lands on it. For an agent the owner holds
    back for later."""
    if not hub.s.project(pid):
        raise LookupError(pid)
    a = hub.s.agent(agent or "")
    if not a or a["project"] != pid:
        raise ValueError(f"{agent} is not an agent on {pid}")
    every = hub.s.setting(SETTING, {}) or {}
    cur = dict(every.get(pid) or {})
    ids = [x for x in cur.get(RESERVE) or [] if x != agent] + ([agent] if on else [])
    for role in SEATS:  # a seat the owner had given it is taken back with it
        if on and (cur.get(role) or {}).get("agent") == agent:
            cur[role] = {k: v for k, v in cur[role].items() if k != "agent"}
            if not cur[role]:
                cur.pop(role)
    cur[RESERVE] = ids
    if not ids:
        cur.pop(RESERVE)
    every[pid] = cur
    hub.s.set_setting(SETTING, {p: v for p, v in every.items() if v})
    return seats(hub, pid)


def set_seat(hub, pid, role, b):
    """The owner's choice for one seat: model, provider, agent ('' clears the choice and the seat fills itself)."""
    if role not in ROLES:
        raise ValueError(f"{role} is not a seat ({', '.join(SEATS)})")
    if not hub.s.project(pid):
        raise LookupError(pid)
    every = hub.s.setting(SETTING, {}) or {}
    cur = dict((every.get(pid) or {}).get(role) or {})
    for k in ("model", "provider", "agent"):
        if k in b:
            v = (b[k] or "").strip()
            if k == "provider" and v and v not in PROVIDERS:
                raise ValueError(f"{v} is not an account type ({', '.join(PROVIDERS)})")
            if k == "agent" and v:
                a = hub.s.agent(v)
                if not a or a["project"] != pid or not _usable(a):
                    raise ValueError(f"{v} is not an agent on {pid} that can hold a seat")
            if v:
                cur[k] = v
            else:
                cur.pop(k, None)
    every.setdefault(pid, {})[role] = cur
    if not cur:
        every[pid].pop(role)
    hub.s.set_setting(SETTING, {p: v for p, v in every.items() if v})
    return seats(hub, pid)


def wiki(hub, pid, viewer=None, talk=True):
    """The project's live wiki as data. talk=False leaves out the last exchange of each session (no transcript read)."""
    pid = hub.context_scope(viewer, pid, read=True)
    p = hub.s.project(pid)
    if not p:
        raise LookupError(pid)
    s = hub.s
    st = seats(hub, pid)
    role_of = {x["agent"]: x["role"] for x in st["seats"] if x["agent"]}
    if st["illustrator"]:
        role_of.setdefault(st["illustrator"], "illustrator")
    done = s.one("SELECT id, title, agent, done_at FROM work WHERE project=? AND status='done' AND done_at IS NOT NULL "
                 "ORDER BY done_at DESC LIMIT 1", (pid,))
    open_work = sorted(fleet.work_list(hub, project=pid)["items"], key=lambda w: (w["priority"], w["id"]))
    mem = memory.view(hub, pid, viewer, n=5)
    sessions = []
    for a in hub.project_agents(pid):
        if a.get("gone"):
            continue
        row = {"agent": a["id"], "seat": role_of.get(a["id"]), "provider": a["provider"], "account": a["account"],
               "model": a["model"], "session_id": a["session_id"], "state": hub.t.status(a)["state"],
               "last_activity": a["last_activity"]}
        if talk:
            from . import shared
            asked, said = shared.last_exchange(hub.transcript_of(a), a["provider"])
            row["asked"], row["said"] = (asked or "")[:400], (said or "")[:600]
        sessions.append(row)
    sessions.sort(key=lambda r: r["last_activity"] or "", reverse=True)
    lead = next((r for r in sessions if r["seat"] == "lead"), None)
    return {
        "project": pid, "name": p["name"], "archived": bool(p.get("archived")), "paused": bool(p.get("paused")),
        "at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "goals": {"goal": p.get("goal") or "", "next_step": p.get("next_step") or "",
                  "status_note": p.get("status_note") or "",
                  "open_work": [{"id": w["id"], "title": w["title"], "priority": w["priority"], "status": w["status"],
                                 "agent": w["agent"], "updated_at": w["updated_at"]} for w in open_work[:20]],
                  "open_work_total": len(open_work),
                  "waiting_on_owner": [{"id": d["id"], "question": d["question"], "agent": d.get("agent")}
                                       for d in s.decisions(pid, open_only=True)]},
        "last_deliverable": {"work": dict(done) if done else None,
                             "lead_said": {"agent": lead["agent"], "at": lead["last_activity"], "text": lead.get("said")}
                             if lead and lead.get("said") else None},
        "memory": {"text": mem["memory"], "updated_at": mem["updated_at"]},
        "nodes": st["seats"], "extras": st["extras"],
        "sessions": {"live": sessions,
                     "past": [{"n": e["id"], "agent": e["agent"], "model": e["model"], "done_at": e["done_at"],
                               "summary": e.get("summary") or e.get("last_ask")} for e in mem["sessions"]],
                     "past_total": mem["total"]},
        "notes": [{"agent": n["agent"], "at": n["created_at"], "text": n["text"]}
                  for n in reversed(s.q("SELECT * FROM notes WHERE project=? ORDER BY id DESC LIMIT 6", (pid,)))],
    }


def markdown(w):
    """The wiki as one dense page: facts first, no prose around them."""
    g, ld = w["goals"], w["last_deliverable"]
    L = [f"# {w['name']} ({w['project']})" + (" [archived]" if w["archived"] else "") + (" [paused]" if w["paused"] else ""),
         f"as of {w['at']}", "", "## Goals", f"- goal: {g['goal'] or '-'}", f"- next step: {g['next_step'] or '-'}"]
    if g["status_note"]:
        L.append(f"- status: {g['status_note']}")
    for d in g["waiting_on_owner"]:
        L.append(f"- WAITING ON OWNER ({d['id']}, {d.get('agent') or '-'}): {d['question']}")
    L += ["", "## Last deliverable"]
    if ld["work"]:
        L.append(f"- work #{ld['work']['id']} done {ld['work']['done_at']} by {ld['work']['agent'] or '-'}: {ld['work']['title']}")
    if ld["lead_said"]:
        L.append(f"- lead {ld['lead_said']['agent']} last said ({ld['lead_said']['at']}): {ld['lead_said']['text']}")
    if not ld["work"] and not ld["lead_said"]:
        L.append("- none recorded")
    L += ["", "## Nodes"]
    for n in w["nodes"]:
        who = f"{n['agent']} ({n['running_provider']}/{n['account']}, {n['running_model']}, idle {n['idle_hours']} h)" \
            if n["agent"] else "nobody"
        L.append(f"- {n['title']} [{n['provider']} {n['model']}]: {who}; {n['state']}, {n['pickup']}")
    L.append("- Live Wiki: this page, kept by Switchboard")
    if w["extras"]:
        L.append("- outside the seats: " + ", ".join(f"{x['agent']} ({x['model']})" for x in w["extras"]))
    L += ["", f"## Open work ({g['open_work_total']})"]
    L += [f"- #{x['id']} P{x['priority']} {x['status']} [{x['agent'] or 'unowned'}] {x['title']}" for x in g["open_work"]] or ["- none"]
    L += ["", "## Sessions"]
    for r in w["sessions"]["live"]:
        L.append(f"- {r['agent']} [{r['seat'] or 'no seat'}] {r['provider']}/{r['account']} {r['model']} {r['state']} "
                 f"last {r['last_activity']} session {r['session_id'] or '-'}")
        if r.get("asked"):
            L.append(f"  - asked: {' '.join(r['asked'].split())}")
        if r.get("said"):
            L.append(f"  - said: {' '.join(r['said'].split())}")
    for e in w["sessions"]["past"]:
        L.append(f"- past #{e['n']} {e['agent']} ({e['model']}) ended {e['done_at']}: {' '.join((e['summary'] or '').split())[:200]}")
    if w["memory"]["text"]:
        L += ["", f"## Memory (updated {w['memory']['updated_at']})", w["memory"]["text"].strip()]
    if w["notes"]:
        L += ["", "## Notes"] + [f"- {n['at']} {n['agent']}: {' '.join(n['text'].split())}" for n in w["notes"]]
    return "\n".join(L) + "\n"


def brief(hub, pid, role, viewer=None, provider=None):
    """What a seat that starts clean is told: its role, then the project as the wiki has it. The same facts for
    every account type, shaped for the one that will read it: Claude gets the page whole inside tags, Codex gets
    headed sections with its rules as a list, and the smaller-context types (DeepSeek, Hermes) get the page cut to
    goals, last deliverable, nodes and the top of the open work."""
    if role not in ROLES:
        raise ValueError(f"{role} is not a seat ({', '.join(SEATS)})")
    d = ROLES[role]
    w = wiki(hub, pid, viewer)
    provider = provider or next(n["provider"] for n in w["nodes"] if n["role"] == role)
    title = {n["role"]: n["title"] for n in w["nodes"]}
    others = "; ".join(f"{title.get(r, ROLES[r]['title'])} = "
                       f"{next((n['agent'] for n in w['nodes'] if n['role'] == r), None) or 'empty'}"
                       for r in SEATS if r != role)
    who = f"You are the {title.get(role, d['title'])} of project {w['name']} ({pid}) in the owner's Switchboard."
    more = "`sb wiki` prints the project's live wiki again; `sb memory` and `sb context` go deeper."
    if provider in ("deepseek", "hermes"):
        w = dict(w, memory={"text": "", "updated_at": None}, notes=[], extras=[],
                 goals=dict(w["goals"], open_work=w["goals"]["open_work"][:8]),
                 sessions={"live": [dict(r, asked="", said="") for r in w["sessions"]["live"]], "past": [], "past_total": 0})
        text = f"{who} {d['job']}\nOther seats: {others}.\nYou start with a clean context. {more}\n\n{markdown(w)}"
    elif provider == "codex":
        text = (f"## Role\n{who}\n\n## Rules\n" + "\n".join(f"- {s.strip()}." for s in d["job"].split(". ") if s.strip(". "))
                + f"\n- Other seats: {others}.\n- You start with a clean context. {more}\n\n## Project\n{markdown(w)}")
    else:
        text = (f"{who} {d['job']}\nThe other seats: {others}.\nYou start with a clean context. {more}\n\n"
                f"<project_wiki>\n{markdown(w)}</project_wiki>\n")
    cap = BRIEF_CAP.get(provider, 30000)
    return text if len(text) <= cap else text[:cap] + "\n[cut here: `sb wiki` has the rest]\n"


def pickup(hub, pid, role, by="owner"):
    """The owner picks a seat back up. Used in the last INHERIT_HOURS: its agent and session as they are. Older:
    the same agent with its conversation cleared (Claude) and the brief. Empty: a new agent on the seat's account
    type and model, on whichever account has room, started with the brief."""
    if role not in ROLES:
        raise ValueError(f"{role} is not a seat ({', '.join(SEATS)})")
    if not hub.s.project(pid):
        raise LookupError(pid)
    seat = next(s for s in seats(hub, pid)["seats"] if s["role"] == role)
    stamp = int(time.time() // 300)
    if seat["state"] == "live":
        return {"project": pid, "role": role, "agent": seat["agent"], "action": "inherited"}
    if seat["agent"]:
        a = hub.s.agent(seat["agent"])
        prov = "deepseek" if a["provider"] == "hermes" and "deepseek" in (a.get("model") or "") else a["provider"]
        cleared = a["provider"] == "claude" and bool(a.get("last_activity"))  # a seat that never ran has nothing to clear
        if cleared:  # typed into its terminal; the hub follows the new session and outlines the old one
            hub.s.enqueue(a["id"], "/clear", frm=by, idem=f"node-clear:{a['id']}:{stamp}")
        text = brief(hub, pid, role, provider=prov)
        if not cleared:
            text = ("Your earlier conversation on this project is more than "
                    f"{INHERIT_HOURS} hours old: treat what follows as the current state.\n\n" + text)
        hub.s.enqueue(a["id"], text, frm=by, idem=f"node-brief:{a['id']}:{stamp}")
        return {"project": pid, "role": role, "agent": a["id"], "action": "cleared" if cleared else "rebriefed"}
    prov, model, acct = place(hub, role, seat["provider"], seat["model"])
    if not acct:
        raise ValueError(f"no account of any type has room for the {seat['title']} seat right now")
    made = hub.create_agent({"provider": PROVIDERS[prov]["cli"], "project": pid, "title": seat_title(role, model),
                             "id": f"{pid}-{role}", "model": model, "account": acct, "permission_mode": "auto",
                             "prompt": brief(hub, pid, role, provider=prov), "from": by,
                             "idem": f"node-start:{pid}:{role}:{stamp}"})
    set_seat(hub, pid, role, {"agent": made["id"]})
    return {"project": pid, "role": role, "agent": made["id"], "action": "started", "provider": prov, "model": model,
            "account": acct, "moved": prov != seat["provider"]}


def _model_word(model):
    m = (model or "").lower()
    return next((w.capitalize() for w in ("fable", "mythos", "opus", "sonnet", "haiku", "astra", "terra", "luna", "sol", "argon", "carbon", "hydrogen", "neon",
                                          "deepseek") if w in m), None)


def standardize(hub, pid, by="owner"):
    """Name a project's panels after their seats and models, and give it a Lead Opus if it has none.
    Nothing here touches a conversation: only the display title changes (the agent's id, session,
    queue, jobs and work items all hang off the id, which stays), each seat is pinned to the agent that holds it
    so it no longer moves with recency, and a new Lead Opus is a row with no process and no message: it costs
    nothing until someone opens it or writes to the project, and it is briefed then."""
    p = hub.s.project(pid)
    if not p:
        raise LookupError(pid)
    out = {"project": pid, "renamed": [], "created": None, "note": None}
    cur = hub.s.agent(p.get("lead")) if p.get("lead") else None
    if cur and _model_word(cur.get("model")) in ("Fable", "Mythos", "Astra"):
        hub.s.upsert_project({"id": pid, "lead": ""})  # a Fable lead is the Architect; the Lead seat is Opus
    st = seats(hub, pid)
    for seat in st["seats"]:
        if not seat["agent"]:
            continue
        a = hub.s.agent(seat["agent"])
        word = _model_word(a.get("model"))
        set_seat(hub, pid, seat["role"], {"agent": a["id"]})
        if not word:
            continue
        title = f"{ROLES[seat['role']]['title'].split()[0]} {word}"
        if a["title"] != title:
            hub.s.set_agent(a["id"], title=title)
            hub.s.event(a["id"], "system", f"renamed from '{a['title']}' to '{title}' by {by}: it holds the {seat['role']} seat")
            out["renamed"].append({"agent": a["id"], "was": a["title"], "now": title, "seat": seat["role"]})
    lead = next(s for s in st["seats"] if s["role"] == "lead")
    if not lead["agent"] and any(s["agent"] for s in st["seats"]) or (not lead["agent"] and st["extras"]):
        prov, model, acct = place(hub, "lead", lead["provider"], lead["model"])
        if not acct:
            out["note"] = "no account has room for a Lead Opus right now"
        else:
            made = hub.create_agent({"provider": PROVIDERS[prov]["cli"], "project": pid, "id": f"{pid}-lead",
                                     "title": f"Lead {_model_word(model) or 'Opus'}", "model": model, "account": acct,
                                     "permission_mode": "auto", "from": by})
            hub.s.set_agent(made["id"], last_activity=None)  # never used: its first contact brings the brief
            set_seat(hub, pid, "lead", {"agent": made["id"]})
            out["created"] = {"agent": made["id"], "provider": prov, "model": model, "account": acct}
    return out


def brief_if_new(hub, agent, by="owner"):
    """A seat that has never run (a Lead Opus made by standardize) gets its brief ahead of its first message."""
    if agent.get("last_activity") or agent.get("gone"):
        return False
    role = next((r for r, c in _stored(hub, agent["project"]).items()
                 if isinstance(c, dict) and c.get("agent") == agent["id"]), None)
    if not role:
        return False
    prov = "deepseek" if agent["provider"] == "hermes" and "deepseek" in (agent.get("model") or "") else agent["provider"]
    _, dup = hub.s.enqueue(agent["id"], brief(hub, agent["project"], role, provider=prov), frm=by,
                           idem=f"node-first-brief:{agent['id']}")
    return not dup


def fold(hub, pid, by="owner"):
    """Bring a project back to its seats without losing anything (extra panels came from mixing
    orchestration, tasking and janitorial work). Every agent outside the seats that is not in the middle of a turn
    is retired: its conversation is queued for an outline in the project's memory (so the wiki carries it), its
    open work items pass to the lead, and its transcript stays on disk (`sb sessions`, `sb resume`). Its jobs keep
    running and belong to the project. A busy agent is left alone and named."""
    if not hub.s.project(pid):
        raise LookupError(pid)
    st = seats(hub, pid)
    lead = next((s["agent"] for s in st["seats"] if s["role"] == "lead"), None)
    folded, busy = [], []
    for x in st["extras"]:
        if x["illustrator"]:
            continue
        a = hub.s.agent(x["agent"])
        if hub.t.status(a)["state"] == "busy" or hub.s.one(
                "SELECT COUNT(*) n FROM messages WHERE to_agent=? AND state IN ('queued','delivered')", (a["id"],))["n"]:
            busy.append(a["id"])
            continue
        if hub.t.running(a["id"]):
            hub.t.exit_cli(a["id"], interrupt=True)
        memory.session_ended(hub, a, a["session_id"], "folded into the project's seats")
        moved = hub.s.x("UPDATE work SET agent=?, updated_at=updated_at WHERE agent=? AND status NOT IN ('done','dropped')",
                        (lead, a["id"])).rowcount
        hub.s.set_agent(a["id"], gone=1, state="stopped")
        hub.s.event(a["id"], "system", f"folded into {pid}'s seats by {by}")
        folded.append({"agent": a["id"], "session_id": a["session_id"], "work_to_lead": moved})
    return {"project": pid, "folded": folded, "left_busy": busy, "lead": lead}


def index(hub):
    """One row per project that is not archived: enough to route a question to the right wiki."""
    rows = []
    for p in hub.s.projects():
        if p.get("archived"):
            continue
        st = seats(hub, p["id"])
        last = max((x["last_activity"] or "" for x in st["seats"]), default="") or None
        rows.append({"project": p["id"], "name": p["name"], "goal": p.get("goal") or "", "next_step": p.get("next_step") or "",
                     "last_activity": last, "seats": {x["role"]: x["agent"] for x in st["seats"]},
                     "live_seats": sum(1 for x in st["seats"] if x["state"] == "live"), "extras": len(st["extras"]),
                     "waiting_on_owner": len(hub.s.decisions(p["id"], open_only=True))})
    rows.sort(key=lambda r: r["last_activity"] or "", reverse=True)
    return {"projects": rows, "roles": {r: {k: ROLES[r][k] for k in ("title", "provider", "model")} for r in SEATS},
            "inherit_hours": INHERIT_HOURS}
