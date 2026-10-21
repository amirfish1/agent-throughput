---
name: session-waste
description: Use when the user asks where a Claude Code / Codex / Kimi session's tokens or money went, why a session was expensive, how wasteful it was, or types /session-waste (optionally with a session id). Runs the local `throughput analyze` tool and reports each fix as a share of spend and in real dollars.
---

# /session-waste: where did this session's tokens go?

`throughput analyze` (from github.com/amirfish1/agent-throughput) prices every model call in a session from the
logs on this machine, slices the cost by what the agent was doing, and simulates fixes on the session's own calls.
Everything runs locally; no transcript text leaves the machine or goes into its database.

## 1. Find the command

Use the first that works:

1. `throughput` on PATH, if `throughput analyze --help` supports a session argument and `--json`.
2. `uvx --from git+https://github.com/amirfish1/agent-throughput throughput`
3. `pipx run --spec git+https://github.com/amirfish1/agent-throughput throughput`

If none works, tell the user to run `pipx install git+https://github.com/amirfish1/agent-throughput` and stop.

## 2. Run it

Run `<cmd> ingest` first to read new and changed session logs into the local database. The first run takes
a minute or two; tell the user before starting. Later runs take seconds. `analyze` does not ingest automatically.

- With a session id or unique prefix: run `<cmd> analyze <id> --json`.
- For the current Claude Code session, pass `$CLAUDE_CODE_SESSION_ID` explicitly when it is available.
- Otherwise list candidates with `<cmd> sessions --order "started_at DESC" --limit 10 --json`.
  For "the expensive one yesterday", use `--order "cost_usd DESC" --since YYYY-MM-DD --until YYYY-MM-DD`
  with the requested date range. Choose the matching session or ask if ambiguous.
- Always pass a session id; the CLI requires it. If a prefix is ambiguous (including a parent id shared by
  sub-agents), inspect the candidates and use a unique id/prefix rather than silently picking another session.

## 3. Report it

Lead with the money, then one line per fix, in this shape:

```
This session cost $118 at list price ≈ $5.88 of your subscription. Score 38/100
(38 = what it would have cost with every fix below, as a share of what it did cost).

1. Start each turn from a brief, not the whole history: 46% of spend ($54 list ≈ $2.70 real).
   <one sentence from `evidence`, in plain words>. Fix: <`fix`, shortened>.
2. Switch from pull to push while waiting: 30% of spend ($35 list ≈ $1.76 real).
   ...
```

- Use the JSON fields: `list_usd`, `real_usd`, `score`, and for each item in `findings`: `title`, `share_pct`
  (the "% of spend"), `list_usd`, `real_usd`, `evidence`, `fix`.
- Findings overlap, so their percentages do not add up to 100 - (score). Don't add them up.
- Add the top two or three rows of `activities` ("what the money went to") in one line if they help explain a fix.
- `minor_findings` (each under 5%) get one line at most, or nothing.
- If `real_usd` is null, say so and give the one command that fixes it:
  `<cmd> plans add --name claude --engine claude --fee <monthly fee> --since <YYYY-MM-DD>` (ask the user for
  their fee and start date; never guess them). `--list-to-real X` is the alternative when a subscription is shared
  with another machine this database does not see.
- If `unpriced_calls` > 0, say the dollars are lower bounds. If all calls are unpriced, report the model
  (`session.model_id`) and unknown cost; do not present the CLI's zero cost or 100/100 score as measured efficiency.

Then stop. Do not change the user's setup, plans or workflow on the strength of this report unless they ask.
