"""Unit tests for lossless path rewriting and round-trip symmetry."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from claude_move.rewrite import (
    PathMapper,
    encode_cwd,
    rewrite_jsonl_file,
    rewrite_session_companion_dir,
)


class TestPathMapper(unittest.TestCase):
    def test_encode_cwd_matches_claude_code(self) -> None:
        self.assertEqual(encode_cwd("/home/alice"), "-home-alice")
        self.assertEqual(encode_cwd("/home/alice/Projects/my_app.v2"), "-home-alice-Projects-my-app-v2")
        self.assertEqual(encode_cwd("/tmp/demo-worktree-_ab1_2c"), "-tmp-demo-worktree--ab1-2c")

    def test_roundtrip_workstation_to_server_and_back(self) -> None:
        to_server = PathMapper(
            src_home="/home/alice",
            dst_home="/home/server",
            src_projects_dir="/home/alice/Projects",
            dst_projects_dir="/home/server",
            project_names=["my-app"],
        )
        to_workstation = PathMapper(
            src_home="/home/server",
            dst_home="/home/alice",
            src_projects_dir="/home/server",
            dst_projects_dir="/home/alice/Projects",
            project_names=["my-app"],
        )

        samples = [
            "/home/alice",
            "/home/alice/Projects/my-app/src/index.ts",
            "/home/alice/.claude/settings.json",
            "cd ~/Projects/my-app && npm test",
            'Read("/home/alice/Projects/my-app/README.md")',
            "/tmp/claude-1000/-home-alice/abc/scratchpad",
        ]

        for original in samples:
            on_server = to_server.map_text(original)
            back_home = to_workstation.map_text(on_server)
            self.assertEqual(back_home, original, f"Failed round-trip via {on_server!r}")

        self.assertEqual(
            to_server.map_text("/home/alice/Projects/my-app/src/index.ts"),
            "/home/server/my-app/src/index.ts",
        )
        self.assertEqual(
            to_server.map_text("/home/alice/.claude/settings.json"),
            "/home/server/.claude/settings.json",
        )
        self.assertEqual(
            to_workstation.map_text("/home/server/.claude/settings.json"),
            "/home/alice/.claude/settings.json",
        )

    def test_boundary_safety_does_not_corrupt_similar_usernames(self) -> None:
        mapper = PathMapper(
            src_home="/home/al",
            dst_home="/home/srv",
            src_projects_dir="/home/al/Projects",
            dst_projects_dir="/home/srv",
        )
        self.assertEqual(mapper.map_text("/home/alex/file.txt"), "/home/alex/file.txt")
        self.assertEqual(mapper.map_text("/home/al_test/file.txt"), "/home/al_test/file.txt")
        self.assertEqual(mapper.map_text("/home/al/file.txt"), "/home/srv/file.txt")

    def test_tracking_path_roundtrip(self) -> None:
        to_srv = PathMapper(
            src_home="/home/alice",
            dst_home="/home/server",
            src_projects_dir="/home/alice/Projects",
            dst_projects_dir="/home/server",
            project_names=["my-app"],
        )
        to_ws = PathMapper(
            src_home="/home/server",
            dst_home="/home/alice",
            src_projects_dir="/home/server",
            dst_projects_dir="/home/alice/Projects",
            project_names=["my-app"],
        )
        rel_srv = to_srv.map_tracking_path("Projects/my-app/docs/PLAN.md", "/home/alice")
        self.assertEqual(rel_srv, "my-app/docs/PLAN.md")
        rel_ws = to_ws.map_tracking_path(rel_srv, "/home/server")
        self.assertEqual(rel_ws, "Projects/my-app/docs/PLAN.md")

    def test_jsonl_and_companion_dir_rewriting(self) -> None:
        mapper = PathMapper(
            src_home="/home/alice",
            dst_home="/home/server",
            src_projects_dir="/home/alice/Projects",
            dst_projects_dir="/home/server",
            project_names=["demo-repo"],
        )
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            src_jsonl = root / "in.jsonl"
            dst_jsonl = root / "out.jsonl"

            records = [
                {
                    "type": "session-owner",
                    "accountUuid": "old-acc",
                    "organizationUuid": "old-org",
                    "sessionId": "11111111-2222-3333-4444-555555555555",
                },
                {
                    "type": "file-history-snapshot",
                    "cwd": "/home/alice",
                    "snapshot": {
                        "trackedFileBackups": {
                            "/home/alice/Projects/demo-repo/app.py": {
                                "backupFileName": "deadbeef@v1",
                                "realParentDir": "/home/alice/Projects/demo-repo",
                            }
                        }
                    },
                },
                {
                    "type": "file-history-delta",
                    "cwd": "/home/alice",
                    "trackingPath": "Projects/demo-repo/app.py",
                    "backup": {"realParentDir": "/home/alice/Projects/demo-repo"},
                },
            ]
            src_jsonl.write_text(
                "\n".join(json.dumps(r) for r in records) + "\n", encoding="utf-8"
            )

            n = rewrite_jsonl_file(
                src_jsonl,
                dst_jsonl,
                mapper=mapper,
                src_cwd="/home/alice",
                dst_account={"accountUuid": "new-acc", "organizationUuid": "new-org"},
            )
            self.assertEqual(n, 3)

            out_recs = [json.loads(ln) for ln in dst_jsonl.read_text(encoding="utf-8").splitlines()]
            self.assertEqual(out_recs[0]["accountUuid"], "new-acc")
            self.assertIn(
                "/home/server/demo-repo/app.py",
                out_recs[1]["snapshot"]["trackedFileBackups"],
            )
            self.assertEqual(
                out_recs[1]["snapshot"]["trackedFileBackups"]["/home/server/demo-repo/app.py"][
                    "realParentDir"
                ],
                "/home/server/demo-repo",
            )
            self.assertEqual(out_recs[2]["trackingPath"], "demo-repo/app.py")

            # Companion subagents + binary file safety
            comp_in = root / "comp_in"
            comp_out = root / "comp_out"
            (comp_in / "subagents").mkdir(parents=True)
            (comp_in / "tool-results").mkdir(parents=True)
            (comp_in / "subagents" / "agent-1.meta.json").write_text(
                json.dumps({"worktreePath": "/home/alice/Projects/demo-repo/.claude/worktrees/w1"}),
                encoding="utf-8",
            )
            (comp_in / "tool-results" / "out.txt").write_text(
                "Saved to /home/alice/Projects/demo-repo/dist/index.js\n", encoding="utf-8"
            )
            raw_bin = b"\x89PNG\r\n\x1a\n\xff\xfe\x00\x80"
            (comp_in / "tool-results" / "img.bin").write_bytes(raw_bin)

            c_count = rewrite_session_companion_dir(comp_in, comp_out, mapper, "/home/alice")
            self.assertEqual(c_count, 3)
            meta = json.loads((comp_out / "subagents" / "agent-1.meta.json").read_text())
            self.assertEqual(meta["worktreePath"], "/home/server/demo-repo/.claude/worktrees/w1")
            self.assertIn(
                "/home/server/demo-repo/dist/index.js",
                (comp_out / "tool-results" / "out.txt").read_text(),
            )
            self.assertEqual((comp_out / "tool-results" / "img.bin").read_bytes(), raw_bin)


if __name__ == "__main__":
    unittest.main()
