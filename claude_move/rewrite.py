"""Lossless path and metadata rewriting for Claude Code session transcripts."""

from __future__ import annotations

import json
import os
import re
import shutil
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

# Characters allowed immediately after a path prefix so '/home/lee' never matches '/home/leeward'.
_BOUNDARY_AFTER = r"(?=$|/|\s|[\"'`:;,)\]}<>|&])"
# Characters allowed immediately before a path or tilde reference.
_BOUNDARY_BEFORE = r"(?:^|(?<=[\s\"'`=(:\[{,;<>|&]))"


def encode_cwd(cwd: str) -> str:
    """Encode a working directory path the exact way Claude Code names ~/.claude/projects/<dir>."""
    return re.sub(r"[^a-zA-Z0-9]", "-", cwd)


@dataclass
class PathMapper:
    """Maps file paths, tilde references, and relative tracking paths between two machines."""

    src_home: str
    dst_home: str
    src_projects_dir: str | None = None
    dst_projects_dir: str | None = None
    project_pairs: list[tuple[str, str]] = field(default_factory=list)
    project_names: list[str] = field(default_factory=list)

    def __post_init__(self) -> None:
        self.src_home = self.src_home.rstrip("/") or "/"
        self.dst_home = self.dst_home.rstrip("/") or "/"
        self.src_projects_dir = (self.src_projects_dir or self.src_home).rstrip("/") or "/"
        self.dst_projects_dir = (self.dst_projects_dir or self.dst_home).rstrip("/") or "/"
        self._rules: list[tuple[re.Pattern[str], str]] = []
        self._build_rules()

    def _rel_to_home(self, home: str, path: str) -> str | None:
        if path == home:
            return ""
        if path.startswith(home + "/"):
            return path[len(home) + 1 :]
        return None

    def _build_rules(self) -> None:
        raw_pairs: list[tuple[str, str]] = []

        # 1. Explicit project directory pairs (e.g., ('/home/alice/Projects/app', '/home/server/app'))
        for s_proj, d_proj in self.project_pairs:
            sp = s_proj.rstrip("/")
            dp = d_proj.rstrip("/")
            if sp and dp and sp != dp:
                raw_pairs.append((sp, dp))

        # 2. Named projects when src_projects_dir == src_home and dst_projects_dir != dst_home
        #    (e.g., '/home/server/app' -> '/home/alice/Projects/app' without touching '/home/server/.claude')
        for p_name in self.project_names:
            clean = p_name.strip("/")
            if not clean or clean.startswith("."):
                continue
            sp = f"{self.src_projects_dir}/{clean}"
            dp = f"{self.dst_projects_dir}/{clean}"
            if sp != dp:
                raw_pairs.append((sp, dp))

        # 3. Generic projects_dir mapping ONLY when src_projects_dir is distinct from src_home
        #    (e.g., '/home/alice/Projects/foo' -> '/home/server/foo').
        if (
            self.src_projects_dir != self.src_home
            and self.src_projects_dir != self.dst_projects_dir
        ):
            raw_pairs.append((self.src_projects_dir, self.dst_projects_dir))

        # 4. Home directory mapping ('/home/alice' -> '/home/server')
        if self.src_home != self.dst_home:
            raw_pairs.append((self.src_home, self.dst_home))

        # Deduplicate preserving longest source path first so specific prefixes win over general ones.
        seen_src: set[str] = set()
        ordered_pairs: list[tuple[str, str]] = []
        for sp, dp in sorted(raw_pairs, key=lambda item: len(item[0]), reverse=True):
            if sp not in seen_src:
                seen_src.add(sp)
                ordered_pairs.append((sp, dp))

        # Single combined alternation regex prevents double-rewriting when dst overlaps src!
        if ordered_pairs:
            alts = "|".join(f"(?P<p{i}>{re.escape(sp)})" for i, (sp, _) in enumerate(ordered_pairs))
            combined = re.compile(f"{alts}{_BOUNDARY_AFTER}")
            replacements = [dp for _, dp in ordered_pairs]
            self._abs_pattern: re.Pattern[str] | None = combined
            self._abs_replacements: list[str] = replacements
        else:
            self._abs_pattern = None
            self._abs_replacements = []

        # Tilde (~/) rules when projects_dir relative depth differs between machines
        src_rel = self._rel_to_home(self.src_home, self.src_projects_dir)
        dst_rel = self._rel_to_home(self.dst_home, self.dst_projects_dir)
        self._tilde_pattern: re.Pattern[str] | None = None
        self._tilde_replacements: list[str] = []

        tilde_pairs: list[tuple[str, str]] = []
        if src_rel is not None and dst_rel is not None and src_rel != dst_rel:
            if src_rel and not dst_rel:
                # e.g. '~/Projects/foo' -> '~/foo' and '~/Projects' -> '~'
                tilde_pairs.append((f"~/{src_rel}", "~"))
            elif not src_rel and dst_rel:
                # e.g. '~/app' -> '~/Projects/app' ONLY for known project names
                for p_name in sorted(set(self.project_names), key=len, reverse=True):
                    clean = p_name.strip("/")
                    if clean and not clean.startswith("."):
                        tilde_pairs.append((f"~/{clean}", f"~/{dst_rel}/{clean}"))
            elif src_rel and dst_rel:
                tilde_pairs.append((f"~/{src_rel}", f"~/{dst_rel}"))

        if tilde_pairs:
            t_alts = "|".join(
                f"(?P<t{i}>{re.escape(tp)})" for i, (tp, _) in enumerate(tilde_pairs)
            )
            self._tilde_pattern = re.compile(f"{_BOUNDARY_BEFORE}(?:{t_alts}){_BOUNDARY_AFTER}")
            self._tilde_replacements = [tr for _, tr in tilde_pairs]

        # Encoded scratchpad paths like '/tmp/claude-1000/-home-alice/...'
        src_enc_home = encode_cwd(self.src_home)
        dst_enc_home = encode_cwd(self.dst_home)
        if src_enc_home != dst_enc_home:
            self._enc_home_pair: tuple[str, str] | None = (
                f"/{src_enc_home}/",
                f"/{dst_enc_home}/",
            )
        else:
            self._enc_home_pair = None

    def map_text(self, text: str) -> str:
        """Rewrite all matching absolute and tilde paths inside an arbitrary text string."""
        if not text:
            return text
        out = text
        if self._abs_pattern is not None:

            def _sub_abs(m: re.Match[str]) -> str:
                assert m.lastgroup is not None
                idx = int(m.lastgroup[1:])
                return self._abs_replacements[idx]

            out = self._abs_pattern.sub(_sub_abs, out)

        if self._tilde_pattern is not None:

            def _sub_tilde(m: re.Match[str]) -> str:
                for idx in range(len(self._tilde_replacements)):
                    if m.group(f"t{idx}") is not None:
                        return self._tilde_replacements[idx]
                return m.group(0)

            out = self._tilde_pattern.sub(_sub_tilde, out)

        if self._enc_home_pair is not None and self._enc_home_pair[0] in out:
            out = out.replace(self._enc_home_pair[0], self._enc_home_pair[1])

        return out

    def map_cwd(self, src_cwd: str) -> str:
        """Map a session working directory to the destination machine."""
        mapped = self.map_text(src_cwd.rstrip("/") or "/")
        return mapped.rstrip("/") or "/"

    def map_tracking_path(self, tracking_path: str, src_cwd: str) -> str:
        """Rewrite relative trackingPath in file-history-delta records when cwd is home."""
        if not tracking_path or tracking_path.startswith("/"):
            return self.map_text(tracking_path)
        if (src_cwd.rstrip("/") or "/") != self.src_home:
            return tracking_path

        src_rel = self._rel_to_home(self.src_home, self.src_projects_dir)
        dst_rel = self._rel_to_home(self.dst_home, self.dst_projects_dir)
        if src_rel is None or dst_rel is None or src_rel == dst_rel:
            return tracking_path

        if src_rel and not dst_rel:
            prefix = src_rel + "/"
            if tracking_path.startswith(prefix):
                return tracking_path[len(prefix) :]
            if tracking_path == src_rel:
                return "."
        elif not src_rel and dst_rel:
            first_seg = tracking_path.split("/", 1)[0]
            if first_seg in self.project_names:
                return f"{dst_rel}/{tracking_path}"
        elif src_rel and dst_rel:
            prefix = src_rel + "/"
            if tracking_path.startswith(prefix):
                return f"{dst_rel}/{tracking_path[len(prefix):]}"
        return tracking_path


