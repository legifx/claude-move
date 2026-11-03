"""End-to-end session migration pipeline (stop -> rsync -> rewrite -> deploy -> resume)."""

from __future__ import annotations

import json
import os
import shlex
import shutil
import signal
import subprocess
import tarfile
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .config import AppConfig, MachineConfig
from .discovery import SessionRecord
from .herdr_ctl import (
    HerdrLaunchResult,
    close_tab,
    interrupt_agent,
    is_herdr_reachable,
    is_tmux_running,
    launch_resumed_claude,
    launch_resumed_claude_tmux,
)
from .rewrite import (
    PathMapper,
    encode_cwd,
    rewrite_jsonl_file,
    rewrite_session_companion_dir,
)

_FORBIDDEN_PROJECT_ROOTS = frozenset(
    {"/", "/home", "/Users", "/root", "/tmp", "/var", "/usr", "/etc", "/opt"}
)


@dataclass
class MoveOptions:
    dry_run: bool = False
    explicit_projects: list[str] = field(default_factory=list)
    all_projects: bool = False
    no_project: bool = False
    include_deps: bool = False
    extra_excludes: list[str] = field(default_factory=list)
    delete: bool = False
    stop_working: bool = False
    force: bool = False
    keep_running: bool = False
    close_source_tab: bool = False
    launcher: str = "auto"  # "auto" | "herdr" | "tmux" | "none"
    no_launch: bool = False
    attach: bool = False
    focus: bool = False
    workspace_id: str | None = None
    claude_args: list[str] = field(default_factory=list)
    initial_prompt: str | None = None


@dataclass
class MoveResult:
    session_id: str
    title: str
    src_machine: str
    dst_machine: str
    src_cwd: str
    dst_cwd: str
    project_pairs: list[tuple[str, str]]
    rewritten_lines: int
    companion_files: int
    herdr_launch: HerdrLaunchResult | None = None
    dry_run: bool = False


def _validate_safe_project_dir(path: str, machine: MachineConfig) -> None:
    norm = path.rstrip("/") or "/"
    if norm in _FORBIDDEN_PROJECT_ROOTS or norm == machine.norm_home:
        raise ValueError(
            f"Refusing to sync {norm!r} as a project directory (matches system or $HOME root)"
        )


