"""Command-line interface for claude-move."""

from __future__ import annotations

import argparse
import sys

from . import __version__
from .config import AppConfig, load_config, resolve_machine, ssh_config_hosts
from .discovery import (
    discover_sessions,
    format_session_line,
    pick_session_interactive,
    resolve_session,
)
from .herdr_ctl import is_herdr_reachable, relay_pull_via_peer_herdr
from .selfdetach import detach, needs_detach
from .transfer import MoveOptions, move_session


def _looks_like_machine(cfg: AppConfig, token: str) -> bool:
    t = token.strip().lower()
    if t in cfg.machines:
        return True
    for m in cfg.machines.values():
        if (m.herdr_machine and m.herdr_machine.lower() == t) or (
            m.ssh and m.ssh.lower() == t
        ):
            return True
    if "@" in token:
        return True
    if t in ssh_config_hosts():
        return True
    return False


def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="claude-move",
        description="Move Claude Code sessions between machines (sync working tree, rewrite paths, resume in Herdr/tmux/SSH).",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""Examples:
  claude-move user@myserver           Zero-config: pick a local session and move to user@myserver
  claude-move                         Interactive picker -> move local session to default_target
  claude-move server a30a98df         Push session a30a98df to 'server' and resume
  claude-move --from server           Interactive picker of sessions on 'server' -> pull here
  claude-move server a30a98df --launcher tmux   Resume inside a remote tmux window
  claude-move server a30a98df --attach          Attach interactively over SSH right after syncing
  claude-move ls [server]             List live and recent Claude sessions locally or on 'server'
  claude-move -n server a30a98df      Dry-run: preview rsync + path rewrite plan without changes
