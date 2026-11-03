"""Session discovery and interactive selection across local and remote machines."""

from __future__ import annotations

import json
import os
import re
import shlex
import subprocess
import sys
import time
from dataclasses import dataclass, field
from typing import Any

from .config import MachineConfig
from .herdr_ctl import is_herdr_reachable, list_agents, list_tabs

_NON_PROJECT_HOME_DIRS = frozenset(
    {
        "Desktop",
        "Documents",
        "Downloads",
        "Library",
        "Movies",
        "Music",
        "Pictures",
        "Public",
        "Templates",
        "Videos",
        "Projects",
        "projects",
        "backups",
        "bin",
        "snap",
        "flutter",
        "android-sdk",
        "node_modules",
    }
)

_PROJECT_MARKERS = (
    ".git",
    "CLAUDE.md",
    "AGENTS.md",
    "pyproject.toml",
    "package.json",
    "Cargo.toml",
    "go.mod",
    "Makefile",
    "README.md",
)


@dataclass
class SessionRecord:
    """Rich metadata for a live or saved Claude Code session."""

    session_id: str
    transcript_path: str
    encoded_cwd: str
    cwd: str
    mtime: float
    size_bytes: int
    title: str = ""
    last_prompt: str = ""
    git_branch: str = ""
    alive: bool = False
    pid: int | None = None
    pane_id: str | None = None
    tab_id: str | None = None
    workspace_id: str | None = None
    agent_name: str | None = None
    agent_status: str | None = None
    tab_label: str | None = None
    cwd_project_root: str | None = None
    cproj_key: str | None = None
    cproj_local: str | None = None
    cproj_server: str | None = None
    touched_projects: list[str] = field(default_factory=list)

    @property
    def short_id(self) -> str:
        return self.session_id[:8]

    @property
    def display_title(self) -> str:
        for candidate in (self.title, self.tab_label, self.cproj_key, self.last_prompt):
            if candidate and candidate.strip():
                clean = " ".join(candidate.strip().split())
                return clean[:64] + ("…" if len(clean) > 64 else "")
        return f"Session {self.short_id}"


