"""End-to-end simulation tests for discovery, zero-config non-Herdr setups, tmux, and migration."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from claude_move.config import AppConfig, MachineConfig, ssh_config_hosts
from claude_move.discovery import discover_sessions, resolve_session
from claude_move.herdr_ctl import build_tmux_launch_shell_cmd
from claude_move.rewrite import encode_cwd
from claude_move.transfer import MoveOptions, move_session, resolve_project_pairs


class TestDiscoveryAndTransfer(unittest.TestCase):
    def test_local_to_local_migration_end_to_end(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            src_home = base / "src_home"
            dst_home = base / "dst_home"
            src_proj = src_home / "Projects" / "widget-app"
            src_proj.mkdir(parents=True)
            (src_proj / ".git").mkdir()
            (src_proj / "main.py").write_text("print('dirty edit')\n", encoding="utf-8")
            (src_proj / "node_modules" / "pkg").mkdir(parents=True)
            (src_proj / "node_modules" / "pkg" / "index.js").write_text("skip", encoding="utf-8")

            dst_home.mkdir(parents=True)
            (dst_home / ".claude.json").write_text(
                json.dumps({"oauthAccount": {"accountUuid": "acc-1", "organizationUuid": "org-1"}}),
                encoding="utf-8",
            )

            sid = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
            enc_src = encode_cwd(str(src_home))
            t_dir = src_home / ".claude" / "projects" / enc_src
            t_dir.mkdir(parents=True)
            t_file = t_dir / f"{sid}.jsonl"
            lines = [
                {"type": "ai-title", "aiTitle": "Widget Refactor", "sessionId": sid},
                {
                    "type": "user",
                    "cwd": str(src_home),
                    "sessionId": sid,
                    "message": {
                        "role": "user",
                        "content": f"Check {src_proj}/main.py please",
                    },
                },
            ]
            t_file.write_text("\n".join(json.dumps(x) for x in lines) + "\n", encoding="utf-8")

            fh_dir = src_home / ".claude" / "file-history" / sid
            fh_dir.mkdir(parents=True)
            (fh_dir / "snap1@v1").write_bytes(b"original content")

            src_m = MachineConfig(
                name="workstation",
                home=str(src_home),
                projects_dir=str(src_home / "Projects"),
            )
            dst_m = MachineConfig(
                name="server",
                home=str(dst_home),
                projects_dir=str(dst_home),
            )
            cfg = AppConfig(
                default_target="server",
                machines={"workstation": src_m, "server": dst_m},
                here="workstation",
            )

            found = discover_sessions(src_m, is_local=True)
            self.assertEqual(len(found), 1)
            sess = resolve_session(found, "aaaaaaaa")
            self.assertEqual(sess.title, "Widget Refactor")
            self.assertIn("widget-app", sess.touched_projects)

            pairs, names = resolve_project_pairs(sess, src_m, dst_m, MoveOptions())
            self.assertEqual(pairs, [(str(src_proj), str(dst_home / "widget-app"))])
            self.assertIn("widget-app", names)

            res = move_session(
                cfg=cfg,
                session=sess,
                src=src_m,
                dst=dst_m,
                src_is_local=True,
                dst_is_local=True,
                opts=MoveOptions(no_launch=True),
                log=lambda *_: None,
            )

            self.assertEqual(res.rewritten_lines, 2)
            self.assertTrue((dst_home / "widget-app" / "main.py").is_file())
            self.assertFalse((dst_home / "widget-app" / "node_modules").exists())

            enc_dst = encode_cwd(str(dst_home))
            out_jsonl = dst_home / ".claude" / "projects" / enc_dst / f"{sid}.jsonl"
            self.assertTrue(out_jsonl.is_file())
            out_text = out_jsonl.read_text(encoding="utf-8")
            self.assertIn(f"{dst_home}/widget-app/main.py", out_text)
            self.assertNotIn(str(src_home), out_text)

            self.assertEqual(
                (dst_home / ".claude" / "file-history" / sid / "snap1@v1").read_bytes(),
                b"original content",
            )
            cj = json.loads((dst_home / ".claude.json").read_text(encoding="utf-8"))
            self.assertTrue(cj["projects"][str(dst_home)]["hasTrustDialogAccepted"])

    def test_standard_non_herdr_arbitrary_cwd_subdirectory(self) -> None:
        """Users without Herdr/cproj who run `cd ~/custom_work/webapp/api && claude` sync cleanly."""
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            src_home = base / "Users" / "bob"
            dst_home = base / "home" / "ubuntu"
            repo_root = src_home / "custom_work" / "webapp"
            sub_cwd = repo_root / "packages" / "api"
            sub_cwd.mkdir(parents=True)
            (repo_root / ".git").mkdir()
            (sub_cwd / "server.ts").write_text("export const ok = true;\n", encoding="utf-8")
            # Even if ~/Projects also exists on Bob's laptop, ~/custom_work/webapp must still be detected!
            (src_home / "Projects").mkdir(parents=True)
            dst_home.mkdir(parents=True)

            sid = "bbbbbbbb-1111-2222-3333-444444444444"
            enc_src = encode_cwd(str(sub_cwd))
            t_dir = src_home / ".claude" / "projects" / enc_src
            t_dir.mkdir(parents=True)
            (t_dir / f"{sid}.jsonl").write_text(
                json.dumps({"type": "user", "cwd": str(sub_cwd), "sessionId": sid, "message": {"role": "user", "content": "hi"}})
                + "\n",
                encoding="utf-8",
            )

            src_m = MachineConfig(
                name="local", home=str(src_home), projects_dir=str(src_home / "Projects")
            )
            dst_m = MachineConfig(name="devbox", home=str(dst_home), projects_dir=str(dst_home))
            cfg = AppConfig(machines={"local": src_m, "devbox": dst_m}, here="local")

            found = discover_sessions(src_m, is_local=True)
            self.assertEqual(len(found), 1)
            sess = resolve_session(found, "webapp")
            self.assertEqual(sess.cwd_project_root, str(repo_root))

            res = move_session(
                cfg=cfg,
                session=sess,
                src=src_m,
                dst=dst_m,
                src_is_local=True,
                dst_is_local=True,
                opts=MoveOptions(launcher="none"),
                log=lambda *_: None,
            )

            expected_dst_cwd = str(dst_home / "custom_work" / "webapp" / "packages" / "api")
            self.assertEqual(res.dst_cwd, expected_dst_cwd)
            self.assertTrue((Path(expected_dst_cwd) / "server.ts").is_file())
            out_jsonl = dst_home / ".claude" / "projects" / encode_cwd(expected_dst_cwd) / f"{sid}.jsonl"
            self.assertTrue(out_jsonl.is_file())

    def test_tmux_command_builder_and_ssh_config_parser(self) -> None:
        cmd = build_tmux_launch_shell_cmd(
            session_id="12345678-aaaa-bbbb-cccc-ddddeeeeffff",
            dst_cwd="/home/ubuntu/webapp",
            window_label="API Refactor",
            claude_args=["--dangerously-skip-permissions"],
            initial_prompt="Continue tests",
        )
        self.assertIn("tmux new-window", cmd)
        self.assertIn("tmux new-session", cmd)
        self.assertIn("--resume 12345678-aaaa-bbbb-cccc-ddddeeeeffff", cmd)
        self.assertIn("'Continue tests'", cmd)

        with tempfile.TemporaryDirectory() as tmp:
            ssh_cfg = Path(tmp) / "config"
            ssh_cfg.write_text(
                "# comment\nHost *\n  ServerAliveInterval 30\nHost devbox staging.internal !bad\n  User ubuntu\n",
                encoding="utf-8",
            )
            hosts = ssh_config_hosts(ssh_cfg)
            self.assertEqual(hosts, {"devbox", "staging.internal"})


if __name__ == "__main__":
    unittest.main()
