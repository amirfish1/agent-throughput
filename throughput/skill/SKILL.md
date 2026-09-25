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

1. `throughput` on PATH, if `throughput analyze --help` mentions `--short` (an older install or a different tool
   with the same name will not).
2. `uvx --from git+https://github.com/amirfish1/agent-throughput throughput`
3. `pipx run --spec git+https://github.com/amirfish1/agent-throughput throughput`

If none works, tell the user to run `pipx install git+https://github.com/amirfish1/agent-throughput` and stop.

## 2. Run it

- No argument: analyze the session you are running in. Run `<cmd> analyze --json` with no id; it reads
  `$CLAUDE_CODE_SESSION_ID`.
- An argument (a session id or a unique prefix of one): `<cmd> analyze <id> --json`.
- "my last session", "the expensive one yesterday": first list candidates with
  `<cmd> sessions --order "cost_usd DESC" --limit 10 --since YYYY-MM-DD` and pick, or ask.

It reads new session logs into its database before analyzing. The first run takes about a minute; tell the user
before you start it. Later runs take seconds.

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
- If `score` is null, the model has no price on file: say which model (`session.model_id`) and that the dollars
  are unknown, not zero.
- If `unpriced_calls` > 0, say the dollars are lower bounds.

Then stop. Do not change the user's setup, plans or workflow on the strength of this report unless they ask.
