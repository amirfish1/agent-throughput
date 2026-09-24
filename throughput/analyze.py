"""Why one session cost what it did, and what would have made it cheaper.

Everything is priced per call at list price (the same rates as ``event_costs``),
then converted to what you really pay with the engine's LIST:REAL ratio for the
months the session ran in (or a ratio you supply, e.g. when one subscription is
shared across machines and this DB only sees one of them).

Findings are ranked by share of the session's list cost, not by share of tokens:
cache reads are cheap per token, so a token share overstates cached-context
problems. Findings overlap (a polling call also carries the large context), so
their shares are not additive.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from typing import List, Optional

from . import actions, fees

# Only findings at or above this share of list cost are reported.
MIN_FINDING_SHARE = 5.0
# What a call was doing, from its label (see actions.py). None = it read a new message, or
# the previous call made no tool calls: reading and replying.
ACTIVITY_OF = {
    actions.READ: "reading and searching code",
    actions.EDIT: "editing code",
    actions.TEST: "testing and checks",
    actions.GIT: "committing and pushing (git)",
    actions.OPS: "servers and deploys (ssh, sudo, services)",
    actions.RUN: "running scripts and commands",
    actions.WORK: "own work (not split: re-ingest)",
    actions.DISPATCH: "handing work to other agents",
    actions.STEER: "handing work to other agents",
    actions.STATUS: "checking on progress",
    actions.WAIT: "waiting (sleep)",
    None: "reading messages and replying",
}
MIN_JOB_CALLS = 3  # shorter stretches of own work are not worth briefing an agent for
MIN_HANDOFFS = 5  # a session that hands work to other agents this often is supervising


class SessionNotFound(LookupError):
    pass


def find_session(conn, ref: str, engine: Optional[str] = None) -> dict:
    """The one session whose source id starts with ``ref``."""
    sql = "SELECT * FROM sessions WHERE source_session_id LIKE ?" + (" AND engine = ?" if engine else "")
    rows = conn.execute(sql + " ORDER BY started_at LIMIT 6", (ref + "%", *([engine] if engine else []))).fetchall()
    if not rows:
        raise SessionNotFound(f"no session id starts with {ref!r}")
    if len(rows) > 1:
        ids = ", ".join(f"{r['engine']}:{r['source_session_id']}" for r in rows[:5])
        raise SessionNotFound(f"{ref!r} matches several sessions ({ids}); give more of the id")
    return dict(rows[0])


def _events(conn, session_id: int) -> list:
    return [dict(r) for r in conn.execute(
        "SELECT c.*, r.input_rate, r.cache_read_rate, r.cache_write_5m_rate, r.cache_write_1h_rate, "
        "r.output_rate, r.long_context_threshold, r.long_context_multiplier "
        "FROM event_costs c LEFT JOIN price_rates r ON r.id = c.rate_id "
        "WHERE c.session_id = ? ORDER BY c.turn_index, c.ts, c.event_id",
        (session_id,),
    )]


def _parts(e: dict, inp=None, cr=None) -> Optional[dict]:
    """List-price dollars of one call, split by bucket. ``inp``/``cr`` override the counts (replay)."""
    if e["cost_usd"] is None:
        return None
    inp = e["input_tokens"] if inp is None else inp
    cr = e["cache_read_tokens"] if cr is None else cr
    cc, cc1h = e["cache_creation_tokens"], e["cache_creation_1h_tokens"]
    mult = 1.0
    if e["long_context_threshold"] is not None and inp + cr + cc > e["long_context_threshold"]:
        mult = e["long_context_multiplier"] or 1.0
    rate = lambda k: e[k] or 0.0  # noqa: E731 - a missing rate only occurs with zero tokens (cost is priced)
    write = (cc - cc1h) * rate("cache_write_5m_rate") + cc1h * (e["cache_write_1h_rate"] or rate("cache_write_5m_rate"))
    return {
        "fresh": mult * inp * rate("input_rate") / 1e6,
        "cache_read": mult * cr * rate("cache_read_rate") / 1e6,
        "cache_write": mult * write / 1e6,
        "output": e["output_tokens"] * rate("output_rate") / 1e6,
        "premium": (mult - 1.0) * (inp * rate("input_rate") + cr * rate("cache_read_rate") + write) / 1e6,
        "multiplier": mult,
    }


def _cost(p: dict) -> float:
    return p["fresh"] + p["cache_read"] + p["cache_write"] + p["output"]


def _month_start(d: date) -> date:
    return d.replace(day=1)


def _next_month(d: date) -> date:
    return (d.replace(day=28) + timedelta(days=4)).replace(day=1)


def real_ratio(conn, engine: str, started_at: str, last_at: str, as_of: Optional[date] = None):
    """``(list:real, source)`` for the engine over the calendar months the session ran in,
    through ``as_of`` (default today); ``(None, reason)`` when no fee covers them."""
    as_of = as_of or datetime.now(timezone.utc).date()
    start = _month_start(fees.parse_day(started_at))
    end = min(_next_month(fees.parse_day(last_at or started_at)), as_of + timedelta(days=1))
    fee = fees.fee_for_range(fees.load_plans(conn, engine), start, end)
    if not fee:
        return None, f"no {engine} plan covers {start.isoformat()}..{end.isoformat()}"
    listed = conn.execute(
        "SELECT SUM(cost_usd) FROM event_costs WHERE engine = ? AND ts >= ? AND ts < ?",
        (engine, start.isoformat(), end.isoformat()),
    ).fetchone()[0] or 0.0
    return listed / fee, f"{engine} list $ / fee over {start.isoformat()}..{end.isoformat()} (this DB only)"


def simulate(events: list, drop_poll: bool = False, fresh_per_turn: bool = False,
             waive_premium: bool = False, delegate_work: bool = False,
             brief_tokens: int = 20_000) -> Optional[float]:
    """List cost of the same calls with some fixes applied (``None`` if a fix needs labels the events lack).

    * ``drop_poll``: calls that only read a wait or a status check are not made (push: the
      finished work wakes the agent instead of the agent re-checking).
    * ``fresh_per_turn``: every turn starts a new session carrying only a ``brief_tokens``
      summary. Within a turn the context still grows exactly as it did; what was carried in
      from earlier turns is replaced by the brief, and the first call of each turn pays for
      its whole prompt uncached (a new session has no cache).
    * ``waive_premium``: no call pays the long-context premium.
    * ``delegate_work``: each stretch of ``MIN_JOB_CALLS`` or more consecutive ``work`` calls in
      a turn is a job a sub-agent does instead, starting from a brief the way a fresh turn
      does, when that is cheaper than doing it in place. The supervisor's own calls are
      priced as they happened (conservative: its context would also have grown less
      without the job's tool output).
    """
    labelled = any(e["action"] is not None for e in events)
    if (drop_poll or delegate_work) and not labelled:
        return None
    if fresh_per_turn and (not events or any(e["turn_index"] is None for e in events)):
        return None

    def cost(p):
        return 0.0 if p is None else _cost(p) - (p["premium"] if waive_premium else 0.0)

    total, turn, base = 0.0, object(), 0
    job = []  # (cost where it happened, cost as a sub-agent job) per call of the current work stretch

    def close_job():
        nonlocal total, job
        here, delegated = sum(c for c, _ in job), sum(d for _, d in job)
        total += min(here, delegated) if len(job) >= MIN_JOB_CALLS else here
        job = []

    for e in events:
        inp, cr, cc = e["input_tokens"], e["cache_read_tokens"], e["cache_creation_tokens"]
        prompt = inp + cr + cc
        first = e["turn_index"] != turn
        if first:
            turn, base = e["turn_index"], prompt
        if job and (first or e["action"] not in actions.WORK_ACTIONS):
            close_job()
        if drop_poll and e["action"] in actions.POLL_ACTIONS:
            continue
        if fresh_per_turn and first:
            p = _parts(e, inp=max(inp, min(prompt, brief_tokens) - cc), cr=0)
        elif fresh_per_turn:
            replay = min(prompt, brief_tokens + max(0, prompt - base))
            p = _parts(e, cr=max(0, cr - (prompt - replay)))
        else:
            p = _parts(e)
        if delegate_work and e["action"] in actions.WORK_ACTIONS:
            if not job:
                job_base = prompt
                d = _parts(e, inp=max(inp, min(prompt, brief_tokens) - cc), cr=0)
            else:
                replay = min(prompt, brief_tokens + max(0, prompt - job_base))
                d = _parts(e, cr=max(0, cr - (prompt - replay)))
            job.append((cost(p), cost(d)))
            continue
        total += cost(p)
    if job:
        close_job()
    return total


def cadence(starts: List[str]) -> Optional[float]:
    """Median seconds between turn starts when the turns look scheduled, else ``None``.

    Scheduled = at least 5 turns, a median gap of 10 minutes or more, and at least
    half of the gaps within 25% of that median (a timer, not a person typing).
    """
    ts = sorted(datetime.fromisoformat(t.replace("Z", "+00:00")) for t in starts if t)
    gaps = sorted((b - a).total_seconds() for a, b in zip(ts, ts[1:]))
    if len(gaps) < 4:
        return None
    median = gaps[len(gaps) // 2]
    if median < 600:
        return None
    regular = sum(1 for g in gaps if abs(g - median) <= 0.25 * median)
    return median if regular * 2 >= len(gaps) else None


def analyze(conn, ref: str, engine: Optional[str] = None, list_to_real: Optional[float] = None,
            brief_tokens: int = 20_000, top_turns: int = 3, as_of: Optional[date] = None) -> dict:
    s = find_session(conn, ref, engine)
    ev = _events(conn, s["id"])
    priced = [(e, _parts(e)) for e in ev]
    priced = [(e, p) for e, p in priced if p is not None]
    list_cost = sum(_cost(p) for _, p in priced)
    unpriced = len(ev) - len(priced)

    if list_to_real:
        ratio, ratio_source = list_to_real, "given with --list-to-real"
    else:
        ratio, ratio_source = real_ratio(conn, s["engine"], s["started_at"], s["last_activity_at"], as_of)
    real = (lambda usd: usd / ratio) if ratio else (lambda usd: None)
    share = (lambda usd: 100.0 * usd / list_cost) if list_cost else (lambda usd: 0.0)

    def money(usd):
        return {"list_usd": round(usd, 2), "real_usd": None if real(usd) is None else round(real(usd), 2),
                "share_pct": round(share(usd), 1)}

    tok = {k: sum(e[k] for e in ev) for k in ("input_tokens", "cache_read_tokens", "cache_creation_tokens", "output_tokens")}
    buckets = [
        ("cache reads (re-sent context)", tok["cache_read_tokens"], sum(p["cache_read"] for _, p in priced)),
        ("cache writes", tok["cache_creation_tokens"], sum(p["cache_write"] for _, p in priced)),
        ("fresh input", tok["input_tokens"], sum(p["fresh"] for _, p in priced)),
        ("output", tok["output_tokens"], sum(p["output"] for _, p in priced)),
    ]
    breakdown = [dict(bucket=b, tokens=t, **money(c)) for b, t, c in buckets if t]

    # The same 100% sliced by what each call was doing (the tool results it read).
    acts = {}
    for e, p in priced:
        a = ACTIVITY_OF.get(e["action"], ACTIVITY_OF[None])
        acts.setdefault(a, [0, 0.0])
        acts[a][0] += 1
        acts[a][1] += _cost(p)
    activities = [dict(activity=a, calls=n, **money(usd)) for a, (n, usd) in sorted(acts.items(), key=lambda kv: -kv[1][1])]

    turns = {}
    for e, p in priced:
        t = turns.setdefault(e["turn_index"], {"turn": e["turn_index"], "started": e["ts"], "calls": 0, "usd": 0.0})
        t["calls"] += 1
        t["usd"] += _cost(p)
    heaviest = sorted(turns.values(), key=lambda t: -t["usd"])[:top_turns] if None not in turns else []
    heaviest = [dict(turn=t["turn"], started=t["started"], calls=t["calls"], **money(t["usd"])) for t in heaviest]

    # Each fix is one simulator flag. Score = cost with every fix applied / actual cost.
    # Fixes are ranked by what each saves alone, then applied cumulatively in that order:
    # a fix's gain is what it adds on top of the ones above it, so the gains sum to 100 - score.
    sim = lambda **kw: simulate(ev, brief_tokens=brief_tokens, **kw)  # noqa: E731
    prompts = [e["input_tokens"] + e["cache_read_tokens"] + e["cache_creation_tokens"] for e in ev]
    poll = [e for e in ev if e["action"] in actions.POLL_ACTIONS]
    waits = sum(1 for e in poll if e["action"] == actions.WAIT)
    premium_calls = sum(1 for _, p in priced if p["premium"] > 0)
    # Of all re-sent context, how much was already there when the turn started
    # (carried over: a fresh session fixes it) vs built up within the turn (delegation fixes it).
    carried, turn_base = 0, {}
    for e, prompt in zip(ev, prompts):
        carried += min(prompt, turn_base.setdefault(e["turn_index"], prompt))
    carried_pct = 100.0 * carried / sum(prompts) if sum(prompts) else 0.0
    handoffs = [e for e in ev if e["action"] in actions.DISPATCH_ACTIONS]
    steers = sum(1 for e in handoffs if e["action"] == actions.STEER)
    per_hour = {}
    for e in handoffs:
        per_hour[e["ts"][:13]] = per_hour.get(e["ts"][:13], 0) + 1
    work_usd = sum(_cost(p) for e, p in priced if e["action"] in actions.WORK_ACTIONS)
    poll_usd = sum(_cost(p) for e, p in priced if e["action"] in actions.POLL_ACTIONS)
    supervising = len(handoffs) >= MIN_HANDOFFS
    supervision = None if not handoffs else {
        "handoffs": len(handoffs), "steers": steers, "peak_per_hour": max(per_hour.values()),
        "work_share_pct": round(share(work_usd), 1), "poll_share_pct": round(share(poll_usd), 1)}
    starts = {}
    for e in ev:
        starts.setdefault(e["turn_index"], e["ts"])
    every = cadence(list(starts.values())) if None not in starts else None
    span = None
    if every:
        span = f"{every / 3600:.0f} hours" if every >= 5400 else ("hour" if every >= 2700 else f"{every / 60:.0f} minutes")
        fresh_fix = (f"Keep it one task, but make each wake-up (about every {span}) a new session: it reads a "
                     "state file (goal, decisions so far, open experiments, latest numbers), acts, appends what "
                     "it decided, and exits. The history lives in the file, not in the context.")
    else:
        fresh_fix = ("When a turn moves to a new step, hand off instead of continuing: write a short note of what "
                     "is decided and what is open, and continue in a new session that starts from it.")
    if carried_pct < 50:
        fresh_fix += (" Most context built up within single turns, which a new session does not fix: let "
                      "sub-agents do the reading and return summaries, so raw tool output never enters this context.")
    candidates = [
        ("push_not_pull", {"drop_poll": True}, "Switch from pull to push while waiting",
         f"{len(poll):,} of {len(ev):,} calls only read the result of a wait ({waits:,}) or a status "
         "check ({:,}), and each re-sent the whole context.".format(len(poll) - waits),
         "Have the work you are waiting on report back (a completion message, hook or notification) "
         "and end the turn, instead of sleeping and re-checking."),
        ("fresh_session", {"fresh_per_turn": True}, "Start each turn from a brief, not the whole history",
         (f"The agent was started {len(starts):,} times (by you, a schedule or auto-continue)"
          + (f", about every {span}" if span else "")
          + f", and each start re-read every earlier turn. Calls re-sent {sum(prompts) / max(len(prompts), 1) / 1000:,.0f}K "
          f"of context on average (peak {max(prompts, default=0) / 1000:,.0f}K); {carried_pct:.0f}% of it was "
          f"carried over from earlier turns. Simulated: every turn starts from a {brief_tokens // 1000}K-token brief."),
         fresh_fix),
        ("delegate_checking", {"delegate_work": True}, "Delegate the checking, not just the building",
         (f"This session handed work to other agents {len(handoffs):,} times ({steers:,} of them interrupted "
          f"an agent mid-task; up to {supervision['peak_per_hour'] if supervision else 0} in one hour), yet "
          f"{share(work_usd):.0f}% of its cost went to work it did itself in its own context: reading code, "
          f"running tests and scripts. Simulated: every stretch of {MIN_JOB_CALLS}+ calls of that work goes to a sub-agent "
          f"starting from a {brief_tokens // 1000}K-token brief, when that is cheaper."),
         ("Send investigation and verification to an agent too: brief it, end the turn, and act on its short "
          "report. Batch corrections into one message per agent instead of steering it mid-task.")),
        ("long_context", {"waive_premium": True}, "Stay under the long-context price step",
         f"{premium_calls:,} calls crossed the model's long-context threshold and paid a higher input rate.",
         "Start a new session (or compact) before the context reaches the threshold."),
    ]
    candidates = [c for c in candidates if c[0] != "delegate_checking" or supervising]
    applicable = [c for c in candidates if sim(**c[1]) is not None]
    alone = {c[0]: sim(**c[1]) for c in applicable}
    applicable.sort(key=lambda c: alone[c[0]])
    optimized = sim(**{k: v for c in applicable for k, v in c[1].items()}) if applicable else list_cost
    score_of = lambda cost: round(100.0 * optimized / cost) if cost > 0 else 100  # noqa: E731
    score = score_of(list_cost)

    findings, minor, flags, prev = [], [], {}, score
    for key, fl, title, evidence, fix in applicable:
        flags.update(fl)
        now = score_of(sim(**flags))
        f = dict(key=key, title=title, score_gain=now - prev, evidence=evidence, fix=fix,
                 **money(list_cost - alone[key]))
        prev = now
        (findings if f["share_pct"] >= MIN_FINDING_SHARE else minor).append(f)

    return {
        "session": {k: s[k] for k in ("engine", "source_session_id", "model_label", "model_id", "project_name",
                                      "started_at", "last_activity_at", "duration_seconds", "compaction_count")},
        "calls": len(ev),
        "turns": len(starts),
        "list_usd": round(list_cost, 2),
        "real_usd": None if real(list_cost) is None else round(real(list_cost), 2),
        "list_to_real": None if ratio is None else round(ratio, 2),
        "ratio_source": ratio_source,
        "unpriced_calls": unpriced,
        "labelled": any(e["action"] is not None for e in ev),
        "score": score,
        "optimized": money(optimized),
        "breakdown": breakdown,
        "activities": activities,
        "heaviest_turns": heaviest,
        "supervision": supervision,
        "findings": findings,
        "minor_findings": minor,
    }
