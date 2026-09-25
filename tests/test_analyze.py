"""Tests for per-call labels, long-context pricing and ``throughput analyze``.

Fixtures are tiny synthetic transcripts with fake ids; no real conversation content.
"""
import contextlib
import io
import json
import os
import unittest
from unittest import mock

from test_throughput import UsageDbCase, _claude_msg, _claude_user, _sf, _write

from throughput import actions, analyze, cli, pricing, render, schema
from throughput.adapters import claude_code, codex


class ActionLabelTests(unittest.TestCase):
    def test_commands(self):
        cases = {
            "sleep 20": "wait",
            "sleep 5 && tail -n 5 worker.log": "status",
            "cd /repo && git status --short": "status",
            "git -C /repo log --oneline -3": "status",
            "API=http://x ccc status abc": "status",
            "gh run watch 123": "status",
            "curl -s http://127.0.0.1/api/health": "status",
            "curl -s -X POST http://127.0.0.1/api/x": "run",
            "rg -n needle src": "read",
            "cd /repo && python3 -m pytest -q": "test",
            "node tests/app.test.cjs": "test",
            "sed -n 1,40p tests/test_x.py": "read",
            "cat > notes.md <<EOF": "edit",
            "git commit -m x": "git",
            "git -c credential.helper=x push origin main": "git",
            "git -C repo ls-remote origin": "status",
            "ssh host uptime": "ops",
            "sudo -n systemctl restart app": "ops",
            "sudo -n systemctl show app": "status",
            "python3 - <<'PY'\nsubprocess.run(['/opt/bin/ccc', 'spawn', '--name', 'x'])\nPY": "dispatch",
            "python3 - <<'PY'\nsubprocess.run(['git', 'push'])\nPY": "git",
            "python3 - <<'PY'\nfor s in p.read_text().splitlines()[-3:]:\n print(s)\nPY": "status",
            "python3 - <<'PY'\nopen('x', 'w').write('1')\nPY": "edit",
            "python3 - <<'PY'\nimport subprocess\nsubprocess.run(['make'])\nPY": "run",
            "python3 - <<'PY'\nimport subprocess\nsubprocess.run(['wt', 'add', 'Q'])\nPY": "dispatch",
            "python3 - <<'PY'\nimport urllib.request\nurllib.request.urlopen('http://h/api/version')\nPY": "status",
            "python3 - <<'PY'\nurlopen(Request(u, data=b'x', method='POST'))\nPY": "run",
            "python3 - <<'PY'\nimport json\nprint(json.load(open('a.json')))\nPY": "read",
            "": "run",
        }
        for cmd, want in cases.items():
            self.assertEqual(actions.tool_label("shell", cmd), want, cmd)

    def test_handoffs(self):
        cases = {
            "CCC_SERVER=http://h:1 /r/ccc send --json --from abc worker-1 'go'": "dispatch",
            "/r/ccc send --steer --json --from abc worker-1 'stop'": "steer",
            "ccc --server http://h:1 ask s 'q?'": "dispatch",
            "wt add QUEUE 'task'": "dispatch",
            "ccc spawn --help": "run",
            "cat SKILL.md && wt add --help": "read",
            "ccc sessions": "status",
            "python3 - <<'PY'\nfor l in open('/x/worker.jsonl'): print(l)\nPY": "status",
        }
        for cmd, want in cases.items():
            self.assertEqual(actions.tool_label("shell", cmd), want, cmd)
        self.assertEqual(actions.tool_label("Task"), "dispatch")
        self.assertEqual(actions.tool_label("spawn_agent"), "dispatch")
        self.assertEqual(actions.combine(["edit", "dispatch"]), "dispatch")
        self.assertEqual(actions.combine(["dispatch", "steer", "wait"]), "steer")

    def test_tools_and_combine(self):
        self.assertEqual(actions.tool_label("sleep"), "wait")
        self.assertEqual(actions.tool_label("TaskOutput"), "status")
        self.assertEqual(actions.tool_label("Edit"), "edit")
        self.assertEqual(actions.tool_label("Read"), "read")
        self.assertEqual(actions.tool_label("TodoWrite"), "run")
        self.assertIsNone(actions.combine([]))
        self.assertEqual(actions.combine(["wait", "wait"]), "wait")
        self.assertEqual(actions.combine(["wait", "status"]), "status")
        self.assertEqual(actions.combine(["status", "read"]), "read")
        self.assertEqual(actions.combine(["read", "edit", "test"]), "edit")


