"""Multiplexer integration: Herdr (local, --machine, socket, or SSH) and tmux fallback."""

from __future__ import annotations

import json
import os
import re
import shlex
import shutil
import subprocess
import time
from dataclasses import dataclass
from typing import Any

from .config import MachineConfig


@dataclass
class HerdrLaunchResult:
    backend: str  # "herdr" or "tmux"
    workspace_id: str
    tab_id: str
    pane_id: str
    agent_name: str
    agent_status: str


def _local_herdr_bin() -> str | None:
    env_bin = os.environ.get("HERDR_BIN_PATH")
    if env_bin and os.path.isfile(env_bin):
        return env_bin
    found = shutil.which("herdr")
    if found:
        return found
    fallback = os.path.expanduser("~/.local/bin/herdr")
    return fallback if os.path.isfile(fallback) else None


def run_herdr(
    machine: MachineConfig,
    is_local: bool,
    args: list[str],
    timeout: int = 30,
) -> dict[str, Any]:
    """Invoke a herdr CLI subcommand against a local or remote Herdr server and parse JSON."""
    herdr_bin = _local_herdr_bin()
    env = dict(os.environ)

    if is_local:
        if not herdr_bin:
            raise RuntimeError("herdr binary is not installed locally")
        cmd = [herdr_bin, *args]
    elif machine.herdr_machine:
        if not herdr_bin:
            raise RuntimeError("herdr binary is not installed locally (needed for --machine)")
        # Strip inherited caller pane/socket overrides so `--machine` targets the remote server cleanly
        for k in (
            "HERDR_SOCKET_PATH",
            "HERDR_PANE_ID",
            "HERDR_TAB_ID",
            "HERDR_WORKSPACE_ID",
        ):
            env.pop(k, None)
        cmd = [herdr_bin, "--machine", machine.herdr_machine, *args]
    elif machine.herdr_socket and os.path.exists(machine.herdr_socket):
        if not herdr_bin:
            raise RuntimeError("herdr binary is not installed locally")
        env["HERDR_SOCKET_PATH"] = machine.herdr_socket
        cmd = [herdr_bin, *args]
    elif machine.reverse_herdr_socket and os.path.exists(machine.reverse_herdr_socket):
        if not herdr_bin:
            raise RuntimeError("herdr binary is not installed locally")
        env["HERDR_SOCKET_PATH"] = machine.reverse_herdr_socket
        cmd = [herdr_bin, *args]
    elif machine.ssh:
        # Works with standard upstream Herdr over plain SSH without any `herdr machine` profile!
        quoted = " ".join(shlex.quote(a) for a in args)
        remote_cmd = f'PATH="$HOME/.local/bin:/usr/local/bin:$PATH" herdr {quoted}'
        cmd = ["ssh", "-o", "BatchMode=yes", machine.ssh, remote_cmd]
    else:
        raise RuntimeError(f"No Herdr route available for machine {machine.name!r}")

    proc = subprocess.run(cmd, capture_output=True, text=True, env=env, timeout=timeout)
    if proc.returncode != 0:
        err_raw = (proc.stderr or proc.stdout).strip()
        raise RuntimeError(f"herdr {' '.join(args[:2])} failed on {machine.name}: {err_raw}")

    stdout = proc.stdout.strip()
    if not stdout:
        return {}
    try:
        parsed = json.loads(stdout)
    except json.JSONDecodeError:
        return {"raw": stdout}
    if isinstance(parsed, dict) and isinstance(parsed.get("result"), dict):
        return parsed["result"]
    return parsed if isinstance(parsed, dict) else {"data": parsed}


def is_herdr_reachable(machine: MachineConfig, is_local: bool) -> bool:
    try:
        res = run_herdr(machine, is_local, ["workspace", "list"], timeout=8)
        return isinstance(res.get("workspaces"), list)
    except Exception:
        return False


