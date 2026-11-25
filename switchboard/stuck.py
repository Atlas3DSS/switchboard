"""Stuck at rest: an Antigravity terminal whose last turn ended on something it cannot get past by itself.

Seen in practice: an agent answered three messages in a row with "please run /compact", then a turn
died on "INVALID_ARGUMENT (code 400)". It read as idle, so nothing told the owner, and it sat for over an hour.

Two cases, both read from the last turn on the mirrored screen (the lines between the last prompt line and the
composer), and only once the terminal has been at rest for QUIET_S:

  asks for /compact   the reply asks the reader to run /compact, or the turn ended on the API's "reduce the size of
                      the prompt or conversation history". The hub types /compact itself, then queues a note telling
                      the agent it was done and to carry on. At most once per RETRY_S per agent; an agent that asks
                      again sooner is paged to the owner instead.
  API error           the turn ended on "⚠ NAME (code N): ..." (a model or setting the API refuses, for example).
                      Nothing safe to type for the owner, so the owner is paged with the error text. 429 is left to
                      hub.quota_tick, which already pages it with the reset time.
"""
import re
import threading
import time

from . import antigravity

QUIET_S = 20     # at rest this long before anything is read as stuck
RETRY_S = 1800   # one /compact per agent in this long
NUDGE_S = 25     # the carry-on note is queued this long after /compact is typed (delivery then waits for rest)
PROMPT = re.compile(r"^>(\s|$)")
ASKS = re.compile(r"(?:please|recommend(?: that)? you|need you to|ask you to|you (?:can|should|must|need to|will need to))"
                  r"\s+(?:now\s+)?(?:run|use|type|enter)\s+(?:the\s+)?[*`]*/compact\b", re.I)
ERROR = re.compile(r"^\s*⚠\s*(\w+ \(code (\d+)\): .+?)\s*$", re.M)
TOO_BIG = re.compile(r"reduce the size of the prompt or conversation history", re.I)
XHIGH_NOTE = ("[sb: your last turn died because your model was set to XHigh thinking, which the API refuses. Switchboard "
              "relaunched you on {model}. Nothing you were asked was answered: read the last message you were given "
              "above and do it now.]")
NUDGE = ("[sb: you asked for /compact. Switchboard ran it for you just now; the owner was not at your terminal and "
         "nobody else was going to. Carry on with the work you were holding, including anything asked of you while "
         "you waited. Do not end a turn waiting for someone to type a slash command: if you are truly blocked, "
         "`sb notify \"...\"` pages the owner.]")


def last_turn(screen):
    """The last turn's lines: after the last prompt line above the composer, up to the composer."""
    lines = screen.splitlines()
    at = [i for i, l in enumerate(lines) if PROMPT.match(l)]
    if not at:
        return ""
    return "\n".join(lines[(at[-2] + 1) if len(at) > 1 else 0:at[-1]])


def read(screen):
    """("compact", why) | ("error", text) | None for a screen at rest."""
    turn = last_turn(screen)
    # an error ends the turn: only the turn's last lines count, so an error quoted in the prompt or a reply does not
    err = ERROR.search("\n".join([l for l in turn.splitlines() if l.strip()][-3:]))
    if err and TOO_BIG.search(turn):
        return "compact", "its turn ended on the API refusing the size of the conversation"
    if err and err.group(2) != "429":
        return "error", err.group(1)[:300]
    if ASKS.search(" ".join(turn.split())):
        return "compact", "it asked for /compact"
    return None


def check(terms, agent, t):
    """Called from the terminal tick, on the delivery thread, for each live terminal."""
    s, aid = terms.s, agent["id"]
    if agent["provider"] != "antigravity" or agent.get("paused") or agent.get("gone") or aid in terms.switching:
        return
    seen = terms.stuck.setdefault(aid, {})
    if seen.get("out") == t.last_output:
        return  # this screen was already judged
    ready, busy, comp = t.view()
    if not ready or busy or comp or t.dialog():
        return
    if t.steady_for() < QUIET_S or time.time() - (t.last_output or 0) < QUIET_S:
        return
    if s.one("SELECT 1 n FROM messages WHERE to_agent=? AND state='delivering'", (aid,)):
        return
    seen["out"] = t.last_output
    found = read(t.text())
    if not found:
        return
    kind, what = found
    if kind == "error" and antigravity.XHIGH_REFUSED in what and getattr(terms, "hub", None) \
            and time.time() - seen.get("lowered", 0) > RETRY_S:
        # The CLI's /model picker offers XHigh for models the API then refuses it for: every turn dies on this 400
        # and the agent sits idle. Nothing to ask the owner: the same
        # model at High is what was meant. Relaunch on it, tell the agent to pick its last message back up.
        # The pick is usually made in the terminal's own /model, which the hub never sees, and its "Model set to"
        # line has often scrolled off by the time a turn dies. The error alone is proof, so with no XHigh name to lower, relaunch on the
        # model the hub has for it: the CLI starts on --model again and the pick is gone.
        named = antigravity.high_for(agent.get("model"), t.text())
        model = named or agent.get("model")
        if model:
            seen["lowered"] = time.time()
            fam = named[:-len("-high")] if named and named.endswith("-high") else None
            if fam and fam not in antigravity.NO_XHIGH:
                antigravity.NO_XHIGH.add(fam)
                s.set_setting("agy_no_xhigh", sorted(antigravity.NO_XHIGH))
            s.event(aid, "system", f"its turn died on the API refusing XHigh thinking: relaunching it on {model}")

            def lower():
                try:
                    terms.hub.relaunch(aid, {"model": model, "confirm": True})
                    s.enqueue(aid, XHIGH_NOTE.format(model=model), frm="system", idem=f"stuck-xhigh:{aid}:{int(seen['lowered'])}")
                    terms.hub.notify(aid, f"was set to an XHigh model the API refuses; Switchboard relaunched it on "
                                     f"{model} and told it to carry on", kind="stuck", level="info",
                                     idem=f"stuck:{aid}:xhigh:{int(seen['lowered'])}")
                except Exception as e:
                    s.event(aid, "error", f"stuck: could not relaunch on {model}: {e}")
                    terms.page(aid, f"stopped on an API error and is sitting idle: {what}", f"stuck:{aid}:{what}")

            threading.Thread(target=lower, daemon=True).start()
            return
    if kind == "error":
        s.event(aid, "error", f"its turn ended on an API error: {what}")
        terms.page(aid, f"stopped on an API error and is sitting idle: {what}", f"stuck:{aid}:{what}")
        return
    ago = time.time() - seen.get("compacted", 0)
    if ago < RETRY_S:
        terms.page(aid, f"asks for /compact again, {int(ago // 60)} min after Switchboard ran it; it is sitting idle",
                   f"stuck:{aid}:compact:{int(seen['compacted'])}")
        return
    seen["compacted"] = time.time()
    s.event(aid, "system", f"{what} and is at rest: typing /compact for it, then telling it to carry on")
    t.write("/compact")
    time.sleep(0.6)
    t.write("\r")

    def nudge():
        try:
            s.enqueue(aid, NUDGE, frm="system", idem=f"stuck-compact:{aid}:{int(seen['compacted'])}")
        except Exception as e:
            s.event(aid, "error", f"stuck: the carry-on note could not be queued: {e}")

    timer = threading.Timer(NUDGE_S, nudge)
    timer.daemon = True
    timer.start()