def resolve_project_pairs(
    session: SessionRecord,
    src: MachineConfig,
    dst: MachineConfig,
    opts: MoveOptions,
) -> tuple[list[tuple[str, str]], list[str]]:
    """Determine (src_project_dir, dst_project_dir) pairs and known project folder names."""
    if opts.no_project:
        names = list(session.touched_projects)
        if session.cproj_key:
            names.append(session.cproj_key)
        if session.cwd_project_root:
            names.append(os.path.basename(session.cwd_project_root))
        return [], sorted(set(names))

    pairs: list[tuple[str, str]] = []
    project_names: set[str] = set(session.touched_projects)

    def _map_single_src_proj(s_path: str) -> tuple[str, str]:
        sp = s_path.rstrip("/")
        _validate_safe_project_dir(sp, src)
        p_name = os.path.basename(sp)
        project_names.add(p_name)

        if sp in src.extra_path_maps:
            return sp, src.extra_path_maps[sp].rstrip("/")

        if sp.startswith(src.norm_projects_dir + "/"):
            rel = sp[len(src.norm_projects_dir) + 1 :]
            top = rel.split("/", 1)[0]
            project_names.add(top)
            return sp, f"{dst.norm_projects_dir}/{rel}"

        if sp.startswith(src.norm_home + "/"):
            rel = sp[len(src.norm_home) + 1 :]
            return sp, f"{dst.norm_home}/{rel}"

        return sp, f"{dst.norm_projects_dir}/{p_name}"

    if opts.explicit_projects:
        for raw_p in opts.explicit_projects:
            expanded = raw_p.strip()
            if not expanded.startswith("/"):
                expanded = f"{src.norm_projects_dir}/{expanded}"
            pairs.append(_map_single_src_proj(expanded))
        return _dedup_pairs(pairs), sorted(project_names)

    # 1. Session cwd itself is inside a project directory (works for ~/Projects/foo, ~/dev/foo, ~/foo, etc.)
    if session.cwd_project_root:
        pairs.append(_map_single_src_proj(session.cwd_project_root))
    else:
        s_cwd = session.cwd.rstrip("/") or "/"
        if s_cwd != src.norm_home and "/cproj/temp/" not in s_cwd and not s_cwd.startswith("/tmp"):
            if src.norm_projects_dir != src.norm_home and s_cwd.startswith(
                src.norm_projects_dir + "/"
            ):
                rel_seg = s_cwd[len(src.norm_projects_dir) + 1 :].split("/", 1)[0]
                if rel_seg and not rel_seg.startswith("."):
                    pairs.append(_map_single_src_proj(f"{src.norm_projects_dir}/{rel_seg}"))
            elif s_cwd.startswith(src.norm_home + "/"):
                rel = s_cwd[len(src.norm_home) + 1 :]
                if rel and not rel.startswith("."):
                    pairs.append(_map_single_src_proj(s_cwd))

    # 2. Optional cproj session association (when cwd is $HOME)
    if session.cproj_local or session.cproj_server:
        if src.ssh is None and session.cproj_local:
            sp = session.cproj_local.rstrip("/")
            if "/cproj/temp/" not in sp and sp != src.norm_home:
                dp = (
                    session.cproj_server.rstrip("/")
                    if session.cproj_server and dst.name == "server"
                    else _map_single_src_proj(sp)[1]
                )
                _validate_safe_project_dir(sp, src)
                project_names.add(os.path.basename(sp))
                pairs.append((sp, dp))
        elif session.cproj_server and session.cproj_server.startswith(src.norm_home + "/"):
            sp = session.cproj_server.rstrip("/")
            dp = (
                session.cproj_local.rstrip("/")
                if session.cproj_local
                else _map_single_src_proj(sp)[1]
            )
            _validate_safe_project_dir(sp, src)
            project_names.add(os.path.basename(sp))
            pairs.append((sp, dp))

    # 3. Primary project touched inside the transcript (or up to 5 with --all-projects)
    if not pairs and session.touched_projects:
        max_auto = 5 if opts.all_projects else 1
        for p_name in session.touched_projects[:max_auto]:
            sp = f"{src.norm_projects_dir}/{p_name}"
            pairs.append(_map_single_src_proj(sp))

    return _dedup_pairs(pairs), sorted(project_names)


def _dedup_pairs(pairs: list[tuple[str, str]]) -> list[tuple[str, str]]:
    seen: set[str] = set()
    out: list[tuple[str, str]] = []
    for sp, dp in pairs:
        if sp not in seen:
            seen.add(sp)
            out.append((sp, dp))
    return out


def _local_pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True


def _kill_pid_gracefully(machine: MachineConfig, is_local: bool, pid: int) -> None:
    """Send SIGTERM and wait up to 5s for exit using portable POSIX signals (Linux + macOS)."""
    if is_local:
        try:
            os.kill(pid, signal.SIGTERM)
        except ProcessLookupError:
            return
        for _ in range(25):
            if not _local_pid_alive(pid):
                return
            time.sleep(0.2)
    elif machine.ssh:
        script = (
            f"kill -TERM {int(pid)} 2>/dev/null || true; "
            f"for i in $(seq 1 25); do kill -0 {int(pid)} 2>/dev/null || exit 0; sleep 0.2; done"
        )
        subprocess.run(
            ["ssh", "-o", "BatchMode=yes", machine.ssh, script],
            capture_output=True,
            text=True,
            timeout=12,
        )


def stop_source_session_if_needed(
    session: SessionRecord,
    src: MachineConfig,
    src_is_local: bool,
    opts: MoveOptions,
    log: Any = print,
) -> None:
    """Ensure the source Claude process is safely at a turn boundary and stopped before moving."""
    if not session.alive or opts.keep_running:
        return

    is_working = session.agent_status == "working" or (
        session.agent_status is None and (time.time() - session.mtime) < 2.5
    )

    if is_working and not opts.force:
        if not opts.stop_working:
            raise RuntimeError(
                f"Session {session.short_id} ({session.display_title!r}) is currently working. "
                "Wait for its turn to finish, press Esc, or pass --stop."
            )
        target = session.agent_name or session.pane_id
        if target and is_herdr_reachable(src, src_is_local):
            log(f"  → Interrupting active turn in {src.name}/{target} (Esc)…")
            interrupt_agent(src, src_is_local, target)
            time.sleep(1.5)

    if session.pid:
        log(f"  → Stopping source Claude process (PID {session.pid}) on {src.name}…")
        _kill_pid_gracefully(src, src_is_local, session.pid)

    if opts.close_source_tab and session.tab_id and is_herdr_reachable(src, src_is_local):
        log(f"  → Closing source Herdr tab {session.tab_id} on {src.name}…")
        close_tab(src, src_is_local, session.tab_id)


