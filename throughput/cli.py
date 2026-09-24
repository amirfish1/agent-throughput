"""``throughput`` command line: ingest and query the usage DB."""

from __future__ import annotations

import argparse
import json
import os
import re
import sqlite3
import sys
import tempfile
from datetime import datetime, timedelta

from . import ingest as ingest_mod
from . import analyze as analyze_mod
from . import fees, pricing, queries, render, schema
from .adapters import ADAPTERS

ENGINE_ALIASES = {"claude": "claude_code", "claude-code": "claude_code", "claude_code": "claude_code",
                  "codex": "codex", "kimi": "kimi"}


def default_db_path() -> str:
    env = os.environ.get("THROUGHPUT_DB", "").strip()
    if env:
        return os.path.expanduser(env)
    base = os.environ.get("XDG_DATA_HOME", "").strip() or os.path.join(os.path.expanduser("~"), ".local", "share")
    return os.path.join(base, "throughput", "throughput.sqlite3")


def _engine(value):
    if value is None:
        return None
    try:
        return ENGINE_ALIASES[value.lower()]
    except KeyError:
        raise argparse.ArgumentTypeError(f"unknown engine {value!r} (claude, codex, kimi)")


def _open(args, must_exist=False):
    path = args.db or default_db_path()
    if must_exist and not os.path.exists(path):
        sys.exit(f"no usage DB at {path}; run 'throughput ingest' first")
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    conn = schema.connect(path)
    if schema.migrate(conn) == 1:
        pricing.backfill_long_context(conn)
    return conn


# Table headers for the columns that mean "what you actually pay" vs "list price".
LABELS = {
    "cost_usd_priced": "list $", "cost_usd": "list $", "trailing_30d_usd": "list $ 30d",
    "real_cost_usd": "real $", "real_usd_per_mtok": "REAL /MTok",
    "real_usd_per_mtok_noncache": "REAL /MTok (non-cache)", "list_to_real": "LIST:REAL",
    "trailing_30d_real_usd": "real $ 30d", "trailing_30d_real_usd_per_mtok": "REAL /MTok 30d",
    "trailing_30d_real_usd_per_mtok_noncache": "REAL /MTok non-cache 30d",
    "trailing_30d_list_to_real": "LIST:REAL 30d", "month_to_date_usd": "list $ mtd",
    "month_to_date_real_usd": "real $ mtd", "month_to_date_list_to_real": "LIST:REAL mtd",
    "month_projected_usd": "list $ month proj", "cache_read_pct": "cache read %",
    "unpriced_pct": "unpriced %", "list_usd_per_mtok": "list /MTok", "list_share_pct": "% of list $",
    "trailing_30d_unpriced_pct": "unpriced % 30d", "monthly_fee": "monthly fee",
    "est_real_cost_usd": "est. real $", "est_real_usd_per_mtok": "est. REAL /MTok",
}
# Unpriced calls are only worth a column when they are a meaningful share of the calls.
UNPRICED_WARN_PCT = 15.0

_DOLLARS = {"cost_usd_priced", "cost_usd", "trailing_30d_usd", "real_cost_usd", "trailing_30d_real_usd",
            "month_to_date_usd", "month_to_date_real_usd", "month_projected_usd", "monthly_fee", "est_real_cost_usd"}
_PER_MTOK = {"list_usd_per_mtok", "real_usd_per_mtok", "real_usd_per_mtok_noncache",
             "trailing_30d_real_usd_per_mtok", "trailing_30d_real_usd_per_mtok_noncache",
             "est_real_usd_per_mtok"}  # dollars per 1M tokens
_TOKENS = {"total_tokens", "trailing_30d_tokens"}
_MULTIPLIERS = {"list_to_real", "trailing_30d_list_to_real", "month_to_date_list_to_real"}
_PCT = {"cache_read_pct", "unpriced_pct", "list_share_pct", "trailing_30d_unpriced_pct"}