# Self-contained Python 3.8+ script that executes locally or over `ssh <host> python3 -c ...`
# Works on both Linux and macOS (POSIX os.kill liveness check + optional /proc or ps env lookup).
_PROBE_SCRIPT = r'''
import glob, json, os, re, subprocess, sys

cfg = json.loads(sys.stdin.read())
home = os.path.expanduser(cfg.get("home") or "~").rstrip("/") or "/"
projects_dir = os.path.expanduser(cfg.get("projects_dir") or home).rstrip("/") or "/"
limit = int(cfg.get("limit") or 50)
non_proj = set(cfg.get("non_project_dirs") or [])
markers = tuple(cfg.get("project_markers") or [])

claude_dir = os.path.join(home, ".claude")
proj_base = os.path.join(claude_dir, "projects")
sess_base = os.path.join(claude_dir, "sessions")
cproj_sess_base = os.path.join(home, ".cache", "cproj", "sessions")

def pid_is_alive(pid):
    try:
        os.kill(int(pid), 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except Exception:
        return False

def read_herdr_env(pid):
    env_map = {}
    proc_env = f"/proc/{pid}/environ"
    if os.path.exists(proc_env):
        try:
            for item in open(proc_env, "rb").read().split(b"\0"):
                if item.startswith(b"HERDR_"):
                    k, _, v = item.decode("utf-8", "replace").partition("=")
                    env_map[k] = v
            return env_map
        except Exception:
            pass
    # macOS / BSD fallback via `ps eww`
    if sys.platform == "darwin":
        try:
            out = subprocess.check_output(["ps", "eww", "-p", str(pid)], text=True, stderr=subprocess.DEVNULL)
            for m in re.finditer(r"\b(HERDR_[A-Z0-9_]+)=([^\s]+)", out):
                env_map[m.group(1)] = m.group(2)
        except Exception:
            pass
    return env_map

def find_project_root_for_cwd(cwd_path):
    c = (cwd_path or "").rstrip("/") or "/"
    if c in ("/", home, "/tmp") or "/cproj/temp/" in c:
        return None
    if c.startswith("/tmp/") and not c.startswith(home + "/"):
        return None
    if not os.path.isdir(c):
        return c if c.startswith(home + "/") else None
    cur = c
    while cur and cur != "/" and cur != home:
        if os.path.exists(os.path.join(cur, ".git")):
            return cur
        parent = os.path.dirname(cur)
        if parent == cur or parent == home or parent == projects_dir:
            break
        cur = parent
    return cur if cur not in ("/", home) else None

# 1. Map live Claude PIDs -> sessionId + optional HERDR_* env
live_by_sid = {}
if os.path.isdir(sess_base):
    for sp in glob.glob(os.path.join(sess_base, "*.json")):
        try:
            sd = json.load(open(sp, encoding="utf-8"))
        except Exception:
            continue
        pid = sd.get("pid")
        sid = sd.get("sessionId")
        if not pid or not sid or not pid_is_alive(pid):
            continue
        env_map = read_herdr_env(pid)
        live_by_sid[sid] = {
            "pid": int(pid),
            "cwd": sd.get("cwd") or "",
            "pane_id": env_map.get("HERDR_PANE_ID"),
            "tab_id": env_map.get("HERDR_TAB_ID"),
            "workspace_id": env_map.get("HERDR_WORKSPACE_ID"),
        }

# 2. Collect transcripts sorted by live-first then mtime desc
candidates = []
if os.path.isdir(proj_base):
    for enc_dir in os.listdir(proj_base):
        dp = os.path.join(proj_base, enc_dir)
        if not os.path.isdir(dp):
            continue
        for fn in os.listdir(dp):
            if not fn.endswith(".jsonl"):
                continue
            sid = fn[:-6]
            fp = os.path.join(dp, fn)
            try:
                st = os.stat(fp)
            except OSError:
                continue
            is_live = 1 if sid in live_by_sid else 0
            candidates.append((is_live, st.st_mtime, st.st_size, sid, enc_dir, fp))

candidates.sort(key=lambda x: (x[0], x[1]), reverse=True)
candidates = candidates[:limit]

proj_re = re.compile(re.escape(projects_dir) + r"/([A-Za-z0-9._-]+)")

def is_valid_project_dir(name):
    if not name or name.startswith(".") or name in non_proj:
        return False
    full = os.path.join(projects_dir, name)
    if not os.path.isdir(full):
        return False
    if projects_dir != home:
        return True
    return any(os.path.exists(os.path.join(full, m)) for m in markers)

records = []
for is_live, mtime, size_b, sid, enc_dir, fp in candidates:
    cwd = ""
    ai_title = ""
    last_prompt = ""
    first_user = ""
    git_branch = ""
    touched_counts = {}

    try:
        with open(fp, encoding="utf-8", errors="replace") as f:
            for raw in f:
                for m in proj_re.finditer(raw):
                    pname = m.group(1)
                    touched_counts[pname] = touched_counts.get(pname, 0) + 1
                if '"cwd"' not in raw and '"ai-title"' not in raw and '"last-prompt"' not in raw and '"user"' not in raw:
                    continue
                try:
                    obj = json.loads(raw)
                except Exception:
                    continue
                if not cwd and isinstance(obj.get("cwd"), str):
                    cwd = obj["cwd"]
                if not git_branch and isinstance(obj.get("gitBranch"), str):
                    git_branch = obj["gitBranch"]
                t = obj.get("type")
                if t == "ai-title" and isinstance(obj.get("aiTitle"), str):
                    ai_title = obj["aiTitle"]
                elif t == "last-prompt" and isinstance(obj.get("lastPrompt"), str):
                    last_prompt = obj["lastPrompt"]
                elif t == "user" and not first_user and not obj.get("isMeta"):
                    msg = obj.get("message") or {}
                    content = msg.get("content")
                    if isinstance(content, str) and not content.startswith("<"):
                        first_user = content[:160]
    except Exception:
        pass

    live_info = live_by_sid.get(sid) or {}
    if not cwd:
        cwd = live_info.get("cwd") or home

    valid_touched = [
        p for p, _ in sorted(touched_counts.items(), key=lambda kv: kv[1], reverse=True)
        if is_valid_project_dir(p)
    ]

    # Optional cproj session badge integration (no-op if cproj is not installed)
    cproj_key = None
    cproj_local = None
    cproj_server = None
    cp_file = os.path.join(cproj_sess_base, f"{sid}.json")
    if os.path.isfile(cp_file):
        try:
            cpd = json.load(open(cp_file, encoding="utf-8"))
            loc = cpd.get("local") or ""
            if "/cproj/temp/" not in loc:
                cproj_key = cpd.get("key")
                cproj_local = loc or None
                cproj_server = cpd.get("server") or None
        except Exception:
            pass

    records.append({
        "session_id": sid,
        "transcript_path": fp,
        "encoded_cwd": enc_dir,
        "cwd": cwd,
        "mtime": mtime,
        "size_bytes": size_b,
        "title": ai_title,
        "last_prompt": last_prompt or first_user,
        "git_branch": git_branch,
        "alive": bool(is_live),
        "pid": live_info.get("pid"),
        "pane_id": live_info.get("pane_id"),
        "tab_id": live_info.get("tab_id"),
        "workspace_id": live_info.get("workspace_id"),
        "cwd_project_root": find_project_root_for_cwd(cwd),
        "cproj_key": cproj_key,
        "cproj_local": cproj_local,
        "cproj_server": cproj_server,
        "touched_projects": valid_touched[:5],
    })

print(json.dumps(records, ensure_ascii=False))
'''