def sync_project_directory(
    src_proj: str,
    dst_proj: str,
    src: MachineConfig,
    dst: MachineConfig,
    src_is_local: bool,
    dst_is_local: bool,
    excludes: list[str],
    delete: bool = False,
) -> None:
    """Sync a project working tree (including dirty edits and .git) via portable POSIX rsync."""
    _validate_safe_project_dir(src_proj, src)
    _validate_safe_project_dir(dst_proj, dst)

    dst_parent = os.path.dirname(dst_proj.rstrip("/")) or "/"
    if dst_is_local:
        os.makedirs(dst_parent, exist_ok=True)
    elif dst.ssh:
        subprocess.run(
            ["ssh", "-o", "BatchMode=yes", dst.ssh, f"mkdir -p {shlex.quote(dst_parent)}"],
            check=True,
            capture_output=True,
            text=True,
            timeout=15,
        )

    src_spec = f"{src_proj.rstrip('/')}/" if src_is_local else f"{src.ssh}:{src_proj.rstrip('/')}/"
    dst_spec = f"{dst_proj.rstrip('/')}/" if dst_is_local else f"{dst.ssh}:{dst_proj.rstrip('/')}/"

    cmd = ["rsync", "-az"]
    if delete:
        cmd.append("--delete")
    for pattern in excludes:
        cmd += ["--exclude", pattern]
    cmd += [src_spec, dst_spec]

    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=600)
    if proc.returncode != 0:
        err = (proc.stderr or proc.stdout).strip()
        raise RuntimeError(f"rsync failed ({src_spec} -> {dst_spec}): {err}")


def _fetch_source_artifacts(
    session: SessionRecord,
    src: MachineConfig,
    src_is_local: bool,
    raw_dir: Path,
) -> None:
    """Stage session files from local disk or remote SSH into raw_dir."""
    sid = session.session_id
    enc = session.encoded_cwd
    rel_candidates = [
        f".claude/projects/{enc}/{sid}.jsonl",
        f".claude/projects/{enc}/{sid}",
        f".claude/file-history/{sid}",
        f".claude/session-env/{sid}",
        f".cache/cproj/sessions/{sid}.json",
    ]
    if (session.cwd.rstrip("/") or "/") != src.norm_home:
        rel_candidates.append(f".claude/projects/{enc}/memory")

    if src_is_local:
        base = Path(src.norm_home)
        for rel in rel_candidates:
            s_path = base / rel
            d_path = raw_dir / rel
            if s_path.is_file():
                d_path.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(s_path, d_path)
            elif s_path.is_dir():
                shutil.copytree(s_path, d_path, dirs_exist_ok=True)
    else:
        if not src.ssh:
            raise RuntimeError(f"No SSH host configured for {src.name}")
        remote_py = (
            "import json, os, subprocess, sys\n"
            "home = os.path.expanduser(sys.argv[1])\n"
            "rels = json.loads(sys.argv[2])\n"
            "existing = [r for r in rels if os.path.exists(os.path.join(home, r))]\n"
            "if not existing:\n"
            "    sys.exit(3)\n"
            "subprocess.run(['tar', '-czf', '-', '-C', home, *existing], check=True)\n"
        )
        proc = subprocess.run(
            [
                "ssh",
                "-o",
                "BatchMode=yes",
                src.ssh,
                f"python3 -c {shlex.quote(remote_py)} {shlex.quote(src.norm_home)} {shlex.quote(json.dumps(rel_candidates))}",
            ],
            capture_output=True,
            timeout=120,
        )
        if proc.returncode != 0:
            err = proc.stderr.decode("utf-8", "replace").strip()
            raise RuntimeError(f"Failed to fetch session {sid} from {src.name}: {err}")

        tar_tmp = raw_dir / "_fetch.tar.gz"
        raw_dir.mkdir(parents=True, exist_ok=True)
        tar_tmp.write_bytes(proc.stdout)
        with tarfile.open(tar_tmp, "r:gz") as tf:
            try:
                tf.extractall(raw_dir, filter="data")
            except TypeError:
                tf.extractall(raw_dir)
        tar_tmp.unlink(missing_ok=True)