def _u(inp, cached, out=100):
    return {"input_tokens": inp, "cached_input_tokens": cached, "output_tokens": out, "total_tokens": inp + out}


MODEL = "gpt-6-astra"


def _rollout(codex_root, sid="cx1"):
    """Two turns: sleep -> read it -> rg -> read it; then a second turn with one call."""
    t = lambda n: f"2026-09-01T10:00:{n:02d}.000Z"  # noqa: E731
    tc = lambda n, u, tot: {"timestamp": t(n), "type": "event_msg", "payload": {  # noqa: E731
        "type": "token_count", "info": {"last_token_usage": u, "total_token_usage": tot}}}
    started = lambda n: {"timestamp": t(n), "type": "event_msg", "payload": {"type": "task_started"}}  # noqa: E731
    call = lambda n, item: {"timestamp": t(n), "type": "response_item", "payload": item}  # noqa: E731
    u1, u2, u3, u4 = _u(10_000, 0), _u(10_500, 10_000), _u(12_000, 10_500), _u(40_000, 12_000)
    run = lambda *us: {k: sum(u[k] for u in us) for k in u1}  # noqa: E731
    p = os.path.join(codex_root, "sessions", "2026", "09", "01", f"rollout-2026-09-01T10-00-00-{sid}.jsonl")
    _write(p, [
        {"timestamp": t(0), "type": "session_meta", "payload": {"id": sid, "timestamp": t(0), "cwd": "/w/proj",
                                                                 "model_provider": "openai"}},
        {"timestamp": t(1), "type": "turn_context", "payload": {"model": MODEL}},
        started(2),
        {"timestamp": t(2), "type": "event_msg", "payload": {"type": "user_message"}},
        call(3, {"type": "function_call", "name": "sleep", "arguments": '{"duration_ms": 20000}'}),
        call(4, {"type": "function_call_output"}),
        tc(5, u1, run(u1)),
        call(6, {"type": "custom_tool_call", "name": "exec",
                 "input": 'text(await tools.exec_command({cmd:"rg -n needle src"}));'}),
        call(7, {"type": "custom_tool_call_output"}),
        tc(8, u2, run(u1, u2)),
        tc(9, u3, run(u1, u2, u3)),
        started(10),
        {"timestamp": t(10), "type": "event_msg", "payload": {"type": "user_message"}},
        tc(11, u4, run(u1, u2, u3, u4)),
    ])
    return p


class CodexLabelTests(UsageDbCase):
    def test_actions_are_what_each_call_read_and_turns_count_messages(self):
        [ps] = codex.parse(_sf("codex", _rollout(self.codex)))
        self.assertEqual([e.action for e in ps.events], [None, "wait", "read", None])
        self.assertEqual([e.turn_index for e in ps.events], [0, 0, 0, 1])

    def test_labels_are_stored(self):
        _rollout(self.codex)
        self.run_ingest(engines=["codex"])
        rows = self.conn.execute("SELECT turn_index, action FROM usage_events ORDER BY id").fetchall()
        self.assertEqual([tuple(r) for r in rows], [(0, None), (0, "wait"), (0, "read"), (1, None)])


