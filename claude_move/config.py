"""Configuration loading and machine resolution for claude-move."""

from __future__ import annotations

import json
import os
import re
import socket
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

DEFAULT_EXCLUDES: list[str] = [
    "node_modules/",
    ".venv/",
    "venv/",
    "__pycache__/",
    ".pytest_cache/",
    ".mypy_cache/",
    ".ruff_cache/",
    ".next/",
    ".nuxt/",
    ".svelte-kit/",
    "target/debug/",
    "target/release/",
    ".gradle/",
    "Library/",
    "Temp/",
]

DEFAULT_PICKER: list[str] = [
    "fzf",
    "--ansi",
    "--no-sort",
    "--reverse",
    "--height=60%",
    "--prompt=claude-move > ",
]

_COMMON_PROJECT_SUBDIRS = ("Projects", "projects", "code", "dev", "repos", "src", "work")


@dataclass
class MachineConfig:
    """Describes one machine endpoint (local or remote)."""

    name: str
    home: str
    projects_dir: str
    hostnames: list[str] = field(default_factory=list)
    ssh: str | None = None
    herdr_machine: str | None = None
    herdr_socket: str | None = None
    reverse_herdr_socket: str | None = None
    claude_bin: str | None = None
    claude_args: list[str] = field(default_factory=list)
    ignore_dirs: list[str] = field(default_factory=list)
    launcher: str | None = None
    extra_path_maps: dict[str, str] = field(default_factory=dict)

    @property
    def norm_home(self) -> str:
        return self.home.rstrip("/") or "/"

    @property
    def norm_projects_dir(self) -> str:
        return (self.projects_dir or self.home).rstrip("/") or "/"

    @property
    def projects_rel_to_home(self) -> str:
        """Relative path from home to projects_dir ('' if equal or outside)."""
        h = self.norm_home
        p = self.norm_projects_dir
        if p == h:
            return ""
        if p.startswith(h + "/"):
            return p[len(h) + 1 :]
        return ""


@dataclass
class AppConfig:
    """Top-level configuration for claude-move."""

    default_target: str | None = None
    launcher: str = "auto"
    picker: list[str] = field(default_factory=lambda: list(DEFAULT_PICKER))
    exclude: list[str] = field(default_factory=lambda: list(DEFAULT_EXCLUDES))
    ignore_dirs: list[str] = field(default_factory=list)
    machines: dict[str, MachineConfig] = field(default_factory=dict)
    here: str = "local"


def config_path() -> Path:
    override = os.environ.get("CLAUDE_MOVE_CONFIG")
    if override:
        return Path(override).expanduser()
    xdg = os.environ.get("XDG_CONFIG_HOME")
    base = Path(xdg).expanduser() if xdg else (Path.home() / ".config")
    return base / "claude-move" / "config.json"


def ssh_config_hosts(ssh_cfg_path: Path | None = None) -> set[str]:
    """Return concrete Host aliases from ~/.ssh/config (ignoring wildcard patterns)."""
    p = ssh_cfg_path or (Path.home() / ".ssh" / "config")
    hosts: set[str] = set()
    if not p.is_file():
        return hosts
    try:
        for raw in p.read_text(encoding="utf-8", errors="replace").splitlines():
            line = raw.strip()
            if not line or line.startswith("#"):
                continue
            m = re.match(r"^[Hh]ost\s+(.+)$", line)
            if not m:
                continue
            for alias in m.group(1).split():
                if "*" not in alias and "?" not in alias and not alias.startswith("!"):
                    hosts.add(alias.lower())
    except OSError:
        pass
    return hosts


def _detect_local_projects_dir(home: Path) -> str:
    for sub in _COMMON_PROJECT_SUBDIRS:
        cand = home / sub
        if cand.is_dir():
            return str(cand)
    return str(home)


def _default_local_machine(name: str = "local") -> MachineConfig:
    home = Path.home()
    return MachineConfig(
        name=name,
        home=str(home),
        projects_dir=_detect_local_projects_dir(home),
        hostnames=[socket.gethostname()],
    )