def list_workspaces(machine: MachineConfig, is_local: bool) -> list[dict[str, Any]]:
    res = run_herdr(machine, is_local, ["workspace", "list"], timeout=10)
    return list(res.get("workspaces") or [])


def list_agents(machine: MachineConfig, is_local: bool) -> list[dict[str, Any]]:
    res = run_herdr(machine, is_local, ["agent", "list"], timeout=10)
    return list(res.get("agents") or [])


def list_tabs(
    machine: MachineConfig, is_local: bool, workspace_id: str | None = None
) -> list[dict[str, Any]]:
    cmd = ["tab", "list"]
    if workspace_id:
        cmd += ["--workspace", workspace_id]
    res = run_herdr(machine, is_local, cmd, timeout=10)
    return list(res.get("tabs") or [])


def interrupt_agent(machine: MachineConfig, is_local: bool, target: str) -> None:
    run_herdr(machine, is_local, ["agent", "send-keys", target, "esc"], timeout=10)


def close_tab(machine: MachineConfig, is_local: bool, tab_id: str) -> None:
    run_herdr(machine, is_local, ["tab", "close", tab_id], timeout=10)


def _sanitize_agent_name(base: str, existing_names: set[str]) -> str:
    slug = re.sub(r"[^a-z0-9_-]", "-", base.lower()).strip("-_")
    if not slug or not slug[0].isalpha():
        slug = f"c-{slug}" if slug else "claude-moved"
    slug = slug[:28]
    if slug not in existing_names:
        return slug
    for idx in range(2, 100):
        cand = f"{slug[:26]}-{idx}"
        if cand not in existing_names:
            return cand
    return f"c-{int(time.time()) % 100000}"


def launch_resumed_claude(
    machine: MachineConfig,
    is_local: bool,
    session_id: str,
    dst_cwd: str,
    tab_label: str,
    workspace_id: str | None = None,
    focus: bool = False,
    claude_args: list[str] | None = None,
    initial_prompt: str | None = None,
) -> HerdrLaunchResult:
    """Create a new Herdr tab on the destination machine and start `claude --resume <UUID>`."""
    workspaces = list_workspaces(machine, is_local)
    if not workspaces:
        raise RuntimeError(f"No Herdr workspaces found on {machine.name}")

    ws_id = workspace_id
    if not ws_id:
        focused_ws = next((w for w in workspaces if w.get("focused")), workspaces[0])
        ws_id = str(focused_ws["workspace_id"])

    clean_label = (tab_label or f"Claude {session_id[:8]}").strip()[:48]
    dst_path_env = (
        f"PATH={machine.norm_home}/.local/bin:/usr/local/bin:/usr/bin:/bin"
        if machine.home
        else None
    )

    tab_cmd = [
        "tab",
        "create",
        "--workspace",
        ws_id,
        "--cwd",
        dst_cwd,
        "--label",
        clean_label,
        "--focus" if focus else "--no-focus",
    ]
    if dst_path_env:
        tab_cmd += ["--env", dst_path_env]

    tab_res = run_herdr(machine, is_local, tab_cmd, timeout=15)
    tab_info = tab_res.get("tab") or {}
    pane_info = tab_res.get("root_pane") or {}
    tab_id = str(tab_info.get("tab_id") or "")
    pane_id = str(pane_info.get("pane_id") or "")
    if not tab_id or not pane_id:
        raise RuntimeError(f"Unexpected herdr tab create response: {tab_res}")

    existing_agents = list_agents(machine, is_local)
    existing_names = {str(a.get("name")) for a in existing_agents if a.get("name")}
    agent_name = _sanitize_agent_name(f"claude-{session_id[:8]}", existing_names)

    native_args = ["--resume", session_id]
    if claude_args:
        native_args.extend(claude_args)

    start_cmd = [
        "agent",
        "start",
        agent_name,
        "--kind",
        "claude",
        "--pane",
        pane_id,
        "--timeout",
        "45000",
        "--",
        *native_args,
    ]

    last_err: Exception | None = None
    agent_info: dict[str, Any] = {}
    for attempt in range(10):
        try:
            start_res = run_herdr(machine, is_local, start_cmd, timeout=55)
            agent_info = start_res.get("agent") or start_res
            last_err = None
            break
        except RuntimeError as exc:
            msg = str(exc)
            last_err = exc
            if ("target_busy" in msg or "target_unavailable" in msg) and attempt < 9:
                time.sleep(0.4)
                continue
            raise

    if last_err is not None:
        raise last_err

    try:
        run_herdr(machine, is_local, ["tab", "rename", tab_id, clean_label], timeout=8)
    except Exception:
        pass

    status = str(agent_info.get("agent_status") or "idle")

    if initial_prompt:
        run_herdr(
            machine,
            is_local,
            ["agent", "prompt", agent_name, initial_prompt],
            timeout=20,
        )
        status = "working"

    return HerdrLaunchResult(
        backend="herdr",
        workspace_id=ws_id,
        tab_id=tab_id,
        pane_id=pane_id,
        agent_name=agent_name,
        agent_status=status,
    )