def rewrite_json_value(
    obj: Any,
    mapper: PathMapper,
    src_cwd: str,
    dst_account: dict[str, str] | None = None,
) -> Any:
    """Recursively rewrite paths inside a parsed JSON transcript record."""
    if isinstance(obj, str):
        return mapper.map_text(obj)
    if isinstance(obj, list):
        return [rewrite_json_value(item, mapper, src_cwd, dst_account) for item in obj]
    if isinstance(obj, dict):
        rec_type = obj.get("type")
        rec_cwd = obj.get("cwd") if isinstance(obj.get("cwd"), str) else src_cwd
        new_dict: dict[str, Any] = {}
        for k, v in obj.items():
            new_key = mapper.map_text(k) if isinstance(k, str) and ("/" in k or "~" in k) else k
            if k == "trackingPath" and isinstance(v, str):
                new_val: Any = mapper.map_tracking_path(v, rec_cwd)
            elif (
                rec_type == "session-owner"
                and dst_account
                and k in ("accountUuid", "organizationUuid")
                and dst_account.get(k)
            ):
                new_val = dst_account[k]
            else:
                new_val = rewrite_json_value(v, mapper, rec_cwd, dst_account)
            new_dict[new_key] = new_val
        return new_dict
    return obj


def rewrite_jsonl_file(
    src_file: Path,
    dst_file: Path,
    mapper: PathMapper,
    src_cwd: str,
    dst_account: dict[str, str] | None = None,
) -> int:
    """Rewrite a .jsonl transcript file line by line, returning line count."""
    dst_file.parent.mkdir(parents=True, exist_ok=True)
    count = 0
    with open(src_file, encoding="utf-8", errors="replace") as fin, open(
        dst_file, "w", encoding="utf-8"
    ) as fout:
        for raw_line in fin:
            stripped = raw_line.rstrip("\r\n")
            if not stripped:
                continue
            count += 1
            try:
                parsed = json.loads(stripped)
                rewritten = rewrite_json_value(parsed, mapper, src_cwd, dst_account)
                fout.write(json.dumps(rewritten, ensure_ascii=False, separators=(",", ":")) + "\n")
            except json.JSONDecodeError:
                fout.write(mapper.map_text(stripped) + "\n")
    return count