def _read_destination_account(dst: MachineConfig, dst_is_local: bool) -> dict[str, str] | None:
    """Read destination oauthAccount UUIDs (never tokens!) so cross-account moves also work."""
    if dst_is_local:
        cj = Path(dst.norm_home) / ".claude.json"
        if not cj.is_file():
            return None
        try:
            data = json.loads(cj.read_text(encoding="utf-8"))
            oa = data.get("oauthAccount") or {}
            if oa.get("accountUuid"):
                return {
                    "accountUuid": str(oa["accountUuid"]),
                    "organizationUuid": str(oa.get("organizationUuid") or ""),
                }
        except Exception:
            return None
        return None

    if not dst.ssh:
        return None
    py = (
        "import json, os\n"
        "p = os.path.expanduser('~/.claude.json')\n"
        "d = json.load(open(p)) if os.path.isfile(p) else {}\n"
        "oa = d.get('oauthAccount') or {}\n"
        "print(json.dumps({'accountUuid': oa.get('accountUuid',''), 'organizationUuid': oa.get('organizationUuid','')}))\n"
    )
    proc = subprocess.run(
        ["ssh", "-o", "BatchMode=yes", dst.ssh, f"python3 -c {shlex.quote(py)}"],
        capture_output=True,
        text=True,
        timeout=15,
    )
    if proc.returncode == 0 and proc.stdout.strip():
        try:
            parsed = json.loads(proc.stdout.strip().splitlines()[-1])
            if parsed.get("accountUuid"):
                return parsed
        except Exception:
            pass
    return None