def is_tmux_running(machine: MachineConfig, is_local: bool) -> bool:
    """Return True if a tmux server currently has at least one active session on `machine`."""
    try:
        if is_local:
            if not shutil.which("tmux"):
                return False
            proc = subprocess.run(
                ["tmux", "list-sessions"], capture_output=True, text=True, timeout=5
            )
            return proc.returncode == 0 and bool(proc.stdout.strip())
        if not machine.ssh:
            return False
        proc = subprocess.run(
            ["ssh", "-o", "BatchMode=yes", machine.ssh, "tmux list-sessions"],
            capture_output=True,
            text=True,
            timeout=8,
        )
        return proc.returncode == 0 and bool(proc.stdout.strip())
    except Exception:
        return False


def build_tmux_launch_shell_cmd(
    session_id: str,
    dst_cwd: str,
    window_label: str,
    claude_bin: str | None = None,
    claude_args: list[str] | None = None,
    initial_prompt: str | None = None,
) -> str:
    """Build a POSIX shell command that launches `claude --resume` in a new or existing tmux session."""
    exe = claude_bin or "claude"
    c_argv = [exe, "--resume", session_id, *(claude_args or [])]
    if initial_prompt:
        c_argv.append(initial_prompt)
    inner_claude = " ".join(shlex.quote(a) for a in c_argv)
    # Keep interactive shell available after claude exits
    pane_cmd = f'export PATH="$HOME/.local/bin:/usr/local/bin:$PATH"; {inner_claude}; exec "${{SHELL:-/bin/bash}}" -l'
    clean_win = re.sub(r"[\r\n\t]", " ", window_label or f"claude-{session_id[:8]}").strip()[:36]

    return (
        "if tmux list-sessions >/dev/null 2>&1; then "
        f"tmux new-window -d -P -F '#{{session_name}}:#{{window_index}}' -n {shlex.quote(clean_win)} -c {shlex.quote(dst_cwd)} {shlex.quote(pane_cmd)}; "
        "else "
        f"tmux new-session -d -P -F '#{{session_name}}:#{{window_index}}' -s claude -n {shlex.quote(clean_win)} -c {shlex.quote(dst_cwd)} {shlex.quote(pane_cmd)}; "
        "fi"
    )