def _money(v):
    """``$XX``: whole dollars, with cents only below $10 (``$3.67``, ``$0.07``)."""
    if abs(v) >= 10:
        return f"${v:,.0f}"
    text = f"{v:.2f}"
    return "$" + (text[:-3] if text.endswith(".00") else text)


def _cents(dollars_per_mtok):
    """Per-1M-token price: cents below $1 (``2.31 cents``), dollars from $1 up (``$1.33``)."""
    if abs(dollars_per_mtok) >= 1:
        return f"${dollars_per_mtok:,.2f}"
    c = dollars_per_mtok * 100
    return f"{c:.1f} cents" if abs(c) >= 10 else f"{c:.2f} cents"


def _tokens(n):
    for div, suffix in ((1e9, "B"), (1e6, "M"), (1e3, "K")):
        if abs(n) >= div:
            x = n / div
            return f"{x:.0f}{suffix}" if x >= 100 else f"{x:.1f}{suffix}"
    return f"{n:,.0f}"


def _fmt(v, col=None, raw=False):
    if v is None:
        return "-"
    if not raw and isinstance(v, (int, float)) and not isinstance(v, bool):
        if col in _DOLLARS:
            return _money(v)
        if col in _PER_MTOK:
            return _cents(v)
        if col in _TOKENS:
            return _tokens(v)
        if col in _MULTIPLIERS:
            return f"{v:,.1f}x"
        if col in _PCT:
            return f"{v:.1f}"
    if isinstance(v, float):
        return f"{v:,.2f}"
    if isinstance(v, int):
        return f"{v:,}"
    return str(v)


_NUMERIC = re.compile(r"-|\$?-?[\d,.]+( cents|[KMBx])?")


def print_table(rows, cols=None, out=sys.stdout, raw=False):
    if not rows:
        print("(no rows)", file=out)
        return
    cols = cols or list(rows[0].keys())
    body = [[_fmt(r.get(c), c, raw) for c in cols] for r in rows]
    heads = [c if raw else LABELS.get(c, c) for c in cols]
    widths = [max(len(h), *(len(b[i]) for b in body)) for i, h in enumerate(heads)]
    print("  ".join(h.ljust(w) for h, w in zip(heads, widths)), file=out)
    print("  ".join("-" * w for w in widths), file=out)
    for b in body:
        print("  ".join(v.rjust(w) if _NUMERIC.fullmatch(v) else v.ljust(w) for v, w in zip(b, widths)), file=out)


def cmd_ingest(args):
    roots = {"claude_code": args.claude_root, "codex": args.codex_root, "kimi": args.kimi_root}
    engines = args.engine or None
    if args.dry_run and not os.path.exists(args.db or default_db_path()):
        # Dry run against a fresh DB: use a throwaway file, never create the real one.
        tmp = tempfile.mkdtemp(prefix="throughput-dry-")
        args.db = os.path.join(tmp, "dry.sqlite3")
    conn = _open(args)
    pricing.ensure_rates(conn)
    log = (lambda m: print(m, file=sys.stderr)) if not args.json else (lambda m: None)
    since_ns = None
    if args.since:
        since_ns = int(datetime.combine(fees.parse_day(args.since), datetime.min.time()).timestamp() * 1e9)
    rep = ingest_mod.ingest(conn, roots, engines, args.full_rebuild, args.dry_run, log, since_ns=since_ns)
    if args.json:
        print(json.dumps(rep.as_dict(), indent=2))
        return 0
    print(("DRY RUN (nothing written) - " if args.dry_run else "") + "ingest complete")
    rows = [{"engine": e, **c} for e, c in sorted(rep.engines.items())]
    print_table(rows, ["engine", "discovered", "inserted", "updated", "unchanged", "skipped", "failed"])
    print(f"usage events added: {rep.events_added:,}; duplicate events left with their first session: "
          f"{rep.duplicate_events_skipped:,}")
    for eng, n in sorted(rep.missing_source_files.items()):
        if n:
            print(f"note: {n} previously ingested {eng} source file(s) no longer exist (rows kept)")
    for eng, n in sorted(rep.older_files.items()):
        if n:
            print(f"note: {n:,} {eng} file(s) last modified before {args.since} were not read; rows already in the "
                  "DB are kept, files never ingested are missing from totals until an ingest without --since")
    bad = [d for d in rep.details if d[1] in ("failed", "missing_root")]
    for eng, kind, path, reason in bad:
        print(f"  {kind.upper()} [{eng}] {path}: {reason}")
    if args.verbose:
        for eng, kind, path, reason in rep.details:
            if kind == "skipped":
                print(f"  skipped [{eng}] {path}: {reason}")
    return 1 if any(d[1] == "failed" for d in rep.details) else 0


