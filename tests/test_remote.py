"""Tests for merging another machine's usage DB (throughput.remote.merge)."""
import os
import shutil
import tempfile
import unittest

from throughput import ingest, remote, schema


def _db(path):
    conn = schema.connect(path)
    schema.migrate(conn)
    return conn


def _session(conn, sid, events, machine="local", engine="claude_code", parent=None):
    cur = conn.execute(
        "INSERT INTO sessions (engine, source_session_id, parent_source_session_id, ingested_at, "
        "usage_event_count, total_tokens, last_activity_at, machine) VALUES (?,?,?,?,?,?,?,?)",
        (engine, sid, parent, "2026-09-01T00:00:00Z", events, events * 100, f"2026-09-01T00:{events:02d}:00Z",
         machine),
    )
    for i in range(events):
        conn.execute(
            "INSERT INTO usage_events (session_id, engine, event_key, ts, model_id, output_tokens) "
            "VALUES (?,?,?,?,?,?)",
            (cur.lastrowid, engine, f"{sid}-e{i}", "2026-09-01T00:00:00Z", "claude-sonnet-5", 100),
        )
    conn.commit()
    return cur.lastrowid


def _row(conn, sid):
    return conn.execute(
        "SELECT machine, usage_event_count, (SELECT COUNT(*) FROM usage_events e WHERE e.session_id = s.id) AS n "
        "FROM sessions s WHERE source_session_id=?", (sid,)).fetchone()


class MergeTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.dst = _db(os.path.join(self.tmp, "dst.sqlite3"))
        self.src_path = os.path.join(self.tmp, "src.sqlite3")
        self.src = _db(self.src_path)

    def tearDown(self):
        self.dst.close()
        self.src.close()
        shutil.rmtree(self.tmp)

    def test_inserts_tagged_and_is_idempotent(self):
        _session(self.src, "a", 2)
        _session(self.src, "b", 1)
        st = remote.merge(self.dst, self.src_path, "vm1")
        self.assertEqual((st["inserted"], st["events_added"]), (2, 3))
        self.assertEqual(tuple(_row(self.dst, "a")), ("vm1", 2, 2))
        st = remote.merge(self.dst, self.src_path, "vm1")
        self.assertEqual((st["inserted"], st["unchanged"], st["events_added"]), (0, 2, 0))

    def test_refresh_when_host_session_grew(self):
        _session(self.src, "a", 2)
        remote.merge(self.dst, self.src_path, "vm1")
        self.src.execute("DELETE FROM sessions")
        self.src.commit()
        _session(self.src, "a", 3)
        st = remote.merge(self.dst, self.src_path, "vm1")
        self.assertEqual(st["refreshed"], 1)
        self.assertEqual(tuple(_row(self.dst, "a")), ("vm1", 3, 3))

    def test_copy_on_two_machines_counted_once(self):
        _session(self.dst, "c", 3, machine="vm1")
        _session(self.src, "c", 3)  # same session copied to vm2: tie keeps what we hold
        st = remote.merge(self.dst, self.src_path, "vm2")
        self.assertEqual(st["kept_other"], 1)
        self.assertEqual(tuple(_row(self.dst, "c")), ("vm1", 3, 3))
        self.src.execute("DELETE FROM sessions")
        self.src.commit()
        _session(self.src, "c", 5)  # vm2's copy has more: it wins
        st = remote.merge(self.dst, self.src_path, "vm2")
        self.assertEqual(st["refreshed"], 1)
        self.assertEqual(tuple(_row(self.dst, "c")), ("vm2", 5, 5))
        self.assertEqual(self.dst.execute("SELECT COUNT(*) FROM sessions").fetchone()[0], 1)

    def test_third_machine_name_is_preserved(self):
        _session(self.src, "d", 1, machine="vm3")
        remote.merge(self.dst, self.src_path, "vm1")
        self.assertEqual(_row(self.dst, "d")["machine"], "vm3")

    def test_parents_relinked(self):
        _session(self.src, "p", 1)
        _session(self.src, "k", 1, parent="p")
        remote.merge(self.dst, self.src_path, "vm1")
        linked = self.dst.execute(
            "SELECT p.source_session_id FROM sessions k JOIN sessions p ON p.id = k.parent_session_id "
            "WHERE k.source_session_id='k'").fetchone()
        self.assertEqual(linked[0], "p")

    def test_rejects_local_name(self):
        with self.assertRaises(ValueError):
            remote.merge(self.dst, self.src_path, "local")

    def test_full_rebuild_keeps_other_machines(self):
        _session(self.dst, "mine", 1)
        _session(self.dst, "theirs", 1, machine="vm1")
        empty = os.path.join(self.tmp, "empty")
        os.makedirs(empty)
        ingest.ingest(self.dst, {"claude_code": empty}, ["claude_code"], full_rebuild=True)
        left = [r[0] for r in self.dst.execute("SELECT source_session_id FROM sessions")]
        self.assertEqual(left, ["theirs"])


class RemoteScriptTest(unittest.TestCase):
    def test_script_reads_named_users_stores(self):
        sh = remote._remote_script(["hermes"], "2026-09-01", ["claude_code"])
        self.assertIn('--claude-root "$H/.claude/projects"', sh)
        self.assertIn("--since 2026-09-01", sh)
        self.assertIn("sudo -n", sh)

    def test_package_tar_has_cli(self):
        import io
        import tarfile
        names = tarfile.open(fileobj=io.BytesIO(remote._package_tar())).getnames()
        self.assertIn("throughput/cli.py", names)
        self.assertFalse(any("__pycache__" in n for n in names))


if __name__ == "__main__":
    unittest.main()
