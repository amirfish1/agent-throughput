"""Claude Code adapter.

Store: ``~/.claude/projects/<encoded-cwd>/<sessionId>.jsonl`` (top-level session)
and ``<sessionId>/subagents/agent-*.jsonl`` (sub-agent runs, each its own row).

Verified facts this parser depends on:
* Every assistant API response is written on several lines (one per content
  block) that all carry the same ``message.id`` and identical ``usage``; usage is
  therefore counted once per ``message.id``. Resumed/forked sessions can replay
  earlier messages into a new file, so the same id may appear in two files; the
  ingester keeps a single owner per ``message.id``.
* ``usage.input_tokens`` excludes cache buckets; cache creation is
  ``cache_creation_input_tokens`` with a 5m/1h split under ``usage.cache_creation``.
* ``model == "<synthetic>"`` marks client-generated messages with no billed usage.
* A response's ``tool_use`` blocks can sit on any of its lines; their results are
  read by the next response. So a call's ``action`` is the label of the previous
  call's tool uses, and a person's message starts a new turn with no action.
"""

from __future__ import annotations

import glob
import os
from typing import Iterator

from .. import actions
from ..types import ParsedSession, SourceFile, UsageEvent
from .common import as_int, iter_json_lines, norm_ts, ts_min_max

ENGINE = "claude_code"
PROVIDER = "anthropic"


def default_root() -> str:
    return os.path.join(os.path.expanduser("~"), ".claude", "projects")


def discover(root: str) -> Iterator[SourceFile]:
    pats = [
        os.path.join(root, "*", "*.jsonl"),
        os.path.join(root, "*", "*", "subagents", "*.jsonl"),
    ]
    for pat in pats:
        for path in sorted(glob.glob(pat)):
            try:
                st = os.stat(path)
            except OSError:
                continue
            yield SourceFile(ENGINE, path, st.st_size, st.st_mtime_ns)


def _is_user_message(rec: dict) -> bool:
    if rec.get("isMeta") or rec.get("isCompactSummary"):
        return False
    content = (rec.get("message") or {}).get("content")
    if isinstance(content, str):
        return bool(content.strip())
    if isinstance(content, list):
        return any(
            isinstance(b, dict) and b.get("type") in ("text", "image") for b in content
        )
    return False


def _tool_use_labels(content) -> list:
    if not isinstance(content, list):
        return []
    out = []
    for b in content:
        if isinstance(b, dict) and b.get("type") == "tool_use":
            inp = b.get("input") if isinstance(b.get("input"), dict) else {}
            cmd = inp.get("command") if (b.get("name") or "").lower() == "bash" else None
            out.append(actions.tool_label(b.get("name"), cmd if isinstance(cmd, str) else None))
    return out


def parse(sf: SourceFile) -> list:
    path = sf.path
    stem = os.path.basename(path)[: -len(".jsonl")]
    parts = path.split(os.sep)
    is_sub = len(parts) >= 3 and parts[-2] == "subagents"
    parent = parts[-3] if is_sub else None
    sid = f"{parent}:{stem}" if is_sub else stem

    ps = ParsedSession(
        engine=ENGINE,
        source_session_id=sid,
        provider=PROVIDER,
        parent_source_session_id=parent,
        is_subagent=is_sub,
        agent_label=stem if is_sub else None,
        source_path=path,
    )
    bad = []

    def on_error(lineno, reason):
        bad.append((lineno, reason))

    seen_msgs = set()
    seen_tool_uuids = set()
    span = (None, None)
    cwds = set()
    cost_state = None
    synthetic_with_tokens = 0
    turn = 0
    made = {}  # event index -> labels of the tool uses that response made
    made_lines = {}  # event index -> line uuids already counted into ``made``
    by_mid = {}  # message id -> event index
    for lineno, rec in iter_json_lines(path, on_error):
        ts = norm_ts(rec.get("timestamp"))
        span = ts_min_max(span, ts)
        if rec.get("cwd"):
            cwds.add(rec["cwd"])
            if ps.working_directory is None:
                ps.working_directory = rec["cwd"]
        if rec.get("gitBranch") and ps.git_branch is None:
            ps.git_branch = rec["gitBranch"]
        if rec.get("version") and ps.source_format_version is None:
            ps.source_format_version = str(rec["version"])
        rtype = rec.get("type")
        if rtype == "assistant":
            msg = rec.get("message") or {}
            uuid = rec.get("uuid")
            content = msg.get("content")
            if isinstance(content, list) and uuid and uuid not in seen_tool_uuids:
                seen_tool_uuids.add(uuid)
                ps.tool_call_count += sum(
                    1 for b in content if isinstance(b, dict) and b.get("type") == "tool_use"
                )
            model = msg.get("model")
            usage = msg.get("usage")
            mid = msg.get("id")
            labels = _tool_use_labels(content)
            if mid in by_mid and labels and uuid not in made_lines[by_mid[mid]]:
                made[by_mid[mid]].extend(labels)
                made_lines[by_mid[mid]].add(uuid)
            if model == "<synthetic>":
                if usage and any(as_int(usage.get(k)) for k in ("input_tokens", "output_tokens")):
                    synthetic_with_tokens += 1
                continue
            if mid:
                if mid in seen_msgs:
                    continue
                seen_msgs.add(mid)
            ps.assistant_message_count += 1
            if not isinstance(usage, dict):
                ps.usage_complete = False
                continue
            cc = usage.get("cache_creation") or {}
            create = as_int(usage.get("cache_creation_input_tokens"))
            one_h = min(as_int(cc.get("ephemeral_1h_input_tokens")), create)
            details = usage.get("output_tokens_details") or {}
            ps.events.append(
                UsageEvent(
                    event_key=mid or f"{sid}:{rec.get('uuid') or lineno}",
                    ts=ts,
                    model_id=model,
                    input_tokens=as_int(usage.get("input_tokens")),
                    cache_read_tokens=as_int(usage.get("cache_read_input_tokens")),
                    cache_creation_tokens=create,
                    cache_creation_1h_tokens=one_h,
                    output_tokens=as_int(usage.get("output_tokens")),
                    reasoning_tokens=as_int(details.get("thinking_tokens")),
                    turn_index=turn,
                )
            )
            idx = len(ps.events) - 1
            made[idx] = list(labels)
            made_lines[idx] = {uuid}
            if mid:
                by_mid[mid] = idx
        elif rtype == "user":
            if _is_user_message(rec):
                ps.user_message_count += 1
                turn += 1
        elif rtype == "system" and rec.get("subtype") == "compact_boundary":
            ps.compaction_count += 1
        elif rtype == "cost-state":
            cost_state = rec
    for i in range(1, len(ps.events)):
        if ps.events[i].turn_index == ps.events[i - 1].turn_index:
            ps.events[i].action = actions.combine(made.get(i - 1, []))
    ps.started_at, ps.last_activity_at = span
    if cost_state:
        ps.metadata["cost_state"] = {
            k: cost_state.get(k) for k in ("totalCostUSD", "modelUsage", "startTime")
        }
    if len(cwds) > 1:
        ps.metadata["cwd_count"] = len(cwds)
    if synthetic_with_tokens:
        ps.warnings.append(f"{synthetic_with_tokens} synthetic message(s) reported tokens; ignored")
    if bad:
        ps.warnings.append(
            f"{len(bad)} unreadable line(s) skipped; first at line {bad[0][0]}: {bad[0][1]}"
        )
    return [ps]