def _month_range(args):
    """Resolve ``--month YYYY-MM`` into (since, until); it cannot be combined with --since/--until."""
    if not getattr(args, "month", None):
        return args.since, getattr(args, "until", None)
    if args.since or getattr(args, "until", None):
        sys.exit("--month replaces --since/--until; use one or the other")
    try:
        first = datetime.strptime(args.month, "%Y-%m").date()
    except ValueError:
        sys.exit(f"bad --month {args.month!r}: use YYYY-MM")
    nxt = (first.replace(day=28) + timedelta(days=4)).replace(day=1)
    return first.isoformat(), nxt.isoformat()


def cmd_sessions(args):
    conn = _open(args, True)
    sub = {"exclude": False, "only": True, "include": None}[args.subagents]
    since, until = _month_range(args)
    rows = queries.list_sessions(conn, args.engine, args.provider, args.model, args.project,
                                 since, until, sub, args.order, args.limit)
    if args.json:
        print(json.dumps(rows, indent=2, default=str))
        return 0
    for r in rows:
        r["started"] = (r["started_at"] or "")[:16]
        r["sid"] = r["source_session_id"][:14]
    print_table(rows, ["engine", "sid", "started", "model_label", "project_name", "message_count",
                       "total_tokens", "cost_usd", "unpriced_event_count"])
    return 0


def _with_unpriced(rows, cols, key):
    """Append the unpriced-% column, filled only for rows over the threshold, and only if any is."""
    over = [r for r in rows if (r.get(key) or 0) > UNPRICED_WARN_PCT]
    if not over:
        return rows, cols, False
    shown = [dict(r, **{key: r[key] if (r.get(key) or 0) > UNPRICED_WARN_PCT else None}) for r in rows]
    return shown, cols + [key], True


def _fee_notes(conn, rows):
    notes = []
    for eng in sorted({r["engine"] for r in rows}):
        if not fees.load_plans(conn, eng):
            notes.append(f"no fee configured for {eng}: its REAL columns are '-' (add one with 'plans add')")
        for name in fees.undated_plans(conn, eng):
            notes.append(f"plan '{name}' has no start date: its fee is applied to every period shown "
                         "(set one with 'plans add --name ... --since YYYY-MM-DD')")
    return notes


