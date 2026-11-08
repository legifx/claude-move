"""Self-move protection: detach when claude-move runs inside the session it is moving.

When Claude Code runs ``claude-move push ...`` through its own Bash tool, claude-move
is a descendant of the very Claude process that step 1 of the pipeline stops with
SIGTERM. Without precautions claude-move dies together with its parent and the move
never happens. This module detects that situation and continues the move in a fully
detached process (new session, no controlling terminal, output appended to a log
file), so the calling tool returns before the source process is stopped.
"""

from __future__ import annotations

import os
import subprocess
import sys
import time
from pathlib import Path

DETACHED_ENV = "CLAUDE_MOVE_DETACHED"
DELAY_ENV = "CLAUDE_MOVE_DETACH_DELAY"
DEFAULT_DELAY_SECONDS = 3.0
_MAX_ANCESTRY_DEPTH = 128


def default_log_path() -> Path:
    cache = os.environ.get("XDG_CACHE_HOME") or os.path.join(os.path.expanduser("~"), ".cache")
    return Path(cache) / "claude-move" / "last-move.log"


def _parent_pid(pid: int) -> int | None:
    """Return the parent PID of ``pid`` (Linux /proc, ``ps`` fallback for macOS/BSD)."""
    try:
        with open(f"/proc/{pid}/stat", encoding="utf-8", errors="replace") as fh:
            stat = fh.read()
        # Format: "pid (comm) state ppid ..." — comm may contain spaces/parens.
        return int(stat[stat.rindex(")") + 2 :].split()[1])
    except (OSError, ValueError, IndexError):
        pass
    try:
        out = subprocess.run(
            ["ps", "-o", "ppid=", "-p", str(int(pid))],
            capture_output=True,
            text=True,
            timeout=5,
        ).stdout.strip()
        return int(out) if out else None
    except (OSError, ValueError, subprocess.SubprocessError):
        return None


def is_descendant_of(ancestor_pid: int, pid: int | None = None) -> bool:
    """True if ``pid`` (default: this process) has ``ancestor_pid`` in its parent chain."""
    if not ancestor_pid or ancestor_pid <= 1:
        return False
    current = os.getpid() if pid is None else pid
    seen: set[int] = set()
    for _ in range(_MAX_ANCESTRY_DEPTH):
        if current == ancestor_pid:
            return True
        if current <= 1 or current in seen:
            return False
        seen.add(current)
        parent = _parent_pid(current)
        if parent is None:
            return False
        current = parent
    return False


def needs_detach(session_pid: int | None, alive: bool, dry_run: bool, keep_running: bool) -> bool:
    """Decide whether this invocation would kill itself when stopping the source session."""
    if dry_run or keep_running or not alive or not session_pid:
        return False
    if os.environ.get(DETACHED_ENV):
        return False  # already detached once — never loop
    return is_descendant_of(session_pid)


def _detach_delay() -> float:
    try:
        return max(0.0, float(os.environ.get(DELAY_ENV, DEFAULT_DELAY_SECONDS)))
    except ValueError:
        return DEFAULT_DELAY_SECONDS


def detach(log_path: Path | None = None) -> bool:
    """Double-fork into a new session with stdio redirected to ``log_path``.

    Returns True in the original (foreground) process, which should print nothing
    more and exit 0. Returns False in the detached grandchild, which continues the
    move after a short delay so the calling tool has returned first.
    """
    log_path = log_path or default_log_path()
    log_path.parent.mkdir(parents=True, exist_ok=True)
    sys.stdout.flush()
    sys.stderr.flush()

    first = os.fork()
    if first > 0:
        os.waitpid(first, 0)  # intermediate child exits immediately
        print(f"claude-move: running inside the session being moved — detached, log: {log_path}")
        sys.stdout.flush()
        return True

    # Intermediate child: new session, then fork again so the worker can never
    # reacquire a controlling terminal and is reparented away from Claude.
    try:
        os.setsid()
        if os.fork() > 0:
            os._exit(0)
        os.environ[DETACHED_ENV] = "1"
        devnull = os.open(os.devnull, os.O_RDONLY)
        logfd = os.open(str(log_path), os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
        os.dup2(devnull, 0)
        os.dup2(logfd, 1)
        os.dup2(logfd, 2)
        os.close(devnull)
        os.close(logfd)
        sys.stdin = open(os.devnull, encoding="utf-8")
        sys.stdout = os.fdopen(1, "w", buffering=1, encoding="utf-8", closefd=False)
        sys.stderr = os.fdopen(2, "w", buffering=1, encoding="utf-8", closefd=False)
        print(f"\n=== claude-move detached worker pid={os.getpid()} at {time.strftime('%Y-%m-%d %H:%M:%S')} ===")
        time.sleep(_detach_delay())
        return False
    except BaseException:
        os._exit(1)
