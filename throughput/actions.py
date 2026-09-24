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
* ``edit``   changed files (patches, Edit/Write tools, ``sed -i``, ``cat >``)
* ``test``   ran tests, type checks, linters or a syntax check
* ``read``   read or searched code, docs or the web (``rg``, ``cat``, ``git diff``, Read/Grep)
* ``git``    committed, pushed, merged or otherwise changed a repository
* ``ops``    worked on servers and services (``ssh``, ``sudo``, ``systemctl``, deploy CLIs)
* ``run``    anything else (scripts, builds, installs, other tools)

``edit``/``test``/``git``/``ops``/``run``/``read`` are the agent's own work
(``WORK_ACTIONS``); schema v2-v3 stored them all as ``work``. When a call read
several kinds: steer > dispatch > edit > test > git > ops > run > read > status > wait.
"""

from __future__ import annotations

import re
from typing import Iterable, Optional

WAIT = "wait"
STATUS = "status"
WORK = "work"  # v2-v3 label for all own work; still accepted
DISPATCH = "dispatch"
STEER = "steer"
READ = "read"
EDIT = "edit"
TEST = "test"
RUN = "run"
GIT = "git"
OPS = "ops"
POLL_ACTIONS = (WAIT, STATUS)
DISPATCH_ACTIONS = (DISPATCH, STEER)
WORK_ACTIONS = (EDIT, TEST, GIT, OPS, RUN, READ, WORK)
_RANK = (STEER, DISPATCH, EDIT, TEST, GIT, OPS, RUN, WORK, READ, STATUS, WAIT)

# Tools whose only job is to wait or to fetch the status of something running.
# Codex: sleep, wait (a running cell), wait_agent. Claude Code: Monitor.
_WAIT_TOOLS = {"sleep", "wait", "wait_agent", "monitor"}
# Reading the output of something already running: Claude Code's BashOutput and
# TaskOutput, Codex's write_stdin (mostly an empty write that polls a live command).
_STATUS_TOOLS = {"bashoutput", "taskoutput", "write_stdin", "list_agents"}
# Handing work to another agent: Codex multi-agent tools, Claude Code's Task/Agent tool.
_DISPATCH_TOOLS = {"spawn_agent", "send_message", "send_input", "task", "agent"}
_READ_TOOLS = {"read", "grep", "glob", "ls", "view_image", "webfetch", "websearch", "web_search", "web__run",
               "notebookread", "read_file", "list_dir", "search"}
_EDIT_TOOLS = {"edit", "write", "multiedit", "notebookedit", "apply_patch", "write_file", "str_replace_editor"}
_READ_CMD = re.compile(r"^\s*(rg|grep|egrep|sed\s+-n|cat|bat|less|ls|find|fd|nl|head|wc|awk|jq|yq|tree|stat|file|"
                       r"diff|git\s+(diff|show|blame|grep|ls-files)|gh\s+(api|search|repo\s+view|issue\s+view))\b")
_TEST_CMD = re.compile(r"\b(pytest|unittest|py_compile|compileall|mypy|pyright|ruff|flake8|eslint|tsc|jest|vitest|"
                       r"playwright|puppeteer|snapshot\.js|shellcheck|node\s+--(test|check)|"
                       r"(npm|pnpm|yarn|bun)\s+(run\s+)?(test|lint|typecheck)|go\s+(test|vet)|cargo\s+(test|check|clippy)|"
                       r"make\s+(test|check|lint)|bash\s+-n)\b|(?:^|[\s/])tests?/\S+\.(sh|cjs|mjs|js|py)\b|\.test\.[cm]?[jt]s\b")
_GIT_CMD = re.compile(r"^\s*git\s+(?:-c\s+\S+\s+)*(add|commit|push|pull|merge|rebase|cherry-pick|revert|tag|clone|"
                      r"worktree|branch|checkout|switch|reset|restore|stash|rm|mv|am|apply|init)\b")
_OPS_CMD = re.compile(r"^\s*(ssh|scp|rsync|sudo|systemctl|launchctl|service|docker|docker-compose|podman|kubectl|helm|"
                      r"terraform|wrangler|vercel|flyctl|fly|heroku|ansible(?:-playbook)?|tailscale|ufw|iptables|"
                      r"certbot|nginx|caddy|crontab|useradd|chown|chmod|ssh-keygen)\b")
_EDIT_CMD = re.compile(r"\bapply_patch\b|\bsed\s+-i\b|\bperl\s+-pi\b|\bcat\s*>{1,2}\s*\S|\btee\s+(-a\s+)?[\w./~]")
# ... or through a CLI: CCC (``ccc send|spawn|ask``) and WatchTower (``wt add``).
_DISPATCH_CMD = re.compile(r"(?:^|[\s;&|(/])(?:ccc\s+(?:--?[\w-]+(?:[= ](?!send|spawn|ask)\S+)?\s+)*(send|spawn|ask)|wt\s+add)\b"
                           r"(?![^\n;&|]*\s--help\b)([^\n;&|]*)")

_SLEEP_ONLY = re.compile(r"^\s*(sleep|timeout)\s+[\d.]+[smh]?\s*;?\s*$")
_SLEEP_PREFIX = re.compile(r"^\s*sleep\s+[\d.]+[smh]?\s*(&&|;)\s*")
# A status check reads the state of something else and changes nothing.
_STATUS_CMD = re.compile(
    r"^\s*("
    r"git\s+(status|log|fetch|ls-remote|rev-parse|merge-base|remote|rev-list|describe|reflog)\b"
    r"|tail\b|ps\b|pgrep\b|pstree\b|uptime\b|jobs\b|journalctl\b|dig\b|ss\b|netstat\b|df\b|du\b|free\b"
    r"|gh\s+(run|pr|workflow)\s+(view|watch|list|checks|status)\b"
    r"|gh\s+pr\s+checks\b"
    r"|kubectl\s+(get|describe|logs|rollout\s+status)\b"
    r"|docker\s+(ps|logs|inspect)\b"
    r"|(?:sudo\s+(?:-n\s+)?)?systemctl\s+(?:--user\s+)?(status|is-active|is-enabled|show|cat|list-units)\b"
    r"|launchctl\s+(list|print)\b|tailscale\s+(status|ip|ping)\b"
    r"|curl\b(?!.*\s-(X|-request)\s*(POST|PUT|PATCH|DELETE))(?!.*\s-(d|-data)\b)"
    r"|wt\s+(ls|list|show|status|queues)\b"
    r"|ccc\s+(ls|list|status|show|tail|log|logs|transcript|sessions|last)\b"
    r")"
)


# Noise in front of the real command: ``VAR=value``, ``cd dir &&``, ``git -C dir``.
_ENV_PREFIX = re.compile(r"^\s*(?:[A-Za-z_][A-Za-z0-9_]*=\S*\s+)+")
_CD_PREFIX = re.compile(r"^\s*cd\s+\S+\s*(?:&&|;)\s*")
_GIT_DIR = re.compile(r"^(\s*git)\s+-C\s+\S+")
# ``tail`` written as an inline Python script: prints the last lines of a file.
_PY_INLINE = re.compile(r"^\s*python3?\s+(?:-\s|-c\s|-\s*<<|<<)")
_PY_LOG = re.compile(r"\.jsonl\b|\.log\b")  # reading a transcript or log to see how something is going
_PY_TAIL = re.compile(r"(?:splitlines|readlines)\(\)\s*\[\s*-\s*\d+\s*:\s*\]")
# Inline Python sorted by what it does: hands work off, runs processes, writes files,
# sends data, checks on something (local APIs, databases, logs), or just reads.
_PY_DISPATCH = re.compile(r"""['"](?:[^'"\s]*/)?(?:wt['"]\s*,\s*['"]add|ccc['"]\s*,\s*['"](?:send|spawn|ask))['"]""")
# The program an inline script starts: ``subprocess.run(['git', ...])``.
_PY_ARGV0 = re.compile(r"""\[\s*['"]([^'"\s]+)['"]\s*,\s*['"]([^'"\s]*)['"]""")
_PY_PROCESS = re.compile(r"subprocess|os\.system|os\.popen|pty\.")
_PY_WRITE = re.compile(r"\.write\(|write_text|write_bytes|unlink|rmtree|os\.remove|\.rename\(|\.replace\(\s*\w+\s*\)|"
                       r"\bopen\([^)]*['\"][wax]b?\+?['\"]")
_PY_SEND = re.compile(r"method\s*=\s*['\"](?:POST|PUT|PATCH|DELETE)|\bdata\s*=|requests\.(?:post|put|patch|delete)|smtplib")
_PY_PROBE = re.compile(r"urlopen|requests\.get|sqlite3|/api/|\.db\b|queue|status|health")


def _python_label(cmd: str) -> str:
    if _PY_DISPATCH.search(cmd):
        return STEER if "--steer" in cmd else DISPATCH
    if _PY_PROCESS.search(cmd):
        started = [_command_label(f"{m.group(1).rsplit('/', 1)[-1]} {m.group(2)}") for m in _PY_ARGV0.finditer(cmd)]
        return combine(l for l in started if l in (GIT, OPS)) or RUN
    if _PY_WRITE.search(cmd):
        return EDIT
    if _PY_SEND.search(cmd):
        return RUN
    if _PY_TAIL.search(cmd) or _PY_LOG.search(cmd) or _PY_PROBE.search(cmd):
        return STATUS
    return READ


def _command_label(cmd: str) -> str:
    """The label for one shell command line (possibly a pipeline)."""
    if not cmd or not cmd.strip():
        return RUN
    handoffs = list(_DISPATCH_CMD.finditer(cmd))
    if handoffs:
        return STEER if any("--steer" in m.group(2) for m in handoffs) else DISPATCH
    if _SLEEP_ONLY.match(cmd):
        return WAIT
    if _PY_INLINE.match(cmd):
        return _python_label(cmd)
    rest = _SLEEP_PREFIX.sub("", cmd, count=1)
    rest = _CD_PREFIX.sub("", _ENV_PREFIX.sub("", rest, count=1), count=1)
    head = re.split(r"\s*(?:\|\||&&|;|\|)\s*", rest.strip(), maxsplit=1)[0]
    head = _GIT_DIR.sub(r"\1", head)
    if _STATUS_CMD.match(head):
        return STATUS
    if _EDIT_CMD.search(rest):
        return EDIT
    if _GIT_CMD.match(head):
        return GIT
    if _OPS_CMD.match(head):
        return OPS
    if _READ_CMD.match(head):
        return READ
    if _TEST_CMD.search(rest):
        return TEST
    return RUN


def tool_label(name: Optional[str], command: Optional[str] = None) -> str:
    """Label one tool call. ``command`` is the shell command when the tool runs one."""
    n = (name or "").lower()
    if n in _WAIT_TOOLS:
        return WAIT
    if n in _STATUS_TOOLS:
        return STATUS
    if n in _DISPATCH_TOOLS:
        return DISPATCH
    if n in _EDIT_TOOLS:
        return EDIT
    if n in _READ_TOOLS:
        return READ
    if command is not None:
        return _command_label(command)
    return RUN


def combine(labels: Iterable[str]) -> Optional[str]:
    """One label for the calls read together: the highest in ``_RANK``; none = ``None``."""
    labels = set(labels)
    if not labels:
        return None
    return next((l for l in _RANK if l in labels), RUN)