def cmd_summary(args):
    conn = _open(args, True)
    since, until = _month_range(args)
    rows = queries.summarize(conn, args.by, since, args.engine, args.model, args.split_model,
                             args.by_family, args.as_of, until)
    if args.json:
        print(json.dumps(rows, indent=2, default=str))
        return 0
    model_view = bool(args.split_model or args.by_family or args.model)
    lead = ["engine", "model"] if args.by == "engine" else ["period", "engine"] + (["model"] if model_view else [])
    if model_view and args.by != "engine":
        # The fee is per engine, so a model view has no REAL columns; show what compares models instead.
        cols = lead + ["calls", "total_tokens", "cache_read_pct", "cost_usd_priced", "list_share_pct",
                       "list_usd_per_mtok", "est_real_cost_usd", "est_real_usd_per_mtok"]
    else:
        cols = lead + ["calls", "total_tokens", "cache_read_pct", "cost_usd_priced", "real_cost_usd",
                       "list_usd_per_mtok", "real_usd_per_mtok", "list_to_real", "est_real_usd_per_mtok"]
    shown, cols, flagged = _with_unpriced(rows, cols, "unpriced_pct")
    print_table(shown, cols)
    print("\nlist $ = API list-price equivalent" + (
        f"; rows with unpriced % shown have more than {UNPRICED_WARN_PCT:.0f}% of calls on models with no "
        "(complete) price, so list $ and LIST:REAL there are lower bounds" if flagged else "") + ".")
    if not model_view or args.by == "engine":
        print("REAL = what you actually pay: your plan fee accrued daily over the period "
              "(monthly fee / days in month).\nREAL /MTok = real $ / all tokens, in cents (dollars from $1); LIST:REAL = list $ / "
              "real $. Days/weeks/months are UTC.")
    if model_view or args.by == "engine":
        print("Model rows: the fee is per engine, so the measured REAL columns are '-' on them. "
              "'est.' columns are ESTIMATES: the engine's fee split across models in proportion to their list $ "
              "(assumes quota is consumed in proportion to list price; Anthropic does not publish how Max limits "
              "weigh models). '% of list $' = share of the engine's list cost in the period; "
              "'list /MTok' = list $ / all tokens, in cents (dollars from $1).")
    for n in _fee_notes(conn, rows):
        print("note: " + n)
    return 0


def cmd_activity(args):
    conn = _open(args, True)
    rows = queries.engine_activity(conn)
    print(json.dumps(rows, indent=2) if args.json else "", end="")
    if not args.json:
        print_table(rows)
    return 0


def cmd_runrate(args):
    conn = _open(args, True)
    rows = queries.run_rate(conn, args.engine, args.as_of)
    print(json.dumps(rows, indent=2) if args.json else "", end="")
    if not args.json:
        cols = ["engine", "trailing_30d_tokens", "trailing_30d_usd", "trailing_30d_real_usd",
                "trailing_30d_real_usd_per_mtok", "trailing_30d_list_to_real",
                "month_to_date_usd", "month_to_date_real_usd", "month_projected_usd"]
        shown, cols, flagged = _with_unpriced(rows, cols, "trailing_30d_unpriced_pct")
        print_table(shown, cols)
        if flagged:
            print(f"\nlist figures for rows with unpriced % shown are lower bounds (>{UNPRICED_WARN_PCT:.0f}% of calls unpriced).")
        for n in _fee_notes(conn, rows):
            print("note: " + n)
    return 0


def cmd_breakeven(args):
    conn = _open(args, True)
    res = queries.break_even(conn, args.fee, args.engine, args.as_of)
    print(json.dumps(res, indent=2))
    return 0


def _day(value):
    try:
        return fees.parse_day(value).isoformat() if value else None
    except ValueError:
        sys.exit(f"bad date {value!r}: use YYYY-MM-DD")


def cmd_plans(args):
    conn = _open(args)
    if args.action == "add":
        if args.currency.upper() != "USD":
            sys.exit("only USD plans are supported (rates and list-price costs are USD)")
        since, until = _day(args.since), _day(args.until)
        if since and until and until <= since:
            sys.exit("--until must be after --since (until is exclusive)")
        # Same name = update in place (this is how you set or fix a plan's dates).
        conn.execute("INSERT OR REPLACE INTO subscription_plans (name, engine, monthly_fee, currency, "
                     "active_from, active_to, note) VALUES (?,?,?,?,?,?,?)",
                     (args.name, args.engine, args.fee, "USD", since, until, args.note))
        conn.commit()
    print_table([dict(r) for r in conn.execute("SELECT * FROM subscription_plans ORDER BY engine, name")])
    print("\nactive_from is inclusive, active_to exclusive; blank = open-ended. "
          "Re-run 'plans add' with the same --name to change a plan.")
    return 0