def discover_sessions(
    machine: MachineConfig,
    is_local: bool,
    limit: int = 40,
) -> list[SessionRecord]:
    """Discover live and saved Claude sessions on a local or remote machine."""
    non_proj = set(_NON_PROJECT_HOME_DIRS) | set(machine.ignore_dirs)
    payload = json.dumps(
        {
            "home": machine.norm_home or "~",
            "projects_dir": machine.norm_projects_dir or machine.norm_home or "~",
            "limit": limit,
            "non_project_dirs": list(non_proj),
            "project_markers": list(_PROJECT_MARKERS),
        }
    )

    if is_local:
        proc = subprocess.run(
            [sys.executable, "-c", _PROBE_SCRIPT],
            input=payload,
            capture_output=True,
            text=True,
            timeout=15,
        )
    else:
        if not machine.ssh:
            raise RuntimeError(
                f"Cannot scan sessions on {machine.name!r}: no SSH host configured"
            )
        proc = subprocess.run(
            ["ssh", "-o", "BatchMode=yes", machine.ssh, f"python3 -c {shlex.quote(_PROBE_SCRIPT)}"],
            input=payload,
            capture_output=True,
            text=True,
            timeout=20,
        )

    if proc.returncode != 0:
        err = (proc.stderr or proc.stdout).strip()
        raise RuntimeError(f"Session discovery failed on {machine.name}: {err}")

    raw_list: list[dict[str, Any]] = json.loads(proc.stdout.strip() or "[]")
    sessions = [SessionRecord(**item) for item in raw_list]

    # Enrich live sessions with Herdr status/titles if Herdr is installed & reachable
    if any(s.alive for s in sessions) and is_herdr_reachable(machine, is_local):
        try:
            agents = list_agents(machine, is_local)
            tabs = list_tabs(machine, is_local)
            agent_by_pane = {str(a.get("pane_id")): a for a in agents if a.get("pane_id")}
            tab_by_id = {str(t.get("tab_id")): t for t in tabs if t.get("tab_id")}
            for s in sessions:
                if s.pane_id and s.pane_id in agent_by_pane:
                    ag = agent_by_pane[s.pane_id]
                    s.agent_name = ag.get("name")
                    s.agent_status = ag.get("agent_status")
                    if not s.title and ag.get("terminal_title_stripped"):
                        s.title = str(ag["terminal_title_stripped"])
                if s.tab_id and s.tab_id in tab_by_id:
                    tb = tab_by_id[s.tab_id]
                    lbl = tb.get("label")
                    if lbl and not str(lbl).isdigit():
                        s.tab_label = str(lbl)
        except Exception:
            pass

    return sessions


def format_age(mtime: float, now: float | None = None) -> str:
    ref = now if now is not None else time.time()
    delta = max(0, int(ref - mtime))
    if delta < 60:
        return f"{delta}s"
    if delta < 3600:
        return f"{delta // 60}m"
    if delta < 86400:
        return f"{delta // 3600}h"
    return f"{delta // 86400}d"


def format_session_line(s: SessionRecord, home: str = "") -> str:
    if s.alive:
        st = s.agent_status or "live"
        loc = s.tab_id or s.pane_id or f"pid:{s.pid}"
        badge = f"● {st:<7} {loc:<10}"
    else:
        badge = f"○ {'saved':<7} {'-':<10}"

    proj_hint = s.cproj_key or (
        os.path.basename(s.cwd_project_root) if s.cwd_project_root else ""
    ) or (s.touched_projects[0] if s.touched_projects else "")
    if not proj_hint and home and s.cwd.startswith(home + "/"):
        proj_hint = s.cwd[len(home) + 1 :].split("/")[0]
    proj_col = (proj_hint or "~")[:20]

    return (
        f"{badge}  {s.short_id}  {format_age(s.mtime):>4}  "
        f"{proj_col:<20}  {s.display_title}"
    )