""",
    )
    p.add_argument("--version", action="version", version=f"claude-move {__version__}")
    p.add_argument(
        "-f",
        "--from",
        dest="from_machine",
        metavar="MACHINE",
        help="Pull a session FROM this remote machine to the current machine",
    )
    p.add_argument(
        "-t",
        "--to",
        dest="to_machine",
        metavar="MACHINE",
        help="Push a session TO this target machine",
    )
    p.add_argument(
        "-n",
        "--dry-run",
        action="store_true",
        help="Preview project sync, path rewrites, and launch plan without changing anything",
    )
    p.add_argument(
        "-c",
        "--continue",
        dest="auto_continue",
        action="store_true",
        help="Send a continuation wake-up prompt right after resuming on the target machine",
    )
    p.add_argument(
        "-p",
        "--prompt",
        metavar="TEXT",
        help="Send a custom prompt right after resuming on the target machine",
    )
    p.add_argument(
        "--project",
        action="append",
        default=[],
        metavar="DIR",
        help="Explicit project directory to sync (repeatable; overrides auto-detection)",
    )
    p.add_argument(
        "--all-projects",
        action="store_true",
        help="Sync all project directories referenced in the transcript (default: primary project only)",
    )
    p.add_argument(
        "--no-project",
        action="store_true",
        help="Skip project working tree rsync (move Claude transcript & state only)",
    )
    p.add_argument(
        "--include-deps",
        action="store_true",
        help="Include node_modules/, .venv/, target/, etc. when syncing project directories",
    )
    p.add_argument(
        "--exclude",
        action="append",
        default=[],
        metavar="PATTERN",
        help="Additional rsync exclude pattern (repeatable)",
    )
    p.add_argument(
        "--delete",
        action="store_true",
        help="Pass --delete to project rsync",
    )
    p.add_argument(
        "--stop",
        action="store_true",
        help="If the source session is currently mid-turn (working), send Esc and stop it cleanly",
    )
    p.add_argument(
        "--force",
        action="store_true",
        help="Proceed even if the source session appears active",
    )
    p.add_argument(
        "--keep-running",
        action="store_true",
        help="Keep the source Claude process running (fork/clone session instead of moving)",
    )
    p.add_argument(
        "--close-tab",
        action="store_true",
        help="Close the source Herdr tab after stopping the source Claude session",
    )
    p.add_argument(
        "--launcher",
        choices=["auto", "herdr", "tmux", "none"],
        default="auto",
        help="Where to launch resumed Claude on target: auto (Herdr -> tmux -> manual), herdr, tmux, or none",
    )
    p.add_argument(
        "--no-launch",
        action="store_true",
        help="Sync and rewrite files only; do not launch Claude on the destination",
    )
    p.add_argument(
        "-a",
        "--attach",
        action="store_true",
        help="Interactively attach (`ssh -t ... claude --resume`) right after migrating",
    )
    p.add_argument(
        "--focus",
        action="store_true",
        help="Focus the newly created Herdr tab on the destination machine",
    )
    p.add_argument(
        "--workspace",
        metavar="WS_ID",
        help="Target Herdr workspace ID (defaults to focused/first workspace)",
    )
    p.add_argument(
        "--skip-permissions",
        action="store_true",
        help="Pass --dangerously-skip-permissions to the resumed Claude process",
    )
    p.add_argument(
        "--claude-arg",
        action="append",
        default=[],
        metavar="ARG",
        help="Additional CLI argument passed to `claude --resume` (repeatable)",
    )
    p.add_argument(
        "--no-machine-args",
        action="store_true",
        help="Ignore default claude_args configured on the destination machine",
    )
    p.add_argument(
        "--limit",
        type=int,
        default=30,
        help="Maximum number of recent sessions to scan (default: 30)",
    )
    p.add_argument(
        "positional",
        nargs="*",
        help="[ls|push|pull] [machine] [session-selector]",
    )
    return p


def _forwardable_flags(ns: argparse.Namespace) -> list[str]:
    """Reconstruct CLI flags when relaying a pull request to a peer machine over Herdr socket."""
    flags: list[str] = []
    if ns.dry_run:
        flags.append("--dry-run")
    if ns.auto_continue:
        flags.append("--continue")
    if ns.prompt:
        flags += ["--prompt", ns.prompt]
    for pr in ns.project:
        flags += ["--project", pr]
    if ns.all_projects:
        flags.append("--all-projects")
    if ns.no_project:
        flags.append("--no-project")
    if ns.include_deps:
        flags.append("--include-deps")
    for ex in ns.exclude:
        flags += ["--exclude", ex]
    if ns.delete:
        flags.append("--delete")
    if ns.stop:
        flags.append("--stop")
    if ns.force:
        flags.append("--force")
    if ns.keep_running:
        flags.append("--keep-running")
    if ns.close_tab:
        flags.append("--close-tab")
    if ns.launcher != "auto":
        flags += ["--launcher", ns.launcher]
    if ns.no_launch:
        flags.append("--no-launch")
    if ns.focus:
        flags.append("--focus")
    if ns.workspace:
        flags += ["--workspace", ns.workspace]
    if ns.skip_permissions:
        flags.append("--skip-permissions")
    if ns.no_machine_args:
        flags.append("--no-machine-args")
    for ca in ns.claude_arg:
        flags += ["--claude-arg", ca]
    return flags


def main(argv: list[str] | None = None) -> int:
    parser = _build_parser()
    ns = parser.parse_args(argv)
    cfg = load_config()
    pos = list(ns.positional)

    # Subcommand: ls / list [machine]
    if pos and pos[0] in ("ls", "list"):
        target_spec = pos[1] if len(pos) > 1 else (ns.from_machine or ns.to_machine or cfg.here)
        target_m = resolve_machine(cfg, target_spec)
        is_loc = target_m.name == cfg.here
        sessions = discover_sessions(target_m, is_local=is_loc, limit=ns.limit)
        if not sessions:
            print(f"No Claude sessions found on {target_m.name}.")
            return 0
        print(f"Sessions on {target_m.name} ({len(sessions)}):\n")
        for s in sessions:
            print(f"  {format_session_line(s, target_m.norm_home)}")
        return 0

    mode = "pull" if ns.from_machine else "push"
    remote_spec: str | None = ns.from_machine if mode == "pull" else ns.to_machine
    selector: str | None = None
    local_m = cfg.machines[cfg.here]
    cached_local_sessions = None

    if pos and pos[0] in ("pull", "push", "to"):
        verb = pos.pop(0)
        if verb == "pull":
            mode = "pull"

    if mode == "pull":
        if not remote_spec:
            if not pos:
                remote_spec = cfg.default_target
            else:
                remote_spec = pos.pop(0)
        if pos:
            selector = pos.pop(0)
        if not remote_spec:
            parser.error("Specify which machine to pull from (e.g. `claude-move --from user@host`)")
    else:
        if len(pos) >= 2:
            remote_spec = remote_spec or pos[0]
            selector = pos[1]
        elif len(pos) == 1:
            tok = pos[0]
            if remote_spec:
                selector = tok
            elif _looks_like_machine(cfg, tok):
                remote_spec = tok
            elif cfg.default_target:
                # Check if `tok` matches a local session; if not, treat it as an ad-hoc SSH hostname!
                cached_local_sessions = discover_sessions(local_m, is_local=True, limit=ns.limit)
                try:
                    resolve_session(cached_local_sessions, tok)
                    remote_spec = cfg.default_target
                    selector = tok
                except LookupError:
                    remote_spec = tok
            else:
                remote_spec = tok
        else:
            remote_spec = remote_spec or cfg.default_target

        if not remote_spec:
            parser.error(
                "Specify a target SSH host (e.g. `claude-move user@server`) or set `default_target` in ~/.config/claude-move/config.json"
            )

    claude_args = list(ns.claude_arg)
    if ns.skip_permissions and "--dangerously-skip-permissions" not in claude_args:
        claude_args.append("--dangerously-skip-permissions")

    prompt_text = ns.prompt
    if not prompt_text and ns.auto_continue:
        prompt_text = (
            "Session resumed on a new machine via claude-move. "
            "Continue directly with the next open step."
        )

    opts = MoveOptions(
        dry_run=ns.dry_run,
        explicit_projects=list(ns.project),
        all_projects=ns.all_projects,
        no_project=ns.no_project,
        include_deps=ns.include_deps,
        extra_excludes=list(ns.exclude),
        delete=ns.delete,
        stop_working=ns.stop,
        force=ns.force,
        keep_running=ns.keep_running,
        close_source_tab=ns.close_tab,
        launcher=ns.launcher,
        no_launch=ns.no_launch,
        attach=ns.attach,
        focus=ns.focus,
        workspace_id=ns.workspace,
        claude_args=claude_args,
        initial_prompt=prompt_text,
    )

    try:
        if mode == "pull":
            src_m = resolve_machine(cfg, remote_spec)
            dst_m = local_m
            sessions = discover_sessions(src_m, is_local=False, limit=ns.limit)
            if not sessions:
                print(f"No sessions found on {src_m.name}.", file=sys.stderr)
                return 1
            chosen = (
                resolve_session(sessions, selector)
                if selector
                else pick_session_interactive(sessions, cfg.picker, src_m.norm_home)
            )
            if not chosen:
                return 130
            if not ns.no_machine_args:
                for ma in dst_m.claude_args:
                    if ma not in opts.claude_args:
                        opts.claude_args.append(ma)
            move_session(
                cfg=cfg,
                session=chosen,
                src=src_m,
                dst=dst_m,
                src_is_local=False,
                dst_is_local=True,
                opts=opts,
            )
            return 0

        # Push mode: local -> dst_m
        dst_m = resolve_machine(cfg, remote_spec, probe_if_adhoc=True)
        if not ns.no_machine_args:
            for ma in dst_m.claude_args:
                if ma not in opts.claude_args:
                    opts.claude_args.append(ma)
        src_m = local_m
        sessions = (
            cached_local_sessions
            if cached_local_sessions is not None
            else discover_sessions(src_m, is_local=True, limit=ns.limit)
        )
        if not sessions:
            print(f"No local Claude sessions found on {src_m.name}.", file=sys.stderr)
            return 1
        chosen = (
            resolve_session(sessions, selector)
            if selector
            else pick_session_interactive(sessions, cfg.picker, src_m.norm_home)
        )
        if not chosen:
            return 130

        # Self-move: when run from inside the very Claude session being moved (e.g. via
        # its Bash tool), stopping the source would kill us too. Continue detached.
        if needs_detach(chosen.pid, chosen.alive, opts.dry_run, opts.keep_running):
            if opts.attach:
                raise RuntimeError(
                    "--attach cannot be used from inside the session being moved "
                    "(no terminal is left to attach from)."
                )
            # The session is "working" precisely because it is running this command;
            # invoking claude-move on yourself is explicit consent to end that turn.
            opts.stop_working = ns.stop = True
            if detach():
                return 0

        # If destination has no inbound SSH (e.g. pushing from server -> NAT'd workstation),
        # relay via a forwarded Herdr socket so the workstation pulls from here!
        if not dst_m.ssh:
            if not dst_m.herdr_socket and local_m.reverse_herdr_socket:
                dst_m.herdr_socket = local_m.reverse_herdr_socket
            if is_herdr_reachable(dst_m, is_local=False):
                print(
                    f"Relaying move of {chosen.short_id} ({chosen.display_title}) to {dst_m.name} via Herdr socket…"
                )
                rc, relay_out = relay_pull_via_peer_herdr(
                    peer=dst_m,
                    here_name=src_m.name,
                    session_id=chosen.session_id,
                    extra_cli_flags=_forwardable_flags(ns),
                )
                if relay_out:
                    print(relay_out)
                return rc
            raise RuntimeError(
                f"Target {dst_m.name!r} has no inbound SSH configured and no forwarded Herdr socket is reachable."
            )

        move_session(
            cfg=cfg,
            session=chosen,
            src=src_m,
            dst=dst_m,
            src_is_local=True,
            dst_is_local=False,
            opts=opts,
        )
        return 0

    except (LookupError, ValueError, RuntimeError) as exc:
        print(f"claude-move: {exc}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    sys.exit(main())