class ClaudeLabelTests(UsageDbCase):
    def test_tool_use_on_a_later_line_of_the_same_response_counts(self):
        sleep = [{"type": "tool_use", "id": "t1", "name": "Bash", "input": {"command": "sleep 30"}}]
        rg = [{"type": "tool_use", "id": "t2", "name": "Bash", "input": {"command": "rg foo"}}]
        result = {"type": "user", "timestamp": "2026-09-01T10:00:03.000Z", "sessionId": "s1",
                  "message": {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "t1"}]}}
        p = os.path.join(self.claude, "proj", "s1.jsonl")
        _write(p, [
            _claude_user("2026-09-01T10:00:00.000Z"),
            _claude_msg("m1", "claude-sonnet-5", "2026-09-01T10:00:01.000Z", uuid="a"),  # text line
            _claude_msg("m1", "claude-sonnet-5", "2026-09-01T10:00:01.100Z", uuid="b", block=sleep),
            result,
            _claude_msg("m2", "claude-sonnet-5", "2026-09-01T10:00:04.000Z", block=rg),
            dict(result, timestamp="2026-09-01T10:00:05.000Z"),
            _claude_msg("m3", "claude-sonnet-5", "2026-09-01T10:00:06.000Z"),
            _claude_user("2026-09-01T10:00:07.000Z"),
            _claude_msg("m4", "claude-sonnet-5", "2026-09-01T10:00:08.000Z"),
        ])
        [ps] = claude_code.parse(_sf("claude_code", p))
        self.assertEqual([e.action for e in ps.events], [None, "wait", "read", None])
        self.assertEqual([e.turn_index for e in ps.events], [1, 1, 1, 2])


class LongContextPricingTests(UsageDbCase):
    def test_premium_doubles_input_side_only_above_threshold(self):
        _rollout(self.codex)
        self.run_ingest(engines=["codex"])
        self.add_rate("gpt-6-astra", 10.0, 1.0, None, 50.0)
        self.conn.execute("UPDATE price_rates SET long_context_threshold=30000, long_context_multiplier=2.0")
        costs = [r["cost_usd"] for r in self.conn.execute("SELECT cost_usd FROM event_costs ORDER BY event_id")]
        # c4: 28,000 fresh + 12,000 cached = 40,000 > 30,000 -> input side x2; output unchanged
        self.assertAlmostEqual(costs[3], (2 * (28_000 * 10 + 12_000 * 1) + 100 * 50) / 1e6)
        # c3: 12,000 <= threshold -> list rates
        self.assertAlmostEqual(costs[2], (1_500 * 10 + 10_500 * 1 + 100 * 50) / 1e6)

    def test_packaged_rates_carry_the_premium(self):
        pricing.load_rates(self.conn)
        row = self.one("SELECT long_context_threshold t, long_context_multiplier m FROM price_rates "
                       "WHERE pricing_key='gpt-6-astra'")
        self.assertEqual((row["t"], row["m"]), (272000, 2.0))


class MigrationTests(unittest.TestCase):
    def test_v1_db_gains_columns_forgets_files_and_gets_premiums(self):
        import tempfile
        d = tempfile.mkdtemp(prefix="usage-db-mig-")
        conn = schema.connect(os.path.join(d, "v1.sqlite3"))
        v1 = schema._TABLES
        for line in ("    turn_index               INTEGER,          -- which incoming message this call answers (NULL = unknown)\n",
                     "    action                   TEXT,             -- wait | status | work | NULL: what the call read (see actions.py)\n",
                     "    long_context_threshold  INTEGER,\n", "    long_context_multiplier REAL,\n"):
            self.assertIn(line, v1)
            v1 = v1.replace(line, "")
        conn.executescript(v1)
        conn.execute("PRAGMA user_version = 1")
        conn.execute("INSERT INTO ingest_files VALUES ('/x', 'codex', 1, 1, 1, 0, 'ok', NULL, 'now')")
        conn.execute("INSERT INTO price_rates (pricing_key, effective_from, input_rate) VALUES ('gpt-6-astra', '1970-01-01', 10)")
        conn.commit()
        self.assertEqual(schema.migrate(conn), 1)  # (v2 -> v3 also forgets the files: labels changed)
        self.assertEqual(pricing.backfill_long_context(conn), 1)
        cols = {r[1] for r in conn.execute("PRAGMA table_info(usage_events)")}
        self.assertTrue({"turn_index", "action"} <= cols)
        self.assertEqual(conn.execute("SELECT COUNT(*) FROM ingest_files").fetchone()[0], 0)
        self.assertEqual(conn.execute("SELECT long_context_threshold FROM price_rates").fetchone()[0], 272000)
        self.assertEqual(conn.execute("PRAGMA user_version").fetchone()[0], schema.SCHEMA_VERSION)
        self.assertEqual(schema.migrate(conn), schema.SCHEMA_VERSION)  # second run is a no-op
        conn.close()