def cmd_rates(args):
    conn = _open(args)
    if args.action == "load":
        n = pricing.load_rates(conn, args.file)
        print(f"loaded {n} rate row(s)")
    rows = [dict(r) for r in conn.execute(
        "SELECT pricing_key, effective_from, input_rate, cache_read_rate, cache_write_5m_rate, "
        "cache_write_1h_rate, output_rate, verified_at FROM price_rates ORDER BY pricing_key, effective_from")]
    print_table(rows)
    return 0


def _dur(seconds):
    if not seconds:
        return "-"
    h, m = divmod(int(seconds) // 60, 60)
    return f"{h}h{m:02d}m" if h else f"{m}m"


def _usd_pair(d):
    real = f" · ≈ {_money(d['real_usd'])} real" if d.get("real_usd") is not None else ""
    return f"{_money(d['list_usd'])} list{real}"


def cmd_analyze(args):
    conn = _open(args, True)
    try:
        r = analyze_mod.analyze(conn, args.session, args.engine, args.list_to_real, args.brief,
                                as_of=fees.parse_day(args.as_of) if args.as_of else None)
    except analyze_mod.SessionNotFound as exc:
        sys.exit(str(exc))
    if args.json:
        print(json.dumps(r, indent=2, default=str))
        return 0
    _print_analysis(r, render.Style(render.color_enabled()), render.width())
    return 0


def _print_analysis(r, st, W):
    s = r["session"]
    ts = lambda v: (v or "")[:16].replace("T", " ")  # noqa: E731
    sid = s["source_session_id"]
    print(st(sid[:8], "bold") + st(sid[8:], "grey") + "  " + st(
        f"{s['engine']} · {s['model_label'] or s['model_id'] or '?'} · {s['project_name'] or '-'}", "cyan"))
    print(st(f"{ts(s['started_at'])} → {ts(s['last_activity_at'])} · {_dur(s['duration_seconds'])} · "
             f"{r['turns']:,} turns · {r['calls']:,} model calls · {s['compaction_count']} compaction{'' if s['compaction_count'] == 1 else 's'}", "grey"))

    print()
    cost = "  " + st("Cost ", "bold") + " " + st(f"{_money(r['list_usd'])} list", "bold")
    if r["real_usd"] is not None:
        cost += "  " + st(f"≈ {_money(r['real_usd'])} real", "bold", "magenta") + st(
            f"   ({r['list_to_real']:.1f}:1, {r['ratio_source']})", "grey")
    else:
        cost += st(f"   (real $ unknown: {r['ratio_source']}; add a plan or pass --list-to-real)", "grey")
    print(cost)
    color = render.score_color(r["score"])
    filled, empty = render.gauge(r["score"] / 100, 30)
    print("  " + st("Score", "bold") + " " + st(f"{r['score']:>3}", "bold", color) + st("/100", "grey")
          + "  " + st(filled, color) + st(empty, "grey"))
    print("  " + st(f"with every fix below: {_usd_pair(r['optimized'])}  "
                    "(100 = no avoidable spend these checks can find)", "grey"))
    if r["unpriced_calls"]:
        print("  " + st(f"note: {r['unpriced_calls']:,} calls have no price on file and are left out, "
                        "so dollars are lower bounds", "yellow"))

    print("\n" + render.rule("Where the money went", W, st))
    label_w = max(len(b["bucket"]) for b in r["breakdown"])
    for b in r["breakdown"]:
        print(f"  {b['bucket']:<{label_w}}  {_tokens(b['tokens']):>6}  "
              f"{st(f'{_money(b['list_usd']):>6}', 'bold')}  {st(render.bar(b['share_pct'] / 100, 24), 'blue')}"
              f" {b['share_pct']:>3.0f}%")

    if r["heaviest_turns"]:
        print("\n" + render.rule("Heaviest turns", W, st))
        for t in r["heaviest_turns"]:
            print(f"  {st(f'#{t['turn']:<4}', 'bold')} {st(ts(t['started']), 'grey')}  {t['calls']:>5,} calls  "
                  f"{st(f'{_money(t['list_usd']):>6}', 'bold')}  {st(render.bar(t['share_pct'] / 100, 24), 'blue')}"
                  f" {t['share_pct']:>3.0f}%")

    print("\n" + render.rule("Fixes", W, st))
    if r["findings"]:
        print("  " + st("ranked by savings · each +X is what the fix adds on top of the ones above it", "grey"))
        for i, f in enumerate(r["findings"], 1):
            gain = f"+{f['score_gain']} points"
            title = f"{i}. {f['title']}"
            print()
            print("  " + st(title, "bold") + " " * max(2, W - 4 - len(title) - len(gain)) + st(gain, "bold", "green"))
            print("     " + st(f"alone saves {f['share_pct']:.0f}%", "green") + st(f" · {_usd_pair(f)}", "grey"))
            for line in render.wrap(f["evidence"], 5, W):
                print("     " + st(line, "dim"))
            for j, line in enumerate(render.wrap(f["fix"], 11, W)):
                print("     " + (st("→ Fix: ", "bold", "yellow") if j == 0 else " " * 7) + line)
    else:
        print(f"  No fix would save {analyze_mod.MIN_FINDING_SHARE:.0f}% or more of this session's cost.")
    if r["minor_findings"]:
        print()
        minor = f"also checked, each under {analyze_mod.MIN_FINDING_SHARE:.0f}%: " + "; ".join(
            f"{f['title']} ({f['share_pct']:.1f}%)" for f in r["minor_findings"])
        for line in render.wrap(minor, 2, W):
            print("  " + st(line, "grey"))
    if not r["labelled"]:
        print("\n  " + st("note: no per-call labels for this session (engine without tool detail, or ingested "
                          "before schema v2); pull/push findings are unavailable", "yellow"))


def cmd_sql(args):
    conn = _open(args, True)
    conn.execute("PRAGMA query_only = ON")
    print_table([dict(r) for r in conn.execute(args.query)], raw=True)
    return 0


def build_parser():
    p = argparse.ArgumentParser(prog="throughput", description=__doc__)
    p.add_argument("--db", help=f"SQLite path (default: {default_db_path()})")
    sub = p.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("ingest", help="read the session stores into the DB (incremental, idempotent)")
    s.add_argument("--engine", type=_engine, action="append", help="limit to an engine (repeatable)")
    s.add_argument("--dry-run", action="store_true", help="parse and report; write nothing")
    s.add_argument("--full-rebuild", action="store_true", help="drop the selected engines' rows and re-ingest")
    s.add_argument("--since", help="only read files modified on or after this local date, YYYY-MM-DD")
    s.add_argument("--claude-root"); s.add_argument("--codex-root"); s.add_argument("--kimi-root")
    s.add_argument("--json", action="store_true"); s.add_argument("-v", "--verbose", action="store_true")
    s.set_defaults(fn=cmd_ingest)

    s = sub.add_parser("sessions", help="list sessions with tokens and cost")
    s.add_argument("--engine", type=_engine); s.add_argument("--provider"); s.add_argument("--model")
    s.add_argument("--project"); s.add_argument("--since"); s.add_argument("--until")
    s.add_argument("--month", help="one calendar month, YYYY-MM (instead of --since/--until)")
    s.add_argument("--subagents", choices=["exclude", "only", "include"], default="exclude")
    s.add_argument("--order", default="started_at DESC"); s.add_argument("--limit", type=int, default=30)
    s.add_argument("--json", action="store_true"); s.set_defaults(fn=cmd_sessions)

    s = sub.add_parser("summary", help="tokens and cost by day/week/month/engine")
    s.add_argument("--by", choices=["day", "week", "month", "engine"], default="month")
    s.add_argument("--since", help="first day included (YYYY-MM-DD)")
    s.add_argument("--until", help="day to stop before, exclusive (YYYY-MM-DD)")
    s.add_argument("--month", help="one calendar month, YYYY-MM (same as --since/--until for that month)")
    s.add_argument("--engine", type=_engine)
    s.add_argument("--model", help="model name substring(s); comma-separate to compare side by side, "
                   "one row per name (e.g. sonnet,fable  or  fable-5-1)")
    s.add_argument("--split-model", action="store_true", help="one row per model version within each period")
    s.add_argument("--by-family", action="store_true",
                   help="like --split-model but Claude versions merge into Opus/Sonnet/Fable/Haiku")
    s.add_argument("--as-of", help="treat this UTC timestamp as 'now' (for the fee accrual)")
    s.add_argument("--json", action="store_true"); s.set_defaults(fn=cmd_summary)

    s = sub.add_parser("activity", help="first/last billed call per engine")
    s.add_argument("--json", action="store_true"); s.set_defaults(fn=cmd_activity)

    s = sub.add_parser("runrate", help="trailing-30-day and month-projected API-equivalent cost")
    s.add_argument("--engine", type=_engine); s.add_argument("--as-of")
    s.add_argument("--json", action="store_true"); s.set_defaults(fn=cmd_runrate)

    s = sub.add_parser("breakeven", help="second-subscription comparison (fee is an input)")
    s.add_argument("--fee", type=float, required=True, help="monthly fee of the candidate subscription")
    s.add_argument("--engine", type=_engine, default="claude_code"); s.add_argument("--as-of")
    s.set_defaults(fn=cmd_breakeven)

    s = sub.add_parser("plans", help="list/add subscription plans (fees are configuration)")
    s.add_argument("action", choices=["list", "add"], nargs="?", default="list")
    s.add_argument("--name"); s.add_argument("--engine", type=_engine); s.add_argument("--fee", type=float)
    s.add_argument("--currency", default="USD"); s.add_argument("--since", help="first day the fee applies (YYYY-MM-DD)")
    s.add_argument("--until", help="day the fee stops applying, exclusive (YYYY-MM-DD)"); s.add_argument("--note")
    s.set_defaults(fn=cmd_plans)

    s = sub.add_parser("rates", help="list rates or load a rates JSON file")
    s.add_argument("action", choices=["list", "load"], nargs="?", default="list")
    s.add_argument("--file"); s.set_defaults(fn=cmd_rates)

    s = sub.add_parser("analyze", help="why one session cost what it did, and what would have been cheaper")
    s.add_argument("session", help="session id or a unique prefix of it")
    s.add_argument("--engine", type=_engine)
    s.add_argument("--list-to-real", type=float, metavar="X",
                   help="LIST:REAL ratio to convert list $ to real $ (default: from your plans and this DB)")
    s.add_argument("--brief", type=int, default=20_000, metavar="TOKENS",
                   help="summary size a fresh session would start from in the what-if (default 20000)")
    s.add_argument("--as-of", help="end of the fee period for the real ratio (YYYY-MM-DD, default today)")
    s.add_argument("--json", action="store_true"); s.set_defaults(fn=cmd_analyze)

    s = sub.add_parser("sql", help="run a read-only SQL query")
    s.add_argument("query"); s.set_defaults(fn=cmd_sql)
    return p


def main(argv=None):
    args = build_parser().parse_args(argv)
    if args.cmd == "plans" and args.action == "add" and not (args.name and args.engine and args.fee is not None):
        sys.exit("plans add requires --name, --engine and --fee")
    try:
        return args.fn(args) or 0
    except sqlite3.Error as exc:
        sys.exit(f"database error: {exc}")


if __name__ == "__main__":
    sys.exit(main())
