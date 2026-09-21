# throughput

What do your AI coding subscriptions **really** cost per token, and is a second one worth it?

`throughput` reads the session logs that Claude Code, Codex and Kimi already write to your disk and builds a local
SQLite database: one row per session (fresh input, cache read, cache write and output tokens, model, message counts,
timestamps) plus one row per API call. It prices every call at list price, then sets that against the flat monthly
fee you actually pay, so you can see both numbers side by side:

```
period   engine       calls   total_tokens  cache read %  list $  real $  list /MTok  REAL /MTok  LIST:REAL
2026-09  claude_code  49,292          6.1B          96.2  $3,775    $140  62.2 cents  2.31 cents      27.0x
```

- **list $**: what the same tokens would cost at API list prices.
- **real $**: your plan fee, accrued daily over the period.
- **REAL /MTok**: real $ ÷ all tokens. **LIST:REAL**: list $ ÷ real $, the list-price dollars each dollar you pay buys.

Standard library only. Your logs are **only read**; conversation text is never copied into the database.

## Install

```bash
pipx install git+https://github.com/amirfish1/agent-throughput   # or: uv tool install ...
# or run from a clone with no install:  python3 -m throughput ...
```

Python 3.9+.

## Use

```bash
throughput ingest                                   # read your session stores (a minute cold, seconds after)
throughput plans add --name claude --engine claude --fee 200 --since 2026-03-11   # what you actually pay
throughput summary --by month                       # tokens, list $, real $, REAL /MTok, LIST:REAL per engine
throughput summary --by month --month 2026-08 --by-family        # one month, per model family
throughput summary --by month --model sonnet,fable  # compare model names side by side
throughput runrate                                  # trailing-30-day and month-to-date, per engine
throughput breakeven --fee 200                      # would a second $200 plan pay for itself?
throughput sessions --order "cost_usd DESC" --limit 10
throughput sql "SELECT * FROM cost_by_month"        # read-only SQL over the views
```

It reads `~/.claude/projects` (Claude Code), `~/.codex/sessions` and `~/.codex/archived_sessions` (Codex) and
`~/.kimi-code` (Kimi; override with `KIMI_CODE_HOME`). The database is `~/.local/share/throughput/throughput.sqlite3`
(override with `--db` or `THROUGHPUT_DB`).

## Read this before trusting a number

- **Costs are API list-price equivalents, not invoices.** A model with no price on file is *unknown*, never free; the
  `unpriced %` column appears when more than 15% of a row's calls are unpriced, and list figures there are lower bounds.
- **Only some prices are verified.** `throughput/rates.json` marks each rate `verified_at` (checked against Claude
  Code's own cost estimates) or leaves it null (carried over from published prices). Check the ones you rely on.
- **Fees are yours to enter.** Nothing is inferred. Give each plan a `--since` (and `--until`): a plan with no start date
  is applied to every period shown, including months before you subscribed.
- **Per-model real cost is an estimate.** The fee is per engine, so model rows show `est.` columns: the fee split in
  proportion to list cost. That assumes quota is consumed in proportion to list price, which no provider guarantees.
- **Cache reads dominate token counts** (about 97% for heavy Claude Code use), which is why REAL /MTok is small. Compare
  LIST:REAL across engines instead.

Store formats, the schema, known gaps and SQL examples are in [docs/reference.md](docs/reference.md).

## Adding an engine

An engine is one module in `throughput/adapters/` exposing `ENGINE`, `default_root()`, `discover(root)` and
`parse(source_file)` (returning `ParsedSession`s made of normalized `UsageEvent`s; see `throughput/types.py`), registered
in `throughput/adapters/__init__.py`. Never estimate tokens: if a store does not record usage, mark the session
`usage_complete = False` with a warning.

## Tests

```bash
python3 -m unittest discover -s tests -v
```

Fixtures are synthetic; no real transcripts are in the repo.

## Status

Single-maintainer and young. The parsers were checked against real Claude Code, Codex and Kimi stores in September 2026;
those formats can change under you. Issues and pull requests welcome.