def _ensure_local_claude_trust(home_dir: Path, dst_cwd: str) -> None:
    """Atomically mark dst_cwd as trusted in ~/.claude.json so --resume never blocks on trust prompt."""
    cj = home_dir / ".claude.json"
    data: dict[str, Any] = {}
    if cj.is_file():
        try:
            data = json.loads(cj.read_text(encoding="utf-8"))
        except Exception:
            return
    projects = data.setdefault("projects", {})
    proj_entry = projects.setdefault(dst_cwd, {})
    if proj_entry.get("hasTrustDialogAccepted") is True:
        return
    proj_entry["hasTrustDialogAccepted"] = True
    proj_entry.setdefault("allowedTools", [])
    tmp = cj.with_suffix(".json.claude-move.tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    os.chmod(tmp, 0o600)
    os.replace(tmp, cj)


def _deploy_staged_artifacts(
    out_dir: Path,
    dst: MachineConfig,
    dst_is_local: bool,
    dst_cwd: str,
) -> None:
    """Deploy rewritten session artifacts into destination $HOME and mark dst_cwd trusted."""
    if dst_is_local:
        dst_home = Path(dst.norm_home)
        Path(dst_cwd).mkdir(parents=True, exist_ok=True)
        for item in out_dir.iterdir():
            target = dst_home / item.name
            if item.is_dir():
                shutil.copytree(item, target, dirs_exist_ok=True)
            else:
                shutil.copy2(item, target)
        _ensure_local_claude_trust(dst_home, dst_cwd)
    else:
        if not dst.ssh:
            raise RuntimeError(f"No SSH host configured for {dst.name}")
        tar_path = out_dir.parent / "_deploy.tar.gz"
        with tarfile.open(tar_path, "w:gz") as tf:
            for item in out_dir.iterdir():
                tf.add(item, arcname=item.name)
        payload = tar_path.read_bytes()
        tar_path.unlink(missing_ok=True)

        remote_py = (
            "import json, os, sys, tarfile, io\n"
            "home = os.path.expanduser(sys.argv[1])\n"
            "dst_cwd = sys.argv[2]\n"
            "os.makedirs(dst_cwd, exist_ok=True)\n"
            "raw = sys.stdin.buffer.read()\n"
            "with tarfile.open(fileobj=io.BytesIO(raw), mode='r:gz') as tf:\n"
            "    try:\n"
            "        tf.extractall(home, filter='data')\n"
            "    except TypeError:\n"
            "        tf.extractall(home)\n"
            "cj = os.path.join(home, '.claude.json')\n"
            "if os.path.isfile(cj):\n"
            "    try:\n"
            "        d = json.load(open(cj, encoding='utf-8'))\n"
            "        pe = d.setdefault('projects', {}).setdefault(dst_cwd, {})\n"
            "        if pe.get('hasTrustDialogAccepted') is not True:\n"
            "            pe['hasTrustDialogAccepted'] = True\n"
            "            pe.setdefault('allowedTools', [])\n"
            "            tmp = cj + '.claude-move.tmp'\n"
            "            with open(tmp, 'w', encoding='utf-8') as f:\n"
            "                json.dump(d, f, ensure_ascii=False, indent=2)\n"
            "                f.write('\\n')\n"
            "            os.chmod(tmp, 0o600)\n"
            "            os.replace(tmp, cj)\n"
            "    except Exception:\n"
            "        pass\n"
        )
        proc = subprocess.run(
            [
                "ssh",
                "-o",
                "BatchMode=yes",
                dst.ssh,
                f"python3 -c {shlex.quote(remote_py)} {shlex.quote(dst.norm_home)} {shlex.quote(dst_cwd)}",
            ],
            input=payload,
            capture_output=True,
            timeout=120,
        )
        if proc.returncode != 0:
            err = proc.stderr.decode("utf-8", "replace").strip()
            raise RuntimeError(f"Failed to deploy session state to {dst.name}: {err}")


def _manual_resume_hint(dst: MachineConfig, dst_is_local: bool, dst_cwd: str, sid: str) -> str:
    inner = f"cd {shlex.quote(dst_cwd)} && claude --resume {sid}"
    if dst_is_local or not dst.ssh:
        return inner
    return f"ssh -t {shlex.quote(dst.ssh)} {shlex.quote(inner)}"


def _attach_interactive(
    dst: MachineConfig,
    dst_is_local: bool,
    dst_cwd: str,
    session_id: str,
    claude_args: list[str],
    initial_prompt: str | None,
) -> None:
    """Replace current process with an interactive `claude --resume` session (local or over SSH -t)."""
    exe = dst.claude_bin or "claude"
    c_argv = [exe, "--resume", session_id, *claude_args]
    if initial_prompt:
        c_argv.append(initial_prompt)

    if dst_is_local:
        os.chdir(dst_cwd)
        os.execvp(exe, c_argv)
    elif dst.ssh:
        quoted_claude = " ".join(shlex.quote(a) for a in c_argv)
        remote_cmd = (
            f'export PATH="$HOME/.local/bin:/usr/local/bin:$PATH"; '
            f"cd {shlex.quote(dst_cwd)} && exec {quoted_claude}"
        )
        os.execvp("ssh", ["ssh", "-t", dst.ssh, remote_cmd])


def move_session(
    cfg: AppConfig,
    session: SessionRecord,
    src: MachineConfig,
    dst: MachineConfig,
    src_is_local: bool,
    dst_is_local: bool,
    opts: MoveOptions,
    log: Any = print,
) -> MoveResult:
    """Execute or dry-run a complete Claude Code session migration."""
    project_pairs, project_names = resolve_project_pairs(session, src, dst, opts)
    mapper = PathMapper(
        src_home=src.norm_home,
        dst_home=dst.norm_home,
        src_projects_dir=src.norm_projects_dir,
        dst_projects_dir=dst.norm_projects_dir,
        project_pairs=project_pairs,
        project_names=project_names,
    )
    dst_cwd = mapper.map_cwd(session.cwd)
    dst_enc = encode_cwd(dst_cwd)
    effective_launcher = (
        "none"
        if opts.no_launch
        else (opts.launcher if opts.launcher != "auto" else (dst.launcher or cfg.launcher or "auto"))
    )

    log(f"Session:   {session.session_id} ({session.display_title})")
    log(f"Route:     {src.name} ({session.cwd})  ──▶  {dst.name} ({dst_cwd})")
    if project_pairs:
        for sp, dp in project_pairs:
            log(f"Project:   {sp}  ──▶  {dp}")
    else:
        log("Project:   (none — session transcript & state only)")

    if opts.dry_run:
        log(f"Transcript: ~/.claude/projects/{session.encoded_cwd}/{session.session_id}.jsonl")
        log(f"         ──▶ ~/.claude/projects/{dst_enc}/{session.session_id}.jsonl")
        if opts.attach:
            log(f"Launch:    would attach interactively on {dst.name} in {dst_cwd}")
        elif effective_launcher != "none":
            log(
                f"Launch:    launcher={effective_launcher} on {dst.name} in {dst_cwd} "
                f"(claude --resume {session.session_id})"
            )
        return MoveResult(
            session_id=session.session_id,
            title=session.display_title,
            src_machine=src.name,
            dst_machine=dst.name,
            src_cwd=session.cwd,
            dst_cwd=dst_cwd,
            project_pairs=project_pairs,
            rewritten_lines=0,
            companion_files=0,
            dry_run=True,
        )

    # 1. Gracefully stop live source session if needed
    stop_source_session_if_needed(session, src, src_is_local, opts, log=log)

    # 2. Sync project working tree(s)
    excludes = [] if opts.include_deps else list(cfg.exclude)
    excludes.extend(opts.extra_excludes)
    for sp, dp in project_pairs:
        log(f"  → Syncing project {os.path.basename(sp)} via rsync…")
        sync_project_directory(
            sp,
            dp,
            src=src,
            dst=dst,
            src_is_local=src_is_local,
            dst_is_local=dst_is_local,
            excludes=excludes,
            delete=opts.delete,
        )

    # 3. Fetch, rewrite paths, and deploy Claude session state
    log("  → Transferring & rewriting Claude session state…")
    dst_account = _read_destination_account(dst, dst_is_local)
    rewritten_lines = 0
    companion_files = 0

    with tempfile.TemporaryDirectory(prefix="claude-move-") as tmp:
        tmp_path = Path(tmp)
        raw_dir = tmp_path / "raw"
        out_dir = tmp_path / "out"
        raw_dir.mkdir()
        out_dir.mkdir()

        _fetch_source_artifacts(session, src, src_is_local, raw_dir)

        sid = session.session_id
        src_enc = session.encoded_cwd
        src_jsonl = raw_dir / ".claude" / "projects" / src_enc / f"{sid}.jsonl"
        if not src_jsonl.is_file():
            raise RuntimeError(f"Source transcript not found: {src_jsonl}")

        dst_jsonl = out_dir / ".claude" / "projects" / dst_enc / f"{sid}.jsonl"
        rewritten_lines = rewrite_jsonl_file(
            src_jsonl, dst_jsonl, mapper=mapper, src_cwd=session.cwd, dst_account=dst_account
        )

        src_comp = raw_dir / ".claude" / "projects" / src_enc / sid
        dst_comp = out_dir / ".claude" / "projects" / dst_enc / sid
        companion_files = rewrite_session_companion_dir(
            src_comp, dst_comp, mapper=mapper, src_cwd=session.cwd, dst_account=dst_account
        )

        src_mem = raw_dir / ".claude" / "projects" / src_enc / "memory"
        if src_mem.is_dir():
            dst_mem = out_dir / ".claude" / "projects" / dst_enc / "memory"
            rewrite_session_companion_dir(
                src_mem, dst_mem, mapper=mapper, src_cwd=session.cwd, dst_account=dst_account
            )

        src_fh = raw_dir / ".claude" / "file-history" / sid
        if src_fh.is_dir():
            dst_fh = out_dir / ".claude" / "file-history" / sid
            shutil.copytree(src_fh, dst_fh, dirs_exist_ok=True)

        src_se = raw_dir / ".claude" / "session-env" / sid
        if src_se.is_dir():
            dst_se = out_dir / ".claude" / "session-env" / sid
            rewrite_session_companion_dir(
                src_se, dst_se, mapper=mapper, src_cwd=session.cwd, dst_account=dst_account
            )

        src_cproj = raw_dir / ".cache" / "cproj" / "sessions" / f"{sid}.json"
        if src_cproj.is_file():
            dst_cproj = out_dir / ".cache" / "cproj" / "sessions" / f"{sid}.json"
            dst_cproj.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src_cproj, dst_cproj)

        _deploy_staged_artifacts(out_dir, dst, dst_is_local, dst_cwd)

    log(f"  ✓ Rewrote {rewritten_lines} transcript lines (+ {companion_files} companion files)")

    if opts.attach:
        log(f"  → Attaching interactively to {dst.name}…")
        _attach_interactive(
            dst=dst,
            dst_is_local=dst_is_local,
            dst_cwd=dst_cwd,
            session_id=session.session_id,
            claude_args=opts.claude_args,
            initial_prompt=opts.initial_prompt,
        )

    # 4. Launch resumed session in destination multiplexer (Herdr -> tmux -> manual hint)
    launch_res: HerdrLaunchResult | None = None
    tab_title = (
        session.tab_label or session.title or session.cproj_key or f"Claude {session.short_id}"
    )

    if effective_launcher == "none":
        log(f"  ✓ Ready on {dst.name}: {_manual_resume_hint(dst, dst_is_local, dst_cwd, session.session_id)}")
    elif effective_launcher == "herdr":
        log(f"  → Opening Herdr tab on {dst.name} and starting claude --resume {session.short_id}…")
        launch_res = launch_resumed_claude(
            machine=dst,
            is_local=dst_is_local,
            session_id=session.session_id,
            dst_cwd=dst_cwd,
            tab_label=tab_title,
            workspace_id=opts.workspace_id,
            focus=opts.focus,
            claude_args=opts.claude_args,
            initial_prompt=opts.initial_prompt,
        )
        log(
            f"  ✓ Running in Herdr on {dst.name}: tab {launch_res.tab_id} · pane {launch_res.pane_id} "
            f"· agent {launch_res.agent_name} ({launch_res.agent_status})"
        )
    elif effective_launcher == "tmux":
        log(f"  → Opening tmux window on {dst.name} and starting claude --resume {session.short_id}…")
        launch_res = launch_resumed_claude_tmux(
            machine=dst,
            is_local=dst_is_local,
            session_id=session.session_id,
            dst_cwd=dst_cwd,
            tab_label=tab_title,
            claude_args=opts.claude_args,
            initial_prompt=opts.initial_prompt,
        )
        log(f"  ✓ Running in tmux on {dst.name}: window {launch_res.tab_id} ({launch_res.agent_status})")
    else:
        # "auto": try Herdr first, then active tmux session, then print manual SSH/resume command
        if is_herdr_reachable(dst, dst_is_local):
            log(f"  → Opening Herdr tab on {dst.name} and starting claude --resume {session.short_id}…")
            launch_res = launch_resumed_claude(
                machine=dst,
                is_local=dst_is_local,
                session_id=session.session_id,
                dst_cwd=dst_cwd,
                tab_label=tab_title,
                workspace_id=opts.workspace_id,
                focus=opts.focus,
                claude_args=opts.claude_args,
                initial_prompt=opts.initial_prompt,
            )
            log(
                f"  ✓ Running in Herdr on {dst.name}: tab {launch_res.tab_id} · pane {launch_res.pane_id} "
                f"· agent {launch_res.agent_name} ({launch_res.agent_status})"
            )
        elif is_tmux_running(dst, dst_is_local):
            log(f"  → Opening tmux window on {dst.name} and starting claude --resume {session.short_id}…")
            launch_res = launch_resumed_claude_tmux(
                machine=dst,
                is_local=dst_is_local,
                session_id=session.session_id,
                dst_cwd=dst_cwd,
                tab_label=tab_title,
                claude_args=opts.claude_args,
                initial_prompt=opts.initial_prompt,
            )
            log(f"  ✓ Running in tmux on {dst.name}: window {launch_res.tab_id} ({launch_res.agent_status})")
        else:
            log(
                f"  ✓ Migrated to {dst.name}. Resume anytime (or pass --launcher tmux / --attach):\n"
                f"    {_manual_resume_hint(dst, dst_is_local, dst_cwd, session.session_id)}"
            )

    return MoveResult(
        session_id=session.session_id,
        title=session.display_title,
        src_machine=src.name,
        dst_machine=dst.name,
        src_cwd=session.cwd,
        dst_cwd=dst_cwd,
        project_pairs=project_pairs,
        rewritten_lines=rewritten_lines,
        companion_files=companion_files,
        herdr_launch=launch_res,
    )