class AnalyzeTests(UsageDbCase):
    def setUp(self):
        super().setUp()
        _rollout(self.codex)
        self.run_ingest(engines=["codex"])
        self.add_rate("gpt-6-astra", 10.0, 1.0, None, 50.0)

    def test_cost_matches_the_views_and_ratio_converts_to_real(self):
        r = analyze.analyze(self.conn, "cx", list_to_real=20)
        listed = self.one("SELECT cost_usd FROM session_costs")["cost_usd"]
        self.assertAlmostEqual(r["list_usd"], round(listed, 2))
        self.assertAlmostEqual(r["real_usd"], round(listed / 20, 2))
        self.assertEqual((r["calls"], r["turns"]), (4, 2))

    def test_push_fix_saves_exactly_the_polling_calls(self):
        ev = analyze._events(self.conn, self.one("SELECT id FROM sessions")["id"])
        full = analyze.simulate(ev)
        poll_call = (500 * 10 + 10_000 * 1 + 100 * 50) / 1e6  # the call that read the sleep
        self.assertAlmostEqual(full - analyze.simulate(ev, drop_poll=True), poll_call)

    def test_fresh_session_with_a_brief_as_large_as_any_prompt_changes_nothing_but_the_cache(self):
        ev = analyze._events(self.conn, self.one("SELECT id FROM sessions")["id"])
        # Turn 2's first call carried 12,000 cached tokens in; a fresh session pays them uncached.
        extra = 12_000 * (10.0 - 1.0) / 1e6
        self.assertAlmostEqual(analyze.simulate(ev, fresh_per_turn=True, brief_tokens=10**9),
                               analyze.simulate(ev) + extra)

    def test_score_and_gains_add_up_to_100(self):
        r = analyze.analyze(self.conn, "cx", list_to_real=20)
        self.assertTrue(0 <= r["score"] <= 100)
        gains = sum(f["score_gain"] for f in r["findings"])
        self.assertLessEqual(r["score"] + gains, 100)
        self.assertIn("push_not_pull", [f["key"] for f in r["findings"] + r["minor_findings"]])

    def test_delegating_never_costs_more_and_needs_a_stretch(self):
        ev = analyze._events(self.conn, self.one("SELECT id FROM sessions")["id"])
        full = analyze.simulate(ev)
        self.assertLessEqual(analyze.simulate(ev, delegate_work=True), full)
        self.assertLessEqual(analyze.simulate(ev, fresh_per_turn=True, delegate_work=True),
                             analyze.simulate(ev, fresh_per_turn=True))
        # the fixture has one work call in a row: too short to brief an agent for
        self.assertAlmostEqual(analyze.simulate(ev, delegate_work=True), full)

    def test_activities_slice_the_whole_cost(self):
        r = analyze.analyze(self.conn, "cx", list_to_real=20)
        self.assertAlmostEqual(sum(a["list_usd"] for a in r["activities"]), r["list_usd"], places=1)
        names = {a["activity"] for a in r["activities"]}
        self.assertEqual(names, {"reading messages and replying", "waiting (sleep)", "reading and searching code"})

    def test_no_supervision_section_without_handoffs(self):
        r = analyze.analyze(self.conn, "cx", list_to_real=20)
        self.assertIsNone(r["supervision"])
        self.assertNotIn("delegate_checking", [f["key"] for f in r["findings"] + r["minor_findings"]])

    def test_cadence_spots_a_timer_not_a_person(self):
        hourly = [f"2026-09-01T{h:02d}:0{h % 3}:00Z" for h in range(8)]
        self.assertAlmostEqual(analyze.cadence(hourly), 3600, delta=180)
        typed = ["2026-09-01T10:00:00Z", "2026-09-01T10:02:00Z", "2026-09-01T11:30:00Z",
                 "2026-09-01T11:31:00Z", "2026-09-01T15:00:00Z", "2026-09-01T15:09:00Z"]
        self.assertIsNone(analyze.cadence(typed))
        self.assertIsNone(analyze.cadence(hourly[:3]))

    def test_no_plan_means_no_real_dollars(self):
        r = analyze.analyze(self.conn, "cx")
        self.assertIsNone(r["real_usd"])
        self.assertIn("no codex plan", r["ratio_source"])

    def test_unknown_or_ambiguous_session(self):
        with self.assertRaises(analyze.SessionNotFound):
            analyze.analyze(self.conn, "nope")

    def test_full_id_is_not_ambiguous_with_its_subagents(self):
        self.conn.execute("INSERT INTO sessions (engine, source_session_id, provider, ingested_at) "
                          "SELECT engine, 'cx1:agent-a1', provider, ingested_at FROM sessions")
        self.assertEqual(analyze.find_session(self.conn, "cx1")["source_session_id"], "cx1")
        with self.assertRaises(analyze.SessionNotFound):
            analyze.find_session(self.conn, "cx")


