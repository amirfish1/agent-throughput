"""Bring usage from other machines into the local DB.

``merge`` folds another throughput DB into this one, tagging its sessions with a
machine name. ``pull`` produces that other DB over ssh: it ships this package to
the host (stdlib only, so no install step), runs ``ingest`` there against the
host's own session stores, snapshots the result and merges it.

A session id seen on two machines (a store copied between hosts) is kept once:
the copy with more usage events wins, and a tie keeps the row already held.
"""

from __future__ import annotations

import io
import os
import shlex
import subprocess
import tarfile
from collections import Counter
from typing import Callable, Iterable, Optional

from . import ingest as ingest_mod
from . import schema

# Where pull keeps its files on the host, relative to the ssh user's home.
REMOTE_DIR = ".local/share/throughput"

_SKIP_SESSION_COLS = {"id", "parent_session_id", "machine"}
_SKIP_EVENT_COLS = {"id", "session_id"}


def _cols(conn, db: str, table: str) -> list:
    return [r[1] for r in conn.execute(f"PRAGMA {db}.table_info({table})")]


def merge(conn, src_path: str, machine: str, log: Callable[[str], None] = lambda _m: None) -> dict:
    """Merge the throughput DB at ``src_path`` into ``conn``; its own sessions become ``machine``.

    Rows the source DB had itself merged from a third machine keep that machine's name.
    Returns counts: inserted, refreshed, unchanged, kept_other (a copy already held from
    another machine had at least as many events), events_added, events_duplicate.
    """
    if not machine or machine == "local":
        raise ValueError("merge needs a machine name other than 'local'")
    if not os.path.exists(src_path):
        raise FileNotFoundError(src_path)
    conn.commit()
    conn.execute("ATTACH DATABASE ? AS src", (src_path,))
    try:
        have = conn.execute("PRAGMA src.user_version").fetchone()[0]
        if have > schema.SCHEMA_VERSION:
            raise RuntimeError(f"{src_path} is schema v{have}, newer than this code (v{schema.SCHEMA_VERSION})")
        src_s = set(_cols(conn, "src", "sessions"))
        src_e = set(_cols(conn, "src", "usage_events"))
        scols = [c for c in _cols(conn, "main", "sessions") if c in src_s and c not in _SKIP_SESSION_COLS]
        ecols = [c for c in _cols(conn, "main", "usage_events") if c in src_e and c not in _SKIP_EVENT_COLS]
        src_machine = "machine" in src_s
        sel = ", ".join(f"s.{c}" for c in scols)
        rows = conn.execute(
            f"SELECT s.id AS src_id, {sel}" + (", s.machine AS src_machine" if src_machine else "")
            + " FROM src.sessions s"
        ).fetchall()
        ins_s = f"INSERT INTO main.sessions ({', '.join(scols)}, machine) VALUES ({', '.join('?' * len(scols))}, ?)"
        ecol_list = ", ".join(ecols)
        ins_e = (f"INSERT OR IGNORE INTO main.usage_events (session_id, {ecol_list}) "
                 f"SELECT ?, {ecol_list} FROM src.usage_events WHERE session_id=?")
        stats = Counter()
        for i, r in enumerate(rows, 1):
            m = machine
            if src_machine and r["src_machine"] and r["src_machine"] != "local":
                m = r["src_machine"]
            held = conn.execute(
                "SELECT id, machine, usage_event_count, total_tokens, last_activity_at FROM main.sessions "
                "WHERE engine=? AND source_session_id=?", (r["engine"], r["source_session_id"]),
            ).fetchone()
            if held is not None:
                if held["machine"] == m:
                    if (held["usage_event_count"], held["total_tokens"], held["last_activity_at"]) == (
                            r["usage_event_count"], r["total_tokens"], r["last_activity_at"]):
                        stats["unchanged"] += 1
                        continue
                    if held["usage_event_count"] > r["usage_event_count"]:
                        # The host lost events we already have (e.g. a pruned file); keep ours.
                        stats["unchanged"] += 1
                        continue
                    kind = "refreshed"
                elif r["usage_event_count"] > held["usage_event_count"]:
                    kind = "refreshed"
                else:
                    stats["kept_other"] += 1
                    continue
                conn.execute("DELETE FROM main.sessions WHERE id=?", (held["id"],))  # events cascade
            else:
                kind = "inserted"
            cur = conn.execute(ins_s, (*[r[c] for c in scols], m))
            new_id = cur.lastrowid
            before = conn.total_changes
            conn.execute(ins_e, (new_id, r["src_id"]))
            added = conn.total_changes - before
            want = conn.execute("SELECT COUNT(*) FROM src.usage_events WHERE session_id=?", (r["src_id"],)).fetchone()[0]
            stats["events_added"] += added
            stats["events_duplicate"] += want - added
            stats[kind] += 1
            if i % 2000 == 0:
                conn.commit()
                log(f"  merged {i:,}/{len(rows):,} sessions")
        ingest_mod._link_parents(conn)
        conn.commit()
    finally:
        conn.execute("DETACH DATABASE src")
    for k in ("inserted", "refreshed", "unchanged", "kept_other", "events_added", "events_duplicate"):
        stats.setdefault(k, 0)
    return dict(stats)


