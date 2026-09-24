"""Label what a model call was spent on, from the tool calls whose results it read.

The label is heuristic and deliberately coarse. It exists to price one pattern:
an agent that waits for something by sleeping and re-checking (pull) instead of
being told when it is done (push). Each re-check re-sends the whole context.

Only the label is stored; commands and tool inputs never leave the adapter.

* ``wait``   every tool call was a pure sleep/wait
* ``status`` every tool call was a wait or a read-only status check
             (process lists, log tails, git status, CI/PR/queue status)
* ``dispatch`` handed work to another agent (spawn, send, queue a task)
* ``steer``  like ``dispatch``, but interrupted an agent mid-turn to redirect it
* ``work``   anything else (reading code, editing, running tests, ...)

When a call read several kinds: steer > dispatch > work > status > wait.
"""

from __future__ import annotations

import re
from typing import Iterable, Optional

WAIT = "wait"
STATUS = "status"
WORK = "work"
DISPATCH = "dispatch"
STEER = "steer"
POLL_ACTIONS = (WAIT, STATUS)
DISPATCH_ACTIONS = (DISPATCH, STEER)

# Tools whose only job is to wait or to fetch the status of something running.
# Codex: sleep, wait (a running cell), wait_agent. Claude Code: Monitor.
_WAIT_TOOLS = {"sleep", "wait", "wait_agent", "monitor"}
# Reading the output of something already running: Claude Code's BashOutput and
# TaskOutput, Codex's write_stdin (mostly an empty write that polls a live command).
_STATUS_TOOLS = {"bashoutput", "taskoutput", "write_stdin", "list_agents"}
# Handing work to another agent: Codex multi-agent tools, Claude Code's Task/Agent tool.
_DISPATCH_TOOLS = {"spawn_agent", "send_message", "send_input", "task", "agent"}
# ... or through a CLI: CCC (``ccc send|spawn|ask``) and WatchTower (``wt add``).
_DISPATCH_CMD = re.compile(r"(?:^|[\s;&|(/])(?:ccc\s+(?:--?[\w-]+(?:[= ](?!send|spawn|ask)\S+)?\s+)*(send|spawn|ask)|wt\s+add)\b"
                           r"(?![^\n;&|]*\s--help\b)([^\n;&|]*)")

_SLEEP_ONLY = re.compile(r"^\s*(sleep|timeout)\s+[\d.]+[smh]?\s*;?\s*$")
_SLEEP_PREFIX = re.compile(r"^\s*sleep\s+[\d.]+[smh]?\s*(&&|;)\s*")
# A status check reads the state of something else and changes nothing.
_STATUS_CMD = re.compile(
    r"^\s*("
    r"git\s+(status|log|fetch)\b"
    r"|tail\b|ps\b|pgrep\b|pstree\b|uptime\b|jobs\b"
    r"|gh\s+(run|pr|workflow)\s+(view|watch|list|checks|status)\b"
    r"|gh\s+pr\s+checks\b"
    r"|kubectl\s+(get|describe|logs|rollout\s+status)\b"
    r"|docker\s+(ps|logs|inspect)\b"
    r"|systemctl\s+(status|is-active)\b|launchctl\s+(list|print)\b"
    r"|curl\b(?!.*\s-(X|-request)\s*(POST|PUT|PATCH|DELETE))(?!.*\s-(d|-data)\b)"
    r"|wt\s+(ls|list|show|status|queues)\b"
    r"|ccc\s+(ls|list|status|show|tail|log|logs|transcript|sessions|last)\b"
    r")"
)


# Noise in front of the real command: ``VAR=value``, ``cd dir &&``, ``git -C dir``.
_ENV_PREFIX = re.compile(r"^\s*(?:[A-Za-z_][A-Za-z0-9_]*=\S*\s+)+")
_CD_PREFIX = re.compile(r"^\s*cd\s+\S+\s*(?:&&|;)\s*")
_GIT_DIR = re.compile(r"^(\s*git)\s+-C\s+\S+")
# ``tail`` written as an inline Python script: prints the last lines of files and
# does nothing else (no writes, no processes, no network sends).
_PY_INLINE = re.compile(r"^\s*python3?\s+(?:-\s|-c\s|-\s*<<|<<)")
_PY_LOG = re.compile(r"\.jsonl\b|\.log\b")  # reading a transcript or log to see how something is going
_PY_TAIL = re.compile(r"(?:splitlines|readlines)\(\)\s*\[\s*-\s*\d+\s*:\s*\]")
_PY_EFFECTS = re.compile(r"subprocess|os\.system|\.write|write_text|unlink|remove|rmtree|mkdir|rename|"
                         r"urlopen|requests\.|\.post\(|\.put\(|open\([^)]*['\"][wa]")


def _command_label(cmd: str) -> str:
    """``wait``, ``status`` or ``work`` for one shell command line (possibly a pipeline)."""
    if not cmd or not cmd.strip():
        return WORK
    handoffs = list(_DISPATCH_CMD.finditer(cmd))
    if handoffs:
        return STEER if any("--steer" in m.group(2) for m in handoffs) else DISPATCH
    if _SLEEP_ONLY.match(cmd):
        return WAIT
    if _PY_INLINE.match(cmd):
        reads = _PY_TAIL.search(cmd) or _PY_LOG.search(cmd)
        return STATUS if reads and not _PY_EFFECTS.search(cmd) else WORK
    rest = _SLEEP_PREFIX.sub("", cmd, count=1)
    rest = _CD_PREFIX.sub("", _ENV_PREFIX.sub("", rest, count=1), count=1)
    head = re.split(r"\s*(?:\|\||&&|;|\|)\s*", rest.strip(), maxsplit=1)[0]
    head = _GIT_DIR.sub(r"\1", head)
    return STATUS if _STATUS_CMD.match(head) else WORK


def tool_label(name: Optional[str], command: Optional[str] = None) -> str:
    """Label one tool call. ``command`` is the shell command when the tool runs one."""
    n = (name or "").lower()
    if n in _WAIT_TOOLS:
        return WAIT
    if n in _STATUS_TOOLS:
        return STATUS
    if n in _DISPATCH_TOOLS:
        return DISPATCH
    if command is not None:
        return _command_label(command)
    return WORK


def combine(labels: Iterable[str]) -> Optional[str]:
    """One label for the calls read together (steer > dispatch > work > status > wait); none = ``None``."""
    labels = list(labels)
    if not labels:
        return None
    if STEER in labels:
        return STEER
    if DISPATCH in labels:
        return DISPATCH
    if all(l == WAIT for l in labels):
        return WAIT
    if all(l in POLL_ACTIONS for l in labels):
        return STATUS
    return WORK