class OneCommandTests(UsageDbCase):
    """``throughput analyze`` from nothing: it reads the logs in, and picks the session by itself."""

    def setUp(self):
        super().setUp()
        self.home = os.path.join(self.tmp, "home")
        _rollout(os.path.join(self.home, ".codex"))
        self.db = os.path.join(self.tmp, "fresh", "t.sqlite3")
        env = {"HOME": self.home, "KIMI_CODE_HOME": os.path.join(self.home, "none"), "NO_COLOR": "1"}
        patcher = mock.patch.dict(os.environ, env)
        patcher.start()
        self.addCleanup(patcher.stop)
        os.environ.pop("CLAUDE_CODE_SESSION_ID", None)

    def run_cli(self, *argv):
        out = io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(io.StringIO()):
            code = cli.main(["--db", self.db, *argv])
        return code, out.getvalue()

    def test_first_run_ingests_and_defaults_to_the_latest_session(self):
        code, out = self.run_cli("analyze", "--json")
        self.assertEqual(code, 0)
        r = json.loads(out)
        self.assertEqual(r["session"]["source_session_id"], "cx1")
        self.assertIsNotNone(r["score"])  # the packaged rates price gpt-6-astra

    def test_current_claude_session_wins_and_short_names_each_fix(self):
        _rollout(os.path.join(self.home, ".codex"), sid="cx2")
        os.environ["CLAUDE_CODE_SESSION_ID"] = "cx1"
        code, out = self.run_cli("analyze", "--short")
        self.assertEqual(code, 0)
        self.assertTrue(out.startswith("cx1"), out)
        self.assertRegex(out, r"score \d+/100")

    def test_short_spells_out_share_and_dollars(self):
        r = {"session": {"source_session_id": "abcdef123", "engine": "claude_code", "model_label": "Opus 5",
                         "model_id": None, "project_name": "p", "duration_seconds": 3600},
             "list_usd": 118.0, "real_usd": 5.9, "score": 38, "unpriced_calls": 0,
             "findings": [{"title": "Switch from pull to push while waiting", "share_pct": 30.2,
                           "list_usd": 35.0, "real_usd": 1.75}]}
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            cli._print_short(r, render.Style(False), 100)
        self.assertEqual(out.getvalue().splitlines()[1:], [
            "$118 list ≈ $5.90 real · score 38/100",
            "1. Switch from pull to push while waiting: 30% of spend ($35 list ≈ $1.75 real)"])

    def test_no_ingest_needs_a_database(self):
        with self.assertRaises(SystemExit):
            self.run_cli("analyze", "--no-ingest")

    def test_install_skill_is_idempotent_and_will_not_clobber_edits(self):
        skills = os.path.join(self.tmp, "skills")
        self.assertEqual(self.run_cli("install-skill", "--dir", skills)[0], 0)
        path = os.path.join(skills, "session-waste", "SKILL.md")
        with open(path, encoding="utf-8") as fh:
            self.assertIn("name: session-waste", fh.read())
        self.assertIn("already installed", self.run_cli("install-skill", "--dir", skills)[1])
        with open(path, "a", encoding="utf-8") as fh:
            fh.write("local edit\n")
        with self.assertRaises(SystemExit):
            self.run_cli("install-skill", "--dir", skills)
        self.assertEqual(self.run_cli("install-skill", "--dir", skills, "--force")[0], 0)