def _package_tar() -> bytes:
    """This package as a tar.gz (no bytecode), to run on the host with ``python3 -m throughput``."""
    pkg = os.path.dirname(os.path.abspath(__file__))
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        tar.add(pkg, arcname="throughput",
                filter=lambda ti: None if "__pycache__" in ti.name or ti.name.endswith(".pyc") else ti)
    return buf.getvalue()


def _remote_script(users: Iterable[Optional[str]], since: Optional[str], engines: Optional[list]) -> str:
    """Bash run on the host: unpack the shipped package, ingest each user's stores, snapshot the DB."""
    flags = ""
    if since:
        flags += f" --since {shlex.quote(since)}"
    for e in engines or []:
        flags += f" --engine {shlex.quote(e)}"
    lines = [
        "set -euo pipefail",
        f'D="$HOME/{REMOTE_DIR}"',
        'mkdir -p "$D"',
        'rm -rf "$D/src.new" && mkdir -p "$D/src.new" && tar xzf "$D/pkg.tgz" -C "$D/src.new"',
        'rm -rf "$D/src" && mv "$D/src.new" "$D/src"',
        'command -v python3 >/dev/null || { echo "python3 not found on host" >&2; exit 3; }',
        'NEED_SUDO=0',
    ]
    run = 'PYTHONPATH="$D/src" python3 -m throughput --db "$D/remote.sqlite3"'
    for u in users:
        if u:
            q = shlex.quote(u)
            lines += [
                f'H=$(getent passwd {q} | cut -d: -f6); [ -n "$H" ] || {{ echo "no such user: "{q} >&2; exit 4; }}',
                f'if [ "$(whoami)" = {q} ] || [ "$(id -u)" = 0 ]; then S=""; else S="sudo -n"; NEED_SUDO=1; fi',
                f'echo "== ingest as "{q}" ($H)" >&2',
                f'$S env PYTHONPATH="$D/src" python3 -m throughput --db "$D/remote.sqlite3" ingest'
                f' --claude-root "$H/.claude/projects" --codex-root "$H/.codex" --kimi-root "$H/.kimi-code"{flags} >&2',
            ]
        else:
            lines += ['echo "== ingest as $(whoami)" >&2', f"{run} ingest{flags} >&2"]
    lines += [
        'if [ "$NEED_SUDO" = 1 ]; then S="sudo -n"; else S=""; fi',
        # A consistent single-file copy (the live DB runs in WAL mode).
        '$S python3 -c "import sqlite3,sys; s=sqlite3.connect(sys.argv[1]); d=sqlite3.connect(sys.argv[2]); '
        's.backup(d); d.close(); s.close()" "$D/remote.sqlite3" "$D/snapshot.sqlite3"',
        'if [ "$NEED_SUDO" = 1 ]; then $S chown "$(id -u):$(id -g)" "$D/snapshot.sqlite3"; fi',
        'echo "$D/snapshot.sqlite3"',
    ]
    return "\n".join(lines) + "\n"


def pull(conn, host: str, users: Optional[list] = None, since: Optional[str] = None,
         engines: Optional[list] = None, machine: Optional[str] = None, snapshot_dir: Optional[str] = None,
         log: Callable[[str], None] = lambda _m: None) -> dict:
    """Ingest on ``host`` over ssh and merge the result as machine ``machine`` (default: the host name).

    ``users``: read these accounts' stores (``sudo -n`` when the ssh user differs and is not root);
    default is the ssh user's own. The snapshot is kept locally under ``snapshot_dir``.
    """
    machine = machine or host
    ssh = ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=15", host]
    log(f"shipping throughput to {host}:~/{REMOTE_DIR}/src")
    subprocess.run(ssh + [f'mkdir -p "$HOME/{REMOTE_DIR}" && cat > "$HOME/{REMOTE_DIR}/pkg.tgz"'],
                   input=_package_tar(), check=True)
    log(f"ingesting on {host} (the first run reads every file and can take minutes)")
    res = subprocess.run(ssh + ["bash -s"], input=_remote_script(users or [None], since, engines).encode(),
                         stdout=subprocess.PIPE, check=False)
    if res.returncode != 0:
        raise RuntimeError(f"remote ingest on {host} failed (exit {res.returncode})")
    remote_snap = res.stdout.decode().strip().splitlines()[-1]
    os.makedirs(snapshot_dir, exist_ok=True)
    local_snap = os.path.join(snapshot_dir, f"{machine}.sqlite3")
    log(f"copying {host}:{remote_snap} -> {local_snap}")
    subprocess.run(["scp", "-q", "-o", "BatchMode=yes", f"{host}:{remote_snap}", local_snap + ".part"], check=True)
    os.replace(local_snap + ".part", local_snap)
    log(f"merging as machine '{machine}'")
    stats = merge(conn, local_snap, machine, log)
    stats["snapshot"] = local_snap
    return stats