def launch_resumed_claude_tmux(
    machine: MachineConfig,
    is_local: bool,
    session_id: str,
    dst_cwd: str,
    tab_label: str,
    claude_args: list[str] | None = None,
    initial_prompt: str | None = None,
) -> HerdrLaunchResult:
    """Launch `claude --resume <UUID>` inside a tmux window (for users without Herdr)."""
    shell_script = build_tmux_launch_shell_cmd(
        session_id=session_id,
        dst_cwd=dst_cwd,
        window_label=tab_label,
        claude_bin=machine.claude_bin,
        claude_args=claude_args,
        initial_prompt=initial_prompt,
    )
    if is_local:
        proc = subprocess.run(
            ["sh", "-c", shell_script], capture_output=True, text=True, timeout=15
        )
    elif machine.ssh:
        proc = subprocess.run(
            ["ssh", "-o", "BatchMode=yes", machine.ssh, shell_script],
            capture_output=True,
            text=True,
            timeout=15,
        )
    else:
        raise RuntimeError(f"Cannot launch tmux on {machine.name}: no SSH route")

    if proc.returncode != 0:
        err = (proc.stderr or proc.stdout).strip()
        raise RuntimeError(f"tmux launch failed on {machine.name}: {err}")

    target_win = (proc.stdout.strip().splitlines() or ["claude:0"])[-1]
    sess_name, _, win_idx = target_win.partition(":")
    return HerdrLaunchResult(
        backend="tmux",
        workspace_id=sess_name or "claude",
        tab_id=target_win,
        pane_id=target_win,
        agent_name=f"claude-{session_id[:8]}",
        agent_status="working" if initial_prompt else "running",
    )


def relay_pull_via_peer_herdr(
    peer: MachineConfig,
    here_name: str,
    session_id: str,
    extra_cli_flags: list[str],
    timeout_sec: int = 300,
) -> tuple[int, str]:
    """Trigger `claude-move --from <here> <session_id>` on peer via forwarded Herdr socket."""
    workspaces = list_workspaces(peer, is_local=False)
    if not workspaces:
        raise RuntimeError(f"No workspaces found on {peer.name} via Herdr socket")
    ws_id = str(workspaces[0]["workspace_id"])

    tab_res = run_herdr(
        peer,
        is_local=False,
        args=["tab", "create", "--workspace", ws_id, "--label", "claude-move-relay", "--no-focus"],
        timeout=15,
    )
    tab_id = str((tab_res.get("tab") or {}).get("tab_id") or "")
    pane_id = str((tab_res.get("root_pane") or {}).get("pane_id") or "")
    if not tab_id or not pane_id:
        raise RuntimeError(f"Could not create relay tab on {peer.name}")

    marker_prefix = f"__CLAUDE_MOVE_RELAY_DONE_{os.getpid()}:"
    cmd_parts = ["claude-move", "--from", here_name, session_id, *extra_cli_flags]
    quoted_cmd = " ".join(shlex.quote(p) for p in cmd_parts)
    shell_line = (
        f'PATH="$HOME/.local/bin:$PATH" {quoted_cmd}; '
        f'printf "\\n{marker_prefix}%d__\\n" "$?"'
    )

    try:
        time.sleep(0.35)
        run_herdr(peer, is_local=False, args=["pane", "run", pane_id, shell_line], timeout=15)
        run_herdr(
            peer,
            is_local=False,
            args=[
                "pane",
                "wait-output",
                pane_id,
                "--regex",
                f"{ re.escape(marker_prefix) }[0-9]+__",
                "--timeout",
                str(timeout_sec * 1000),
            ],
            timeout=timeout_sec + 10,
        )
        read_res = run_herdr(
            peer,
            is_local=False,
            args=[
                "pane",
                "read",
                pane_id,
                "--source",
                "recent-unwrapped",
                "--lines",
                "160",
                "--format",
                "text",
            ],
            timeout=15,
        )
        output_text = str(read_res.get("text") or read_res.get("raw") or "")
        m = re.search(rf"{re.escape(marker_prefix)}(\d+)__", output_text)
        rc = int(m.group(1)) if m else 1
        cleaned_lines = [
            ln
            for ln in output_text.splitlines()
            if marker_prefix not in ln and "claude-move" not in ln[:40]
        ]
        return rc, "\n".join(cleaned_lines).strip()
    finally:
        try:
            close_tab(peer, is_local=False, tab_id=tab_id)
        except Exception:
            pass