def load_config(path: Path | None = None) -> AppConfig:
    """Load configuration from disk, or construct zero-config defaults."""
    cfg_file = path or config_path()
    raw: dict[str, Any] = {}
    if cfg_file.is_file():
        with open(cfg_file, encoding="utf-8") as f:
            raw = json.load(f)

    global_ignore = [str(d) for d in (raw.get("ignore_dirs") or [])]
    global_launcher = str(raw.get("launcher") or "auto").lower()

    machines: dict[str, MachineConfig] = {}
    for key, val in (raw.get("machines") or {}).items():
        if not isinstance(val, dict):
            continue
        m_name = key.lower()
        home = val.get("home") or ""
        projects_dir = val.get("projects_dir") or home
        m_ignore = list(dict.fromkeys([*global_ignore, *(str(x) for x in (val.get("ignore_dirs") or []))]))
        machines[m_name] = MachineConfig(
            name=m_name,
            home=home,
            projects_dir=projects_dir,
            hostnames=[str(h) for h in (val.get("hostnames") or [])],
            ssh=val.get("ssh"),
            herdr_machine=val.get("herdr_machine"),
            herdr_socket=(
                os.path.expanduser(val["herdr_socket"])
                if val.get("herdr_socket")
                else None
            ),
            reverse_herdr_socket=(
                os.path.expanduser(val["reverse_herdr_socket"])
                if val.get("reverse_herdr_socket")
                else None
            ),
            claude_bin=val.get("claude_bin"),
            claude_args=[str(a) for a in (val.get("claude_args") or [])],
            ignore_dirs=m_ignore,
            launcher=str(val["launcher"]).lower() if val.get("launcher") else None,
            extra_path_maps=dict(val.get("extra_path_maps") or {}),
        )

    hostname = socket.gethostname()
    env_here = os.environ.get("CLAUDE_MOVE_MACHINE", "").strip().lower()
    here: str | None = None
    if env_here and env_here in machines:
        here = env_here
    else:
        for m_name, m_cfg in machines.items():
            if hostname in m_cfg.hostnames or hostname.lower() == m_name:
                here = m_name
                break

    if not here:
        here = "local"
        if "local" not in machines:
            m = _default_local_machine("local")
            m.ignore_dirs = list(global_ignore)
            machines["local"] = m
    else:
        if not machines[here].home:
            machines[here].home = str(Path.home())
        if not machines[here].projects_dir:
            machines[here].projects_dir = _detect_local_projects_dir(Path(machines[here].home))

    default_target = raw.get("default_target")
    if isinstance(default_target, str):
        default_target = default_target.lower()
    if default_target == here:
        peers = [m for m in machines if m != here]
        default_target = peers[0] if peers else None
    elif not default_target:
        peers = [m for m in machines if m != here]
        if len(peers) == 1:
            default_target = peers[0]

    picker = raw.get("picker") if isinstance(raw.get("picker"), list) else list(DEFAULT_PICKER)
    exclude = raw.get("exclude") if isinstance(raw.get("exclude"), list) else list(DEFAULT_EXCLUDES)

    return AppConfig(
        default_target=default_target,
        launcher=global_launcher,
        picker=[str(x) for x in picker],
        exclude=[str(x) for x in exclude],
        ignore_dirs=global_ignore,
        machines=machines,
        here=here,
    )


def probe_remote_home(ssh_target: str, timeout: int = 8) -> tuple[str, str]:
    """Query $HOME and common project directories over SSH for zero-config hosts."""
    candidates = " ".join(_COMMON_PROJECT_SUBDIRS)
    script = (
        'printf "%s\\n" "$HOME"; '
        f"for d in {candidates}; do "
        'if [ -d "$HOME/$d" ]; then printf "%s\\n" "$HOME/$d"; exit 0; fi; '
        "done; "
        'printf "%s\\n" "$HOME"'
    )
    proc = subprocess.run(
        ["ssh", "-o", "BatchMode=yes", "-o", f"ConnectTimeout={timeout}", ssh_target, script],
        capture_output=True,
        text=True,
        timeout=timeout + 3,
    )
    if proc.returncode != 0:
        err = (proc.stderr or proc.stdout).strip()
        raise RuntimeError(f"SSH connection to {ssh_target!r} failed: {err}")
    lines = [ln.strip() for ln in proc.stdout.splitlines() if ln.strip()]
    if not lines:
        raise RuntimeError(f"Could not determine $HOME on {ssh_target!r}")
    remote_home = lines[0]
    projects_dir = lines[1] if len(lines) > 1 else remote_home
    return remote_home, projects_dir


def resolve_machine(cfg: AppConfig, spec: str, probe_if_adhoc: bool = True) -> MachineConfig:
    """Resolve a machine name or ad-hoc SSH host to a MachineConfig."""
    key = spec.strip().lower()
    if key in cfg.machines:
        m = cfg.machines[key]
        if m.name != cfg.here and not m.home and m.ssh and probe_if_adhoc:
            m.home, m.projects_dir = probe_remote_home(m.ssh)
        return m

    for m in cfg.machines.values():
        if (m.herdr_machine and m.herdr_machine.lower() == key) or (
            m.ssh and m.ssh.lower() == key
        ):
            if m.name != cfg.here and not m.home and m.ssh and probe_if_adhoc:
                m.home, m.projects_dir = probe_remote_home(m.ssh)
            return m

    if probe_if_adhoc:
        r_home, r_proj = probe_remote_home(spec.strip())
    else:
        r_home, r_proj = "", ""
    return MachineConfig(
        name=spec.strip(),
        home=r_home,
        projects_dir=r_proj,
        ssh=spec.strip(),
        ignore_dirs=list(cfg.ignore_dirs),
    )