def resolve_session(
    sessions: list[SessionRecord],
    selector: str,
) -> SessionRecord:
    """Resolve a user selector (UUID/prefix, pane/tab ID, agent name, project name, or title)."""
    sel = selector.strip()
    sel_low = sel.lower()

    if sel_low in ("current", "."):
        env_tab = os.environ.get("HERDR_TAB_ID")
        env_pane = os.environ.get("HERDR_PANE_ID")
        for s in sessions:
            if (env_pane and s.pane_id == env_pane) or (env_tab and s.tab_id == env_tab):
                return s
        cwd_now = os.getcwd()
        for s in sessions:
            if s.cwd == cwd_now or s.cwd_project_root == cwd_now:
                return s
        raise LookupError("Could not match 'current' to a Claude session in this tab or cwd")

    # 1. Exact UUID, pane_id, tab_id, or agent_name
    for s in sessions:
        if (
            s.session_id.lower() == sel_low
            or (s.pane_id and s.pane_id.lower() == sel_low)
            or (s.tab_id and s.tab_id.lower() == sel_low)
            or (s.agent_name and s.agent_name.lower() == sel_low)
        ):
            return s

    # 2. UUID prefix match
    uuid_matches = [s for s in sessions if s.session_id.lower().startswith(sel_low)]
    if len(uuid_matches) == 1:
        return uuid_matches[0]
    if len(uuid_matches) > 1:
        ids = ", ".join(m.short_id for m in uuid_matches[:5])
        raise LookupError(f"Ambiguous UUID prefix {sel!r} (matches: {ids})")

    # 3. Project name or title substring match (prefer live sessions if unambiguous)
    sub_matches = [
        s
        for s in sessions
        if (s.cproj_key and sel_low == s.cproj_key.lower())
        or (s.cwd_project_root and sel_low == os.path.basename(s.cwd_project_root).lower())
        or any(sel_low == p.lower() for p in s.touched_projects)
        or sel_low in s.display_title.lower()
    ]
    if len(sub_matches) == 1:
        return sub_matches[0]
    live_subs = [s for s in sub_matches if s.alive]
    if len(live_subs) == 1:
        return live_subs[0]
    if len(sub_matches) > 1:
        opts = ", ".join(f"{m.short_id} ({m.display_title[:25]})" for m in sub_matches[:5])
        raise LookupError(f"Ambiguous selector {sel!r} — matches multiple sessions: {opts}")

    raise LookupError(f"No session matching {sel!r}")


def pick_session_interactive(
    sessions: list[SessionRecord],
    picker_cmd: list[str],
    home: str = "",
) -> SessionRecord | None:
    """Interactively pick a session via fzf (if available and TTY) or numbered stdin prompt."""
    if not sessions:
        return None
    if not sys.stdin.isatty():
        raise RuntimeError("No session selector given and stdin is not an interactive TTY")

    lines = [f"{idx:>2}  {format_session_line(s, home)}" for idx, s in enumerate(sessions, 1)]

    if picker_cmd:
        exe = picker_cmd[0]
        from shutil import which

        if which(exe):
            proc = subprocess.run(
                picker_cmd,
                input="\n".join(lines) + "\n",
                stdout=subprocess.PIPE,
                text=True,
            )
            if proc.returncode != 0 or not proc.stdout.strip():
                return None
            chosen = proc.stdout.strip().splitlines()[0].strip()
            m = re.match(r"^(\d+)\s+", chosen)
            if m:
                i = int(m.group(1)) - 1
                if 0 <= i < len(sessions):
                    return sessions[i]

    print("Select a Claude session to move:\n", file=sys.stderr)
    for ln in lines[:20]:
        print(f"  {ln}", file=sys.stderr)
    print("", file=sys.stderr)
    try:
        ans = input(f"Session number [1-{min(20, len(sessions))}] (Enter to cancel): ").strip()
    except (EOFError, KeyboardInterrupt):
        return None
    if not ans:
        return None
    if ans.isdigit():
        idx = int(ans) - 1
        if 0 <= idx < len(sessions):
            return sessions[idx]
    return resolve_session(sessions, ans)