def rewrite_session_companion_dir(
    src_dir: Path,
    dst_dir: Path,
    mapper: PathMapper,
    src_cwd: str,
    dst_account: dict[str, str] | None = None,
) -> int:
    """Copy and rewrite a session companion directory (<UUID>/subagents, tool-results, etc.)."""
    if not src_dir.is_dir():
        return 0
    files_processed = 0
    for root, _, files in os.walk(src_dir):
        rel_root = Path(root).relative_to(src_dir)
        target_root = dst_dir / rel_root
        target_root.mkdir(parents=True, exist_ok=True)
        for fname in files:
            s_path = Path(root) / fname
            d_path = target_root / fname
            files_processed += 1
            if fname.endswith(".jsonl"):
                rewrite_jsonl_file(s_path, d_path, mapper, src_cwd, dst_account)
            elif fname.endswith(".json"):
                try:
                    data = json.loads(s_path.read_text(encoding="utf-8"))
                    rewritten = rewrite_json_value(data, mapper, src_cwd, dst_account)
                    d_path.write_text(
                        json.dumps(rewritten, ensure_ascii=False, indent=2) + "\n",
                        encoding="utf-8",
                    )
                except (UnicodeDecodeError, json.JSONDecodeError):
                    shutil.copy2(s_path, d_path)
            else:
                # Rewrite UTF-8 text files (such as tool-results/*.txt); copy binary files intact.
                try:
                    text = s_path.read_text(encoding="utf-8")
                    d_path.write_text(mapper.map_text(text), encoding="utf-8")
                except UnicodeDecodeError:
                    shutil.copy2(s_path, d_path)
    return files_processed