class UnpricedTests(UsageDbCase):
    def test_nothing_priced_means_no_score_not_a_perfect_one(self):
        _rollout(self.codex)
        self.run_ingest(engines=["codex"])  # no rate rows at all
        r = analyze.analyze(self.conn, "cx")
        self.assertIsNone(r["score"])
        self.assertEqual((r["findings"], r["minor_findings"]), ([], []))
        self.assertEqual(r["unpriced_calls"], 4)


class RateTopUpTests(UsageDbCase):
    def test_new_packaged_models_are_added_and_edited_rates_kept(self):
        pricing.ensure_rates(self.conn)
        n = self.one("SELECT COUNT(*) n FROM price_rates")["n"]
        self.conn.execute("UPDATE price_rates SET input_rate = 99 WHERE pricing_key = 'claude-sonnet-5'")
        self.conn.execute("DELETE FROM price_rates WHERE pricing_key = 'claude-opus-5-5'")
        self.conn.commit()
        pricing.ensure_rates(self.conn)
        self.assertEqual(self.one("SELECT COUNT(*) n FROM price_rates")["n"], n)
        self.assertEqual(self.one("SELECT input_rate FROM price_rates WHERE pricing_key = 'claude-sonnet-5'")[0], 99)
        self.assertEqual(self.one("SELECT output_rate FROM price_rates WHERE pricing_key = 'claude-opus-5-5'")[0], 20.0)


class IngestSinceTests(UsageDbCase):
    def test_older_files_are_left_alone(self):
        path = _rollout(self.codex)
        os.utime(path, (1_000_000_000, 1_000_000_000))  # 2001
        rep = self.run_ingest(engines=["codex"], since_ns=int(1.7e18))
        self.assertEqual(rep.older_files["codex"], 1)
        self.assertEqual(rep.missing_source_files["codex"], 0)
        self.assertEqual(self.one("SELECT COUNT(*) n FROM sessions")["n"], 0)
        self.run_ingest(engines=["codex"])
        self.assertEqual(self.one("SELECT COUNT(*) n FROM sessions")["n"], 1)


class RenderTests(unittest.TestCase):
    def test_bars_and_plain_style(self):
        self.assertEqual(render.bar(0.5, 4), "██  ")
        self.assertEqual(render.bar(1 / 32, 4), "▏   ")
        self.assertEqual(render.bar(2.0, 3), "███")
        self.assertEqual(render.Style(False)("x", "bold"), "x")
        self.assertEqual(render.Style(True)("x", "bold", "red"), "\033[1;31mx\033[0m")

    def test_no_color_env_wins(self):
        from unittest import mock
        with mock.patch.dict(os.environ, {"NO_COLOR": "1", "FORCE_COLOR": "1"}):
            self.assertFalse(render.color_enabled())


if __name__ == "__main__":
    unittest.main()
